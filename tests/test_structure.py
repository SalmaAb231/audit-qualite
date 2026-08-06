"""
Tests unitaires du module structure/agent_structure.py.

Un test par vérification (verifier_*) avec un DataFrame "sale" qui déclenche
l'anomalie attendue et un DataFrame "sain" qui n'en déclenche aucune, plus des
tests d'agrégation (analyser), de décision (fichier_exploitable) et de
conformité au contrat commun (docs/contrat_anomalie.md).

Usage :
    python -m pytest tests/test_structure.py -v
"""

import pandas as pd

from structure.agent_structure import (
    analyser,
    fichier_exploitable,
    verifier_colonnes_dupliquees,
    verifier_colonnes_sans_nom,
    verifier_colonnes_vides,
    verifier_fichier_vide,
    verifier_lignes_dupliquees,
    verifier_taux_valeurs_manquantes,
    verifier_types_incoherents,
)

NOM_FICHIER_TEST = "test.xlsx"

CHAMPS_CONTRAT = {
    "fichier", "ligne", "colonnes", "type", "severite",
    "source", "valeur", "justification", "confiance",
}


# ---------------------------------------------------------------------------
# verifier_fichier_vide
# ---------------------------------------------------------------------------


def test_verifier_fichier_vide_detecte_0_ligne():
    anomalies = verifier_fichier_vide(NOM_FICHIER_TEST, pd.DataFrame(columns=["A", "B"]))
    assert len(anomalies) == 1
    assert anomalies[0]["severite"] == "critical"


def test_verifier_fichier_vide_detecte_0_colonne():
    anomalies = verifier_fichier_vide(NOM_FICHIER_TEST, pd.DataFrame(index=[0, 1, 2]))
    assert len(anomalies) == 1
    assert anomalies[0]["severite"] == "critical"


def test_verifier_fichier_vide_df_sain():
    df_sain = pd.DataFrame({"A": [1, 2]})
    assert verifier_fichier_vide(NOM_FICHIER_TEST, df_sain) == []


# ---------------------------------------------------------------------------
# verifier_colonnes_sans_nom
# ---------------------------------------------------------------------------


def test_verifier_colonnes_sans_nom_avec_donnees():
    df = pd.DataFrame({"Nom": ["a", "b", "c"], "Unnamed: 3": [1, 2, 3]})
    anomalies = verifier_colonnes_sans_nom(NOM_FICHIER_TEST, df)
    assert len(anomalies) == 1
    assert anomalies[0]["severite"] == "high"
    assert anomalies[0]["colonnes"] == ["Unnamed: 3"]


def test_verifier_colonnes_sans_nom_vide():
    df = pd.DataFrame({"Nom": ["a", "b", "c"], "Unnamed: 4": [None, None, None]})
    anomalies = verifier_colonnes_sans_nom(NOM_FICHIER_TEST, df)
    assert len(anomalies) == 1
    assert anomalies[0]["severite"] == "low"


def test_verifier_colonnes_sans_nom_df_sain():
    df_sain = pd.DataFrame({"Nom": ["a", "b"], "Prix": [10, 20]})
    assert verifier_colonnes_sans_nom(NOM_FICHIER_TEST, df_sain) == []


# ---------------------------------------------------------------------------
# verifier_colonnes_dupliquees
# ---------------------------------------------------------------------------


def test_verifier_colonnes_dupliquees_detecte():
    df = pd.DataFrame([[1, "x", "y"], [2, "x2", "y2"]], columns=["A", "A", "B"])
    anomalies = verifier_colonnes_dupliquees(NOM_FICHIER_TEST, df)
    assert len(anomalies) == 1
    assert anomalies[0]["severite"] == "high"
    assert anomalies[0]["colonnes"] == ["A"]


def test_verifier_colonnes_dupliquees_df_sain():
    df_sain = pd.DataFrame({"A": [1], "B": [2]})
    assert verifier_colonnes_dupliquees(NOM_FICHIER_TEST, df_sain) == []


# ---------------------------------------------------------------------------
# verifier_colonnes_vides
# ---------------------------------------------------------------------------


def test_verifier_colonnes_vides_detecte():
    df = pd.DataFrame({"Nom": ["a", "b", "c"], "Vide": [None, None, None]})
    anomalies = verifier_colonnes_vides(NOM_FICHIER_TEST, df)
    assert len(anomalies) == 1
    assert anomalies[0]["severite"] == "medium"
    assert anomalies[0]["colonnes"] == ["Vide"]


def test_verifier_colonnes_vides_exclut_colonnes_sans_nom():
    # Une colonne vide ET sans nom est couverte par verifier_colonnes_sans_nom,
    # pas ici : pas de double signalement de la même colonne.
    df = pd.DataFrame({"Nom": ["a", "b"], "Unnamed: 2": [None, None]})
    assert verifier_colonnes_vides(NOM_FICHIER_TEST, df) == []


def test_verifier_colonnes_vides_df_sain():
    df_sain = pd.DataFrame({"Nom": ["a", None, "c"]})
    assert verifier_colonnes_vides(NOM_FICHIER_TEST, df_sain) == []


# ---------------------------------------------------------------------------
# verifier_lignes_dupliquees
# ---------------------------------------------------------------------------


def test_verifier_lignes_dupliquees_detecte():
    df = pd.DataFrame({"Nom": ["a", "b", "a"], "Prix": [10, 20, 10]})
    anomalies = verifier_lignes_dupliquees(NOM_FICHIER_TEST, df)
    assert len(anomalies) == 1
    assert anomalies[0]["severite"] == "medium"
    assert anomalies[0]["ligne"] == 2
    assert "1 ligne" in anomalies[0]["valeur"]


