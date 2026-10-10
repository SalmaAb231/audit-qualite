# Déploiement AWS EC2 avec Docker

## Architecture réellement fournie par le dépôt

Le dashboard Streamlit et le pipeline tournent dans un conteneur Docker sur
EC2. En mode `STORAGE_MODE=s3`, l'application utilise S3 via la chaîne de
credentials boto3 ; sur EC2, le chemin recommandé est un rôle IAM, sans clés
AWS statiques dans le conteneur.

```text
Utilisateur
    |
    v
EC2 Amazon Linux 2023
    |
    +-- Docker
    |    |
    |    +-- Streamlit :8501
    |    +-- Pipeline Data Quality
    |    +-- Structure / Statistics / Coherence / Realism
    |    +-- Judge / Reporting
    |
    +-- IAM Role AuditQualiteEC2Role
             |
             v
        Amazon S3
             |
             +-- input/
             +-- reports/
             +-- quarantine/
             +-- history/

Coherence / Realism
    |
    v
OpenRouter
```

Le script [`deployment/deploy_ec2.sh`](deployment/deploy_ec2.sh) ne construit
pas d'image : il charge une image déjà présente localement sur l'instance,
puis remplace le conteneur `audit-qualite`. L'image est actuellement transférée
vers EC2 hors de ce script ; ECR n'est pas utilisé.

## Configuration de référence

| Élément | Valeur |
|---|---|
| Région | `eu-west-1` |
| Bucket S3 | `audit-qualite-leasing-pfa-hn-2026-01-239280167189-eu-west-1-an` |
| Préfixes | `input/`, `reports/`, `quarantine/`, `history/` |
| IAM Role EC2 | `AuditQualiteEC2Role` |
| Conteneur | `audit-qualite` |
| Image | `audit-qualite-pfa:latest` |
| Port Streamlit | `8501` |
| Restart policy | `unless-stopped` |
| Rapport par run | `reports/runs/<run_id>/` |
| Historique | `history/runs/YYYY/MM/DD/<run_id>.json` |

Elastic IP, domaine, Nginx et HTTPS ne sont pas utilisés actuellement.

## IAM et variables

Créer sur EC2 un fichier local, par exemple `~/audit-qualite.env`, avec des
permissions `600` :

```dotenv
STORAGE_MODE=s3
AWS_REGION=eu-west-1
S3_BUCKET=audit-qualite-leasing-pfa-hn-2026-01-239280167189-eu-west-1-an
S3_INPUT_PREFIX=input/
S3_REPORTS_PREFIX=reports/
S3_QUARANTINE_PREFIX=quarantine/
S3_HISTORY_PREFIX=history/
OPENROUTER_API_KEY=<clé fournie localement>
```

Ne jamais versionner ce fichier, une clé AWS, une clé OpenRouter ou un fichier
`.pem`. Le rôle IAM doit autoriser au minimum la lecture de `input/` et
`history/`, ainsi que l'écriture de `reports/`, `quarantine/` et `history/`.

## Construction et chargement de l'image

Depuis la racine du projet, construire et transférer l'image par le mécanisme
choisi par l'équipe. Le dépôt ne lance aucune de ces commandes
automatiquement :

```bash
docker build -t audit-qualite-pfa:latest .
docker save -o audit-qualite-pfa.tar audit-qualite-pfa:latest
scp -i "<clé-SSH>" audit-qualite-pfa.tar ec2-user@<EC2_PUBLIC_DNS>:/home/ec2-user/
```

Sur EC2 :

```bash
docker load -i ~/audit-qualite-pfa.tar
chmod +x ~/deployment/deploy_ec2.sh
~/deployment/deploy_ec2.sh
```

Le script exige `STORAGE_MODE`, `OPENROUTER_API_KEY` et une image locale
existante. Il écoute par défaut sur `0.0.0.0:8501` côté Docker et applique
`--restart unless-stopped`.

## Vérifications après lancement

```bash
docker ps --filter "name=^/audit-qualite$"
docker logs --tail 100 audit-qualite
docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' audit-qualite
```

Le Security Group doit limiter SSH et le port 8501 aux sources nécessaires.
