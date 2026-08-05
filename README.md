# audit-qualite

Projet d'audit de qualité de données pour des fichiers Excel de renting
automobile. Pipeline cible (hybride) : validation de structure (pandas) →
détection statistique (pandas) → agents LLM (Cohérence, Réalisme) → Judge →
Reporting, via l'API OpenRouter. Déploiement futur sur AWS (S3 + EC2).

**État actuel** : socle du projet. L'ingestion locale (`ingestion/local_loader.py`,
inventaire) et le socle LLM commun (`agents/llm_client.py`, `agents/parse_utils.py`)
sont fonctionnels et validés par `agents/test_premier_appel.py`. Le loader S3
(`ingestion/s3_client.py`) est prêt pour le déploiement mais n'est pas encore
utilisé en local. Aucun agent complet (Cohérence, Réalisme, Judge, Reporting)
n'est encore implémenté.

## Choix de conception : schema-agnostic

Les fichiers source ont des structures de colonnes différentes, et chaque
nouveau fichier peut avoir une structure inédite (noms de colonnes, ordre,
langue variables selon le loueur/constructeur). Le projet ne fait donc
**aucun mapping de schéma ni normalisation de colonnes** : chaque fichier
est traité tel quel, avec ses colonnes réelles. Ce sont les agents LLM
(ajoutés dans une étape ultérieure du pipeline) qui s'adapteront dynamiquement
aux colonnes de chaque fichier, plutôt que de dépendre d'un schéma standard
figé à l'avance. L'inventaire (`ingestion/profiling.py`) reflète ce choix :
il recense la diversité des structures de colonnes à titre purement
informatif, sans jamais tenter de les unifier.

## Structure du projet

```
audit-qualite/
├── .env.example          # Modèle de configuration (variables d'environnement)
├── config.py             # Configuration centralisée (chemins, seuils, LLM)
├── docs/
│   └── contrat_anomalie.md   # Format JSON commun d'anomalie
├── data/                 # Données locales (LOAD_LOCK_202603/04/05), exclu de Git
│   └── cache/            # Cache des fichiers téléchargés depuis S3, exclu de Git
├── ingestion/
│   ├── local_loader.py   # Lecture des fichiers Excel/CSV locaux (développement)
│   ├── s3_client.py      # Lecture/écriture S3 (déploiement), même interface que local_loader
│   ├── test_s3_connexion.py  # Diagnostic de connexion S3 (à lancer au déploiement)
│   └── profiling.py      # Script d'inventaire des fichiers
├── agents/
│   ├── llm_client.py     # Client OpenRouter commun (retries, fallback, logging)
│   ├── parse_utils.py    # Parsing JSON robuste + validation du contrat d'anomalie
│   └── test_premier_appel.py  # Preuve du socle LLM sur un fichier réel
├── structure/            # Validation de structure (à venir)
├── stats/                # Détection statistique (à venir)
├── judge/                # Agrégation des verdicts (à venir)
├── reporting/            # Génération de rapports (à venir)
├── jeu_validation/       # Jeux de données de validation (à venir)
└── resultats/            # Sorties générées (inventaire, rapports, etc.)
```

## Installation pour un nouveau membre

1. Cloner le dépôt :

   ```powershell
   git clone <url-du-depot>
   cd audit-qualite
   ```

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
   clé `OPENROUTER_API_KEY` (une clé personnelle, à ne jamais commiter) :

   ```powershell
   copy .env.example .env
   ```

   Le chemin `DATA_DIR` par défaut (`./data`) convient pour un usage local ;
   les variables AWS peuvent rester vides tant qu'on ne travaille pas sur le
   déploiement (voir « Architecture d'ingestion » ci-dessous).

5. Récupérer le dossier `data/` séparément : il n'est **pas** versionné dans
   Git (fichiers volumineux et potentiellement confidentiels). Demander aux
   autres membres l'emplacement de partage actuel (S3, disque partagé, etc.)
   et le placer à la racine du projet.

## Architecture d'ingestion : LocalLoader vs S3Loader

Le pipeline accède aux fichiers de données via une interface commune,
implémentée par deux classes interchangeables :

- **`ingestion/local_loader.py` (`LocalLoader`)** — utilisé en développement
  local. Lit les fichiers directement depuis `DATA_DIR` (`./data` par défaut).
  Aucune configuration AWS n'est nécessaire.
- **`ingestion/s3_client.py` (`S3Loader`)** — utilisé au déploiement (AWS
  EC2 + S3 uniquement). Télécharge les fichiers depuis le bucket S3 configuré
  (`S3_BUCKET` / `S3_INPUT_PREFIX`) vers un cache local (`data/cache/`, non
  re-téléchargé si la taille n'a pas changé), puis délègue la lecture du
  fichier mis en cache à `LocalLoader`. Fonctionne avec des clés AWS explicites
  dans `.env`, ou sans clés si un rôle IAM est attaché à l'instance EC2.

Les deux classes exposent exactement la même interface publique
(`lister_fichiers()`, `charger(...)`), ce qui permet à l'orchestrateur de
basculer de l'une à l'autre en changeant une seule ligne :

```python
loader = LocalLoader()   # développement local
loader = S3Loader()      # déploiement AWS
```

`S3Loader` expose en plus `uploader(...)` (dépôt des rapports générés sur S3)
et `verifier_connexion()`. Pour vérifier une configuration S3 avant un
déploiement (credentials, accès au bucket, contenu du préfixe d'entrée) :

```powershell
python -m ingestion.test_s3_connexion
```

## Lancer l'inventaire des fichiers

Depuis la racine du projet, avec l'environnement virtuel activé :

```powershell
python -m ingestion.profiling
```

Le script parcourt tous les fichiers présents dans `data/`, affiche une
barre de progression, et génère `resultats/inventaire.csv` avec, pour
chaque fichier : nom, mois d'origine, taille, nombre de lignes/colonnes,
liste des colonnes, langue probable et éventuelle erreur de lecture. Un
résumé (totaux, répartition par mois, familles de schémas) s'affiche en
fin d'exécution.
