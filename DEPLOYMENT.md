# Déploiement AWS

Ce document est la référence technique du déploiement validé pour
`SalmaAb231/audit-qualite`, branche `hanane`. Après réussite des tests et de la
publication ECR, GitHub Actions déclenche automatiquement le déploiement EC2
par AWS Systems Manager avec l'image taguée par le SHA du commit.

## 1. Architecture

```text
Développeur
    |
    | git push origin hanane
    v
GitHub Actions
    |
    +------------------------------+
    | job test-build-push          |
    |                              |
    | checkout                     |
    | Python 3.12                  |
    | pytest tests/ -q             |
    | Docker build                 |
    | GitHub OIDC -> AWS IAM       |
    +---------------+--------------+
                    |
                    | push latest + <GITHUB_SHA>
                    v
              Amazon ECR privé
                    |
                    | image <GITHUB_SHA>
                    v
    +------------------------------+
    | job deploy-ec2               |
    | needs: test-build-push       |
    | GitHub OIDC -> AWS IAM       |
    | SSM SendCommand              |
    +---------------+--------------+
                    |
                    v
        AWS Systems Manager
        document AWS-RunShellScript
                    |
                    v
        EC2 audit-qualite-pfa-ec2
        Instance ID : i-080e098227f618f29
        Private IP  : 172.31.1.146
        Elastic IP  : 52.210.165.0
                    |
        /home/ec2-user/deploy_ec2.sh
                    |
           Docker audit-qualite
                    |
        Streamlit / Data Quality :8501
                    |
        +-----------+-----------+
        |           |           |
    Structure   Statistics   Coherence ----> OpenRouter
                                |
                             Realism ------> OpenRouter
                                |
                              Judge
                                |
                            Reporting
                                |
                         Amazon S3 privé
                input/ reports/ quarantine/ history/
```

Le déploiement EC2 est automatiquement déclenché après la réussite du job de
tests, build et push ECR. GitHub ne se connecte pas à l'instance avec SSH.

L'accès utilisateur suit un chemin distinct du déploiement :

```text
Utilisateur autorisé
    |
    | http://52.210.165.0:8501
    v
Security Group : TCP 8501 depuis My IP
    |
    v
Elastic IP 52.210.165.0
    |
    v
EC2 -> Docker -> audit-qualite -> Streamlit
```

## 2. Prérequis

| Élément | Configuration validée |
|---|---|
| Compte AWS | `239280167189` |
| Région | `eu-west-1` |
| Dépôt GitHub | `SalmaAb231/audit-qualite` |
| Branche | `hanane` |
| Instance EC2 | `i-080e098227f618f29` |
| Nom EC2 | `audit-qualite-pfa-ec2` |
| Système | Amazon Linux 2023 x86_64 |
| Type | `t3.micro` |
| Stockage | 8 GiB gp3 |
| Adresse privée EC2 | `172.31.1.146` |
| Elastic IP | `52.210.165.0` |
| Conteneur | `audit-qualite` |
| Port Streamlit | `8501` |
| Restart policy | `unless-stopped` |

L'instance doit disposer de Docker, AWS CLI et `curl`. Le daemon Docker doit
être accessible à `ec2-user`. L'agent SSM doit être actif et l'instance doit
apparaître `Online` dans Systems Manager Fleet Manager.

## 3. Elastic IP

| Élément | Valeur validée |
|---|---|
| Elastic IPv4 publique | `52.210.165.0` |
| Allocation ID | `eipalloc-06e2f50746cebc273` |
| Association ID | `eipassoc-0aa8b053f9e0ac555` |
| Instance associée | `i-080e098227f618f29` |
| Nom EC2 | `audit-qualite-pfa-ec2` |
| Adresse privée EC2 | `172.31.1.146` |
| DNS public | `ec2-52-210-165-0.eu-west-1.compute.amazonaws.com` |
| Région | `eu-west-1` |

Amazon Elastic IP fournit une adresse IPv4 publique fixe. L'adresse est
associée à l'interface réseau de l'instance et reste l'adresse publique de
référence après un Stop/Start tant que son allocation et son association sont
conservées.

