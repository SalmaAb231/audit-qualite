"""
Tests unitaires du module stats/agent_stats.py.

Un test par vérification (verifier_*) avec un DataFrame "sale" qui déclenche
l'anomalie attendue et un DataFrame "sain" qui n'en déclenche aucune, plus des
tests d'agrégation (analyser) et de conformité au contrat commun
(docs/contrat_anomalie.md).

Usage :
    python -m pytest tests/test_stats.py -v
"""

import pandas as pd

from stats.agent_stats import (
    analyser,
    verifier_cardinalite_suspecte,
    verifier_colonnes_quasi_constantes,
    verifier_incoherence_entre_colonnes,
    verifier_outliers_numeriques,
    verifier_valeurs_negatives_minoritaires,
)

NOM_FICHIER_TEST = "test.xlsx"

CHAMPS_CONTRAT = {
    "fichier", "ligne", "colonnes", "type", "severite",
    "source", "valeur", "justification", "confiance",
}


# ---------------------------------------------------------------------------
# verifier_outliers_numeriques
# ---------------------------------------------------------------------------


def test_verifier_outliers_numeriques_detecte():
    valeurs_normales = [10, 11, 12, 13, 9, 10, 11, 12, 13, 10, 11, 12, 13, 9]
    df = pd.DataFrame({"Prix": valeurs_normales + [500]})
    anomalies = verifier_outliers_numeriques(NOM_FICHIER_TEST, df)
    assert len(anomalies) == 1
    assert anomalies[0]["severite"] == "medium"
    assert anomalies[0]["colonnes"] == ["Prix"]
    assert "500" in anomalies[0]["valeur"]


def test_verifier_outliers_numeriques_df_sain():
    df_sain = pd.DataFrame({"Prix": [10, 11, 12, 13, 9, 10, 11, 12, 13, 10, 11, 12]})
    assert verifier_outliers_numeriques(NOM_FICHIER_TEST, df_sain) == []


def test_verifier_outliers_numeriques_ignore_sous_le_minimum():
    # Moins de SEUIL_MIN_VALEURS_OUTLIERS valeurs : check ignoré, même avec une valeur extrême.
    df = pd.DataFrame({"Prix": [10, 11, 12, 500]})
    assert verifier_outliers_numeriques(NOM_FICHIER_TEST, df) == []


# ---------------------------------------------------------------------------
# verifier_valeurs_negatives_minoritaires
# ---------------------------------------------------------------------------


def test_verifier_valeurs_negatives_minoritaires_detecte():
    df = pd.DataFrame({"Marge": [10, 20, 30, 40, 50, 60, 70, 80, 90, 100, -5]})
    anomalies = verifier_valeurs_negatives_minoritaires(NOM_FICHIER_TEST, df)
    assert len(anomalies) == 1
    assert anomalies[0]["severite"] == "medium"
    assert anomalies[0]["colonnes"] == ["Marge"]


def test_verifier_valeurs_negatives_minoritaires_colonne_a_dominante_negative_non_signalee():
    df = pd.DataFrame({"Ecart": [-10, -20, -30, -40, -50, -60, -70, -80, -90, -100, 5]})
    assert verifier_valeurs_negatives_minoritaires(NOM_FICHIER_TEST, df) == []


def test_verifier_valeurs_negatives_minoritaires_df_sain():
    df_sain = pd.DataFrame({"Marge": [10, 20, 30, 40, 50]})
    assert verifier_valeurs_negatives_minoritaires(NOM_FICHIER_TEST, df_sain) == []


# ---------------------------------------------------------------------------
# verifier_colonnes_quasi_constantes
# ---------------------------------------------------------------------------


def test_verifier_colonnes_quasi_constantes_detecte():
    df = pd.DataFrame({"Pays": ["NL"] * 99 + ["FR"]})
    anomalies = verifier_colonnes_quasi_constantes(NOM_FICHIER_TEST, df)
    assert len(anomalies) == 1
    assert anomalies[0]["severite"] == "low"
    assert anomalies[0]["colonnes"] == ["Pays"]


def test_verifier_colonnes_quasi_constantes_df_sain():
    df_sain = pd.DataFrame({"Pays": ["NL", "FR", "BE", "DE", "NL", "FR", "BE", "DE"] * 5})
    assert verifier_colonnes_quasi_constantes(NOM_FICHIER_TEST, df_sain) == []


# ---------------------------------------------------------------------------
# verifier_cardinalite_suspecte
# ---------------------------------------------------------------------------


