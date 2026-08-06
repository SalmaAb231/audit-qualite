"""
Détecteur statistique du pipeline audit-qualite.

Deuxième détecteur du pipeline (après structure/) : juge la plausibilité
statistique des valeurs, sans connaître le sens métier des colonnes
(schema-agnostic) et sans aucun appel LLM. Ne juge jamais la forme du
fichier (rôle de structure/) ni la cohérence métier qui nécessite une
connaissance du domaine (rôle futur des agents LLM Cohérence et Réalisme) —
sauf l'incohérence entre deux colonnes NUMÉRIQUES CORRÉLÉES
(verifier_incoherence_entre_colonnes), détectable mathématiquement : une
"petite voiture à prix énorme" casse la relation habituelle entre puissance
et prix, même si ni la puissance ni le prix ne sont individuellement
aberrants.

Convention 'ligne' (docs/contrat_anomalie.md) : une anomalie qui ne concerne
pas une ligne précise (colonne quasi-constante, cardinalité suspecte...)
utilise ligne=0. Voir stats/regles.md.

Usage :
    python -m stats.agent_stats <chemin_ou_nom_de_fichier>
"""

import json
import re
import sys
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

FACTEUR_IQR = 1.5
SEUIL_MIN_VALEURS_OUTLIERS = 10
SEUIL_MAX_PART_OUTLIERS = 0.30
NB_INDEX_OUTLIERS_AFFICHES = 10

SEUIL_POS_DOMINANT_MIN = 0.90
SEUIL_NEG_MINORITAIRE_MAX = 0.10

SEUIL_QUASI_CONSTANTE = 0.99
SEUIL_MIN_VALEURS_QUASI_CONSTANTE = 20

SEUIL_CARDINALITE_SUSPECTE = 0.95
SEUIL_MIN_VALEURS_CARDINALITE = 50
NOMS_IDENTIFIANTS_RE = re.compile(r"(url|id|ref|lien|code|key|sku|vin)", re.IGNORECASE)

SEUIL_CORRELATION_MIN = 0.7
FACTEUR_IQR_RESIDUS = 3.5  # plus strict que FACTEUR_IQR : voir verifier_incoherence_entre_colonnes
SEUIL_MIN_PAIRES_VALEURS = 30
NB_PAIRES_MAX_TRAITEES = 20

TYPE_ANOMALIE = "statistique"
SOURCE_ANOMALIE = "stats"


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


def _bornes_iqr(serie: pd.Series, facteur: float = FACTEUR_IQR) -> tuple:
    """Bornes IQR [Q1 - facteur*IQR, Q3 + facteur*IQR]."""
    q1 = serie.quantile(0.25)
    q3 = serie.quantile(0.75)
    iqr = q3 - q1
    return q1 - facteur * iqr, q3 + facteur * iqr


# ---------------------------------------------------------------------------
# Vérifications individuelles
# ---------------------------------------------------------------------------


def verifier_outliers_numeriques(nom_fichier: str, df: pd.DataFrame) -> list:
    """
    Signale, par colonne numérique, les valeurs statistiquement aberrantes
    (méthode IQR : hors de [Q1 - 1.5*IQR, Q3 + 1.5*IQR]). Une seule anomalie
    agrégée par colonne (pas une par valeur), pour ne pas noyer le rapport.
    Si plus de SEUIL_MAX_PART_OUTLIERS des valeurs sont hors bornes, la
    colonne n'est probablement pas unimodale : la méthode IQR n'est pas
    fiable dans ce cas, le check est ignoré pour cette colonne.
    """
    anomalies = []
    for i, colonne in enumerate(df.columns):
        serie = df.iloc[:, i]
        if not pd.api.types.is_numeric_dtype(serie):
            continue

        valeurs = serie.dropna()
        if len(valeurs) < SEUIL_MIN_VALEURS_OUTLIERS:
            continue

        borne_basse, borne_haute = _bornes_iqr(valeurs)
        if borne_haute <= borne_basse:
            continue  # pas de dispersion (colonne quasi-constante) : rien à détecter ici

        masque = (valeurs < borne_basse) | (valeurs > borne_haute)
        nb_outliers = int(masque.sum())
        if nb_outliers == 0:
            continue

        part_outliers = nb_outliers / len(valeurs)
        if part_outliers > SEUIL_MAX_PART_OUTLIERS:
            continue  # probablement pas unimodale, méthode IQR pas fiable ici

        index_outliers = list(valeurs.index[masque])
        premiere_ligne = int(index_outliers[0])
        apercu_valeurs = valeurs[masque].head(NB_INDEX_OUTLIERS_AFFICHES).tolist()

        anomalies.append(_anomalie(
            nom_fichier, premiere_ligne, [colonne], "medium",
            f"{nb_outliers} valeur(s) hors de l'intervalle attendu "
            f"[{borne_basse:.1f}, {borne_haute:.1f}], ex. {apercu_valeurs}",
            f"{nb_outliers} valeur(s) de '{colonne}' sont statistiquement aberrantes par "
            "rapport au reste du fichier (méthode IQR).",
            0.7,
        ))
    return anomalies