Le dashboard a été testé à l'adresse :

```text
http://52.210.165.0:8501
```

Les identifiants d'allocation et d'association sont des identifiants de
ressource, pas des credentials. Ils ne donnent pas accès au compte AWS.

## 4. S3

Bucket utilisé :

```text
audit-qualite-leasing-pfa-hn-2026-01-239280167189-eu-west-1-an
```

| Préfixe | Rôle |
|---|---|
| `input/` | fichiers Excel/CSV à auditer |
| `reports/` | rapports HTML et verdicts générés |
| `quarantine/` | fichiers rejetés ou incomplets selon la logique applicative |
| `history/` | historique des exécutions et métriques |

Les rapports d'un run sont notamment rangés sous
`reports/runs/<run_id>/`. L'historique immuable utilise
`history/runs/YYYY/MM/DD/<run_id>.json`. Le bucket reste privé ; le dashboard
et le pipeline y accèdent avec le rôle de l'instance.

## 5. IAM EC2 Role

`AuditQualiteEC2Role` est utilisé par EC2 au runtime. Ses responsabilités sont
séparées de celles du rôle GitHub :

- lecture et écriture S3 selon la policy du projet ;
- `AmazonEC2ContainerRegistryReadOnly` pour l'authentification et le
  `docker pull` depuis ECR ;
- `AmazonSSMManagedInstanceCore` pour la communication avec Systems Manager.

Les credentials sont temporaires et fournis à l'instance par IAM. Aucune
`AWS_ACCESS_KEY_ID` ni `AWS_SECRET_ACCESS_KEY` statique n'est nécessaire sur
EC2.

## 6. Amazon ECR

Repository officiel :

```text
239280167189.dkr.ecr.eu-west-1.amazonaws.com/audit-qualite-pfa
```

Chaque build validé publie :

- `latest`, dernière image publiée ;
- `<commit_sha>`, image précise, immuable et traçable.

Le déploiement automatique utilise exclusivement `<commit_sha>`, jamais
`latest`. La relation est donc directe :

```text
commit Git = tag image Docker = version déployée sur EC2
```

Cette stratégie garantit la traçabilité, la reproductibilité, le rollback et
l'audit du déploiement.

## 7. GitHub OIDC

GitHub Actions s'authentifie sans secret AWS permanent :

```text
GitHub Actions
    |
    v
token OIDC temporaire
    |
    v
AWS IAM
    |
    v
GitHubActionsECRRole
```

OIDC évite de stocker `AWS_ACCESS_KEY_ID` et `AWS_SECRET_ACCESS_KEY` dans
GitHub. Les credentials ont une durée limitée, la relation de confiance est
restreinte au repository et à la branche, et les permissions suivent le
principe du moindre privilège.

La relation de confiance autorisée correspond à :

```text
repository : SalmaAb231/audit-qualite
branch     : hanane
subject    : repo:SalmaAb231/audit-qualite:ref:refs/heads/hanane
```

## 8. GitHubActionsECRRole

GitHub Actions assume par OIDC :

```text
arn:aws:iam::239280167189:role/GitHubActionsECRRole
```

Ce rôle couvre les besoins CI/CD validés :

- publication des images dans ECR ;
- envoi d'une commande SSM à l'instance autorisée ;
- lecture du statut et du résultat de cette commande.

La séparation des responsabilités est explicite :

```text
GitHubActionsECRRole = CI/CD
AuditQualiteEC2Role  = runtime EC2
```

## 9. Systems Manager

Le job GitHub utilise Systems Manager Run Command, document
`AWS-RunShellScript`, pour commander EC2 sans accès réseau entrant depuis
GitHub.

```text
GitHub Actions
    -> AWS Systems Manager
    -> AWS-RunShellScript
    -> EC2
    -> /home/ec2-user/deploy_ec2.sh
```

Commande distante validée :

```bash
sudo -u ec2-user -H env IMAGE_TAG=<commit_sha> /home/ec2-user/deploy_ec2.sh
```

Le workflow récupère uniquement le `CommandId`, attend la fin de Run Command,
puis lit `Status`, `StandardOutputContent` et `StandardErrorContent`. Cette
chaîne a été testée avec succès.

