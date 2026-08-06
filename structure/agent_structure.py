"""
Détecteur de structure du pipeline audit-qualite.

Premier détecteur du pipeline : vérifie qu'un fichier est bien formé
(colonnes sans nom, dupliquées ou vides, lignes dupliquées, taux de valeurs
manquantes, types incohérents), indépendamment de la plausibilité de son
contenu métier (rôle futur de stats/ et des agents LLM). 100% déterministe,
aucun appel LLM.

Convention 'ligne' (docs/contrat_anomalie.md) : une anomalie qui ne concerne
pas une ligne précise (colonne vide, colonne dupliquée, fichier vide...)
utilise ligne=0 (portée fichier/colonne). Voir structure/regles.md.

Usage :
    python -m structure.agent_structure <chemin_ou_nom_de_fichier>
"""

import json
import re
import sys
from collections import Counter
from pathlib import Path

import pandas as pd
from loguru import logger
from tabulate import tabulate

# Permet l'exécution directe du script en plus de l'exécution en tant que module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from ingestion.local_loader import LocalLoader

# ---------------------------------------------------------------------------
# Constantes (aucun seuil en dur dans les fonctions)
# ---------------------------------------------------------------------------

SEUIL_NAN_MEDIUM = 0.50
SEUIL_NAN_HIGH = 0.90

TAILLE_ECHANTILLON_TYPES = 200
SEUIL_MELANGE_TYPE_BAS = 0.60
SEUIL_MELANGE_TYPE_HAUT = 0.95

NB_INDEX_DUPLIQUES_AFFICHES = 20

TYPE_ANOMALIE = "structure"
SOURCE_ANOMALIE = "stats"

