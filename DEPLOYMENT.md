# Déploiement AWS — audit-qualite

Ce guide décrit les opérations à réaliser manuellement pour reconstruire le
déploiement. Aucune commande AWS, construction Docker ou mise en production
n'a été exécutée lors de sa rédaction. Les ressources actuelles ci-dessous
sont celles communiquées par le propriétaire, sans vérification distante.
Les commandes multilignes utilisent Bash (EC2 ou Git Bash/WSL sur le PC).

## 1. Vue d'ensemble

Le projet contrôle la qualité de fichiers Excel de leasing automobile.
Streamlit (`dashboard/app.py`) fournit le dashboard et déclenche le pipeline
(`orchestrateur.py`). Les agents Structure, Statistics, Coherence et Realism
produisent les analyses ; Coherence et Realism appellent OpenRouter. Le Judge
déterministe génère un verdict, puis le reporting produit HTML et JSON.
Le stockage S3 conserve entrées, rapports versionnés, historique et quarantaine.

Amazon EC2 héberge Docker et Nginx. L'image est construite sur le PC puis publiée
dans Amazon ECR. EC2 la récupère avec son rôle IAM, qui permet aussi l'accès S3.
Nginx reçoit HTTP sur l'Elastic IP et relaie les requêtes vers Streamlit.

## 2. Architecture

```text
Utilisateur
  | HTTP :80
Elastic IP 63.34.231.100
  |
EC2 / Nginx :80
  | http://127.0.0.1:8501 (WebSocket inclus)
Docker / Streamlit :8501
  |
Pipeline
  |
Agents Structure / Statistics / Coherence / Realism
  |                              +--> OpenRouter (Coherence / Realism)
Judge
  |
Reporting HTML / JSON
  |
S3 : input/ ; reports/runs/<run_id>/ ; history/ ; quarantine/
  ^
  +--- accès via AuditQualiteEC2Role (EC2)

PC / futur CI-CD
  |
Docker Build (linux/amd64)
  |
Amazon ECR
  |
EC2 Docker Pull --> Docker / Streamlit
```

## 3. Ressources AWS

| Ressource | Valeur actuelle |
| --- | --- |
| Région | `eu-west-1` |
| Bucket S3 privé | `audit-qualite-leasing-pfa-870900715939-eu-west-1-an` |
| Préfixes | `input/`, `reports/`, `quarantine/`, `history/` |
| Rapports versionnés | `reports/runs/<run_id>/` |
| Historique consommé par le dashboard | `history/runs/` |
| IAM Policy S3 | `AuditQualiteS3Policy` |
| Rôle EC2 | `AuditQualiteEC2Role` |
| Instance | `audit-qualite-ec2`, Amazon Linux 2023, x86_64, t3.micro |
| Volume | gp3, 12 GiB |
| Repository ECR privé | `audit-qualite` |
| URI ECR | `870900715939.dkr.ecr.eu-west-1.amazonaws.com/audit-qualite` |
| Image / conteneur | `audit-qualite:latest` / `audit-qualite` |
| Elastic IP | `63.34.231.100` |
| Nginx / Streamlit | TCP 80 / TCP 8501 |

