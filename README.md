# Audit Qualité Leasing Automobile

## Présentation

Pipeline d'audit de qualité pour des fichiers Excel/CSV de renting
automobile (location longue durée de véhicules). Chaque fichier passe par
une série de détecteurs indépendants qui produisent des **anomalies** dans
un format JSON commun, agrégées ensuite en un **verdict** par fichier
(`ACCEPTED` / `WARNING` / `REJECTED`) puis mises en forme dans des
**rapports HTML**. Le mode local est utilisé pour le développement ; le mode
S3 est disponible pour un déploiement sur AWS EC2 avec un rôle IAM.

## Problématique

Les fichiers de leasing automobile proviennent de sources hétérogènes : noms
de colonnes, formats, langues et règles métier peuvent varier. Un contrôle
manuel est difficile à reproduire, coûteux à maintenir et peu traçable. La
plateforme automatise ces contrôles sans imposer un schéma unique aux fichiers
d'entrée.

## Objectifs

- détecter les anomalies structurelles, statistiques, logiques et métier ;
- produire un verdict explicable et des rapports exploitables ;
- conserver les entrées, résultats, quarantaines et historiques dans S3 ;
- fournir un dashboard Streamlit pour lancer et suivre les audits ;
- garantir qu'un commit validé correspond exactement à l'image déployée ;
- automatiser les tests, la publication ECR et le déploiement EC2 avec un
  rollback sûr.

Un quatrième état technique, `INCOMPLETE`, est utilisé lorsqu'une étape LLM
obligatoire n'a pas terminé. Ce n'est pas un verdict qualité : dans ce cas le
score, le grade et le verdict restent à `null` pour éviter un résultat
faussement rassurant.

## Pipeline Data Quality

```
Fichier (Excel/CSV)
      │
      ▼
1. structure/     — la forme du fichier est-elle valide ? (colonnes, doublons, types)
      │  si anomalie "critical" → arrêt, verdict REJECTED direct, aucun coût LLM
      ▼
2. stats/  ET  agents/agent_coherence.py   — EN PARALLÈLE (deux canaux indépendants)
      │  stats/ : valeurs statistiquement plausibles ? (IQR, corrélations) — tout le fichier, 100% mathématique
      │  Cohérence : contradiction logique entre colonnes d'une même ligne ? — tout le fichier, LLM
      ▼
3. agents/agent_realisme.py — les valeurs ont-elles un sens dans le monde réel automobile ?
      │  LLM, UNIQUEMENT sur les lignes suspectes (repérées par 1-2) + échantillon de contrôle,
      │  puis vérification par recherche web (ddgs, gratuit) des anomalies candidates
      ▼
4. judge/          — agrège toutes les anomalies (peu importe qui les a trouvées) → score, grade, verdict
      ▼
5. reporting/      — rapport HTML par fichier + rapport consolidé sur tous les fichiers traités
      ▲
      └── orchestrateur.py fait circuler chaque fichier à travers ces 5 étapes
```

## Architecture générale

Le dashboard Streamlit et le pipeline complet s'exécutent dans le conteneur
Docker `audit-qualite` sur EC2. Un push sur `hanane` déclenche désormais toute
la chaîne CI/CD, jusqu'au déploiement automatique par AWS Systems Manager.

