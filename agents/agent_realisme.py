"""
Agent LLM « Réalisme » du pipeline audit-qualite.

Troisième détecteur du pipeline (après structure/ et stats/) : juge le
RÉALISME individuel des valeurs au regard des connaissances du monde réel de
l'automobile (marques, modèles, puissances, prix, carburants, années...),
schema-agnostic comme les détecteurs précédents. Différent de l'agent
Cohérence (incohérence logique ENTRE plusieurs champs d'une même ligne, ex.
date_fin < date_debut) : Réalisme juge si une valeur a un sens dans le monde
réel, en s'appuyant sur les connaissances générales du modèle — pas de
recherche web à ce stade (voir agents/regles_realisme.md, limite connue).

100% des anomalies produites ont type="realisme" et source="llm" : ces deux
champs sont réécrits après coup, jamais laissés tels que renvoyés par le
modèle (défense contre un LLM qui se tromperait de type).

Convention 'ligne' (docs/contrat_anomalie.md) : identique aux autres modules,
index pandas 0-based. Le modèle reçoit une colonne 'ligne' explicite dans le
tableau envoyé et doit la reprendre telle quelle plutôt que recalculer une
position.

Usage :
    python -m agents.agent_realisme <chemin_ou_nom_de_fichier>
"""

import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd
from loguru import logger
from tabulate import tabulate

# Permet l'exécution directe du script en plus de l'exécution en tant que module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from agents.llm_client import LLMClient
from agents.parse_utils import valider_anomalies
from ingestion.local_loader import LocalLoader

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

TYPE_ANOMALIE = "realisme"
SOURCE_ANOMALIE = "llm"

PROMPT_SYSTEME_BASE = (
    "Tu es un auditeur expert du marché automobile, spécialisé dans les données "
    "de location longue durée (renting) de véhicules. Ta mission unique est de "
    "juger le RÉALISME de chaque valeur, au regard de tes connaissances du monde "
    "réel de l'automobile (marques, modèles, motorisations, puissances, prix, "
    "années de production, carburants, etc.) — PAS la cohérence logique entre "
    "plusieurs colonnes d'une même ligne (ex. une date de fin avant une date de "
    "début) : c'est le rôle d'un autre agent. Ignore aussi les problèmes de "
    "structure du fichier (colonnes vides, types mélangés) et les simples "
    "valeurs statistiquement extrêmes sans lien avec la réalité métier : ce "
    "n'est pas ton rôle non plus.\n\n"
    "Exemples de ce qui EST dans ton périmètre : une Fiat Panda annoncée avec "
    "900 ch ; un prix de location de 5€/mois pour un SUV premium ; une année de "
    "mise en circulation dans le futur ; un carburant 'Diesel' pour un modèle "
    "qui n'existe qu'en version 100% électrique.\n\n"
    "Le fichier ne suit aucun schéma fixe : utilise les noms de colonnes "
    "exactement comme fournis, sans supposer leur sens au-delà de ce que leur "
    "nom et leurs valeurs indiquent. La première colonne du tableau, 'ligne', "
    "est l'index exact à reprendre dans le champ 'ligne' de chaque anomalie — "
    "ne la recalcule jamais.\n\n"
    "Réponds UNIQUEMENT en JSON strict, sans texte autour ni bloc markdown : "
    '{"anomalies": [...]}. Chaque anomalie doit avoir exactement ces champs : '
    "fichier (string), ligne (entier, repris de la colonne 'ligne'), colonnes "
    '(liste de string), type (toujours "realisme"), severite ("critical", '
    '"high", "medium" ou "low"), source (toujours "llm"), valeur (description '
    "courte de la valeur posant problème), justification (explication en une "
    "phrase), confiance (nombre entre 0.0 et 1.0). N'invente jamais d'anomalie "
    'pour remplir la réponse : si rien n\'est invraisemblable, réponds {"anomalies": []}.'
)


# ---------------------------------------------------------------------------
# Construction des prompts et découpage en chunks
# ---------------------------------------------------------------------------


