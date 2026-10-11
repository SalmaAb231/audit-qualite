# Déploiement AWS validé : GitHub Actions, Amazon ECR et EC2

Ce document décrit le déploiement réellement validé pour le dépôt
`SalmaAb231/audit-qualite`, branche `hanane`. La publication de l'image est
automatique ; son déploiement sur EC2 reste une action manuelle exécutée sur
l'instance.

```text
Développeur
    |
    | git push sur hanane
    v
GitHub Actions
    |
    +-- tests Python 3.12
    +-- build de l'image Docker
    +-- authentification AWS temporaire par OIDC
    +-- publication ECR : latest + <commit_sha>
    v
Amazon ECR : audit-qualite-pfa
    |
    | déploiement déclenché manuellement sur EC2
    v
deployment/deploy_ec2.sh
    |
    +-- authentification ECR avec AuditQualiteEC2Role
    +-- récupération de l'image
    +-- remplacement contrôlé du conteneur
    +-- health check local et rollback automatique
    v
Conteneur audit-qualite : Streamlit sur le port 8501
    |
    +-- Structure / Statistics / Coherence / Realism
    +-- Judge / Reporting
    +-- stockage Amazon S3
```

Le workflow GitHub Actions ne se connecte pas à EC2 et ne remplace pas le
conteneur. Il ne faut donc pas présenter chaque push comme un déploiement EC2
automatique.

## 1. Prérequis AWS

La configuration de référence est la suivante :

| Élément | Valeur validée |
|---|---|
| Compte AWS | `239280167189` |
| Région | `eu-west-1` |
| Système EC2 | Amazon Linux 2023 |
| IAM Role EC2 | `AuditQualiteEC2Role` |
| Registre ECR | `239280167189.dkr.ecr.eu-west-1.amazonaws.com` |
| Repository ECR | `audit-qualite-pfa` |
| Conteneur | `audit-qualite` |
| Port hôte / conteneur | `8501` / `8501` |
| Restart policy | `unless-stopped` |

Sur EC2, Docker, AWS CLI et `curl` doivent être installés. Le daemon Docker
doit être accessible par l'utilisateur qui exécute le script. Le fichier
`deployment/deploy_ec2.sh` doit être présent sur l'instance et exécutable.

## 2. Amazon S3

Le bucket utilisé est :

```text
audit-qualite-leasing-pfa-hn-2026-01-239280167189-eu-west-1-an
```

Les préfixes applicatifs sont :

- `input/` pour les fichiers à auditer ;
- `reports/` pour les verdicts et rapports, notamment
  `reports/runs/<run_id>/` ;
- `quarantine/` pour les sources rejetées ou incomplètes ;
- `history/` pour l'historique immuable, notamment
  `history/runs/YYYY/MM/DD/<run_id>.json`.

Le bucket reste privé. L'application y accède avec le rôle IAM de l'instance,
et non avec des identifiants AWS permanents dans le fichier d'environnement.

## 3. IAM Role EC2

Le rôle `AuditQualiteEC2Role` est attaché à l'instance. Il fournit :

- les permissions S3 nécessaires au projet : lecture des entrées et de
  l'historique, écriture des rapports, de la quarantaine et de l'historique ;
- une permission de lecture ECR, par exemple la politique gérée
  `AmazonEC2ContainerRegistryReadOnly`, afin de s'authentifier et récupérer
  les images.

Le script s'appuie sur les credentials temporaires délivrés automatiquement
par ce rôle. Aucune variable d'identifiant AWS statique n'est requise sur EC2.

## 4. Amazon ECR

Amazon ECR est le registre officiel des images Docker du projet :

```text
239280167189.dkr.ecr.eu-west-1.amazonaws.com/audit-qualite-pfa
```

Chaque publication réussie crée deux tags :

- `latest`, la version la plus récemment publiée ;
- `<commit_sha>`, la version immuable rattachée à un commit Git précis.

Le SHA est préférable pour un déploiement totalement reproductible et pour un
rollback. Le script ne supprime aucune image nécessaire à un retour arrière.

## 5. GitHub OIDC

GitHub Actions s'authentifie auprès d'AWS par OpenID Connect (OIDC). Il n'y a
donc ni AWS Access Key permanente ni AWS Secret Access Key enregistrée dans
GitHub. À chaque exécution autorisée, GitHub reçoit des credentials temporaires
en assumant un rôle IAM au périmètre limité.

La relation de confiance du rôle doit être limitée au dépôt et à la branche :

```text
repository : SalmaAb231/audit-qualite
branch     : hanane
subject    : repo:SalmaAb231/audit-qualite:ref:refs/heads/hanane
```

Cette configuration applique le principe du moindre privilège.

## 6. GitHubActionsECRRole

Le workflow assume le rôle suivant :