Le job cible `i-080e098227f618f29` par son Instance ID. Il ne dépend pas de
l'Elastic IP `52.210.165.0`, du DNS public ni de l'adresse IP du client. Un
changement de réseau utilisateur ou l'association de l'Elastic IP ne modifie
donc pas le chemin GitHub Actions → OIDC → SSM → EC2.

SSM remplace tout besoin de connexion SSH depuis GitHub, de clé `.pem` dans
GitHub, de SCP ou d'ouverture du port 22 aux runners GitHub.

## 10. GitHub Actions CI/CD

Le workflow [`.github/workflows/ecr.yml`](.github/workflows/ecr.yml) se
déclenche sur un push vers `hanane` et avec `workflow_dispatch`. Ses
permissions GitHub restent minimales :

```yaml
permissions:
  contents: read
  id-token: write
```

### Job `test-build-push`

1. checkout du repository ;
2. configuration de Python 3.12 ;
3. installation de `requirements.txt` ;
4. exécution de `pytest tests/ -q` ;
5. authentification AWS par OIDC ;
6. login ECR ;
7. build Docker ;
8. push des tags `latest` et `${GITHUB_SHA}`.

### Job `deploy-ec2`

Le job déclare `needs: test-build-push`. Aucun déploiement ne commence donc si
les tests, le build ou le push ECR échouent.

Il effectue :

1. une nouvelle authentification AWS par OIDC ;
2. `SSM SendCommand` vers `i-080e098227f618f29` ;
3. la récupération du `CommandId` ;
4. l'attente de la commande SSM ;
5. la récupération du statut, de stdout et de stderr ;
6. l'échec du job si le statut final n'est pas `Success`.

Pour `workflow_dispatch`, `${GITHUB_SHA}` désigne également la révision du
workflow exécutée et reste la version déployée.

## 11. Variables d'environnement EC2

Le fichier runtime est :

```text
/home/ec2-user/audit-qualite.env
```

Le script l'adresse par défaut comme `~/audit-qualite.env` lorsqu'il s'exécute
avec `ec2-user`. Ses permissions recommandées sont `600` :

```bash
chmod 600 ~/audit-qualite.env
```

Exemple sans valeur sensible :

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

La vraie clé OpenRouter ne doit jamais être affichée ni versionnée. Les rôles
IAM remplacent toute clé AWS statique dans ce fichier.

## 12. `deploy_ec2.sh`

Le script source est versionné dans
[`deployment/deploy_ec2.sh`](deployment/deploy_ec2.sh). Sa copie opérationnelle
testée sur EC2 se trouve dans `/home/ec2-user/deploy_ec2.sh`.

Il réalise les opérations suivantes :

1. vérification de Docker, AWS CLI, `curl` et du daemon Docker ;
2. vérification de l'existence du fichier d'environnement sans afficher son
   contenu ;
3. authentification ECR avec `AuditQualiteEC2Role` ;
4. construction de l'URI à partir du repository et de `IMAGE_TAG` ;
5. récupération de l'image demandée avant de toucher au conteneur courant ;
6. gestion d'un ancien `audit-qualite-old` ;
7. arrêt contrôlé du conteneur courant ;
8. conservation de ce conteneur sous le nom `audit-qualite-old` ;
9. lancement du nouveau conteneur `audit-qualite` ;
10. application de `--restart unless-stopped` ;
11. exposition de `8501:8501` ;
12. injection de `~/audit-qualite.env` avec `--env-file` ;
13. vérification de l'état `Running` ;
14. health check HTTP local avec plusieurs tentatives limitées ;
15. affichage de `DEPLOYMENT SUCCESS` après validation ;
16. rollback automatique en cas d'échec.

Limitation actuelle : le workflow déploie les images automatiquement, mais ne
synchronise pas une nouvelle version de `deployment/deploy_ec2.sh` vers EC2.
Si le script versionné change, sa copie `/home/ec2-user/deploy_ec2.sh` doit être
mise à jour séparément avant de dépendre de cette nouvelle version.

## 13. Déploiement automatique

