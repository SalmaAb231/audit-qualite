"""
Utilitaires de parsing et de validation des réponses JSON produites par les
agents LLM.

Les modèles ne renvoient pas toujours du JSON parfaitement formé (texte
autour, blocs markdown, virgules terminales, guillemets simples, réponse
tronquée par une limite de tokens...). extraire_json() tente plusieurs
stratégies de récupération avant d'abandonner. valider_anomalies() vérifie
ensuite que chaque anomalie extraite respecte le contrat commun défini dans
docs/contrat_anomalie.md.
"""

import json
import re
from typing import Any

from loguru import logger

# ---------------------------------------------------------------------------
# Extraction JSON robuste
# ---------------------------------------------------------------------------

BLOC_MARKDOWN_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
VIRGULE_TERMINALE_RE = re.compile(r",\s*([}\]])")

PAIRES_OUVRANTES = {"{": "}", "[": "]"}
PAIRES_FERMANTES = {"}": "{", "]": "["}


def extraire_json(texte: str) -> Any | None:
    """
    Tente d'extraire un objet ou tableau JSON exploitable d'une réponse LLM.

    Stratégies essayées dans l'ordre, sur le texte entier puis sur chaque
    bloc markdown ```json ... ``` trouvé, puis sur la sous-chaîne comprise
    entre la première accolade/crochet ouvrant et la dernière fermante :
    1. parsing direct ;
    2. suppression des virgules terminales avant '}' ou ']' ;
    3. remplacement des guillemets simples par des doubles (si le texte
       n'utilise déjà aucun guillemet double, pour éviter de casser des
       chaînes valides contenant des apostrophes) ;
    4. réparation d'un JSON tronqué (fermeture des crochets/accolades
       encore ouverts en fin de texte).
    En dernier recours, recherche par regex du premier bloc {...} ou [...].

    Retourne la structure Python correspondante, ou None si rien n'a pu
    être extrait.
    """
    if not texte or not texte.strip():
        return None

    candidats = [texte.strip()]
    candidats.extend(bloc.strip() for bloc in BLOC_MARKDOWN_RE.findall(texte))

    sous_chaine = _extraire_sous_chaine_delimitee(texte)
    if sous_chaine:
        candidats.append(sous_chaine)

    for candidat in candidats:
        resultat = _tenter_parsing(candidat)
        if resultat is not None:
            return resultat

    for motif in (r"\{.*\}", r"\[.*\]"):
        correspondance = re.search(motif, texte, re.DOTALL)
        if correspondance:
            resultat = _tenter_parsing(correspondance.group(0))
            if resultat is not None:
                return resultat

    logger.warning("Impossible d'extraire un JSON valide de la réponse LLM.")
    return None


def _extraire_sous_chaine_delimitee(texte: str) -> str | None:
    """Isole le texte entre la première accolade/crochet ouvrant et la dernière fermante."""
    debut = next((i for i, c in enumerate(texte) if c in "{["), None)
    if debut is None:
        return None
    fin = next((i for i in range(len(texte) - 1, debut, -1) if texte[i] in "}]"), None)
    if fin is None or fin <= debut:
        return None
    return texte[debut : fin + 1]


def _tenter_parsing(candidat: str) -> Any | None:
    """Essaie plusieurs transformations successives jusqu'à obtenir un JSON valide."""
    if not candidat:
        return None

    transformations = [
        lambda s: s,
        lambda s: VIRGULE_TERMINALE_RE.sub(r"\1", s),
        lambda s: VIRGULE_TERMINALE_RE.sub(r"\1", s.replace("'", '"')) if '"' not in s else s,
        lambda s: _reparer_json_tronque(VIRGULE_TERMINALE_RE.sub(r"\1", s)),
    ]

    for transformer in transformations:
        try:
            texte_transforme = transformer(candidat)
            return json.loads(texte_transforme)
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def _reparer_json_tronque(texte: str) -> str:
    """
    Ferme les crochets/accolades et la chaîne encore ouverts en fin de texte,
    en ignorant ceux rencontrés à l'intérieur de chaînes JSON. Utile quand la
    réponse du modèle a été coupée par une limite de tokens de sortie.
    """
    ouverts = []
    dans_chaine = False
    echappement = False

    for c in texte:
        if dans_chaine:
            if echappement:
                echappement = False
            elif c == "\\":
                echappement = True
            elif c == '"':
                dans_chaine = False
            continue

        if c == '"':
            dans_chaine = True
        elif c in PAIRES_OUVRANTES:
            ouverts.append(c)
        elif c in PAIRES_FERMANTES:
            if ouverts and ouverts[-1] == PAIRES_FERMANTES[c]:
                ouverts.pop()

    reparation = texte
    if dans_chaine:
        reparation += '"'
    for ouvrant in reversed(ouverts):
        reparation += PAIRES_OUVRANTES[ouvrant]
    return reparation