```text
arn:aws:iam::239280167189:role/GitHubActionsECRRole
```

Ce rôle doit seulement permettre les actions requises pour publier dans le
repository ECR `audit-qualite-pfa`. Il est distinct de
`AuditQualiteEC2Role`, utilisé par l'instance pour lire ECR et accéder à S3.

## 7. GitHub Actions

Le workflow versionné dans [`.github/workflows/ecr.yml`](.github/workflows/ecr.yml)
se déclenche :

- automatiquement lors d'un push sur `hanane` ;
- manuellement avec `workflow_dispatch`.

Ses permissions GitHub minimales sont :

```yaml
permissions:
  contents: read
  id-token: write
```

Il effectue, dans cet ordre : checkout, installation de Python 3.12,
installation de `requirements.txt`, `pytest tests/ -q`, configuration des
credentials AWS par OIDC, connexion à ECR, build Docker et publication des
deux tags. Le build et la publication ne s'exécutent que si les tests ont
réussi.

## 8. Publication de l'image dans ECR

Le flux de publication est automatique :

```text
push hanane
    -> tests
    -> build Docker
    -> authentification OIDC
    -> ECR : latest + <commit_sha>
```

La chaîne a été validée en conditions réelles : déclenchement du workflow,
tests, build, authentification OIDC et publication des deux tags dans ECR.
Cette étape publie l'image mais ne modifie aucun conteneur EC2.

## 9. Connexion à EC2

Le déploiement est déclenché par un opérateur connecté à l'instance :

```bash
ssh -i "<private-key.pem>" ec2-user@<EC2_PUBLIC_IP>
cd ~/audit-qualite
chmod +x deployment/deploy_ec2.sh
```

Le port SSH doit être autorisé depuis l'adresse publique courante du poste
client, selon les règles du Security Group décrites plus bas.

## 10. Déploiement depuis ECR

Depuis la racine du projet sur EC2, déployer la dernière image publiée :

```bash
IMAGE_TAG=latest ./deployment/deploy_ec2.sh
```

Pour déployer une version précise et immuable :

```bash
IMAGE_TAG=<commit_sha> ./deployment/deploy_ec2.sh
```

Le script :

1. vérifie Docker, AWS CLI, `curl`, le daemon Docker et le fichier
   d'environnement, sans afficher son contenu ;
2. s'authentifie à ECR avec `AuditQualiteEC2Role` ;
3. récupère l'image demandée avant de toucher au conteneur courant ;
4. arrête le conteneur courant, désactive son redémarrage automatique et le
   conserve sous le nom `audit-qualite-old` ;
5. lance `audit-qualite` avec `--restart unless-stopped`, le port `8501` et
   le fichier d'environnement ;
6. vérifie que le conteneur reste actif puis teste
   `http://127.0.0.1:8501` avec un nombre limité de tentatives ;
7. affiche `DEPLOYMENT SUCCESS` si le service répond.

Un éventuel `audit-qualite-old` antérieur est supprimé uniquement lorsqu'un
conteneur courant plus récent va prendre sa place comme sauvegarde. Les images
Docker ne sont pas nettoyées automatiquement.

## 11. Variables d'environnement

Le fichier runtime se trouve par défaut dans `~/audit-qualite.env`. Il doit
rester local à EC2 et être protégé :

```bash
chmod 600 ~/audit-qualite.env
```

Exemple sans secret réel :

```dotenv
STORAGE_MODE=S3
AWS_REGION=eu-west-1
S3_BUCKET=audit-qualite-leasing-pfa-hn-2026-01-239280167189-eu-west-1-an
S3_INPUT_PREFIX=input/
S3_REPORTS_PREFIX=reports/
S3_QUARANTINE_PREFIX=quarantine/
S3_HISTORY_PREFIX=history/
OPENROUTER_API_KEY=<your-openrouter-api-key>
```

Un autre chemin peut être fourni sans modifier le script :

```bash
ENV_FILE=/chemin/protege/audit-qualite.env IMAGE_TAG=<commit_sha> \
  ./deployment/deploy_ec2.sh
```

Ne jamais afficher, versionner ou copier dans la documentation la valeur réelle
de `OPENROUTER_API_KEY`. Ne jamais ajouter de credentials AWS permanents à ce
fichier.

## 12. Security Group

La configuration actuelle limite les accès :

| Port | Source |
|---|---|
| `22` (SSH) | `My IP` uniquement |
| `8501` (Streamlit) | `My IP` uniquement |

Le port SSH ne doit pas être ouvert à `0.0.0.0/0`. Si le poste client change de
Wi-Fi, son adresse IP publique peut changer : il faut alors actualiser `My IP`
dans le Security Group.