Après un push sur `hanane` :

```text
tests réussis
    -> image latest + SHA publiée dans ECR
    -> job deploy-ec2
    -> SSM Run Command
    -> IMAGE_TAG=<GITHUB_SHA>
    -> récupération de l'image SHA sur EC2
    -> remplacement contrôlé du conteneur
    -> health check
    -> Success ou rollback
```

Le workflow attend le résultat distant. Un envoi SSM réussi ne suffit pas à
rendre le job vert : le statut final de l'invocation doit être `Success`.

## 14. Health Check

Le script teste localement :

```text
http://127.0.0.1:8501
```

Il vérifie d'abord que le conteneur reste en état `Running`, puis effectue
jusqu'à 15 tentatives HTTP espacées de quelques secondes. Une première
tentative peut échouer pendant le démarrage de Streamlit ; ce n'est pas à elle
seule un échec de déploiement. Le déploiement est accepté dès qu'une tentative
ultérieure réussit dans la limite configurée.

## 15. Rollback

### Rollback automatique

Si le nouveau conteneur ne démarre pas, quitte ou ne passe pas le health
check, le script :

1. affiche les derniers logs disponibles ;
2. arrête et retire uniquement le nouveau conteneur défaillant ;
3. restaure `audit-qualite-old` sous le nom `audit-qualite` ;
4. rétablit `unless-stopped` ;
5. redémarre l'ancienne version.

Résultat attendu lorsqu'une sauvegarde existe :

```text
DEPLOYMENT FAILED - ROLLBACK COMPLETED
```

### Retour vers une image ECR précise

Les tags SHA permettent de redéployer exactement une version antérieure :

```bash
sudo -u ec2-user -H env IMAGE_TAG=<ancien_commit_sha> /home/ec2-user/deploy_ec2.sh
```

Cette méthode reproductible évite de dépendre indéfiniment du conteneur de
sauvegarde temporaire.

## 16. Security Group

| Port | Source actuelle |
|---|---|
| `22` (SSH) | `My IP` uniquement |
| `8501` (Streamlit) | `My IP` uniquement |

Le port 22 ne doit jamais être ouvert à `0.0.0.0/0`. Un changement de Wi-Fi
peut modifier l'adresse IP publique du poste client ; il faut alors actualiser
`My IP` pour retrouver l'accès humain à SSH ou au dashboard.

Il faut distinguer deux adresses :

- l'Elastic IP `52.210.165.0` est l'adresse publique fixe du **serveur EC2** ;
- `My IP` est l'adresse publique du **client autorisé** par le Security Group.

L'IP cliente peut changer avec le Wi-Fi, le fournisseur Internet ou le réseau.
Il peut alors être nécessaire d'actualiser les deux règles `My IP`, même si
l'Elastic IP du serveur reste stable. Ce changement n'affecte pas le CI/CD
GitHub → SSM : GitHub ne se connecte pas à EC2 par SSH et cible l'Instance ID.

Le dashboard est accessible à l'adresse :

```text
http://52.210.165.0:8501
```

L'accès reste limité par le Security Group et n'est donc pas ouvert à tous les
réseaux.

L'indication « Non sécurisé » du navigateur est attendue puisque HTTPS n'est
pas configuré. Cette configuration convient à l'environnement PFA restreint à
`My IP`, pas à une exposition publique de production.

## 17. Validation de l'architecture

Les éléments suivants ont été réellement validés :

- image Docker construite par GitHub Actions ;
- tests GitHub Actions réussis ;
- OIDC GitHub → AWS réussi ;
- push ECR réussi ;
- tags `latest` et SHA présents ;
- récupération d'une image ECR sur EC2 réussie ;
- utilisation de `AuditQualiteEC2Role` ;
- agent SSM connecté ;
- instance EC2 visible `Online` dans Fleet Manager ;
- Elastic IP `52.210.165.0` associée et testée ;
- document `AWS-RunShellScript` testé ;
- commande SSM testée ;
- `/home/ec2-user/deploy_ec2.sh` testé via SSM ;
- health check réussi ;
- CI/CD complet testé ;
- jobs `test-build-push` et `deploy-ec2` en `Success` ;
- dashboard accessible après déploiement sur `http://52.210.165.0:8501` ;
- audit réel réussi ;
- Structure, Statistics, Coherence et Realism en `SUCCESS` ;
- pipeline en `COMPLETE` ;
- Judge fonctionnel ;
- historique S3 fonctionnel.