def _construire_prompt_systeme() -> str:
    """
    Ajoute la date du jour à PROMPT_SYSTEME_BASE. Le modèle n'a aucune horloge
    interne fiable (ses connaissances datent de son entraînement) : sans cette
    date explicite, il juge "dans le futur" ou "dans le passé" en se basant
    sur la date d'entraînement supposée, pas sur la vraie date d'aujourd'hui —
    découvert en test réel (voir agents/regles_realisme.md, limite connue).
    Recalculée à chaque appel (pas une constante figée à l'import) et incluse
    dans le hash de cache de agents/llm_client.py : le cache s'invalide donc
    naturellement d'un jour sur l'autre pour les jugements liés à une date.
    """
    aujourdhui = date.today().isoformat()
    return (
        PROMPT_SYSTEME_BASE
        + f"\n\nDate du jour : {aujourdhui}. Toute date antérieure ou égale à "
        "aujourd'hui est normale (passé ou présent) ; seule une date "
        "STRICTEMENT postérieure à aujourd'hui est \"dans le futur\". Base-toi "
        "uniquement sur cette date fournie, jamais sur une estimation de la "
        "date actuelle."
    )


def _construire_prompt_utilisateur(nom_fichier: str, chunk: pd.DataFrame) -> str:
    """Sérialise un chunk en markdown, colonnes réelles incluses, avec l'index en colonne 'ligne'."""
    tableau = chunk.rename_axis("ligne").to_markdown(index=True)
    return (
        f"Fichier : {nom_fichier}\n"
        f"Colonnes ({len(chunk.columns)}) : {list(chunk.columns)}\n\n"
        f"Données ({len(chunk)} lignes) :\n\n{tableau}"
    )


def _decouper_en_chunks(df: pd.DataFrame, taille_chunk: int) -> list:
    """Découpe df en tranches de taille_chunk lignes (index d'origine préservé). taille_chunk <= 0 -> un seul chunk."""
    if taille_chunk <= 0 or len(df) <= taille_chunk:
        return [df]
    return [df.iloc[i : i + taille_chunk] for i in range(0, len(df), taille_chunk)]


# ---------------------------------------------------------------------------
# Appel LLM par chunk
# ---------------------------------------------------------------------------


def _analyser_chunk(nom_fichier: str, chunk: pd.DataFrame, client: LLMClient, num_chunk: int, nb_chunks: int) -> list:
    """
    Analyse un chunk via le LLM et retourne ses anomalies validées. N'échoue
    jamais : une exception ou un échec d'appel est journalisé et traité comme
    "aucune anomalie détectée sur ce chunk" plutôt que de bloquer les suivants.
    """
    try:
        reponse, metadata = client.appeler(
            system_prompt=_construire_prompt_systeme(),
            user_prompt=_construire_prompt_utilisateur(nom_fichier, chunk),
            model=config.MODELES["realisme"],
            temperature=config.TEMPERATURES["realisme"],
            max_tokens=config.MAX_TOKENS["realisme"],
            json_attendu=True,
        )
    except Exception as erreur:
        logger.error(f"{nom_fichier} chunk {num_chunk}/{nb_chunks} : appel LLM échoué ({erreur}).")
        return []

    if not metadata.succes:
        logger.warning(
            f"{nom_fichier} chunk {num_chunk}/{nb_chunks} : appel LLM en échec "
            f"(modèle={metadata.modele}, erreur={metadata.erreur})."
        )

    anomalies_brutes = []
    if isinstance(reponse, dict):
        anomalies_brutes = reponse.get("anomalies", [])
    elif isinstance(reponse, list):
        anomalies_brutes = reponse

    anomalies_validees = valider_anomalies(anomalies_brutes)
    for anomalie in anomalies_validees:
        anomalie["fichier"] = nom_fichier
        anomalie["type"] = TYPE_ANOMALIE
        anomalie["source"] = SOURCE_ANOMALIE

    logger.info(
        f"{nom_fichier} chunk {num_chunk}/{nb_chunks} : {len(anomalies_validees)} anomalie(s) "
        f"de réalisme (modèle={metadata.modele}, depuis_cache={metadata.depuis_cache})."
    )
    return anomalies_validees


