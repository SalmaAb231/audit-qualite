"""
Agent LLM « Réalisme » du pipeline audit-qualite.

Troisième détecteur du pipeline (après structure/ et stats/) : juge le
RÉALISME individuel des valeurs au regard des connaissances du monde réel de
l'automobile (marques, modèles, puissances, prix, carburants, années...),
schema-agnostic comme les détecteurs précédents. Différent de l'agent
Cohérence (incohérence logique ENTRE plusieurs champs d'une même ligne, ex.
date_fin < date_debut) : Réalisme juge si une valeur a un sens dans le monde
réel, en s'appuyant sur les connaissances générales du modèle, puis vérifie
ses propres anomalies candidates par une recherche web (bibliothèque `ddgs`,
DuckDuckGo, gratuite, sans clé API) avant de les retenir — voir
« Vérification web » dans agents/regles_realisme.md.

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
import time
from datetime import date
from pathlib import Path

import pandas as pd
from ddgs import DDGS
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

STATUT_SUCCESS = "SUCCESS"
STATUT_PARTIAL = "PARTIAL"
STATUT_FAILED = "FAILED"

# Graine de l'échantillonnage des fichiers géants (voir analyser()) : un
# tirage reproductible plutôt qu'aléatoire à chaque run, pour pouvoir
# comparer deux exécutions sur le même fichier. Valeur locale à ce module,
# comme SEED_ECHANTILLONNAGE dans agents/agent_coherence.py.
SEED_ECHANTILLONNAGE = 42

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

# Nombre de résultats de recherche web récupérés par anomalie candidate.
NB_RESULTATS_RECHERCHE = 3
# Pause entre deux recherches ddgs (limite le risque de blocage par DuckDuckGo).
PAUSE_ENTRE_RECHERCHES_SECONDES = 1.0

PROMPT_VERIFICATION_SYSTEME = (
    "Tu as précédemment signalé des anomalies de réalisme sur des données de "
    "location automobile, à partir de tes seules connaissances internes. On "
    "te montre maintenant, pour chacune, un extrait de résultats de recherche "
    "web récents. Ta tâche : dire si chaque anomalie reste valide, ou doit "
    "être RETIRÉE parce que le web montre clairement que ton évaluation "
    "initiale était fausse (ex. un modèle que tu pensais inexistant apparaît "
    "bien dans des résultats récents et fiables).\n\n"
    "Par défaut, GARDE l'anomalie (confirmee=true) : ne la retire que si la "
    "preuve web est claire et directement pertinente. Des résultats vides, "
    "hors sujet ou contradictoires entre eux ne suffisent PAS à retirer une "
    "anomalie.\n\n"
    "Réponds UNIQUEMENT en JSON strict, sans texte autour ni bloc markdown : "
    '{"verifications": [{"index": 0, "confirmee": true, "raison": "..."}, ...]}, '
    "un objet par anomalie fournie, dans le même ordre, avec le même index (0-based)."
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
# Vérification web des anomalies candidates
# ---------------------------------------------------------------------------


def _construire_requete_recherche(anomalie: dict) -> str:
    """Requête de recherche à partir des champs déjà produits par le modèle (schema-agnostic : aucune hypothèse sur les noms de colonnes du fichier)."""
    valeur = str(anomalie.get("valeur", "")).strip()
    colonnes = " ".join(anomalie.get("colonnes") or [])
    return f"{valeur} {colonnes}".strip()


def _rechercher_web(requete: str) -> str:
    """
    Recherche web via DuckDuckGo (bibliothèque `ddgs`, gratuite, sans clé
    API). Ne lève jamais d'exception : un échec (réseau, blocage DDG,
    requête vide, aucun résultat) retourne une chaîne vide, journalisé en
    warning — jamais bloquant pour le pipeline.
    """
    if not requete.strip():
        return ""
    try:
        resultats = DDGS().text(requete, max_results=NB_RESULTATS_RECHERCHE)
    except Exception as erreur:
        logger.warning(f"Recherche web échouée pour {requete!r} : {erreur}")
        return ""

    if not resultats:
        return ""
    return "\n".join(f"- {r.get('title', '')} : {r.get('body', '')}" for r in resultats)


def _construire_prompt_verification_utilisateur(anomalies: list, resultats_recherche: list) -> str:
    blocs = []
    for i, (anomalie, resultat) in enumerate(zip(anomalies, resultats_recherche)):
        blocs.append(
            f"### Anomalie {i}\n"
            f"Colonne(s) : {', '.join(anomalie.get('colonnes') or [])}\n"
            f"Valeur signalée : {anomalie.get('valeur')}\n"
            f"Justification initiale : {anomalie.get('justification')}\n"
            f"Résultats de recherche web :\n{resultat or '(aucun résultat trouvé)'}"
        )
    return "\n\n".join(blocs)


def _verifier_avec_recherche_web(nom_fichier: str, anomalies: list, client: LLMClient) -> list:
    """
    Deuxième passe, un seul appel LLM pour tout le chunk (pas un par
    anomalie) : confronte chaque anomalie candidate à un extrait de
    recherche web et retire celles que le modèle juge contredites par des
    preuves claires. Défensif à chaque étape (recherche, appel LLM,
    parsing) : le moindre échec conserve les anomalies candidates telles
    quelles plutôt que de perdre du signal. Coupable via
    config.RECHERCHE_WEB_ACTIVEE. Voir agents/regles_realisme.md.
    """
    if not config.RECHERCHE_WEB_ACTIVEE:
        return anomalies

    resultats_recherche = []
    for anomalie in anomalies:
        resultats_recherche.append(_rechercher_web(_construire_requete_recherche(anomalie)))
        time.sleep(PAUSE_ENTRE_RECHERCHES_SECONDES)

    if not any(resultats_recherche):
        logger.warning(f"{nom_fichier} : aucun résultat de recherche web, anomalies conservées sans vérification.")
        return anomalies

    try:
        reponse, metadata = client.appeler(
            system_prompt=PROMPT_VERIFICATION_SYSTEME,
            user_prompt=_construire_prompt_verification_utilisateur(anomalies, resultats_recherche),
            model=config.MODELES["realisme"],
            temperature=config.TEMPERATURES["realisme"],
            max_tokens=config.MAX_TOKENS["realisme"],
            json_attendu=True,
        )
    except Exception as erreur:
        logger.warning(f"{nom_fichier} : appel de vérification web échoué ({erreur}), anomalies conservées telles quelles.")
        return anomalies

    if not metadata.succes or not isinstance(reponse, dict):
        logger.warning(f"{nom_fichier} : réponse de vérification web invalide, anomalies conservées telles quelles.")
        return anomalies

    verifications = {
        v.get("index"): v
        for v in (reponse.get("verifications") or [])
        if isinstance(v, dict) and isinstance(v.get("index"), int)
    }

    anomalies_confirmees = []
    for i, anomalie in enumerate(anomalies):
        verif = verifications.get(i)
        if verif is not None and verif.get("confirmee") is False:
            logger.info(
                f"{nom_fichier} : anomalie retirée après vérification web (colonne(s) "
                f"{anomalie.get('colonnes')}, valeur={anomalie.get('valeur')!r}) : {verif.get('raison', '')}"
            )
            continue
        anomalies_confirmees.append(anomalie)

    return anomalies_confirmees


# ---------------------------------------------------------------------------
# Appel LLM par chunk
# ---------------------------------------------------------------------------


def _analyser_chunk(nom_fichier: str, chunk: pd.DataFrame, client: LLMClient, num_chunk: int, nb_chunks: int) -> dict:
    """
    Analyse un chunk via le LLM et retourne ses anomalies avec son statut.
    Une exception ou un appel en échec reste non bloquant pour les chunks
    suivants, mais n'est plus confondu avec un succès sans anomalie.
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
        return {
            "anomalies": [],
            "succes": False,
            "error": f"exception pendant l'appel LLM : {erreur}",
            "nb_lignes": len(chunk),
        }

    if not metadata.succes:
        logger.warning(
            f"{nom_fichier} chunk {num_chunk}/{nb_chunks} : appel LLM en échec "
            f"(modèle={metadata.modele}, erreur={metadata.erreur})."
        )
        return {
            "anomalies": [],
            "succes": False,
            "error": metadata.erreur or "appel LLM en échec sans détail",
            "nb_lignes": len(chunk),
        }

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

    if anomalies_validees:
        anomalies_validees = _verifier_avec_recherche_web(nom_fichier, anomalies_validees, client)

    logger.info(
        f"{nom_fichier} chunk {num_chunk}/{nb_chunks} : {len(anomalies_validees)} anomalie(s) "
        f"de réalisme (modèle={metadata.modele}, depuis_cache={metadata.depuis_cache})."
    )
    return {
        "anomalies": anomalies_validees,
        "succes": True,
        "error": None,
        "nb_lignes": len(chunk),
    }


