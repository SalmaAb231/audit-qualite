"""
Tests unitaires du module judge/agent_judge.py.

100% déterministe (aucun appel LLM, aucun mock nécessaire) : les tests
portent directement sur le calcul de score/grade/verdict et sur
l'agrégation, avec les poids réels de config.py.

Usage :
    python -m pytest tests/test_agent_judge.py -v
"""

import pytest

import config
from judge.agent_judge import (
    _calculer_score,
    _generer_justification,
    _grade_from_score,
    _verdict_from_score,
    analyser,
)

NOM_FICHIER_TEST = "test.xlsx"

CHAMPS_VERDICT = {
    "fichier", "nb_anomalies_total", "score", "verdict", "grade",
    "justification", "anomalies_par_type", "anomalies_par_severite",
}


def _anomalie(severite="medium", type_="statistique", ligne=0, colonnes=None, confiance=0.8):
    return {
        "fichier": NOM_FICHIER_TEST,
        "ligne": ligne,
        "colonnes": colonnes or ["Colonne"],
        "type": type_,
        "severite": severite,
        "source": "stats",
        "valeur": "peu importe",
        "justification": f"justification {severite}",
        "confiance": confiance,
    }


# ---------------------------------------------------------------------------
# Les 3 cas demandés
# ---------------------------------------------------------------------------


def test_analyser_fichier_propre():
    verdict = analyser(NOM_FICHIER_TEST, [])
    assert verdict["score"] == 1.0
    assert verdict["verdict"] == "ACCEPTED"
    assert verdict["grade"] == "A"
    assert verdict["nb_anomalies_total"] == 0


def test_analyser_fichier_avec_quelques_anomalies():
    # Avec les poids actuels (low=0.2), une seule anomalie 'low' donne
    # score=0.8 : WARNING (0.75 <= 0.8 < 0.90), mais grade C (0.70-0.84),
    # PAS B (0.85-0.94) — B est arithmétiquement inatteignable dès qu'il y a
    # au moins une anomalie (voir judge/regles_judge.md, limite connue #1).
    verdict = analyser(NOM_FICHIER_TEST, [_anomalie(severite="low")])
    assert verdict["score"] == pytest.approx(0.8)
    assert verdict["verdict"] == "WARNING"
    assert verdict["grade"] == "C"


def test_analyser_fichier_tres_problematique():
    # high (0.7) + low (0.2) = pénalité 0.9 -> score exactement 0.1
    anomalies = [_anomalie(severite="high"), _anomalie(severite="low", ligne=1)]
    verdict = analyser(NOM_FICHIER_TEST, anomalies)
    assert verdict["score"] == pytest.approx(0.1)
    assert verdict["verdict"] == "REJECTED"
    assert verdict["grade"] == "F"


# ---------------------------------------------------------------------------
# _calculer_score
# ---------------------------------------------------------------------------


def test_calculer_score_liste_vide():
    assert _calculer_score([]) == 1.0


def test_calculer_score_exemple_de_lenonce():
    # Exemple fourni : critical (1.0x2=2.0) + high (0.7) + medium (0.4) = 3.1 -> score clampé à 0.0
    anomalies = [_anomalie(severite="critical"), _anomalie(severite="high"), _anomalie(severite="medium")]
    assert _calculer_score(anomalies) == 0.0


def test_calculer_score_ne_descend_jamais_sous_zero():
    anomalies = [_anomalie(severite="critical") for _ in range(5)]
    assert _calculer_score(anomalies) == 0.0


def test_calculer_score_doublement_critical_sature_deja_sans_lui():
    # PONDERATION_SEVERITE["critical"] vaut déjà 1.0 : une seule anomalie
    # critical sature déjà le score à 0.0 même sans le doublement (limite
    # connue #3, judge/regles_judge.md) — ce test documente ce résultat.
    assert _calculer_score([_anomalie(severite="critical")]) == 0.0


def test_calculer_score_severite_invalide_repli_medium():
    # medium (repli) est plafonné comme les autres sévérités non bloquantes :
    # 0.4 > PLAFOND_PENALITE_NON_BLOQUANTE (0.25), donc la pénalité effective
    # est le plafond, pas le poids brut.
    assert _calculer_score([_anomalie(severite="inconnue")]) == pytest.approx(1.0 - config.PLAFOND_PENALITE_NON_BLOQUANTE)