Ce changement d'adresse du client est différent d'un changement de l'IPv4
publique de l'instance EC2. Comme aucune Elastic IP n'est actuellement
utilisée, l'adresse publique de l'instance peut elle aussi changer après un
arrêt/redémarrage ; l'URL et la commande de connexion doivent alors employer la
nouvelle adresse.

## 13. Vérification

Sur EC2 :

```bash
docker ps --filter "name=^/audit-qualite$"
docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' audit-qualite
curl --fail http://127.0.0.1:8501
docker logs --tail 100 audit-qualite
```

Depuis le poste autorisé par le Security Group :

```text
http://<EC2_PUBLIC_IP>:8501
```

Le navigateur peut afficher « Non sécurisé » car HTTPS n'est pas configuré.
C'est accepté pour l'environnement PFA actuel, restreint à `My IP`. Une
exposition publique ou de production devrait ajouter HTTPS et une
authentification.

La validation réelle couvre la chaîne complète actuellement prévue :

- push sur `hanane`, déclenchement GitHub Actions, tests et build réussis ;
- authentification OIDC et publication ECR réussies avec `latest` et le SHA du
  commit ;
- authentification de l'instance avec `AuditQualiteEC2Role`, récupération de
  l'image ECR et lancement du nouveau conteneur réussis ;
- dashboard accessible sur le port 8501 ;
- audit réel terminé avec Structure, Statistics, Coherence et Realism en
  `SUCCESS`, pipeline en `COMPLETE`, Judge fonctionnel et historique S3
  persisté.

## 14. Rollback

Si le nouveau conteneur s'arrête, ne démarre pas ou échoue au health check, le
script affiche ses derniers logs, supprime uniquement ce nouveau conteneur et
restaure `audit-qualite-old` avec son nom, sa policy
`unless-stopped` et son état démarré. Il affiche alors :

```text
DEPLOYMENT FAILED - ROLLBACK COMPLETED
```

Le rollback manuel recommandé redéploie un tag ECR immuable :

```bash
IMAGE_TAG=<ancien_commit_sha> ./deployment/deploy_ec2.sh
```

Cette méthode reste reproductible et ne dépend pas indéfiniment de la présence
du conteneur temporaire `audit-qualite-old`.

## 15. Redéploiement d'une nouvelle version

1. Pousser le commit sur `hanane`.
2. Attendre que le workflow GitHub Actions ait réussi et publié les tags ECR.
3. Se connecter à EC2.
4. Exécuter le script avec `latest`, ou de préférence avec le SHA affiché par
   le commit validé.
5. Vérifier le dashboard et un audit avant de supprimer la sauvegarde.

Le déclenchement de l'étape 4 par GitHub Actions est une amélioration future ;
il n'est pas automatique aujourd'hui.

## 16. Troubleshooting

- **Authentification ECR refusée** : vérifier que
  `AuditQualiteEC2Role` est attaché à l'instance, que la région est
  `eu-west-1` et que la permission ECR ReadOnly est présente.
- **Tag introuvable** : vérifier que le workflow correspondant au commit a
  réussi et que le SHA fourni est complet et exact.
- **Docker inaccessible** : vérifier le service Docker et les droits de
  l'utilisateur EC2 sur le daemon.
- **Health check en échec** : consulter les logs affichés par le script, puis
  ceux du conteneur restauré si nécessaire. Vérifier aussi que le port 8501
  n'est pas occupé par un autre processus.
- **Dashboard inaccessible depuis le poste** : tester d'abord l'URL locale sur
  EC2, puis contrôler `My IP`, l'IPv4 publique actuelle de l'instance et la
  règle entrante du port 8501.
- **Erreur S3** : contrôler le nom du bucket, les quatre préfixes et les
  permissions S3 de `AuditQualiteEC2Role`.
- **Erreur OpenRouter** : vérifier uniquement la présence de la variable dans
  le fichier protégé ; ne jamais imprimer sa valeur dans un terminal partagé.

## 17. Nettoyage contrôlé

Après validation fonctionnelle de la nouvelle version, le conteneur de secours
arrêté peut être supprimé explicitement :

```bash
docker ps -a --filter "name=^/audit-qualite-old$"
docker rm audit-qualite-old
```

Ne pas supprimer le fichier `~/audit-qualite.env`. Ne pas lancer de nettoyage
global des images : conserver au moins les images référencées par les versions
de rollback encore nécessaires.

## Composants non utilisés actuellement

L'architecture actuelle n'utilise ni Elastic IP, ni domaine, ni Nginx, ni
HTTPS, ni Load Balancer, ni ECS, ni Fargate, ni EKS. Ces composants ne sont pas
des prérequis de la procédure ci-dessus.

Des améliorations futures possibles sont l'automatisation contrôlée du
déploiement EC2, HTTPS avec authentification, une adresse stable et une couche
d'entrée adaptée à une exposition de production.