```text
Développeur
    |
    | push sur hanane
    v
GitHub Actions
    |
    +-- job test-build-push
    |     +-- Python 3.12 et pytest tests/ -q
    |     +-- build Docker si les tests réussissent
    |     +-- OIDC puis push ECR : latest + <commit_sha>
    v
Amazon ECR
239280167189.dkr.ecr.eu-west-1.amazonaws.com/audit-qualite-pfa
    |
    | image taguée avec le SHA du commit
    |
    v
GitHub Actions / job deploy-ec2
    |
    +-- needs: test-build-push
    +-- OIDC puis SSM SendCommand
    v
AWS Systems Manager / AWS-RunShellScript
    |
    v
EC2 audit-qualite-pfa-ec2
    +-- Instance ID : i-080e098227f618f29
    +-- Private IP  : 172.31.1.146
    +-- Elastic IP  : 52.210.165.0
    |
    v
/home/ec2-user/deploy_ec2.sh
    |
    +-- authentification ECR avec AuditQualiteEC2Role
    +-- docker pull :<commit_sha>
    +-- health check et rollback automatique
    v
EC2 / Docker / audit-qualite :8501
    |
    +-- Dashboard Streamlit
    +-- Structure Agent
    +-- Statistics Agent
    +-- Coherence Agent ------> OpenRouter
    +-- Realism Agent --------> OpenRouter
    +-- Judge
    +-- Reporting
    |
    v
Amazon S3 privé
    +-- input/
    +-- reports/
    +-- quarantine/
    +-- history/

Utilisateur autorisé
    |
    | http://52.210.165.0:8501
    v
Security Group (port 8501 depuis My IP)
    |
    v
Elastic IP 52.210.165.0 -> EC2 -> Streamlit
```

Amazon ECR est le registre officiel des images du projet. `latest` désigne la
dernière image publiée ; le tag `<commit_sha>` identifie une version immuable
et traçable. Le job `deploy-ec2` utilise le SHA, et non `latest`, afin que le
commit Git, l'image Docker et la version exécutée sur EC2 correspondent
exactement. Les tags SHA permettent aussi un rollback reproductible.

## CI/CD

Le workflow complet est automatique :

```text
push hanane -> tests -> build Docker -> Amazon ECR -> SSM -> EC2
```

Le workflow [`.github/workflows/ecr.yml`](.github/workflows/ecr.yml) se
déclenche sur un push vers `hanane` ou avec `workflow_dispatch`. Ses
permissions sont limitées à `contents: read` et `id-token: write`. Il assume
temporairement le rôle
`arn:aws:iam::239280167189:role/GitHubActionsECRRole` avec GitHub OIDC, sans
AWS Access Key ni AWS Secret Access Key permanentes dans GitHub. La relation de
confiance est limitée au dépôt `SalmaAb231/audit-qualite` et à la branche
`hanane`.

Le job `deploy-ec2`, dépendant de `test-build-push`, s'authentifie à nouveau
par OIDC puis utilise SSM pour exécuter la copie opérationnelle du script sur
l'instance avec `IMAGE_TAG=${GITHUB_SHA}`. Il attend la fin de la commande et
remonte son statut, sa sortie standard et sa sortie d'erreur. Le script
récupère l'image immuable, remplace le conteneur avec sauvegarde temporaire,
effectue un health check local et restaure l'ancienne version en cas d'échec.

Le job cible l'instance par Systems Manager avec l'Instance ID
`i-080e098227f618f29`. Il ne dépend ni de l'Elastic IP `52.210.165.0` ni de
l'adresse IP du poste utilisateur. L'Elastic IP stabilise l'accès au
dashboard, sans modifier le fonctionnement du CI/CD.

Ainsi, le commit Git, le tag de l'image ECR et la version réellement déployée
sont identiques. L'ensemble de cette chaîne a été validé en conditions
réelles, jusqu'à l'exécution d'un audit complet, au verdict du Judge et à
l'historique S3.

La procédure AWS, la configuration du Security Group, les variables runtime,
les vérifications et le rollback sont détaillés dans
[`DEPLOYMENT.md`](DEPLOYMENT.md).

## Choix de conception : schema-agnostic

Les fichiers source ont des structures de colonnes différentes, et chaque
nouveau fichier peut avoir une structure inédite (noms de colonnes, ordre,
langue variables selon le loueur/constructeur). Le projet ne fait donc
**aucun mapping de schéma ni normalisation de colonnes** : chaque fichier
est traité tel quel, avec ses colonnes réelles. Les vérifications
mathématiques (`structure/`, `stats/`) s'adaptent dynamiquement à n'importe
quelle colonne numérique/texte ; les agents LLM (`Réalisme`, `Cohérence`)
lisent les noms de colonnes réels sans schéma imposé.

## Le contrat commun d'anomalie

Tous les détecteurs (déterministes ou LLM) produisent des anomalies dans le
**même format JSON**, défini dans [`docs/contrat_anomalie.md`](docs/contrat_anomalie.md) :