def test_calculer_score_plafonne_les_severites_non_bloquantes():
    # 20 anomalies medium (poids brut 0.4 chacune, total 8.0) : sans plafond,
    # score écrasé à 0 ; avec, borné à PLAFOND_PENALITE_NON_BLOQUANTE (0.25).
    anomalies = [_anomalie(severite="medium", ligne=i) for i in range(20)]
    assert _calculer_score(anomalies) == pytest.approx(1.0 - config.PLAFOND_PENALITE_NON_BLOQUANTE)


def test_calculer_score_severites_graves_non_plafonnees():
    # 3 anomalies 'high' (0.7 chacune, total 2.1) : bien au-delà du plafond
    # non-bloquant, mais 'high' n'est jamais plafonné -> score toujours à 0.
    anomalies = [_anomalie(severite="high", ligne=i) for i in range(3)]
    assert _calculer_score(anomalies) == 0.0


def test_analyser_beaucoup_anomalies_non_bloquantes_reste_warning_pas_rejete():
    # Réplique la découverte de calibration (stats/regles.md) : un détecteur
    # purement statistique qui produit un grand nombre d'anomalies medium/low
    # ne doit jamais, à lui seul, faire descendre le verdict sous WARNING.
    anomalies = [_anomalie(severite="medium", type_="statistique", ligne=i) for i in range(100)]
    verdict = analyser(NOM_FICHIER_TEST, anomalies)
    assert verdict["verdict"] == "WARNING"
    assert verdict["score"] == pytest.approx(1.0 - config.PLAFOND_PENALITE_NON_BLOQUANTE)


# ---------------------------------------------------------------------------
# _grade_from_score (bornes de config.SEUILS_GRADE)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("score,grade_attendu", [
    (1.0, "A"), (0.95, "A"), (0.9499, "B"),
    (0.85, "B"), (0.8499, "C"),
    (0.70, "C"), (0.6999, "D"),
    (0.50, "D"), (0.4999, "F"),
    (0.0, "F"),
])
def test_grade_from_score(score, grade_attendu):
    assert _grade_from_score(score) == grade_attendu


# ---------------------------------------------------------------------------
# _verdict_from_score (bornes de config.SEUIL_ACCEPTED / SEUIL_WARNING)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("score,verdict_attendu", [
    (1.0, "ACCEPTED"), (0.90, "ACCEPTED"), (0.8999, "WARNING"),
    (0.75, "WARNING"), (0.7499, "REJECTED"),
    (0.0, "REJECTED"),
])
def test_verdict_from_score(score, verdict_attendu):
    assert _verdict_from_score(score) == verdict_attendu


# ---------------------------------------------------------------------------
# Agrégation par type / sévérité
# ---------------------------------------------------------------------------


def test_analyser_compte_par_type_et_severite_toutes_les_cles_incluses():
    anomalies = [
        _anomalie(severite="high", type_="structure"),
        _anomalie(severite="high", type_="structure"),
        _anomalie(severite="low", type_="realisme"),
    ]
    verdict = analyser(NOM_FICHIER_TEST, anomalies)

    assert verdict["anomalies_par_type"] == {"structure": 2, "statistique": 0, "coherence": 0, "realisme": 1}
    assert verdict["anomalies_par_severite"] == {"critical": 0, "high": 2, "medium": 0, "low": 1}


def test_analyser_anomalies_none_traite_comme_liste_vide():
    verdict = analyser(NOM_FICHIER_TEST, None)
    assert verdict["score"] == 1.0
    assert verdict["nb_anomalies_total"] == 0


# ---------------------------------------------------------------------------
# _generer_justification
# ---------------------------------------------------------------------------


def test_generer_justification_aucune_anomalie():
    justification = _generer_justification("ACCEPTED", [])
    assert "aucune anomalie" in justification.lower()


def test_generer_justification_cite_lanomalie_la_plus_grave():
    anomalies = [_anomalie(severite="low", ligne=1), _anomalie(severite="critical", ligne=7, colonnes=["Prix"])]
    justification = _generer_justification("REJECTED", anomalies)
    assert "ligne 7" in justification
    assert "Prix" in justification


# ---------------------------------------------------------------------------
# Conformité de la sortie de analyser()
# ---------------------------------------------------------------------------


def test_analyser_retourne_exactement_les_champs_attendus():
    verdict = analyser(NOM_FICHIER_TEST, [_anomalie()])
    assert set(verdict.keys()) == CHAMPS_VERDICT
    assert verdict["fichier"] == NOM_FICHIER_TEST
    assert isinstance(verdict["score"], float)
    assert 0.0 <= verdict["score"] <= 1.0
    assert verdict["verdict"] in {"ACCEPTED", "WARNING", "REJECTED"}
    assert verdict["grade"] in {"A", "B", "C", "D", "F"}
