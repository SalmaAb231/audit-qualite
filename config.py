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
from loguru import logger

# Charge les variables du fichier .env s'il existe (ignoré silencieusement sinon)
load_dotenv()

# Mode de stockage utilisé par l'orchestrateur : fichiers locaux ou objets S3.
STORAGE_MODE = os.getenv("STORAGE_MODE", "local").lower()
if STORAGE_MODE not in {"local", "s3"}:
    logger.warning(
        f"STORAGE_MODE invalide ({STORAGE_MODE!r}) : valeurs acceptées = 'local' ou 's3'. "
        "Utilisation du mode local par défaut."
    )
    STORAGE_MODE = "local"

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

# Un fichier JSON immuable par exécution, consommable par le futur dashboard
HISTORY_DIR = RESULTATS_DIR / "history"

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
# Préfixe où sont déposés les rapports générés en mode S3
S3_REPORTS_PREFIX = os.getenv("S3_REPORTS_PREFIX", "reports/")
# Préfixe où sont copiés les fichiers rejetés ou non validés
S3_QUARANTINE_PREFIX = os.getenv("S3_QUARANTINE_PREFIX", "quarantine/")
# Préfixe de l'historique immuable des exécutions
S3_HISTORY_PREFIX = os.getenv("S3_HISTORY_PREFIX", "history/")

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
        "google/gemma-4-31b-it:free",
    ],

    # Contrainte dure : tool calling fiable pour la recherche web autonome
    # → gpt-oss-120b : tool calling natif + sorties structurées
    "realisme": [
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "google/gemma-4-31b-it:free",        # supporte les tools aussi
        "google/gemma-4-26b-a4b-it:free",    # gratuit, contexte 262K, sorties structurées
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
    # Doublé le 2026-08-12 : troncature observée à 8192 sur un chunk dense
    # en anomalies (fichier à 51 colonnes), qui a fait perdre une anomalie
    # (champs coupés en plein milieu, rejetée par valider_anomalies).
    "coherence": 16384,  # raisonnement + JSON d'anomalies potentiellement long
    "realisme": 4096,    # raisonnement + résultats de recherche web
    "judge": 1024,       # justification courte
    "reporting": 4096,   # rapports consolidés = textes longs
}

# Vérification des anomalies Réalisme par recherche web (agents/agent_realisme.py,
# bibliothèque `ddgs` : DuckDuckGo, gratuite, sans clé API) avant de les
# retenir définitivement — réduit les faux positifs dus à des connaissances
# figées à l'entraînement du modèle (ex. un modèle de véhicule récent jugé
# "inexistant"). Un seul appel LLM de vérification par chunk (pas par
# anomalie). À couper (False) si ddgs devient indisponible/peu fiable, sans
# toucher au code — voir agents/regles_realisme.md.
RECHERCHE_WEB_ACTIVEE = True

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

# Budget FREE tier global pour une opération LLM logique (un chunk). Ce
# plafond inclut les retries et les changements de modèle, fallback compris.
MAX_LLM_ATTEMPTS_PER_CHUNK = 4

# Circuit breaker mémoire/process : après deux réponses 429 explicitement
# attribuées au provider upstream, le modèle est ignoré pendant cinq minutes.
OPENROUTER_CIRCUIT_BREAKER_THRESHOLD = 2
OPENROUTER_CIRCUIT_BREAKER_COOLDOWN_SECONDS = 300
FREE_TIER_RETRY_BACKOFF_SECONDS = 2.0

# Cache des réponses LLM : chaque appel est sauvegardé (hash du prompt →
# réponse) ; un prompt identique est servi depuis le cache SANS consommer
# de requête. Essentiel avec 50 requêtes/jour : le débogage du parsing ou
# du scoring se fait en rejouant le cache, jamais en rappelant l'API.
CACHE_LLM_DIR = BASE_DIR / "cache_llm"

# Compteurs quotidiens locaux isolés par clé OpenRouter. llm_client.py dérive
# le nom du fichier depuis un fingerprint SHA-256 de la clé réellement utilisée ;
# config.py ne construit aucun chemin à partir du secret lui-même.
QUOTA_LLM_DIR = CACHE_LLM_DIR / "quota"