```json
{
  "fichier": "NL_B2B_ABT_OFF.xlsx",
  "ligne": 42,
  "colonnes": ["Price"],
  "type": "realisme",
  "severite": "critical",
  "source": "llm",
  "valeur": "8699956",
  "justification": "Un loyer mensuel de 8 699 956€ est irréaliste pour ce véhicule.",
  "confiance": 0.99
}
```

- `type` : `structure` / `statistique` / `coherence` / `realisme` — qui a détecté.
- `severite` : `critical` / `high` / `medium` / `low` — gravité, pondérée dans `config.py`.
- `source` : `stats` (déterministe) ou `llm`.

C'est ce contrat commun qui permet à `judge/` d'agréger les anomalies de
tous les détecteurs sans traitement spécifique par source, et à
`reporting/` de les afficher de façon uniforme.

## Agents IA et composants du pipeline

### `config.py` (racine)

Configuration centralisée : **tout seuil, tout chemin, tout paramètre LLM
passe par ce fichier**, jamais codé en dur dans un module. Contient
notamment :
- `DATA_DIR`, `RESULTATS_DIR`, `CACHE_LLM_DIR` : chemins.
- `MODELES`, `TEMPERATURES`, `MAX_TOKENS` : un jeu de modèles OpenRouter par
  agent (avec cascade de fallback), tous `:free`.
- `RATE_LIMIT`, `CHUNK_SIZE`, `MAX_LIGNES_PAR_FICHIER`,
  `TAUX_ECHANTILLON_CONTROLE` : contraintes du tier gratuit OpenRouter et
  découpage des données envoyées aux agents LLM.
- `PONDERATION_SEVERITE`, `FACTEUR_PENALITE_CRITICAL`, `SEUILS_GRADE`,
  `PLAFOND_PENALITE_NON_BLOQUANTE`, `SEUIL_ACCEPTED`, `SEUIL_WARNING` :
  tout ce qui sert au calcul du score/grade/verdict dans `judge/`.

### `orchestrateur.py` (racine)

Le chef de gare : pour chaque fichier, enchaîne `structure/` → (arrêt si
`critical`) → `stats/` **et** `agent_coherence` **en parallèle**
(`ThreadPoolExecutor`, deux canaux indépendants sur le même fichier déjà
chargé — Cohérence reçoit directement ce `df` et les anomalies de structure
déjà calculées, sans recharger le fichier ni revérifier son exploitabilité)
→ sélection des lignes suspectes (anomalies de structure + stats +
cohérence, plus un échantillon de contrôle) → `agent_realisme` → `judge/` →
`reporting/`. Les deux agents LLM remontent `SUCCESS`, `PARTIAL` ou `FAILED` ;
si une étape obligatoire reste incomplète (quota ou erreur), le Judge ne
produit ni verdict qualité ni score. Un dossier de résultats séparé par mois d'origine
(`resultats/LOAD_LOCK_AAAAMM/`) évite toute collision entre deux fichiers de
même nom dans des mois différents. Détails et décisions de conception dans
[`regles_orchestrateur.md`](regles_orchestrateur.md).
Les garanties de reprise et de propagation des erreurs sont détaillées dans
[`docs/fiabilite_pipeline.md`](docs/fiabilite_pipeline.md).

⚠️ Cohérence traite tout le fichier (pas seulement les lignes suspectes) :
elle coûte beaucoup plus de requêtes LLM que Réalisme. Sur un run
multi-fichiers, le quota gratuit (45/jour) peut s'épuiser sur un seul gros
fichier — voir « Contraintes importantes » plus bas.

```powershell
python -m orchestrateur                          # tous les fichiers de DATA_DIR
python -m orchestrateur NL_B2B_BRO_OFF.xlsx       # fichier(s) nommé(s)
```

### `structure/` — 1er détecteur : la forme du fichier

- **`agent_structure.py`** : vérifie que le fichier est bien formé
  (colonnes sans nom/dupliquées/vides, lignes dupliquées, taux de valeurs
  manquantes, types de données incohérents dans une colonne texte). 100%
  déterministe (pandas), aucun LLM. `fichier_exploitable()` décide si une
  anomalie `critical` doit stopper tout le reste du pipeline pour ce
  fichier (seul module à pouvoir prendre cette décision).
