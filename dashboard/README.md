# Data Quality Audit Dashboard

Le dashboard Streamlit fournit deux zones distinctes :

- **Monitoring en lecture seule** : Overview, Files, Anomalies, Agents, Quarantine et Runs utilisent les JSON de `history/runs/` pour leurs agrégats. Les verdicts détaillés, rapports et sources sont lus sur demande via `artifacts.py` ; aucune de ces pages ne relance le pipeline.
- **Action** : New Audit appelle directement `orchestrateur.executer_audit()` et déclenche le pipeline existant. Il ne modifie ni verdict, ni anomalies, ni quarantine, ni history manuellement.

## Architecture

- `app.py` : configuration Streamlit, navigation, cache et filtres globaux.
- `data_loader.py` : chargement history LOCAL/S3 et tolérance aux JSON invalides.
- `transformations.py` : DataFrames `runs_df`, `files_df`, `agents_df`, `severity_df`.
- `components.py` : composants et présentation partagés.
- `audit_service.py` : interface Python entre l'UI et l'orchestrateur.
- `artifacts.py` : lectures contrôlées LOCAL/S3, téléchargement des rapports et sources, cache des artefacts.
- `anomaly_data.py` : détails et traçabilité des anomalies issus des verdicts persistés.
- `pages/` : pages fonctionnelles du dashboard.

La navigation automatique du dossier `pages/` est désactivée dans `.streamlit/config.toml` : la sidebar de `app.py` reste l'unique navigation visible.

## Dashboard UI

L'interface utilise un style sobre bleu/indigo centralisé dans `styles.py`, avec des composants réutilisables pour les headers, KPI, badges de statut, résumés d'audit et états vides. La navigation, les tableaux et les cartes restent responsives avec le layout Streamlit `wide`. Cette couche est strictement visuelle : elle ne modifie ni les calculs, ni l'orchestrateur, ni le comportement de New Audit.

## Modes de stockage

En mode LOCAL, l'historique est lu sous `HISTORY_DIR/runs/` et les fichiers disponibles sont listés par `LocalLoader` sous `DATA_DIR`.

En mode S3, l'historique est lu sous `S3_HISTORY_PREFIX/runs/` et les entrées sont listées par `S3Loader` sous `S3_INPUT_PREFIX`. Le bucket n'est jamais rendu public. Le code utilise la chaîne standard de credentials boto3 : variables d'environnement ou profil sur un poste, rôle IAM sur EC2.

Avec la valeur recommandée `S3_HISTORY_PREFIX=history/`, le writer et le dashboard
utilisent exactement `history/runs/`. La construction des clés est POSIX, y compris
sous Windows. Une valeur déjà terminée par `runs/` est normalisée sans produire
`runs/runs/`.

```powershell
$env:STORAGE_MODE = 'local'
streamlit run dashboard/app.py
```

```powershell
$env:STORAGE_MODE = 's3'
streamlit run dashboard/app.py
```

Pour un lancement direct écoutant sur le réseau (hors conteneur, à limiter par
les règles réseau appropriées) :

```powershell
streamlit run dashboard/app.py --server.address 0.0.0.0
```

Le déploiement EC2 utilise Docker, ECR et Nginx : le port de l'hôte est publié
sur `127.0.0.1:8501` par défaut. Voir [DEPLOYMENT.md](../DEPLOYMENT.md).

## New Audit

Les trois portées sont disponibles : Single File, Multiple Files et Full LOAD_LOCK Folder. Une sélection multiple ou un dossier complet exige une confirmation explicite et rappelle le risque de consommation du quota OpenRouter. Un clic produit une exécution logique, un `run_id` et un seul document history. Le traitement reste synchrone et séquentiel.

Valider d'abord un petit fichier, puis deux petits fichiers, et seulement ensuite un LOAD_LOCK complet. Il n'existe pas de faux bouton Stop : interrompre le serveur avec `Ctrl+C` interrompt aussi la session Streamlit.

L'upload manuel de nouveaux fichiers n'est volontairement pas inclus dans cette première version. New Audit travaille uniquement avec les fichiers déjà présents dans la source d'entrée.

## Cache

Le history et ses DataFrames sont mis en cache pendant 300 secondes, la liste
des entrées pendant 120 secondes, et les artefacts pendant
`ARTIFACT_CACHE_TTL_SECONDS` (900 secondes par défaut). Le bouton **Refresh data**
appelle `st.cache_data.clear()` : il invalide ces caches, sans relancer d'audit.
Après un audit, le cache est vidé et le nouveau run est relu depuis history.
Les détails d'anomalies restent dans les verdicts référencés ; ils ne sont pas
dupliqués dans history. Les sources et copies en quarantaine peuvent évoluer
indépendamment des rapports versionnés par run.

## Permissions IAM conceptuelles

Monitoring uniquement :

- `s3:ListBucket` sur l'ARN du bucket, avec un filtre autorisant au minimum `history/*` (et `history/` si la policy distingue les deux valeurs) ;
- `s3:GetObject` sur `history/*`, et éventuellement `reports/*` et `quarantine/*`.

New Audit ajoute les permissions du pipeline :

- `s3:GetObject` sur `input/*` ;
- `s3:PutObject` sur `reports/*`, `quarantine/*` et `history/*` ;
- `s3:ListBucket` autorisant `input/*` ainsi que `history/*`, puisque la même application relit history après l'audit.

`s3:PutObject` sur `input/*` n'est pas requis tant que l'upload manuel reste désactivé. Adapter les ARN au bucket configuré sans inscrire de credentials dans le code.

Une policy qui autorise seulement `GetObject` sur `input/*` et `PutObject` sur
`reports/*` / `quarantine/*` est insuffisante. Il faut explicitement ajouter :

- `s3:PutObject` sur `<bucket-arn>/history/*` pour le writer ;
- `s3:GetObject` sur `<bucket-arn>/history/*` pour le dashboard ;
- `s3:ListBucket` sur `<bucket-arn>` avec le préfixe `history/*` pour le listing.

Le dashboard journalise le bucket et le préfixe ou la clé concernés, jamais les
credentials. Un `AccessDenied` n'est plus présenté comme un historique simplement vide.

## Limites actuelles

- l'exécution est synchrone : une analyse longue immobilise la session qui l'a lancée ;
- aucun worker, file de tâches ou mécanisme d'annulation n'est fourni ;
- les pages affichent les compteurs d'anomalies présents dans history, sans inventer de détail absent ;
- les rapports sont référencés par leur chemin ou clé, sans rendre S3 public ;
- la protection contre un second audit concerne la session Streamlit courante ; aucun verrou global multi-utilisateurs n'est fourni ;
- EC2, Docker, ECR et Nginx sont documentés dans [DEPLOYMENT.md](../DEPLOYMENT.md) ; HTTPS et CI/CD ne sont pas configurés dans le dépôt.