# ---------------------------------------------------------------------------
# Validation du contrat d'anomalie (docs/contrat_anomalie.md)
# ---------------------------------------------------------------------------

CHAMPS_REQUIS = (
    "fichier", "ligne", "colonnes", "type", "severite",
    "source", "valeur", "justification", "confiance",
)
TYPES_VALIDES = {"structure", "statistique", "coherence", "realisme"}
SEVERITES_VALIDES = {"critical", "high", "medium", "low"}
SOURCES_VALIDES = {"stats", "llm"}
SEVERITE_PAR_DEFAUT = "medium"


def valider_anomalies(liste: list) -> list:
    """
    Filtre et corrige une liste d'anomalies brutes (issues d'extraire_json)
    pour ne conserver que celles conformes au contrat commun.

    Corrections tolérées (journalisées en warning) : sévérité manquante ou
    hors énumération -> 'medium' ; 'colonnes' fourni comme chaîne unique ->
    liste d'un élément ; 'ligne' fourni comme chaîne numérique -> entier ;
    'confiance' hors [0, 1] -> tronquée aux bornes.

    Toute anomalie qui reste invalide après ces corrections (champ requis
    manquant, énumération invalide, type incorrect) est rejetée et journalisée
    en warning, sans jamais interrompre le traitement des suivantes.
    """
    anomalies_valides = []

    for i, anomalie in enumerate(liste):
        if not isinstance(anomalie, dict):
            logger.warning(f"Anomalie #{i} ignorée : ce n'est pas un objet JSON (type={type(anomalie).__name__}).")
            continue

        anomalie = dict(anomalie)

        if anomalie.get("severite") not in SEVERITES_VALIDES:
            logger.warning(
                f"Anomalie #{i} : sévérité invalide/absente ({anomalie.get('severite')!r}), "
                f"corrigée en '{SEVERITE_PAR_DEFAUT}'."
            )
            anomalie["severite"] = SEVERITE_PAR_DEFAUT

        if isinstance(anomalie.get("colonnes"), str):
            anomalie["colonnes"] = [anomalie["colonnes"]]

        if isinstance(anomalie.get("ligne"), str) and anomalie["ligne"].strip().isdigit():
            anomalie["ligne"] = int(anomalie["ligne"].strip())

        confiance = anomalie.get("confiance")
        if isinstance(confiance, (int, float)) and not isinstance(confiance, bool):
            if confiance < 0.0 or confiance > 1.0:
                borne = max(0.0, min(1.0, float(confiance)))
                logger.warning(f"Anomalie #{i} : confiance hors bornes ({confiance}), corrigée en {borne}.")
                anomalie["confiance"] = borne

        champs_manquants = [c for c in CHAMPS_REQUIS if anomalie.get(c) in (None, "")]
        if champs_manquants:
            logger.warning(f"Anomalie #{i} rejetée : champ(s) manquant(s) {champs_manquants}.")
            continue

        if anomalie["type"] not in TYPES_VALIDES:
            logger.warning(f"Anomalie #{i} rejetée : type invalide {anomalie['type']!r}.")
            continue

        if anomalie["source"] not in SOURCES_VALIDES:
            logger.warning(f"Anomalie #{i} rejetée : source invalide {anomalie['source']!r}.")
            continue

        if not isinstance(anomalie["colonnes"], list):
            logger.warning(f"Anomalie #{i} rejetée : 'colonnes' doit être une liste.")
            continue

        if not isinstance(anomalie["ligne"], int) or isinstance(anomalie["ligne"], bool):
            logger.warning(f"Anomalie #{i} rejetée : 'ligne' doit être un entier.")
            continue

        confiance = anomalie["confiance"]
        if not isinstance(confiance, (int, float)) or isinstance(confiance, bool) or not (0.0 <= float(confiance) <= 1.0):
            logger.warning(f"Anomalie #{i} rejetée : 'confiance' doit être un nombre entre 0 et 1.")
            continue
        anomalie["confiance"] = float(confiance)

        anomalies_valides.append(anomalie)

    logger.info(f"{len(anomalies_valides)}/{len(liste)} anomalie(s) validée(s).")
    return anomalies_valides