## 18. Troubleshooting

- **Le job `test-build-push` échoue** : corriger les tests, le build ou la
  publication ECR. `deploy-ec2` ne doit pas démarrer.
- **OIDC est refusé** : vérifier le repository, la branche, le subject de la
  trust policy et l'ARN de `GitHubActionsECRRole`.
- **SSM SendCommand échoue** : vérifier que l'instance est `Online`, que
  `AmazonSSMManagedInstanceCore` est attaché à `AuditQualiteEC2Role` et que le
  rôle GitHub peut cibler cette instance.
- **Le statut SSM n'est pas `Success`** : lire stdout et stderr dans le job
  `deploy-ec2`. Le script y affiche son succès ou le résultat du rollback.
- **Tag ECR introuvable** : vérifier que le SHA déployé correspond au SHA
  publié par le même run GitHub Actions.
- **Health check en échec** : examiner les logs remontés, l'état du conteneur
  et l'occupation locale du port 8501.
- **Dashboard inaccessible** : tester le health check local, puis vérifier
  l'association de l'Elastic IP `52.210.165.0` et la règle `My IP` du port
  8501.
- **Erreur S3** : contrôler le bucket, les quatre préfixes et la policy S3 de
  `AuditQualiteEC2Role`.
- **Script GitHub différent du script EC2** : mettre à jour séparément la
  copie opérationnelle, car le workflow ne la synchronise pas encore.

## 19. Coûts et arrêt EC2

L'instance `t3.micro`, le volume gp3, ECR, S3 et l'Elastic IP/IPv4 publique
peuvent générer des coûts selon la tarification AWS en vigueur. Aucun montant
fixe n'est documenté : il faut consulter la facturation AWS du compte.

Arrêter EC2 hors période d'utilisation suspend le calcul de l'instance et rend
le dashboard ainsi que les déploiements indisponibles jusqu'au redémarrage.
Le volume EBS, les images ECR et les objets S3 persistent et peuvent continuer
à être facturés. L'Elastic IP `52.210.165.0` reste l'adresse publique de
référence après le redémarrage tant qu'elle reste allouée et associée à
l'instance ou à son interface réseau.

Éviter de conserver une allocation Elastic IP détachée et devenue inutile. À
l'inverse, ne pas libérer celle-ci tant qu'elle doit rester l'adresse stable
du projet.

## 20. Nettoyage contrôlé

Après validation fonctionnelle d'une nouvelle version, le conteneur de secours
arrêté peut être inspecté puis supprimé explicitement :

```bash
docker ps -a --filter "name=^/audit-qualite-old$"
docker rm audit-qualite-old
```

Ne jamais supprimer `~/audit-qualite.env`. Ne pas lancer de nettoyage global
des images nécessaires au rollback. Une stratégie de rétention ECR pourra
ultérieurement supprimer de façon contrôlée les images qui ne sont plus
requises.

## 21. Améliorations futures

Les options suivantes ne sont pas nécessaires au PFA actuel :

- synchronisation automatique de `deploy_ec2.sh` vers EC2 ;
- HTTPS, reverse proxy, authentification du dashboard et domaine personnalisé ;
- CloudWatch, monitoring et alertes ;
- Infrastructure as Code avec Terraform ;
- stratégie de rétention ECR ;
- ECS/Fargate si un besoin d'évolutivité le justifie.

Les services actuellement utilisés sont EC2, Elastic IP, S3, ECR, les rôles
IAM, Systems Manager, Docker, GitHub Actions, GitHub OIDC, OpenRouter et
Streamlit.

L'architecture actuelle n'utilise ni domaine personnalisé, ni HTTPS, ni Nginx,
ni Load Balancer, ni ECS, ni Fargate, ni Kubernetes, ni EKS, ni Terraform.
