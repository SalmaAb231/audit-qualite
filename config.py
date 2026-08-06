"""
Configuration centralisée du projet audit-qualite.

Toutes les constantes (chemins, paramètres LLM, contraintes du tier gratuit
OpenRouter, seuils de verdict, pondérations de sévérité) sont définies ici
pour éviter qu'elles soient dispersées dans le code. Les valeurs sensibles
(clés API, identifiants AWS) sont lues depuis le fichier .env via python-dotenv.

Règle du projet : aucun module ne définit ses propres constantes de
configuration — tout passe par ce fichier. Changer de modèle LLM, de seuil
ou de chemin = modifier UNE ligne ici, rien d'autre.
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

# Dossier de sortie des résultats (inventaire, anomalies, rapports, etc.)
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

# Dossier de cache local des fichiers téléchargés depuis S3
# (voir ingestion/s3_client.py)
CACHE_DIR = DATA_DIR / "cache"

# ---------------------------------------------------------------------------
# OpenRouter / agents LLM
# ---------------------------------------------------------------------------

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Modèles OpenRouter par agent — 100 % tier gratuit (:free).
#
# Chaque agent a une LISTE de modèles : le premier est le modèle principal,
# les suivants sont des fallbacks. Logique attendue dans agents/llm_client.py :
#   modèle 1 (retries épuisés ou indisponible) → modèle 2 → modèle 3
#   → MODELE_FALLBACK en tout dernier recours.
#
# ⚠️ Le catalogue :free d'OpenRouter tourne sans préavis : vérifier
# régulièrement la disponibilité sur https://openrouter.ai/models (filtre FREE)
# et mettre à jour ces listes si un modèle disparaît.
MODELES = {
    # Agent le plus sollicité + raisonnement logique exigeant
    # → le meilleur modèle gratuit disponible (contexte 1M, support tools)
    "coherence": [
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "openai/gpt-oss-120b:free",
        "google/gemma-4-31b-it:free",
    ],

    # Contrainte dure : tool calling fiable pour la recherche web autonome
    # → gpt-oss-120b : tool calling natif + sorties structurées
    "realisme": [
        "openai/gpt-oss-120b:free",
        "nvidia/nemotron-3-ultra-550b-a55b:free",
    ],

    # Tâche simple (justification du verdict, quelques phrases) → modèle léger
    "judge": [
        "google/gemma-4-26b-a4b-it:free",
        "openai/gpt-oss-20b:free",
    ],

    # Rédaction des rapports → modèle léger
    "reporting": [
        "google/gemma-4-26b-a4b-it:free",
        "google/gemma-4-31b-it:free",
    ],
}

# Filet de sécurité ultime : auto-routeur d'OpenRouter qui sélectionne
# automatiquement un modèle gratuit disponible. À n'utiliser qu'en dernier
# recours (on ne sait pas quel modèle répond → mauvais pour la
# reproductibilité des scores).
MODELE_FALLBACK = "openrouter/free"

# Température par agent (0.0 = déterministe, recommandé pour la détection
# d'anomalies ; légèrement plus élevée pour la rédaction de rapports)
TEMPERATURES = {
    "coherence": 0.0,
    "realisme": 0.0,
    "judge": 0.0,
    "reporting": 0.2,
}

# Nombre maximal de tokens de sortie par agent.
#
# ⚠️ Nemotron 3 Ultra et gpt-oss-120b sont des modèles de RAISONNEMENT :
# leurs tokens de réflexion interne comptent dans cette limite. Une valeur
# trop basse → réflexion qui consomme tout le budget → JSON final tronqué
# → erreur de parsing. Les tokens sont gratuits sur les :free, être généreux
# ne coûte que de la latence.
MAX_TOKENS = {
    "coherence": 8192,   # raisonnement + JSON d'anomalies potentiellement long
    "realisme": 4096,    # raisonnement + résultats de recherche web
    "judge": 1024,       # justification courte
    "reporting": 4096,   # rapports consolidés = textes longs
}

# ---------------------------------------------------------------------------
# Contraintes du tier gratuit OpenRouter (modèles :free)
# ---------------------------------------------------------------------------
# Limites officielles : 20 requêtes/minute ; 50 requêtes/jour sans crédit
# acheté, 1 000 requêtes/jour après un achat unique de 10 $ (jamais expiré).
# agents/llm_client.py DOIT respecter ces valeurs.

RATE_LIMIT = {
    "pause_entre_appels": 3.5,   # secondes entre 2 appels → reste sous 20 req/min
    "max_requetes_jour": 45,     # marge sous la limite de 50 (passer à 950 si achat 10$)
    "retries_max": 3,            # tentatives par modèle sur erreur 429 / 5xx
    "backoff_initial": 5.0,      # secondes d'attente au 1er retry, doublée ensuite
}

# Cache des réponses LLM : chaque appel est sauvegardé (hash du prompt →
# réponse) ; un prompt identique est servi depuis le cache SANS consommer
# de requête. Essentiel avec 50 requêtes/jour : le débogage du parsing ou
# du scoring se fait en rejouant le cache, jamais en rappelant l'API.
CACHE_LLM_DIR = BASE_DIR / "cache_llm"

# Compteur de requêtes quotidien persistant (JSON : {"date": ..., "compteur": N}).
# llm_client.py incrémente à chaque appel réel et BLOQUE proprement à
# max_requetes_jour avec un message clair.
COMPTEUR_REQUETES_FICHIER = CACHE_LLM_DIR / "compteur_quotidien.json"

# ---------------------------------------------------------------------------
# Découpage des données envoyées aux agents LLM
# ---------------------------------------------------------------------------

# Nombre de lignes de données envoyées par appel LLM (chunk).
# Compromis : gros chunks = moins de requêtes (précieux à 50/jour), mais
# réponse JSON plus longue et risque que le modèle rate des anomalies au
# milieu du chunk. Rester entre 50 et 100 ; affiner sur le jeu de validation.
CHUNK_SIZE = 80

# Nombre maximal de lignes envoyées au LLM par fichier (0 = fichier complet).
# Sert à échantillonner les gros fichiers pendant le DÉVELOPPEMENT pour
# limiter la consommation de requêtes. À mettre à 0 pour les runs réels.
MAX_LIGNES_PAR_FICHIER = 2000

# Taux d'échantillonnage aléatoire des lignes "normales" envoyées en contrôle
# aux agents LLM en plus des lignes suspectes (utilisé par l'orchestrateur
# si le scan complet n'est pas activé). Entre 0.0 et 1.0.
TAUX_ECHANTILLON_CONTROLE = 0.10

# ---------------------------------------------------------------------------
# Seuils de verdict global (score de confiance agrégé, entre 0 et 1)
# ---------------------------------------------------------------------------

SEUIL_ACCEPTED = 0.90
SEUIL_WARNING = 0.75
# En dessous de SEUIL_WARNING : verdict REJECTED

# ---------------------------------------------------------------------------
# Pondérations de sévérité des anomalies (utilisées pour agréger un score)
# ---------------------------------------------------------------------------
# Doivent rester cohérentes avec docs/contrat_anomalie.md.

PONDERATION_SEVERITE = {
    "critical": 1.0,
    "high": 0.7,
    "medium": 0.4,
    "low": 0.2,
}

# Types d'anomalies autorisés (voir docs/contrat_anomalie.md)
TYPES_ANOMALIE = ("structure", "statistique", "coherence", "realisme")

# Sources de détection autorisées
SOURCES_ANOMALIE = ("stats", "llm")