Pour une reconstruction depuis zéro, créer dans la console AWS le bucket privé
(blocage de l'accès public activé), le repository ECR privé, la policy et le rôle
EC2, puis l'instance avec son Security Group et l'Elastic IP associée. Les préfixes
S3 sont des clés d'objets, pas des répertoires à provisionner. Charger ensuite les
entrées autorisées sous `input/`, par exemple `input/LOAD_LOCK_202603/fichier.xlsx`.
Associer le rôle via un profil d'instance. Prévoir une sortie réseau HTTPS vers
S3, ECR, OpenRouter et la recherche web de Realism, ainsi que DNS.

Le bucket, le compte ECR et l'Elastic IP appartiennent à cette installation.
Pour un autre compte, adapter les noms, URI, policies, configuration Nginx et
variables ; une Elastic IP nouvelle ne reprend pas nécessairement cette adresse.
L'AMI exacte, le VPC, le subnet, les identifiants des ressources et les documents
IAM actuels ne sont pas présents dans ce dépôt : les relever dans AWS avant
une reconstruction identique. Ce guide ne constitue pas un provisionnement IaC.

## 4. IAM / S3 / ECR

EC2 utilise `AuditQualiteEC2Role`, avec une relation de confiance autorisant
`ec2.amazonaws.com` à effectuer `sts:AssumeRole`. Attacher `AuditQualiteS3Policy`
et les permissions ECR nécessaires au téléchargement de l'image.

Pour les opérations visibles dans le code, la policy S3 doit permettre :

- `s3:ListBucket` sur l'ARN du bucket, pour les quatre préfixes et leur contenu ;
- `s3:GetObject` sur `input/*`, `reports/*`, `history/*` et `quarantine/*` ;
- `s3:PutObject` sur `reports/*`, `history/*` et `quarantine/*`.

La quarantaine copie la source ; le pipeline n'a pas besoin de supprimer l'entrée.
Si `ListBucket` est limité par `s3:prefix`, le diagnostic à la racine ci-dessous
peut être refusé : tester alors séparément `input/`, `reports/`, `history/` et
`quarantine/`, sans élargir la policy uniquement pour ce diagnostic.
Un chiffrement SSE-KMS personnalisé demanderait aussi des permissions KMS ;
aucune configuration KMS n'est confirmée ici.

ECR nécessite `ecr:GetAuthorizationToken` sur `*`, puis
`ecr:BatchGetImage`, `ecr:GetDownloadUrlForLayer` et
`ecr:BatchCheckLayerAvailability` limités au repository
`arn:aws:ecr:eu-west-1:870900715939:repository/audit-qualite`.
Le PC utilise sa propre identité de développement autorisée au push ECR ;
le rôle EC2 n'a besoin que de la lecture ECR.
Voir les [exemples officiels ECR](https://docs.aws.amazon.com/AmazonECR/latest/userguide/repository-policy-examples.html).

Diagnostics manuels sur EC2, AWS CLI v2 installée (`aws --version`) :

```bash
aws sts get-caller-identity
aws s3 ls s3://audit-qualite-leasing-pfa-870900715939-eu-west-1-an/
```

L'identité attendue est une session `assumed-role/AuditQualiteEC2Role/...`.
**NE PAS mettre `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` ou
`AWS_SESSION_TOKEN` dans le fichier d'environnement EC2.** Ne pas configurer
de profil à clés statiques sur EC2 ; boto3 récupère les credentials temporaires
du rôle. Le script ignore les fichiers de credentials/configuration AWS du PC.

AWS CLI v2 peut être préinstallée sur l'AMI ; si absente, suivre la
[procédure officielle Linux x86_64](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)
et vérifier la signature de l'archive.
Avec Docker en réseau bridge, autoriser IMDSv2 et vérifier que la limite de sauts
permet l'accès depuis le conteneur (AWS recommande `2` pour cet usage).
Un STS réussi sur l'hôte ne prouve pas l'accès IAM depuis Docker. Voir la
[documentation IMDS AWS](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/configuring-IMDS-new-instances.html).

## 5. Security Group

| Port TCP | Source actuelle | Rôle |
| --- | --- | --- |
| 22 | My IP (`adresse/32`) | Administration SSH |
| 8501 | My IP (`adresse/32`) | Ancien accès direct de diagnostic Streamlit |
| 80 | `0.0.0.0/0` | Dashboard HTTP via Nginx |

Ne jamais exposer SSH 22 à `0.0.0.0/0`. Actualiser My IP lorsque l'adresse
du poste change. Une connexion initiale peut utiliser
`ssh -i /chemin/prive/cle.pem ec2-user@63.34.231.100` ; garder la clé hors dépôt.
Une fois Nginx validé, fermer la règle 8501 entrante. Le script lie déjà ce port
à `127.0.0.1` : même avec une règle Security Group, l'accès direct distant est
indisponible avec cette liaison locale. Aucun accès public S3 n'est requis.

## 6. Installation Docker

Sur Amazon Linux 2023 :

```bash
sudo dnf update -y
sudo dnf install -y docker
sudo systemctl enable --now docker
sudo usermod -aG docker ec2-user
```

Le script nécessite aussi `curl` sur **l'hôte EC2**, pour le contrôle de santé :

```bash
curl --version
# Si absent (curl-minimal peut déjà être fourni par l'AMI) :
sudo dnf install -y curl-minimal
```

Docker, son daemon, AWS CLI et curl sont vérifiés avant le remplacement d'un
conteneur. Le fichier d'environnement doit être lisible et en mode `600`.

Se déconnecter puis se reconnecter en SSH pour appliquer le groupe :

```bash
docker --version
docker ps
```

L'accès au groupe Docker donne des privilèges élevés sur l'hôte ; le réserver
aux administrateurs. Vérifier l'espace libre (`df -h`) avant un pull sur 12 GiB.

## 7. Installation Nginx

```bash
sudo dnf install -y nginx
sudo systemctl enable --now nginx
sudoedit /etc/nginx/conf.d/audit-qualite.conf
```

Contenu du fichier (chargé dans le contexte HTTP de Nginx) :

```nginx
map $http_upgrade $audit_qualite_connection_upgrade {
    default upgrade;
    '' close;
}

server {
    listen 80;
    server_name 63.34.231.100;
    client_max_body_size 200m;

    location / {
        proxy_pass http://127.0.0.1:8501;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $audit_qualite_connection_upgrade;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        proxy_buffering off;
    }
}
```

Inspecter la configuration existante avec `sudo nginx -T` : conserver les autres
sites utiles, éviter un doublon `server_name` ou `default_server`. Le nom explicite
ci-dessus permet de servir l'Elastic IP même si la page d'accueil par défaut existe.
Les headers Upgrade/Connection permettent les
[WebSockets Nginx](https://nginx.org/en/docs/http/websocket.html) de Streamlit.
Si SELinux bloque le proxy, diagnostiquer les refus avant d'ajuster sa policy ;
ne pas désactiver SELinux globalement.

```bash
sudo nginx -t
sudo systemctl reload nginx
```

## 8. Construction Docker en local

Depuis la racine du projet sur le PC, Docker configuré pour les conteneurs Linux :

```bash
docker build -t audit-qualite:latest .
docker images audit-qualite
```

L'instance est x86_64 : sur un poste ARM, utiliser à la place
`docker build --platform linux/amd64 -t audit-qualite:latest .`.
Le Dockerfile existant lance Python 3.13 / Streamlit. `.dockerignore` exclut les
secrets, données et caches privés du contexte envoyé au moteur Docker. Les
motifs récursifs protègent aussi les fichiers `.env` et PEM dans les sous-dossiers.
`requirements.txt` inclut `xlrd` pour les anciens fichiers `.xls` et déclare
explicitement `botocore`, importé par le client S3 ; `langdetect` a été retiré,
le profiling utilisant une heuristique interne.

Limite de reproductibilité constatée : `requirements.txt` ne fixe pas les
versions, `python:3.13-slim` est une référence mutable et `latest` peut changer.
Les fichiers existants sont conservés ; ce guide reproduit la procédure, pas
une reconstruction bit à bit. Pour redéployer exactement une image validée,
conserver son digest ECR et utiliser `IMAGE_URI=...@sha256:...` dans le script.
Un verrouillage des dépendances/base Python reste un chantier distinct.

## 9. Push vers Amazon ECR

Depuis le PC, utiliser son identité AWS locale autorisée (profil AWS CLI hors
du dépôt). Le repository `audit-qualite` doit exister dans `eu-west-1`.

```bash
aws ecr get-login-password --region eu-west-1 \
  | docker login --username AWS --password-stdin \
    870900715939.dkr.ecr.eu-west-1.amazonaws.com
docker tag audit-qualite:latest \
  870900715939.dkr.ecr.eu-west-1.amazonaws.com/audit-qualite:latest
docker push \
  870900715939.dkr.ecr.eu-west-1.amazonaws.com/audit-qualite:latest
```

ECR est la méthode de distribution principale. À chaque version, conserver aussi
un tag distinct (par exemple `v1.0.1`) et le digest retourné lors du push pour
retrouver l'image précédente ; `latest` seul ne permet pas ce suivi.

## 10. Pull depuis EC2

Sur EC2, avec le rôle IAM :

```bash
aws ecr get-login-password --region eu-west-1 \
  | docker login --username AWS --password-stdin \
    870900715939.dkr.ecr.eu-west-1.amazonaws.com
docker pull \
  870900715939.dkr.ecr.eu-west-1.amazonaws.com/audit-qualite:latest
```

## 11. Variables d'environnement EC2

Le fichier de production est privé et local à EC2. Aucun document existant ne
confirme son chemin actuel : `$HOME/.env` est la valeur demandée retenue pour
le script, **à vérifier sur l'instance**. Si le fichier existant est
`~/audit-qualite.env`, conserver ce nom et passer `ENV_FILE="$HOME/audit-qualite.env"`.
Ne pas écraser un fichier de production existant avec les exemples.

Créer/éditer le fichier choisi avec un éditeur privé (pas une commande contenant
la clé dans l'historique du shell). Exemple de contenu, à adapter sur EC2 :

```dotenv
STORAGE_MODE=s3
AWS_REGION=eu-west-1
S3_BUCKET=audit-qualite-leasing-pfa-870900715939-eu-west-1-an
S3_INPUT_PREFIX=input/
S3_REPORTS_PREFIX=reports/
S3_QUARANTINE_PREFIX=quarantine/
S3_HISTORY_PREFIX=history/
OPENROUTER_API_KEY=replace_me
DATA_DIR=./data
ARTIFACT_CACHE_TTL_SECONDS=900
```

Remplacer uniquement dans le fichier privé la valeur fictive OpenRouter par sa
clé. Respecter le format Docker `KEY=value`, sans guillemets, `export`, expansion
shell ou commentaire après une valeur. Le script refuse les doublons.
`DATA_DIR` et `ARTIFACT_CACHE_TTL_SECONDS` sont les deux autres variables
effectivement lues par `config.py`. Les noms de modèles LLM sont des constantes
Python, pas des variables d'environnement supplémentaires.

```bash
ENV_FILE="$HOME/.env" # adapter au chemin réel sans renommer le fichier existant
chmod 600 "$ENV_FILE"
```

Interdits même vides dans ce fichier EC2 : `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`. Sur le PC, préférer un profil
AWS CLI séparé. Le `.env` local actuel n'est pas modifié par ce travail.

## 12. Lancement Docker

Commande correspondant au mapping historique (accès direct limité par le
Security Group à My IP) :

```bash
docker run -d \
  --name audit-qualite \
  --restart unless-stopped \
  -p 8501:8501 \
  --env-file "$ENV_FILE" \
  870900715939.dkr.ecr.eu-west-1.amazonaws.com/audit-qualite:latest
```

Avec Nginx sur le même hôte, remplacer `-p 8501:8501` par
`-p 127.0.0.1:8501:8501` pour n'écouter que localement. C'est la valeur par défaut
du script ; le port du conteneur reste 8501. Utiliser une seule méthode de lancement.

```bash
docker ps
docker logs --tail 100 audit-qualite
```

Les logs manuels doivent rester sur l'hôte privé ; vérifier et masquer toute
donnée sensible avant partage. Le script limite sa sortie aux 30 dernières
lignes et masque la valeur exacte de la clé OpenRouter configurée.

Après démarrage, le script vérifie
`http://127.0.0.1:${HOST_PORT}/_stcore/health` avec curl : timeout de connexion
3 secondes, timeout total 10 secondes par essai, six essais au maximum espacés
de 2 secondes (au plus 70 secondes d'attente réseau et de pauses). Seule la
réponse `ok` est acceptée. Un échec provoque une sortie non nulle, sans annoncer
« Deployment completed. ». Les commandes manuelles ci-dessus ne réalisent pas
ce contrôle automatiquement. Ce contrôle confirme Streamlit ; il ne valide
ni S3, ni OpenRouter, ni l'exécution des agents.

## 13. Restart automatique

```bash
docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' audit-qualite
systemctl is-enabled docker
```

Résultats attendus : `unless-stopped` et `enabled`. Docker démarre au boot et
relance le conteneur après redémarrage, sauf s'il a été explicitement arrêté.
Dans ce dernier cas, utiliser `docker start audit-qualite` pour le réactiver.

## 14. Elastic IP

L'adresse de référence est l'Elastic IP fixe **63.34.231.100**, associée à
l'instance. Elle évite le changement d'adresse après Stop / Start tant que
l'association et l'allocation sont conservées. Ne pas utiliser une ancienne
adresse publique dynamique comme adresse de référence et ne pas libérer l'EIP
si l'on souhaite conserver cette URL.

## 15. Accès au dashboard

Adresse actuelle : **http://63.34.231.100**.

```text
Internet :80 --> Nginx --> localhost:8501 --> Streamlit
```

Nginx masque le port 8501 dans l'URL utilisateur. Il n'y a volontairement ni nom
de domaine ni HTTPS afin de limiter les coûts et la complexité du PFA.
HTTP ne chiffre pas les échanges ; en tenir compte pour les données présentées
et les fichiers envoyés. Aucun mécanisme d'authentification supplémentaire n'est
ajouté par ce guide.

## 16. Validation fonctionnelle

À réaliser manuellement après déploiement, avec un fichier de validation autorisé
et un quota OpenRouter suffisant ; ces vérifications métier n'ont pas été lancées ici.

- [ ] Elastic IP accessible.
- [ ] Nginx actif (`systemctl is-active nginx`).
- [ ] Docker actif (`systemctl is-active docker`).
- [ ] Conteneur `audit-qualite` Up.
- [ ] Dashboard accessible, navigation et WebSocket fonctionnels.
- [ ] Environnement S3 affiché ; rôle IAM accessible depuis le conteneur.
- [ ] Nouveau fichier lu depuis `input/`.
- [ ] Structure SUCCESS.
- [ ] Statistics SUCCESS.
- [ ] Coherence SUCCESS.
- [ ] Realism SUCCESS.
- [ ] Pipeline COMPLETE.
- [ ] Judge exécuté et verdict créé.
- [ ] Rapport HTML créé.
- [ ] JSON créé.
- [ ] `reports/runs/<run_id>/` créé.
- [ ] `history/` mis à jour (`history/runs/` lu par le dashboard).
- [ ] `quarantine/` utilisée si nécessaire, source conservée selon le code existant.

La quarantaine est une copie, uniquement en mode S3, lorsque le verdict vaut
`REJECTED` ou `pipeline_status` vaut `INCOMPLETE`. `WARNING` seul ne suffit pas.
Sa clé est `quarantine/<LOAD_LOCK ou sans_mois>/<nom_fichier>`, sans `run_id` :
une copie suivante peut remplacer la précédente.

Les verdicts et rapports individuels sont écrits dans
`resultats/runs/<run_id>/<LOAD_LOCK ou sans_mois>/` puis dans
`reports/runs/<run_id>/<LOAD_LOCK ou sans_mois>/`. Le consolidé est placé à la
racine du run. L'historique est écrit dans
`resultats/history/runs/YYYY/MM/DD/<run_id>.json` puis dans
`history/runs/YYYY/MM/DD/<run_id>.json`. Les échecs d'upload sont journalisés et
ne changent pas automatiquement le statut métier ; vérifier les objets S3.

Le cache d'entrée S3 conserve un sidecar `.s3-cache.json` contenant son identité
distante et le SHA-256 local. Une taille identique ne suffit plus à réutiliser
un fichier : ETag, version, date, bucket et clé doivent correspondre. Un ancien
cache sans sidecar est rafraîchi. Le téléchargement conditionnel et temporaire
évite de publier un cache incomplet ; un échec n'autorise pas à analyser l'ancien
fichier. Le checkpoint Cohérence calcule ensuite son hash sur cette source locale
validée. Voir [docs/fiabilite_pipeline.md](docs/fiabilite_pipeline.md).

Un conteneur Up ne suffit pas à valider S3, les agents ou OpenRouter. En cas
d'échec, examiner les logs privés, les permissions, la région, les préfixes,
IMDSv2 et le quota OpenRouter avant de relancer un audit.

## 17. Mise à jour d'une version

Workflow manuel actuel : PC → modification du code → build → tag → push ECR ;
EC2 → pull → arrêt/remplacement → lancement → contrôle `docker ps` et logs.
Les commandes PC sont celles des sections 8 et 9.

Avant remplacement, attendre la fin des audits actifs et relever l'image
précédente (`docker inspect -f '{{.Image}}' audit-qualite`). Les objets S3 restent
dans S3. Sans volumes, le cache LLM, les checkpoints et les sorties locales de
la couche du conteneur disparaissent à sa suppression : sauvegarder les éléments
locaux nécessaires avant cette opération. Aucun volume ni mécanisme de checkpoint
n'est modifié ici. Le script refuse de remplacer un conteneur ayant des mounts
pour éviter de perdre leur configuration.

Placer la version revue de `deployment/deploy_ec2.sh` sur EC2 (par exemple dans
`~/audit-qualite/deployment/`), conserver les fins de ligne LF puis exécuter
(le fichier `deployment/.gitattributes` impose LF pour les scripts lors des futurs checkouts) :

```bash
cd "$HOME/audit-qualite"
bash -n deployment/deploy_ec2.sh
ENV_FILE="$HOME/.env" bash deployment/deploy_ec2.sh
# Si le fichier actuel porte un autre nom :
# ENV_FILE="$HOME/audit-qualite.env" bash deployment/deploy_ec2.sh
# Version identifiée : IMAGE_TAG=v1.0.1 ENV_FILE="$HOME/.env" bash deployment/deploy_ec2.sh
# Image exacte : IMAGE_URI=<registry>/<repository>@sha256:<digest> bash deployment/deploy_ec2.sh
```

Variables configurables : `AWS_REGION`, `ECR_REGISTRY`, `ECR_REPOSITORY`,
`IMAGE_TAG`, `IMAGE_URI`, `CONTAINER_NAME`, `ENV_FILE`, `HOST_PORT`,
`CONTAINER_PORT` et `HOST_BIND_ADDRESS` (défaut `127.0.0.1`). Si `IMAGE_URI` est
explicite, son registre doit correspondre à `ECR_REGISTRY` pour le login.
Ne pas lancer le script avec `bash -x` ni exporter de credentials/profil AWS.

Équivalent manuel sur EC2, après pull réussi (section 10), en vérifiant d'abord
que le conteneur existe et en conservant sa configuration utile :

```bash
docker stop audit-qualite
docker rm audit-qualite
docker run -d \
  --name audit-qualite \
  --restart unless-stopped \
  -p 127.0.0.1:8501:8501 \
  --env-file "$ENV_FILE" \
  870900715939.dkr.ecr.eu-west-1.amazonaws.com/audit-qualite:latest
docker ps
docker logs --tail 30 audit-qualite
```

Au premier déploiement, omettre stop/rm. Le remplacement entraîne une brève
indisponibilité. Le script ne réalise pas de rollback automatique : en cas
d'échec, utiliser le tag/digest précédent conservé avec `IMAGE_TAG`/`IMAGE_URI`,
puis revalider le dashboard. Il ne supprime aucune image Docker.
Cette procédure sera ensuite automatisée par GitHub Actions CI/CD ; aucun
workflow ni secret GitHub n'est configuré dans cette étape.

## 18. Arrêt EC2 / coûts

Dans la console : EC2 → Instances → `audit-qualite-ec2` → Instance state →
Stop instance. Attendre la fin des audits avant l'arrêt.

- L'Elastic IP reste associée et l'adresse reste stable au redémarrage.
- L'arrêt réduit les coûts compute ; le stockage EBS et l'IPv4 publique peuvent
  continuer à être facturés, ainsi que S3/ECR selon l'usage.
- Un AWS Budget à **5 USD** a été configuré selon le contexte fourni. C'est un
  suivi avec alertes, pas une garantie de plafonnement ni un arrêt automatique.

## 19. Sécurité et écarts constatés

Ne jamais committer `.env`, `.env.*` (sauf `.env.example`, exclusivement fictif),
`*.pem` ou `audit-qualite.env`. Ne jamais publier les valeurs réelles de
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN` ou
`OPENROUTER_API_KEY`, ni les afficher dans les logs ou la documentation.
Garder le fichier EC2 en mode `600` et protéger aussi le répertoire Docker
de l'utilisateur, où le login ECR peut enregistrer un jeton temporaire.

`.gitignore` protège les nouveaux fichiers, mais n'efface pas un secret déjà
suivi ou présent dans l'historique. Si une exposition antérieure est découverte,
révoquer/renouveler la clé puis traiter l'historique séparément.
`.dockerignore` empêche les secrets et données locales d'entrer dans l'image
via `COPY . .` ; une ancienne image contenant un secret doit être remplacée.

Écarts entre documentation/contexte et fichiers inspectés :

- Le chemin du fichier d'environnement EC2 n'est pas attesté dans le dépôt.
  `$HOME/.env` est une valeur par défaut configurable, pas une observation distante.
- Le README décrit désormais les uploads S3, les chemins par run, le Judge v2
  et le dashboard. Les anciennes mentions d'un déploiement uniquement futur
  ou d'un upload S3 non implémenté ont été corrigées.
- La procédure locale recommande désormais Python 3.13 comme le Dockerfile.
  Le runtime Docker n'a pas été changé ; la précédente mention de Python 3.12
  ne constitue pas une validation de compatibilité avec cette version.
- Le Dockerfile et `.dockerignore` existaient déjà mais n'étaient pas suivis
  par Git au début de cette intervention. Ils ne sont pas ajoutés à l'index ici.
- `.pytest_tmp*/` couvre déjà `.pytest_tmp_codex/` ; aucune règle doublon n'est ajoutée.
- Le script adopte une liaison locale 8501 pour Nginx ; la commande historique
  avec exposition sur toutes les interfaces est conservée en section 12.
- Les versions des dépendances et de la base Docker restent non figées
  (section 8) ; les ressources AWS restent déclaratives et non vérifiées à distance.

Les corrections techniques portent sur la fraîcheur du cache du chargeur S3,
les contrôles du script, les dépendances et les exclusions de secrets. Les
algorithmes des agents, du Judge, du pipeline, du dashboard, des checkpoints,
de l'historisation et les conditions de quarantaine restent inchangés.
