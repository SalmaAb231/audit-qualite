"""
Configuration centralisée du projet audit-qualite.

Toutes les constantes (chemins, paramètres LLM, seuils de verdict,
pondérations de sévérité) sont définies ici pour éviter qu'elles soient
dispersées dans le code. Les valeurs sensibles (clés API, identifiants
AWS) sont lues depuis le fichier .env via python-dotenv.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Charge les variables du fichier .env s'il existe (ignoré silencieusement sinon)
load_dotenv()

# Racine du projet (dossier contenant ce fichier)
BASE_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Données locales
# ---------------------------------------------------------------------------

# Dossier contenant les données (sous-dossiers LOAD_LOCK_AAAAMM)
DATA_DIR = Path(os.getenv("DATA_DIR", "./data")).resolve()

# Extensions de fichiers de données prises en charge par l'ingestion
EXTENSIONS_SUPPORTEES = (".xlsx", ".xls", ".csv")

# Dossier de sortie des résultats (inventaire, rapports, etc.)
RESULTATS_DIR = BASE_DIR / "resultats"

# ---------------------------------------------------------------------------
# AWS / S3 (utilisé uniquement au déploiement ; sans effet en local, où
# ingestion/local_loader.py est utilisé à la place de ingestion/s3_client.py)
# ---------------------------------------------------------------------------

AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "")
AWS_REGION = os.getenv("AWS_REGION", "eu-west-1")
S3_BUCKET = os.getenv("S3_BUCKET", "")

# Préfixe où sont déposés les fichiers de données à auditer
S3_INPUT_PREFIX = os.getenv("S3_INPUT_PREFIX", "input/")
# Préfixe où seront déposés les rapports générés (module reporting/, à venir)
S3_REPORTS_PREFIX = os.getenv("S3_REPORTS_PREFIX", "reports/")

# Dossier de cache local des fichiers téléchargés depuis S3 (voir ingestion/s3_client.py)
CACHE_DIR = DATA_DIR / "cache"

# ---------------------------------------------------------------------------
# OpenRouter / agents LLM
# ---------------------------------------------------------------------------

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Modèle OpenRouter utilisé par chaque agent du pipeline
MODELES = {
    "coherence": "nvidia/nemotron-3-ultra-550b-a55b:free",
    "realisme":  "google/gemma-4-31b-it:free",
    "judge":     "google/gemma-4-26b-a4b-it:free",
    "reporting": "google/gemma-4-26b-a4b-it:free",
}


# Modèle de secours utilisé si le modèle principal d'un agent échoue
# après épuisement de ses retries (voir agents/llm_client.py)
MODELE_FALLBACK = "openrouter/free"

# Température par agent (0.0 = déterministe, recommandé pour la détection d'anomalies)
TEMPERATURES = {
    "coherence": 0.0,
    "realisme": 0.0,
    "judge": 0.0,
    "reporting": 0.2,
}

# Nombre maximal de tokens de sortie par agent
MAX_TOKENS = {
    "coherence": 2048,
    "realisme": 2048,
    "judge": 1024,
    "reporting": 2048,
}

# Nombre maximal de lignes envoyées au LLM par fichier (0 = fichier complet).
# Sert à échantillonner les gros fichiers pendant le développement, pour
# limiter la latence et le coût des appels API ; non utilisé tant que les
# agents ne sont pas implémentés.
MAX_LIGNES_PAR_FICHIER = 2000

# ---------------------------------------------------------------------------
# Seuils de verdict global (score de confiance agrégé, entre 0 et 1)
# ---------------------------------------------------------------------------

SEUIL_ACCEPTED = 0.90
SEUIL_WARNING = 0.75
# En dessous de SEUIL_WARNING : verdict REJECTED

# ---------------------------------------------------------------------------
# Pondérations de sévérité des anomalies (utilisées pour agréger un score)
# ---------------------------------------------------------------------------

PONDERATION_SEVERITE = {
    "critical": 1.0,
    "high": 0.7,
    "medium": 0.4,
    "low": 0.2,
}