def verifier_valeurs_negatives_minoritaires(nom_fichier: str, df: pd.DataFrame) -> list:
    """
    Signale les colonnes numériques à dominante positive (>= 90% de valeurs
    >= 0) qui contiennent malgré tout une minorité de valeurs négatives
    (< 10% des valeurs non nulles) : changement de signe suspect. Une colonne
    majoritairement négative n'est pas signalée (probable convention de
    signe intentionnelle, ex. colonne "écart" ou "ajustement").
    """
    anomalies = []
    for i, colonne in enumerate(df.columns):
        serie = df.iloc[:, i]
        if not pd.api.types.is_numeric_dtype(serie):
            continue

        valeurs = serie.dropna()
        nb_valeurs = len(valeurs)
        if nb_valeurs == 0:
            continue

        nb_negatives = int((valeurs < 0).sum())
        if nb_negatives == 0:
            continue

        part_negatives = nb_negatives / nb_valeurs
        part_positives = 1.0 - part_negatives

        if part_positives < SEUIL_POS_DOMINANT_MIN or part_negatives >= SEUIL_NEG_MINORITAIRE_MAX:
            continue

        anomalies.append(_anomalie(
            nom_fichier, 0, [colonne], "medium",
            f"{nb_negatives} valeur(s) négative(s) sur {nb_valeurs} ({part_negatives:.1%}), "
            f"alors que la colonne est à {part_positives:.1%} positive",
            f"'{colonne}' est presque toujours positive ; {nb_negatives} valeur(s) négative(s) "
            "détonnent par rapport au reste de la colonne.",
            0.6,
        ))
    return anomalies


def verifier_colonnes_quasi_constantes(nom_fichier: str, df: pd.DataFrame) -> list:
    """
    Signale les colonnes où une seule valeur représente au moins
    SEUIL_QUASI_CONSTANTE des valeurs non nulles (avec au moins
    SEUIL_MIN_VALEURS_QUASI_CONSTANTE valeurs, pour ne pas juger sur un
    petit échantillon). Sévérité 'low' : pas bloquant, mais souvent le
    signe d'une colonne peu informative ou d'un artefact de scraping.
    """
    anomalies = []
    for i, colonne in enumerate(df.columns):
        serie = df.iloc[:, i]
        valeurs = serie.dropna()
        nb_valeurs = len(valeurs)
        if nb_valeurs < SEUIL_MIN_VALEURS_QUASI_CONSTANTE:
            continue

        comptes = valeurs.value_counts()
        valeur_dominante = comptes.index[0]
        nb_dominant = int(comptes.iloc[0])
        part_dominante = nb_dominant / nb_valeurs

        if part_dominante < SEUIL_QUASI_CONSTANTE:
            continue

        anomalies.append(_anomalie(
            nom_fichier, 0, [colonne], "low",
            f"{part_dominante:.1%} des valeurs valent '{valeur_dominante}' ({nb_dominant}/{nb_valeurs})",
            f"La colonne '{colonne}' ne varie presque jamais : probablement peu informative "
            "ou artefact d'extraction.",
            0.6,
        ))
    return anomalies