- **`regles.md`** : le détail de chaque vérification, ses seuils, et les
  limites découvertes en testant sur données réelles (ex. pourquoi un index
  pandas mal exporté ou un champ optionnel très creux ne sont plus classés
  `high`).

```powershell
python -m structure.agent_structure <fichier_ou_chemin>
```

### `stats/` — 2e détecteur : la plausibilité statistique

- **`agent_stats.py`** : 5 vérifications mathématiques sur les colonnes
  numériques (outliers via IQR, signes négatifs minoritaires, colonnes
  quasi-constantes, cardinalité suspecte) plus une vérification
  **multi-colonnes** (`verifier_incoherence_entre_colonnes`) qui détecte
  une incohérence entre deux colonnes numériques corrélées (ex. "petite
  voiture, prix énorme") sans connaître leur sens métier — via régression
  linéaire + détection d'outliers sur les résidus. Aucun LLM.
- **`regles.md`** : seuils et surtout les limites découvertes en testant
  sur données réelles (sous-populations de véhicules qui cassent une
  régression globale, colonnes à faible cardinalité qui produisent des
  milliers de faux positifs si on ne les exclut pas).

```powershell
python -m stats.agent_stats <fichier_ou_chemin>
```

### `agents/` — socle LLM commun + agents Cohérence et Réalisme

- **`llm_client.py`** : client HTTP pour l'API OpenRouter, partagé par tous
  les agents LLM (Cohérence, Réalisme, à terme Judge/Reporting). Gère la
  cascade de modèles (`config.MODELES`), les retries progressifs, le cache
  disque des réponses (`cache_llm/`, pour ne jamais repayer une requête
  identique) et le respect strict du quota gratuit quotidien
  (`quota_restant()`).
  Un JSON invalide est une erreur retentable : le client applique le backoff,
  essaie la cascade de modèles puis `openrouter/free` en dernier recours.
- **`parse_utils.py`** : extraction robuste de JSON depuis une réponse LLM
  (qui peut être mal formée, tronquée, entourée de texte) et validation de
  chaque anomalie contre le contrat commun.
- **`agent_coherence.py`** : détecte les contradictions logiques **entre
  colonnes d'une même ligne** (ex. motorisation électrique avec une
  consommation en L/100km). Lit tout le fichier (échantillonné seulement
  au-delà de `config.SEUIL_FICHIER_GEANT`), gère son propre checkpoint
  versionné, identifié par chemin source relatif + SHA-256 du contenu +
  version (`resultats/coherence/checkpoints/`)
  et sauvegarde son résultat complet dans `resultats/coherence/<fichier>.json`.
  Seuls les chunks réussis sont réutilisés à la reprise ; les chunks en échec
  sont automatiquement retentés. Une modification du contenu ou de la version
  de checkpoint crée une nouvelle identité.
  `analyser_fichier(chemin, df=None, anomalies_structure=None)` : les deux
  derniers paramètres sont optionnels — fournis par `orchestrateur.py`
  (appel en parallèle de `stats/`) pour éviter de recharger le fichier et de
  revérifier son exploitabilité une seconde fois ; absents en usage autonome
  (charge et vérifie elle-même, comme avant).
- **`prompts/coherence.md`** : le prompt système de cet agent (fichier
  séparé plutôt qu'une constante Python, pour l'itérer sans toucher au code).