# ---------------------------------------------------------------------------
# Fonction principale
# ---------------------------------------------------------------------------


def analyser_avec_statut(
    nom_fichier: str,
    df: pd.DataFrame,
    client: LLMClient | None = None,
    echantillonner: bool = True,
) -> dict:
    """
    Découpe df en chunks et retourne les anomalies ainsi que l'état global
    SUCCESS/PARTIAL/FAILED et les compteurs d'exécution.

    Un fichier "géant" (> config.SEUIL_FICHIER_GEANT lignes) est échantillonné
    à config.MAX_LIGNES_PAR_FICHIER lignes (tirage aléatoire reproductible,
    SEED_ECHANTILLONNAGE, index d'origine retrié après tirage) ; en dessous de
    ce seuil, le fichier est traité intégralement, sans troncature. Même
    règle que agents/agent_coherence.py (`SEED_ECHANTILLONNAGE` différente,
    définie localement à chaque module), pour un comportement cohérent entre
    les deux agents LLM sur les plus gros fichiers — voir regles_realisme.md.

    `echantillonner=False` est utilisé par l'orchestrateur : celui-ci a déjà
    choisi les lignes suspectes et l'agent doit alors analyser exactement le
    lot reçu. Le mode autonome historique conserve son filet de sécurité.
    """
    if df is None or df.empty:
        return {
            "anomalies": [],
            "status": STATUT_SUCCESS,
            "analyse_complete": True,
            "chunks_total": 0,
            "chunks_success": 0,
            "chunks_failed": 0,
            "nb_lignes_recues": 0,
            "nb_lignes_analysees": 0,
            "nb_lignes_echec": 0,
            "error": None,
        }

    nb_lignes_recues = len(df)
    if echantillonner and len(df) > config.SEUIL_FICHIER_GEANT:
        taille_echantillon = min(config.MAX_LIGNES_PAR_FICHIER, len(df))
        logger.warning(
            f"{nom_fichier} : fichier géant ({len(df)} lignes), échantillon de "
            f"{taille_echantillon} lignes (seed={SEED_ECHANTILLONNAGE})."
        )
        df = df.sample(n=taille_echantillon, random_state=SEED_ECHANTILLONNAGE).sort_index()

    client = client or LLMClient()
    chunks = _decouper_en_chunks(df, config.CHUNK_SIZE)

    anomalies = []
    chunks_success = 0
    chunks_failed = 0
    nb_lignes_analysees = 0
    nb_lignes_echec = 0
    erreurs = []
    for i, chunk in enumerate(chunks):
        resultat_chunk = _analyser_chunk(nom_fichier, chunk, client, i + 1, len(chunks))
        if resultat_chunk["succes"]:
            chunks_success += 1
            nb_lignes_analysees += resultat_chunk["nb_lignes"]
            anomalies.extend(resultat_chunk["anomalies"])
        else:
            chunks_failed += 1
            nb_lignes_echec += resultat_chunk["nb_lignes"]
            if resultat_chunk.get("error"):
                erreurs.append(str(resultat_chunk["error"]))

    if chunks_failed == 0:
        status = STATUT_SUCCESS
    elif chunks_success == 0:
        status = STATUT_FAILED
    else:
        status = STATUT_PARTIAL

    logger.info(f"{nom_fichier} : {len(anomalies)} anomalie(s) de réalisme au total sur {len(chunks)} chunk(s).")
    return {
        "anomalies": anomalies,
        "status": status,
        "analyse_complete": status == STATUT_SUCCESS,
        "chunks_total": len(chunks),
        "chunks_success": chunks_success,
        "chunks_failed": chunks_failed,
        "nb_lignes_recues": nb_lignes_recues,
        "nb_lignes_analysees": nb_lignes_analysees,
        "nb_lignes_echec": nb_lignes_echec,
        "error": " ; ".join(dict.fromkeys(erreurs)) or None,
    }


def analyser(nom_fichier: str, df: pd.DataFrame, client: LLMClient | None = None) -> list:
    """API historique : retourne uniquement les anomalies pour les appels autonomes existants."""
    return analyser_avec_statut(nom_fichier, df, client=client, echantillonner=True)["anomalies"]


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