def verifier_cardinalite_suspecte(nom_fichier: str, df: pd.DataFrame) -> list:
    """
    Signale les colonnes texte où la quasi-totalité des valeurs non nulles
    sont uniques (>= SEUIL_CARDINALITE_SUSPECTE), avec au moins
    SEUIL_MIN_VALEURS_CARDINALITE valeurs. Exclut les colonnes dont le nom
    suggère un identifiant légitimement unique (URL, ID, référence...) :
    risque de faux positif élevé sinon (l'inventaire du projet montre de
    nombreuses colonnes URL/Page_URL quasi-uniques par nature).
    """
    anomalies = []
    for i, colonne in enumerate(df.columns):
        if NOMS_IDENTIFIANTS_RE.search(str(colonne)):
            continue

        serie = df.iloc[:, i]
        if pd.api.types.is_numeric_dtype(serie) or pd.api.types.is_datetime64_any_dtype(serie):
            continue

        valeurs = serie.dropna()
        nb_valeurs = len(valeurs)
        if nb_valeurs < SEUIL_MIN_VALEURS_CARDINALITE:
            continue

        nb_uniques = int(valeurs.nunique())
        ratio_unique = nb_uniques / nb_valeurs
        if ratio_unique < SEUIL_CARDINALITE_SUSPECTE:
            continue

        anomalies.append(_anomalie(
            nom_fichier, 0, [colonne], "low",
            f"{ratio_unique:.1%} de valeurs uniques ({nb_uniques}/{nb_valeurs})",
            f"La colonne '{colonne}' a une cardinalité proche du nombre de lignes : "
            "probablement peu adaptée à une analyse catégorielle.",
            0.5,
        ))
    return anomalies


def _regression_simple(x: pd.Series, y: pd.Series):
    """Régression linéaire simple y = pente*x + origine (covariance/variance). None si x constant."""
    variance_x = x.var()
    if not variance_x or pd.isna(variance_x):
        return None
    pente = x.cov(y) / variance_x
    origine = y.mean() - pente * x.mean()
    return pente, origine


def verifier_incoherence_entre_colonnes(nom_fichier: str, df: pd.DataFrame) -> list:
    """
    Signale les lignes qui rompent une relation linéaire habituellement
    observée entre deux colonnes numériques corrélées (ex. puissance et
    prix) — capture mathématiquement une incohérence métier (ex. "petite
    voiture, prix énorme") sans connaître le sens des colonnes : ni la
    puissance ni le prix ne sont individuellement aberrants, seule leur
    combinaison l'est.

    Pour chaque paire de colonnes numériques avec au moins
    SEUIL_MIN_PAIRES_VALEURS lignes communes non nulles et une corrélation
    >= SEUIL_CORRELATION_MIN en valeur absolue : régression linéaire simple,
    puis détection IQR sur les résidus. Au plus NB_PAIRES_MAX_TRAITEES paires
    sont traitées (les plus corrélées d'abord), pour bornir le coût sur les
    fichiers à beaucoup de colonnes numériques.

    La sélection des paires utilise une corrélation de RANG (Spearman),
    pas de Pearson : Pearson est très sensible à une seule valeur extrême,
    or c'est justement ce qu'on cherche à détecter — une ligne isolée qui
    casse la tendance suffirait à faire chuter artificiellement une
    corrélation de Pearson et à faire ignorer la paire à tort.

    Agrégation par ligne : une ligne qui casse plusieurs paires ne produit
    qu'une seule anomalie (colonnes = union des colonnes impliquées).
    """
    colonnes_numeriques = [
        colonne for i, colonne in enumerate(df.columns)
        if pd.api.types.is_numeric_dtype(df.iloc[:, i])
    ]
    if len(colonnes_numeriques) < 2:
        return []

    matrice_correlation = df[colonnes_numeriques].corr(method="spearman")

    paires_candidates = []
    for idx_a, col_a in enumerate(colonnes_numeriques):
        for col_b in colonnes_numeriques[idx_a + 1:]:
            correlation = matrice_correlation.loc[col_a, col_b]
            if pd.isna(correlation) or abs(correlation) < SEUIL_CORRELATION_MIN:
                continue
            paires_candidates.append((abs(correlation), col_a, col_b))

    if not paires_candidates:
        return []

    paires_candidates.sort(key=lambda p: p[0], reverse=True)
    if len(paires_candidates) > NB_PAIRES_MAX_TRAITEES:
        logger.warning(
            f"{nom_fichier} : {len(paires_candidates)} paires de colonnes corrélées trouvées, "
            f"seules les {NB_PAIRES_MAX_TRAITEES} plus corrélées sont analysées."
        )
        paires_candidates = paires_candidates[:NB_PAIRES_MAX_TRAITEES]

    lignes_en_defaut = {}  # index de ligne -> {"colonnes": set, "details": [str, ...]}

    for correlation, col_a, col_b in paires_candidates:
        communes = df[[col_a, col_b]].dropna()
        if len(communes) < SEUIL_MIN_PAIRES_VALEURS:
            continue

        regression = _regression_simple(communes[col_a], communes[col_b])
        if regression is None:
            continue
        pente, origine = regression

        predictions = pente * communes[col_a] + origine
        residus = communes[col_b] - predictions
        # FACTEUR_IQR_RESIDUS (3.5) plutôt que FACTEUR_IQR (1.5) : une régression
        # globale sur tout le fichier peut être décalée par un sous-groupe cohérent
        # (ex. véhicules électriques vs thermiques, relations prix/puissance
        # différentes) sans qu'il s'agisse d'une anomalie. Un facteur plus strict
        # laisse passer ce décalage systématique modéré et ne retient que les
        # lignes vraiment isolées, très loin de la tendance (validé empiriquement
        # sur données réelles : ce sous-groupe disparaît des résultats à 3.5x IQR).
        borne_basse, borne_haute = _bornes_iqr(residus, facteur=FACTEUR_IQR_RESIDUS)
        if borne_haute <= borne_basse:
            continue

        masque = (residus < borne_basse) | (residus > borne_haute)
        if not masque.any():
            continue

        for idx in residus.index[masque]:
            entree = lignes_en_defaut.setdefault(idx, {"colonnes": set(), "details": []})
            entree["colonnes"].update([col_a, col_b])
            entree["details"].append(
                f"{col_a}={communes.loc[idx, col_a]} et {col_b}={communes.loc[idx, col_b]} "
                f"(corrélation habituelle={correlation:.2f})"
            )

    anomalies = []
    for idx in sorted(lignes_en_defaut):
        entree = lignes_en_defaut[idx]
        details = "; ".join(entree["details"])
        anomalies.append(_anomalie(
            nom_fichier, int(idx), sorted(entree["colonnes"], key=str), "medium",
            f"{details} : très éloigné de la relation observée sur le reste du fichier",
            "Cette ligne casse une relation habituellement observée entre plusieurs colonnes "
            "numériques du fichier : la combinaison de valeurs est statistiquement incohérente "
            "ici, même si aucune des valeurs n'est individuellement aberrante.",
            0.75,
        ))
    return anomalies