- **`agent_realisme.py`** : juge si une valeur a un sens dans le **monde
  réel automobile** (une marque/modèle/puissance/prix qui existe vraiment),
  par opposition à Cohérence (contradiction logique entre colonnes) ou
  `stats/` (aberration purement statistique). Découpe les lignes reçues en
  chunks (`config.CHUNK_SIZE`), injecte la date du jour dans le prompt
  (sans ça, le modèle ne sait pas si une date est dans le futur ou le
  passé). Ne décide jamais lui-même quelles lignes analyser : c'est
  `orchestrateur.py` qui lui fournit un sous-ensemble de lignes. Vérifie
  ensuite ses propres anomalies candidates par une **recherche web**
  (bibliothèque `ddgs`, DuckDuckGo, gratuite, sans clé API) : un seul appel
  LLM de vérification par chunk (pas par anomalie), qui confirme ou retire
  une anomalie selon des résultats de recherche récents — pilotable via
  `config.RECHERCHE_WEB_ACTIVEE`.
  L'API structurée `analyser_avec_statut()` expose les compteurs de chunks et
  de lignes ainsi que `SUCCESS`/`PARTIAL`/`FAILED`. L'orchestrateur désactive
  son échantillonnage interne pour analyser exactement le lot qu'il a choisi.
- **`regles_realisme.md`** : frontière avec Cohérence, mécanisme de
  vérification web en détail, limites connues.
- **`test_premier_appel.py`** : script de preuve du socle LLM sur un
  fichier réel (historique, antérieur aux agents complets).
- **`test_coherence.py`**, **`test_coherence_injection.py`** : tests dédiés
  à l'agent Cohérence (calibration, injection d'anomalies synthétiques).

`agent_coherence.py` n'a pas de CLI : s'utilise via
`AgentCoherence().analyser_fichier(chemin)` (voir `test_coherence.py`).

```powershell
python -m agents.agent_realisme <fichier_ou_chemin>
```

### `judge/` — agrégation et verdict

- **`agent_judge.py`** : ne détecte rien, agrège les anomalies déjà
  produites par tous les détecteurs et calcule un score (0 à 100), un grade
  (A à F) et un verdict (`ACCEPTED`/`WARNING`/`REJECTED`), 100%
  déterministe. Une anomalie `critical`/`high` pèse sans limite ; les
  `medium`/`low` sont plafonnées (`config.PLAFOND_PENALITE_NON_BLOQUANTE`)
  pour qu'un détecteur purement statistique ne puisse jamais, à lui seul,
  rejeter un fichier.
  Il est entièrement déterministe et ne consomme aucun quota LLM. Lorsque
  `pipeline_status="INCOMPLETE"`, il conserve le diagnostic mais ne publie
  aucun score, grade ou verdict qualité.
- **`regles_judge.md`** : la formule de score en détail, et les
  découvertes de calibration sur données réelles (pourquoi ce plafond a
  été ajouté, pourquoi certaines notes comme "B" restent difficiles à
  atteindre).

```powershell
python -m judge.agent_judge <chemin_json_anomalies>
```

### `reporting/` — rapports HTML