def test_verifier_cardinalite_suspecte_detecte():
    df = pd.DataFrame({"Description": [f"desc_{i}" for i in range(60)]})
    anomalies = verifier_cardinalite_suspecte(NOM_FICHIER_TEST, df)
    assert len(anomalies) == 1
    assert anomalies[0]["severite"] == "low"
    assert anomalies[0]["colonnes"] == ["Description"]


def test_verifier_cardinalite_suspecte_exclut_les_identifiants():
    df = pd.DataFrame({"URL": [f"http://exemple.test/{i}" for i in range(60)]})
    assert verifier_cardinalite_suspecte(NOM_FICHIER_TEST, df) == []


def test_verifier_cardinalite_suspecte_df_sain():
    df_sain = pd.DataFrame({"Categorie": (["A", "B", "C"] * 20)})
    assert verifier_cardinalite_suspecte(NOM_FICHIER_TEST, df_sain) == []


# ---------------------------------------------------------------------------
# verifier_incoherence_entre_colonnes
# ---------------------------------------------------------------------------


def _construire_df_correles(rupture: bool):
    puissance = [10 * i for i in range(1, 41)]
    prix = [p * 100 for p in puissance]
    if rupture:
        prix[5] = 900000  # Power=60 mais Price 150x plus élevé qu'attendu par la tendance du fichier
    return pd.DataFrame({"Power": puissance, "Price": prix})


def test_verifier_incoherence_entre_colonnes_detecte():
    anomalies = verifier_incoherence_entre_colonnes(NOM_FICHIER_TEST, _construire_df_correles(rupture=True))
    assert len(anomalies) == 1
    assert anomalies[0]["severite"] == "medium"
    assert anomalies[0]["ligne"] == 5
    assert set(anomalies[0]["colonnes"]) == {"Power", "Price"}


def test_verifier_incoherence_entre_colonnes_df_sain():
    assert verifier_incoherence_entre_colonnes(NOM_FICHIER_TEST, _construire_df_correles(rupture=False)) == []


def test_verifier_incoherence_entre_colonnes_sans_correlation():
    puissance = list(range(1, 41))
    prix = ([1, 100] * 20)  # alterne sans lien avec puissance : corrélation quasi nulle
    df = pd.DataFrame({"Power": puissance, "Price": prix})
    assert verifier_incoherence_entre_colonnes(NOM_FICHIER_TEST, df) == []


# ---------------------------------------------------------------------------
# analyser (agrégation)
# ---------------------------------------------------------------------------


def _construire_df_multi_defauts():
    n = 100
    power = [10 * i for i in range(1, n + 1)]
    price = [p * 100 for p in power]
    price[5] = 900000  # incohérence Power/Price, ligne 5

    return pd.DataFrame({
        "Power": power,
        "Price": price,
        "Pays": ["NL"] * (n - 1) + ["FR"],           # quasi-constante
        "Description": [f"desc_{i}" for i in range(n)],  # cardinalité suspecte
        "Remise": [10] * (n - 1) + [-5],              # négative minoritaire
    })


def test_analyser_agrege_plusieurs_anomalies():
    anomalies = analyser("multi.xlsx", _construire_df_multi_defauts())
    colonnes_signalees = [tuple(a["colonnes"]) for a in anomalies]

    assert any("Pays" in cols for cols in colonnes_signalees)
    assert any("Description" in cols for cols in colonnes_signalees)
    assert any("Remise" in cols for cols in colonnes_signalees)
    assert any(a["ligne"] == 5 and {"Power", "Price"} <= set(a["colonnes"]) for a in anomalies)
    assert len(anomalies) >= 4


def test_analyser_df_vide():
    assert analyser("vide.xlsx", pd.DataFrame()) == []


# ---------------------------------------------------------------------------
# Conformité au contrat commun (docs/contrat_anomalie.md)
# ---------------------------------------------------------------------------


def test_toutes_les_anomalies_respectent_le_contrat():
    anomalies = analyser("multi.xlsx", _construire_df_multi_defauts())
    assert anomalies

    for anomalie in anomalies:
        assert set(anomalie.keys()) == CHAMPS_CONTRAT
        assert anomalie["type"] == "statistique"
        assert anomalie["source"] == "stats"
        assert isinstance(anomalie["ligne"], int)
        assert isinstance(anomalie["colonnes"], list)
        assert anomalie["severite"] in {"critical", "high", "medium", "low"}
        assert isinstance(anomalie["confiance"], float)
        assert 0.0 <= anomalie["confiance"] <= 1.0