# ---------------------------------------------------------------------------
# Fonctions principales
# ---------------------------------------------------------------------------

VERIFICATIONS = (
    verifier_outliers_numeriques,
    verifier_valeurs_negatives_minoritaires,
    verifier_colonnes_quasi_constantes,
    verifier_cardinalite_suspecte,
    verifier_incoherence_entre_colonnes,
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
    Enchaîne les 5 vérifications statistiques et retourne la liste agrégée
    des anomalies détectées, chacune conforme à docs/contrat_anomalie.md.
    Ne lève jamais d'exception. Un DataFrame vide ne produit aucune anomalie
    (rien à calculer ; le cas fichier vide est du ressort de structure/).
    """
    if df is None or df.empty:
        return []

    anomalies = []
    for verification in VERIFICATIONS:
        anomalies.extend(_executer_verification(verification, nom_fichier, df))
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
        print("Aucune anomalie statistique détectée.")
        return

    lignes = [
        [a["severite"], ", ".join(a["colonnes"]) or "-", a["valeur"], a["justification"]]
        for a in anomalies
    ]
    print(tabulate(lignes, headers=["Sévérité", "Colonne(s)", "Valeur", "Justification"], tablefmt="grid"))


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage : python -m stats.agent_stats <chemin_ou_nom_de_fichier>")
        sys.exit(1)

    chemin = _resoudre_chemin(sys.argv[1])
    df = LocalLoader().charger(chemin)
    if df is None:
        logger.error(f"Échec du chargement de {chemin}")
        sys.exit(1)

    nom_fichier = chemin.name
    anomalies = analyser(nom_fichier, df)

    print("\n" + "=" * 60)
    print(f"ANOMALIES STATISTIQUES — {nom_fichier}")
    print("=" * 60)
    _afficher_anomalies(anomalies)

    config.RESULTATS_DIR.mkdir(parents=True, exist_ok=True)
    fichier_sortie = config.RESULTATS_DIR / f"stats_{chemin.stem}.json"
    fichier_sortie.write_text(json.dumps(anomalies, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(f"Anomalies sauvegardées dans {fichier_sortie}")


if __name__ == "__main__":
    main()