- **`agent_reporting.py`** : `generer_rapport_fichier()` (une page HTML par
  fichier : score, verdict, tableau des anomalies triées par sévérité) et
  `generer_rapport_consolide()` (vue d'ensemble sur tous les fichiers d'un
  run : répartition des verdicts, anomalies par type/sévérité, top des
  anomalies les plus fréquentes, évolution par mois). Tout texte affiché
  est échappé (protection contre l'injection HTML, les justifications
  provenant en bout de chaîne d'un LLM lisant des données externes).
  Les analyses incomplètes sont affichées explicitement avec leur propre
  badge et des valeurs `-` pour le score et le grade absents.
- **`regles_reporting.md`** : format attendu des données en entrée,
  notion de "même anomalie" pour le calcul de fréquence.

```powershell
python -m reporting.agent_reporting fichier <chemin_verdict.json>
python -m reporting.agent_reporting consolide [dossier_resultats]
```

### `ingestion/` — accès aux fichiers de données

- **`local_loader.py`** (`LocalLoader`) : lit les fichiers depuis `DATA_DIR`
  local (développement).
- **`s3_client.py`** (`S3Loader`) : même interface publique que
  `LocalLoader`, mais télécharge depuis S3 avec cache local — utilisé au
  déploiement AWS uniquement.
- **`test_s3_connexion.py`** : diagnostic de connexion S3 avant déploiement.
- **`profiling.py`** : génère `resultats/inventaire.csv` (colonnes, taille,
  langue probable). En exécution autonome, il inventorie `DATA_DIR`; depuis
  l'orchestrateur, il utilise le loader actif LOCAL/S3 et uniquement la
  sélection du run. Cet inventaire reste purement informatif — jamais utilisé
  pour unifier les schémas.

```powershell
python -m ingestion.profiling
python -m ingestion.test_s3_connexion
```

## Stockage S3 et traçabilité

En mode `STORAGE_MODE=s3`, les entrées sont lues sous `input/`. Les rapports
et verdicts sont écrits sous `reports/runs/<run_id>/`, tandis que l'historique
immuable est écrit sous `history/runs/YYYY/MM/DD/<run_id>.json`. Les fichiers
`REJECTED` ou `INCOMPLETE` sont copiés sous `quarantine/`. Le dashboard relit
l'historique et les artefacts via le même bucket ; il ne rend aucun objet S3
public.

Le bucket de référence documenté pour le déploiement est
`audit-qualite-leasing-pfa-hn-2026-01-239280167189-eu-west-1-an`. Le code accepte aussi
un autre bucket via `S3_BUCKET`.

| Préfixe | Rôle |
|---|---|
| `input/` | fichiers Excel/CSV à auditer |
| `reports/` | rapports HTML et verdicts générés |
| `quarantine/` | sources rejetées ou incomplètes selon la logique applicative |
| `history/` | historique immuable des exécutions et métriques |

## Dashboard

Le dashboard Streamlit est servi par le conteneur sur le port `8501`. Il
permet de lancer un audit et de consulter les vues Overview, Files, Anomalies,
Agents, Quarantine et Runs à partir des artefacts et historiques S3. Le bucket
n'est jamais rendu public. Les détails fonctionnels figurent dans
[`dashboard/README.md`](dashboard/README.md).

### Accès au dashboard

Adresse validée :

```text
http://52.210.165.0:8501
```

`52.210.165.0` est l'Elastic IP fixe associée à l'instance EC2 ; Streamlit
écoute sur le port `8501`. Le Security Group limite actuellement ce port à
`My IP`, donc le dashboard n'est pas accessible depuis n'importe quel réseau.
HTTPS n'est pas configuré et le navigateur peut afficher « Non sécurisé ».

## Docker

L'image Python 3.12 embarque l'application et démarre Streamlit sur
`0.0.0.0:8501`. GitHub Actions construit l'image après les tests et la publie
dans ECR avec les tags `latest` et `<commit_sha>`. Sur EC2, le conteneur
`audit-qualite` utilise `--restart unless-stopped` et le fichier runtime
`~/audit-qualite.env`.

## Infrastructure AWS

L'infrastructure validée se trouve dans le compte `239280167189`, région
`eu-west-1` :

- EC2 `audit-qualite-pfa-ec2`, instance `i-080e098227f618f29`, Amazon Linux
  2023 x86_64, type `t3.micro`, volume gp3 de 8 GiB ;
- Elastic IP fixe `52.210.165.0`, associée à l'adresse privée EC2
  `172.31.1.146` ;
- repository ECR privé `audit-qualite-pfa` ;
- bucket S3 privé indiqué ci-dessus ;
- AWS Systems Manager pour commander le déploiement sans connexion SSH depuis
  GitHub.

Deux rôles IAM ont des responsabilités séparées :

- `GitHubActionsECRRole`, assumé temporairement par GitHub OIDC, publie dans
  ECR et pilote la commande SSM ;
- `AuditQualiteEC2Role`, attaché à EC2, fournit l'accès S3, la lecture ECR et
  `AmazonSSMManagedInstanceCore`.

Aucune AWS Access Key statique n'est nécessaire dans GitHub ou sur EC2.

## Rollback et résilience

Le script conserve le conteneur précédent sous `audit-qualite-old`. Si le
nouveau conteneur ne démarre pas, quitte ou échoue au health check local sur
`http://127.0.0.1:8501`, il affiche ses logs et restaure automatiquement
l'ancienne version avec la policy `unless-stopped`. Un tag SHA ECR antérieur
permet également un redéploiement précis et reproductible.

## Sécurité

- authentification AWS temporaire par OIDC et séparation des rôles IAM ;
- aucun secret AWS permanent, aucune clé `.pem` et aucune connexion SSH depuis
  GitHub Actions ;
- déploiement distant exclusivement par SSM ;
- `.env` et `audit-qualite.env` non versionnés, `.env.example` sans secret ;
- ECR et S3 privés ;
- ports `22` et `8501` limités à `My IP` dans le Security Group.

L'Elastic IP `52.210.165.0` est l'adresse publique fixe du **serveur**.
`My IP` est l'adresse publique du **client** autorisé par le Security Group.
Un changement de Wi-Fi, de réseau ou de fournisseur peut donc imposer
d'actualiser `My IP`, même si l'adresse du serveur reste fixe. Cette évolution
de l'IP cliente n'affecte pas le CD GitHub → SSM.

## Tests et validation

### `docs/`

- **`contrat_anomalie.md`** : le format JSON commun décrit plus haut, avec
  le détail des énumérations et l'usage du score de confiance.

### `jeu_validation/`

Jeux de données de validation, avec vérité terrain, pour calibrer les
agents LLM sans dépendre uniquement du jugement humain au cas par cas.
- **`coherence/`** : fichiers de vérité terrain (`*_verite_terrain.json`)
  pour l'agent Cohérence, et les scripts qui les génèrent
  (`generer_fichier_teste.py`, `generer_fichier_teste_2.py` — injectent des
  incohérences synthétiques dans un extrait de fichier réel, pour mesurer
  le rappel de l'agent sur des cas connus).

### `tests/`

Un fichier de test par module (`test_structure.py`, `test_stats.py`,
`test_agent_coherence.py`, `test_agent_realisme.py`, `test_agent_judge.py`,
`test_agent_reporting.py`, `test_orchestrateur.py`), plus des tests de
conformité au contrat commun. Les scripts historiques de validation manuelle
de Cohérence vivent aussi à côté de son code (`agents/test_coherence.py`,
`agents/test_coherence_injection.py`).

Aucun test ne fait d'appel réseau réel : les tests LLM utilisent un client
factice (voir `_ClientFactice` dans `test_agent_realisme.py` et
`test_orchestrateur.py`) pour ne jamais consommer le quota gratuit partagé.
Deux fixtures `autouse` protègent spécifiquement contre les fuites
accidentelles : `config.RECHERCHE_WEB_ACTIVEE` désactivée par défaut (sinon
un vrai appel réseau `ddgs`), et les dossiers de résultats/checkpoints de
Cohérence redirigés vers `tmp_path`.

```powershell
python -m pytest -q
```

### Validation de l'architecture

La chaîne complète a été validée en conditions réelles : tests et build dans
GitHub Actions, OIDC, push ECR avec les deux tags, SSM Run Command, déploiement
du tag SHA, health check, jobs `test-build-push` et `deploy-ec2` en `Success`,
dashboard accessible, audit réel réussi, quatre agents en `SUCCESS`, pipeline
en `COMPLETE`, Judge fonctionnel et historique S3 disponible.

### Dossiers non versionnés (exclus par `.gitignore`)

- **`data/`** : fichiers Excel/CSV réels à auditer, organisés par mois
  (`LOAD_LOCK_202603/`, `LOAD_LOCK_202604/`...). Volumineux et
  potentiellement confidentiel : jamais commité, à récupérer séparément
  (voir « Installation » ci-dessous).
- **`resultats/`** : toutes les sorties générées (`structure_*.json`,
  `stats_*.json`, `realisme_*.json`, `verdict_*.json`, `rapport_*.html`,
  `inventaire.csv`) — régénérées à chaque exécution, jamais commitées.
- **`cache_llm/`** : cache disque des réponses LLM (évite de repayer une
  requête identique) et compteurs de quota quotidien par clé sous
  `quota/compteur_<fingerprint>.json`. État d'exécution local, jamais commité.
  Le fingerprint est formé des 12 premiers caractères hexadécimaux du SHA-256
  de la clé ; la clé elle-même n'est ni stockée ni journalisée.

## Exécution locale

1. Cloner le dépôt et se placer sur son propre travail (branche personnelle,
   ex. `git checkout -b <prenom>`).

2. Créer un environnement virtuel Python 3.12 et l'activer :

   ```powershell
   python -m venv .venv
   .venv\Scripts\activate
   ```

3. Installer les dépendances :

   ```powershell
   pip install -r requirements.txt
   ```

4. Créer son propre `.env` à partir du modèle, puis renseigner **sa propre**
   clé `OPENROUTER_API_KEY` (personnelle, à ne jamais commiter) :

   ```powershell
   copy .env.example .env
   ```

5. Récupérer le dossier `data/` séparément (non versionné) : demander aux
   autres membres l'emplacement de partage actuel et le placer à la racine
   du projet.

6. Vérifier que tout fonctionne :

   ```powershell
   python -m pytest tests/ -v
   ```

## Contraintes importantes à connaître avant de lancer un run réel

- **Quota LLM par clé, désormais beaucoup plus tendu** :
  `config.RATE_LIMIT["max_requetes_jour"] = 45` pour chaque clé OpenRouter,
  partagé par Cohérence et Réalisme lorsqu'ils utilisent cette même clé ; le
  Judge est déterministe. Un appel LLM peut prendre de 30 secondes à
  plusieurs minutes sur le tier gratuit. Depuis l'ajout de Cohérence (traite
  tout le fichier, pas juste les lignes suspectes) et de la vérification web
  de Réalisme (un appel de plus par chunk), **un seul gros fichier peut
  épuiser le quota du jour à lui seul** — envisager l'achat unique de 10$
  sur OpenRouter (45 → 1000 requêtes/jour) si des runs plus larges sont
  nécessaires. `agents.llm_client.quota_restant()` donne le solde du jour ;
  `orchestrateur.main()` s'arrête proprement quand il atteint 0. Changer de
  clé sélectionne automatiquement un compteur distinct. L'ancien
  `cache_llm/compteur_quotidien.json` est ignoré et peut être supprimé
  manuellement ; les caches de réponses existants restent inchangés.
- **Fichiers de même nom dans des mois différents** : plusieurs fichiers
  `data/LOAD_LOCK_*/` portent le même nom (ex. `NL_B2B_BRO_OFF.xlsx` existe
  dans 3 dossiers mensuels). `orchestrateur.py` traite chaque copie
  séparément et range ses résultats par mois — mais si vous testez un
  module individuellement (`python -m structure.agent_structure <nom>`),
  la résolution par nom seul renvoie systématiquement la **première**
  copie trouvée : donnez un chemin complet si vous visez une copie précise.
- **`ddgs` (recherche web de Réalisme) n'a pas d'API officielle** : peut se
  dégrader ou se bloquer sans préavis. Désactivable sans toucher au code via
  `config.RECHERCHE_WEB_ACTIVEE = False`.

## État actuel du pipeline

| Étape | Statut |
|---|---|
| Structure | ✅ Implémenté, testé, calibré sur données réelles |
| Stats | ✅ Implémenté, testé, calibré sur données réelles |
| Cohérence | ✅ Implémenté, branché dans l'orchestrateur (en parallèle de Stats) |
| Réalisme | ✅ Implémenté, testé, calibré, avec vérification web |
| Judge | ✅ Implémenté, testé, calibré |
| Reporting | ✅ Implémenté (HTML), testé |
| Orchestrateur | ✅ Implémenté, testé — pipeline complet de bout en bout |
| Dépôt S3 des rapports (`config.S3_REPORTS_PREFIX`) | ✅ Implémenté en mode S3, sous `reports/runs/<run_id>/` |

## Déploiement

Après réussite de `test-build-push`, le job `deploy-ec2` déclenche
automatiquement le déploiement du tag `${GITHUB_SHA}` sur EC2 avec Systems
Manager. La procédure technique, le rollback, le Security Group, les
variables runtime et le dépannage sont détaillés dans
[`DEPLOYMENT.md`](DEPLOYMENT.md).

Limitation actuelle : le workflow déploie automatiquement les images, mais ne
synchronise pas le fichier `deployment/deploy_ec2.sh`. Sa copie opérationnelle
`/home/ec2-user/deploy_ec2.sh` doit être mise à jour séparément si le script
versionné change.