# ---------------------------------------------------------------------------
# Découpage des données envoyées aux agents LLM
# ---------------------------------------------------------------------------
# Nombre de lignes de données envoyées par appel LLM (chunk). Constante COMMUNE
# aux agents Cohérence et Réalisme. 500 lignes ≈ 15K tokens ≈ 11% du plus petit
# contexte des cascades (131K) : compromis quota (45 req/jour) / fiabilité de
# lecture, choisi sans test d'attention. Le rappel mesuré par injection en
# phase d'évaluation validera (ou fera ajuster) cette valeur.
CHUNK_SIZE = 500

# Au-delà de ce nombre de lignes, un fichier est "géant" : il est échantillonné
# au lieu d'être lu en entier (5-6 fichiers sur 202 sont concernés). Tous les
# autres fichiers sont TOUJOURS analysés intégralement.
SEUIL_FICHIER_GEANT = 20000

# Taille de l'échantillon (tirage seed=42, index d'origine conservés) appliqué
# aux SEULS fichiers géants — y compris lors des runs réels. Ne jamais mettre 0
# pour un run complet : les géants coûteraient ~2 600 chunks à eux seuls.
# Augmenter = meilleure couverture des géants contre plus d'appels (arbitrage
# après le run pilote).
MAX_LIGNES_PAR_FICHIER = 2000

# Composition de l'échantillon des fichiers géants : part de lignes tirées au
# hasard, le complément étant les lignes priorisées par le canal statistique.
# (Seul usage restant de cette constante depuis le passage aux canaux
# parallèles ; la supprimer si cette composition n'est pas implémentée.)
TAUX_ECHANTILLON_CONTROLE = 0.10

# ---------------------------------------------------------------------------
# Paramètres historiques sur [0, 1] : le Judge v2 utilise JUDGE_* ci-dessous.
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

# ---------------------------------------------------------------------------
# Ancien Judge : conservé pour compatibilité, inutilisé par le Judge v2.
# ---------------------------------------------------------------------------
# Une anomalie 'critical' compte double dans le calcul de pénalité (son
# poids PONDERATION_SEVERITE["critical"] est multiplié par ce facteur, les
# autres sévérités restent inchangées).
FACTEUR_PENALITE_CRITICAL = 2.0

# Notes de A à F par tranche de score (borne inférieure incluse). Parcourues
# dans cet ordre : la première tranche atteinte l'emporte, sinon "F".
SEUILS_GRADE = {
    "A": 0.95,
    "B": 0.85,
    "C": 0.70,
    "D": 0.50,
}

# Plafond de la pénalité cumulée des sévérités "non bloquantes" (medium et
# low, voir docs/contrat_anomalie.md — "high"/"critical" restent sans
# plafond). Découvert en calibrant sur données réelles déjà validées : sans
# ce plafond, un détecteur purement mathématique (stats/) peut accumuler des
# milliers d'anomalies medium/low sur un gros fichier (ex. un léger écart
# d'arrondi répété sur 200 000 lignes) et fait chuter le score à 0, alors
# qu'aucune de ces anomalies n'est individuellement bloquante. Avec ce
# plafond, stats/ seul (qui ne produit jamais de high/critical) ne peut
# jamais faire descendre un fichier sous WARNING — cohérent avec la
# séparation déjà établie ("stats/ ne décide jamais qu'un fichier est
# inexploitable", rôle réservé à structure/). Valeur choisie pour que le
# pire cas (uniquement du medium/low, peu importe la quantité) atterrisse
# exactement à la frontière SEUIL_WARNING : 1.0 - 0.25 = 0.75.
PLAFOND_PENALITE_NON_BLOQUANTE = 0.25

# Types d'anomalies autorisés (voir docs/contrat_anomalie.md)
TYPES_ANOMALIE = ("structure", "statistique", "coherence", "realisme")

# Sources de détection autorisées
SOURCES_ANOMALIE = ("stats", "llm")

# Judge v2 : poids distincts des poids historiques encore utilisés par Cohérence.
JUDGE_POIDS_SEVERITE = {"critical": 1.0, "high": 0.6, "medium": 0.25, "low": 0.1}
JUDGE_SEUIL_ACCEPTED = 90.0
JUDGE_SEUIL_WARNING = 70.0
JUDGE_SEUILS_GRADE = {"A": 90.0, "B": 80.0, "C": 70.0, "D": 60.0}
JUDGE_SIMILARITE_COLONNES = 0.8
JUDGE_SIMILARITE_PROBLEME = 0.85