# ---------------------------------------------------------------------------
# Fonction principale
# ---------------------------------------------------------------------------


def analyser(nom_fichier: str, df: pd.DataFrame, client: LLMClient | None = None) -> list:
    """
    Découpe df en chunks (config.CHUNK_SIZE) et interroge l'agent Réalisme sur
    chacun, en tronquant d'abord à config.MAX_LIGNES_PAR_FICHIER lignes si
    celui-ci est non nul (0 = fichier complet). Retourne la liste agrégée des
    anomalies, chacune conforme à docs/contrat_anomalie.md avec
    type="realisme" et source="llm".

    Ne lève jamais d'exception. Un DataFrame vide retourne [] immédiatement,
    sans consommer de requête LLM.
    """
    if df is None or df.empty:
        return []

    if config.MAX_LIGNES_PAR_FICHIER > 0 and len(df) > config.MAX_LIGNES_PAR_FICHIER:
        logger.warning(
            f"{nom_fichier} : {len(df)} lignes tronquées à {config.MAX_LIGNES_PAR_FICHIER} "
            "(config.MAX_LIGNES_PAR_FICHIER) pour limiter la consommation de requêtes LLM."
        )
        df = df.head(config.MAX_LIGNES_PAR_FICHIER)

    client = client or LLMClient()
    chunks = _decouper_en_chunks(df, config.CHUNK_SIZE)

    anomalies = []
    for i, chunk in enumerate(chunks):
        anomalies.extend(_analyser_chunk(nom_fichier, chunk, client, i + 1, len(chunks)))

    logger.info(f"{nom_fichier} : {len(anomalies)} anomalie(s) de réalisme au total sur {len(chunks)} chunk(s).")
    return anomalies


# ---------------------------------------------------------------------------
# Point d'entrée CLI
# ---------------------------------------------------------------------------


def _resoudre_chemin(argument: str) -> Path:
    """Accepte un chemin direct, ou un nom de fichier à rechercher dans DATA_DIR."""
    chemin = Path(argument)
    if chemin.exists():
        return chemin

    loader = LocalLoader()
    for info in loader.lister_fichiers():
        if info.nom == argument:
            return info.chemin
    raise FileNotFoundError(f"Fichier introuvable (ni chemin direct, ni dans {config.DATA_DIR}) : {argument}")


def _afficher_anomalies(anomalies: list) -> None:
    if not anomalies:
        print("Aucune anomalie de réalisme détectée.")
        return

    lignes = [
        [a["ligne"], a["severite"], ", ".join(a["colonnes"]) or "-", a["valeur"], a["justification"]]
        for a in anomalies
    ]
    print(tabulate(lignes, headers=["Ligne", "Sévérité", "Colonne(s)", "Valeur", "Justification"], tablefmt="grid"))


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage : python -m agents.agent_realisme <chemin_ou_nom_de_fichier>")
        sys.exit(1)

    chemin = _resoudre_chemin(sys.argv[1])
    df = LocalLoader().charger(chemin)
    if df is None:
        logger.error(f"Échec du chargement de {chemin}")
        sys.exit(1)

    nom_fichier = chemin.name
    anomalies = analyser(nom_fichier, df)

    print("\n" + "=" * 60)
    print(f"ANOMALIES DE RÉALISME — {nom_fichier}")
    print("=" * 60)
    _afficher_anomalies(anomalies)

    config.RESULTATS_DIR.mkdir(parents=True, exist_ok=True)
    fichier_sortie = config.RESULTATS_DIR / f"realisme_{chemin.stem}.json"
    fichier_sortie.write_text(json.dumps(anomalies, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(f"Anomalies sauvegardées dans {fichier_sortie}")


if __name__ == "__main__":
    main()