NOM_VIDE_RE = re.compile(r"^unnamed:\s*\d+$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Construction des anomalies (contrat docs/contrat_anomalie.md)
# ---------------------------------------------------------------------------


def _anomalie(nom_fichier: str, ligne: int, colonnes: list, severite: str,
              valeur: str, justification: str, confiance: float) -> dict:
    """Construit un dict d'anomalie strictement conforme au contrat commun."""
    return {
        "fichier": nom_fichier,
        "ligne": ligne,
        "colonnes": [str(c) for c in colonnes],
        "type": TYPE_ANOMALIE,
        "severite": severite,
        "source": SOURCE_ANOMALIE,
        "valeur": valeur,
        "justification": justification,
        "confiance": confiance,
    }


def _nom_colonne_absent(nom) -> bool:
    """Vrai si le nom de colonne est vide/blanc ou un artefact pandas 'Unnamed: N'."""
    nom_str = str(nom).strip()
    if not nom_str:
        return True
    return bool(NOM_VIDE_RE.match(nom_str))


# ---------------------------------------------------------------------------
# Vérifications individuelles
# ---------------------------------------------------------------------------


def verifier_fichier_vide(nom_fichier: str, df: pd.DataFrame) -> list:
    """Signale un fichier sans aucune ligne ou aucune colonne exploitable (critical)."""
    if df is not None and len(df) > 0 and len(df.columns) > 0:
        return []

    nb_lignes = len(df) if df is not None else 0
    nb_colonnes = len(df.columns) if df is not None else 0
    return [_anomalie(
        nom_fichier, 0, [], "critical",
        f"{nb_lignes} ligne(s), {nb_colonnes} colonne(s)",
        "Le fichier ne contient aucune ligne ou aucune colonne exploitable.",
        1.0,
    )]


def verifier_colonnes_sans_nom(nom_fichier: str, df: pd.DataFrame) -> list:
    """
    Signale les colonnes sans nom (vide, blanc, ou 'Unnamed: N' généré par pandas
    à la lecture d'un fichier sans en-tête sur cette colonne).
    Sévérité 'high' si la colonne contient des données, 'low' si elle est
    entièrement vide (simple artefact d'export sans impact).
    """
    anomalies = []
    for i, colonne in enumerate(df.columns):
        if not _nom_colonne_absent(colonne):
            continue

        serie = df.iloc[:, i]
        nb_valeurs = int(serie.notna().sum())

        if nb_valeurs == 0:
            anomalies.append(_anomalie(
                nom_fichier, 0, [colonne], "low",
                "0 valeur non nulle sur une colonne sans nom",
                f"La colonne '{colonne}' n'a pas de nom mais ne contient aucune donnée : "
                "simple artefact d'export, sans impact.",
                0.6,
            ))
        else:
            anomalies.append(_anomalie(
                nom_fichier, 0, [colonne], "high",
                f"{nb_valeurs} valeur(s) non nulle(s) sans nom de colonne",
                f"La colonne '{colonne}' contient {nb_valeurs} valeurs mais n'a pas de nom — "
                "probable décalage de colonnes à l'export.",
                0.9,
            ))
    return anomalies


def verifier_colonnes_dupliquees(nom_fichier: str, df: pd.DataFrame) -> list:
    """Signale les noms de colonnes qui apparaissent plusieurs fois (high)."""
    compteur = Counter(df.columns)
    anomalies = []
    for nom, nb in compteur.items():
        if nb <= 1:
            continue
        anomalies.append(_anomalie(
            nom_fichier, 0, [nom], "high",
            f"{nb} colonne(s) nommée(s) '{nom}'",
            f"Le nom de colonne '{nom}' apparaît {nb} fois : risque de confusion ou de perte "
            "de données à la lecture.",
            1.0,
        ))
    return anomalies


def verifier_colonnes_vides(nom_fichier: str, df: pd.DataFrame) -> list:
    """
    Signale les colonnes entièrement vides (100% NaN), à l'exclusion des colonnes
    sans nom (déjà couvertes par verifier_colonnes_sans_nom) pour éviter un double
    signalement de la même colonne.
    """
    anomalies = []
    nb_lignes = len(df)
    for i, colonne in enumerate(df.columns):
        if _nom_colonne_absent(colonne):
            continue

        serie = df.iloc[:, i]
        if serie.notna().sum() > 0:
            continue

        anomalies.append(_anomalie(
            nom_fichier, 0, [colonne], "medium",
            f"0 valeur non nulle sur {nb_lignes} ligne(s)",
            f"La colonne '{colonne}' est entièrement vide (100% de valeurs manquantes).",
            1.0,
        ))
    return anomalies


def verifier_lignes_dupliquees(nom_fichier: str, df: pd.DataFrame) -> list:
    """
    Signale les lignes strictement identiques à une autre ligne du fichier.
    Une seule anomalie agrégée est produite (pas une par ligne dupliquée),
    pour ne pas noyer le rapport ; elle liste le nombre de doublons et un
    aperçu des index concernés.
    """
    masque = df.duplicated(keep="first")
    if not masque.any():
        return []

    index_dupliques = list(df.index[masque])
    nb = len(index_dupliques)
    premiere_ligne = int(index_dupliques[0])
    apercu = index_dupliques[:NB_INDEX_DUPLIQUES_AFFICHES]
    suffixe = ", ..." if nb > len(apercu) else ""

    return [_anomalie(
        nom_fichier, premiere_ligne, [], "medium",
        f"{nb} ligne(s) dupliquée(s), ex. index {apercu}{suffixe}",
        f"{nb} lignes sont des doublons exacts d'une autre ligne du fichier "
        f"(première occurrence en ligne {premiere_ligne}).",
        1.0,
    )]


def verifier_taux_valeurs_manquantes(nom_fichier: str, df: pd.DataFrame) -> list:
    """
    Signale les colonnes avec un taux de valeurs manquantes élevé (medium >= 50%,
    high >= 90%). Exclut les colonnes sans nom et les colonnes à 100% de NaN,
    déjà couvertes par les vérifications précédentes.
    """
    anomalies = []
    nb_lignes = len(df)
    if nb_lignes == 0:
        return anomalies

    for i, colonne in enumerate(df.columns):
        if _nom_colonne_absent(colonne):
            continue

        serie = df.iloc[:, i]
        nb_manquantes = int(serie.isna().sum())
        taux = nb_manquantes / nb_lignes

        if taux >= 1.0:
            continue  # colonne entièrement vide, déjà couverte par verifier_colonnes_vides
        elif taux >= SEUIL_NAN_HIGH:
            severite = "high"
            libelle_seuil = "plus de 90%"
        elif taux >= SEUIL_NAN_MEDIUM:
            severite = "medium"
            libelle_seuil = "plus de la moitié"
        else:
            continue

        anomalies.append(_anomalie(
            nom_fichier, 0, [colonne], severite,
            f"{taux:.1%} de valeurs manquantes ({nb_manquantes}/{nb_lignes})",
            f"La colonne '{colonne}' a {libelle_seuil} de ses valeurs manquantes ({taux:.1%}).",
            1.0,
        ))
    return anomalies


def _masque_numerique_europeen(echantillon: pd.Series) -> pd.Series:
    """
    Masque booléen : valeur convertible en nombre. Gère le format européen
    (point ou espace = séparateur de milliers, virgule = séparateur décimal,
    ex. '2.364,40' ou '2 364,40') en plus du format standard, pour ne pas
    signaler ces formats très courants dans les données de renting comme
    une anomalie.
    """
    nettoyees = echantillon.str.strip()
    nettoyees = nettoyees.str.replace(r"(?<=\d)[.\s](?=\d{3}(?:\D|$))", "", regex=True)
    nettoyees = nettoyees.str.replace(",", ".", regex=False)
    return pd.to_numeric(nettoyees, errors="coerce").notna()


# Repère syntaxique grossier d'une date (jj/mm/aaaa, jj-mm-aaaa, jj.mm.aaaa...)
# avant de tenter une conversion réelle : évite de faire analyser des nombres
# à séparateurs (ex. '2.364,40') par le parseur de dates, très permissif.
DATE_VRAISEMBLABLE_RE = re.compile(
    r"^\d{1,4}[/\-.]\d{1,2}[/\-.]\d{1,4}([ T]\d{1,2}:\d{2}(:\d{2})?)?$"
)


def _masque_date(echantillon: pd.Series) -> pd.Series:
    """
    Masque booléen : valeur convertible en date (jour en premier, format
    européen). Ne tente la conversion que sur les valeurs qui ressemblent
    syntaxiquement à une date (DATE_VRAISEMBLABLE_RE), pour éviter que le
    parseur de dates (très permissif) n'interprète à tort des nombres à
    séparateurs comme des dates.
    """
    resultat = pd.Series(False, index=echantillon.index)
    candidats = echantillon[echantillon.str.match(DATE_VRAISEMBLABLE_RE)]
    if candidats.empty:
        return resultat
    convertis = pd.to_datetime(candidats, errors="coerce", dayfirst=True, format="mixed")
    resultat.loc[candidats.index] = convertis.notna()
    return resultat


def verifier_types_incoherents(nom_fichier: str, df: pd.DataFrame) -> list:
    """
    Signale les colonnes texte qui mélangent des valeurs numériques ou de
    type date avec du texte non convertible, sur un échantillon de valeurs
    non nulles. Ni "quasiment tout" (>= 95%, colonne homogène y compris au
    format européen) ni "quasiment rien" (< 60%, colonne textuelle normale) :
    seul le ratio intermédiaire (60-95%) est un mélange suspect.

    Ne s'applique qu'aux colonnes déjà textuelles pour pandas (dtype 'object'
    ou 'str' selon la version de pandas) : une colonne que pandas a réussi à
    typer nombre/date/booléen à la lecture est par construction homogène.
    """
    anomalies = []
    for i, colonne in enumerate(df.columns):
        if _nom_colonne_absent(colonne):
            continue

        serie = df.iloc[:, i]
        if (
            pd.api.types.is_numeric_dtype(serie)
            or pd.api.types.is_datetime64_any_dtype(serie)
            or pd.api.types.is_bool_dtype(serie)
        ):
            continue

        echantillon = serie.dropna().astype(str).head(TAILLE_ECHANTILLON_TYPES)
        if echantillon.empty:
            continue

        masque_numerique = _masque_numerique_europeen(echantillon)
        masque_date = _masque_date(echantillon)
        ratio_numerique = masque_numerique.mean()
        ratio_date = masque_date.mean()

        if ratio_numerique >= ratio_date:
            ratio_dominant, masque_dominant, nature = ratio_numerique, masque_numerique, "numériques"
        else:
            ratio_dominant, masque_dominant, nature = ratio_date, masque_date, "de type date"

        if not (SEUIL_MELANGE_TYPE_BAS <= ratio_dominant < SEUIL_MELANGE_TYPE_HAUT):
            continue

        exemples = echantillon[~masque_dominant].head(2).tolist()
        exemple_txt = ", ".join(f"'{v}'" for v in exemples)

        anomalies.append(_anomalie(
            nom_fichier, 0, [colonne], "medium",
            f"{ratio_dominant:.1%} des valeurs non nulles sont {nature}, le reste est du texte"
            + (f" (ex. {exemple_txt})" if exemple_txt else ""),
            f"La colonne '{colonne}' mélange des valeurs {nature} ({ratio_dominant:.1%}) et du "
            "texte non convertible : type de données incohérent.",
            0.7,
        ))
    return anomalies


# ---------------------------------------------------------------------------
# Fonctions principales
# ---------------------------------------------------------------------------

# Ordre d'exécution des vérifications (hors verifier_fichier_vide, lancée à part)
VERIFICATIONS = (
    verifier_colonnes_sans_nom,
    verifier_colonnes_dupliquees,
    verifier_colonnes_vides,
    verifier_lignes_dupliquees,
    verifier_taux_valeurs_manquantes,
    verifier_types_incoherents,
)


def _executer_verification(verification, nom_fichier: str, df: pd.DataFrame) -> list:
    """Exécute une vérification en isolant toute exception : jamais bloquant pour les suivantes."""
    try:
        return verification(nom_fichier, df)
    except Exception as erreur:
        logger.error(f"Échec de {verification.__name__} sur {nom_fichier} : {erreur}")
        return []


def analyser(nom_fichier: str, df: pd.DataFrame) -> list:
    """
    Enchaîne toutes les vérifications de structure et retourne la liste agrégée
    des anomalies détectées, chacune conforme à docs/contrat_anomalie.md.
    Ne lève jamais d'exception. Si le fichier est vide, les autres vérifications
    sont sautées (sans objet sur un DataFrame à 0 ligne/colonne).
    """
    anomalies_vide = _executer_verification(verifier_fichier_vide, nom_fichier, df)
    if anomalies_vide:
        return anomalies_vide

    anomalies = []
    for verification in VERIFICATIONS:
        anomalies.extend(_executer_verification(verification, nom_fichier, df))
    return anomalies


def fichier_exploitable(anomalies: list) -> bool:
    """Retourne False si au moins une anomalie 'critical' rend le fichier inexploitable."""
    return not any(a.get("severite") == "critical" for a in anomalies)


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
        print("Aucune anomalie de structure détectée.")
        return

    lignes = [
        [a["severite"], ", ".join(a["colonnes"]) or "-", a["valeur"], a["justification"]]
        for a in anomalies
    ]
    print(tabulate(lignes, headers=["Sévérité", "Colonne(s)", "Valeur", "Justification"], tablefmt="grid"))


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage : python -m structure.agent_structure <chemin_ou_nom_de_fichier>")
        sys.exit(1)

    chemin = _resoudre_chemin(sys.argv[1])
    df = LocalLoader().charger(chemin)
    if df is None:
        logger.error(f"Échec du chargement de {chemin}")
        sys.exit(1)

    nom_fichier = chemin.name
    anomalies = analyser(nom_fichier, df)

    print("\n" + "=" * 60)
    print(f"ANOMALIES DE STRUCTURE — {nom_fichier}")
    print("=" * 60)
    _afficher_anomalies(anomalies)
    print(f"\nFichier exploitable pour la suite du pipeline : {fichier_exploitable(anomalies)}")

    config.RESULTATS_DIR.mkdir(parents=True, exist_ok=True)
    fichier_sortie = config.RESULTATS_DIR / f"structure_{chemin.stem}.json"
    fichier_sortie.write_text(json.dumps(anomalies, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(f"Anomalies sauvegardées dans {fichier_sortie}")


if __name__ == "__main__":
    main()