def test_verifier_lignes_dupliquees_df_sain():
    df_sain = pd.DataFrame({"Nom": ["a", "b", "c"], "Prix": [10, 20, 30]})
    assert verifier_lignes_dupliquees(NOM_FICHIER_TEST, df_sain) == []


# ---------------------------------------------------------------------------
# verifier_taux_valeurs_manquantes
# ---------------------------------------------------------------------------


def _construire_df_manquantes():
    return pd.DataFrame({
        "A": [1, 2, 3, 4] + [None] * 6,   # 6/10 = 60% -> medium
        "B": [1] + [None] * 9,             # 9/10 = 90% -> high
        "C": [1, 2, 3, 4, 5, 6, 7] + [None] * 3,  # 3/10 = 30% -> rien
        "D": [None] * 10,                  # 100% -> exclu (couvert par verifier_colonnes_vides)
    })


def test_verifier_taux_valeurs_manquantes():
    anomalies = verifier_taux_valeurs_manquantes(NOM_FICHIER_TEST, _construire_df_manquantes())
    par_colonne = {a["colonnes"][0]: a["severite"] for a in anomalies}
    assert par_colonne["A"] == "medium"
    assert par_colonne["B"] == "high"
    assert "C" not in par_colonne
    assert "D" not in par_colonne


def test_verifier_taux_valeurs_manquantes_df_sain():
    df_sain = pd.DataFrame({"A": [1, 2, 3, 4, 5]})
    assert verifier_taux_valeurs_manquantes(NOM_FICHIER_TEST, df_sain) == []


# ---------------------------------------------------------------------------
# verifier_types_incoherents
# ---------------------------------------------------------------------------


def test_verifier_types_incoherents_detecte_melange():
    df = pd.DataFrame({
        "Prix": ["100", "200", "300", "400", "500", "600", "700", "N/A", "sur devis", "inconnu"],
    })
    anomalies = verifier_types_incoherents(NOM_FICHIER_TEST, df)
    assert len(anomalies) == 1
    assert anomalies[0]["severite"] == "medium"
    assert anomalies[0]["colonnes"] == ["Prix"]


def test_verifier_types_incoherents_format_europeen_non_signale():
    # Nombres au format européen (virgule décimale, espace = milliers) : convertibles
    # à 100%, pas une anomalie.
    df = pd.DataFrame({"Prix": ["1 234,56", "2 345,10", "100,00", "50,00", "999,99"]})
    assert verifier_types_incoherents(NOM_FICHIER_TEST, df) == []


def test_verifier_types_incoherents_format_europeen_point_milliers_non_signale():
    # Nombres au format européen avec point = milliers, virgule = décimale
    # (ex. loyers néerlandais réels) : ne doit pas être confondu avec une date.
    df = pd.DataFrame({"LeaseRate": ["2.364,40", "796,61", "1.422,88", "2.480,56", "861,76"]})
    assert verifier_types_incoherents(NOM_FICHIER_TEST, df) == []


def test_verifier_types_incoherents_texte_pur_non_signale():
    df = pd.DataFrame({"Couleur": ["rouge", "bleu", "vert", "jaune", "noir"]})
    assert verifier_types_incoherents(NOM_FICHIER_TEST, df) == []


# ---------------------------------------------------------------------------
# analyser (agrégation) et fichier_exploitable
# ---------------------------------------------------------------------------


def _construire_df_multi_defauts():
    return pd.DataFrame({
        "Nom": ["a", "b", "a"],
        "Prix": ["100", "200", "100"],
        "Vide": [None, None, None],
        "Unnamed: 5": [None, None, None],
    })


def test_analyser_agrege_plusieurs_anomalies():
    anomalies = analyser("multi.xlsx", _construire_df_multi_defauts())
    types_severite = {(a["type"], a["severite"]) for a in anomalies}
    assert ("structure", "low") in types_severite       # Unnamed: 5, vide
    assert ("structure", "medium") in types_severite     # Vide + ligne dupliquée
    assert len(anomalies) >= 3


def test_analyser_court_circuite_sur_fichier_vide():
    anomalies = analyser("vide.xlsx", pd.DataFrame())
    assert len(anomalies) == 1
    assert anomalies[0]["severite"] == "critical"


def test_fichier_exploitable():
    assert fichier_exploitable([]) is True
    assert fichier_exploitable([{"severite": "medium"}, {"severite": "low"}]) is True
    assert fichier_exploitable([{"severite": "medium"}, {"severite": "critical"}]) is False


# ---------------------------------------------------------------------------
# Conformité au contrat commun (docs/contrat_anomalie.md)
# ---------------------------------------------------------------------------


def test_toutes_les_anomalies_respectent_le_contrat():
    anomalies = analyser("multi.xlsx", _construire_df_multi_defauts())
    assert anomalies

    for anomalie in anomalies:
        assert set(anomalie.keys()) == CHAMPS_CONTRAT
        assert anomalie["type"] == "structure"
        assert anomalie["source"] == "stats"
        assert isinstance(anomalie["ligne"], int)
        assert isinstance(anomalie["colonnes"], list)
        assert anomalie["severite"] in {"critical", "high", "medium", "low"}
        assert isinstance(anomalie["confiance"], float)
        assert 0.0 <= anomalie["confiance"] <= 1.0
