"""
Tests unitaires du module reporting/agent_reporting.py.

100% déterministe (pure mise en forme HTML, aucun appel LLM) : les tests
vérifient le contenu texte généré plutôt que d'analyser le HTML avec un
parseur, pour rester simples et rapides.

Usage :
    python -m pytest tests/test_agent_reporting.py -v
"""

from reporting.agent_reporting import generer_rapport_consolide, generer_rapport_fichier

NOM_FICHIER_TEST = "test.xlsx"


def _anomalie(severite="medium", type_="statistique", ligne=0, colonnes=None, justification="justification", confiance=0.8):
    return {
        "fichier": NOM_FICHIER_TEST,
        "ligne": ligne,
        "colonnes": colonnes or ["Colonne"],
        "type": type_,
        "severite": severite,
        "source": "stats",
        "valeur": "peu importe",
        "justification": justification,
        "confiance": confiance,
    }


def _verdict(fichier=NOM_FICHIER_TEST, score=1.0, verdict="ACCEPTED", grade="A", nb_anomalies_total=0, **extra):
    return {
        "fichier": fichier,
        "nb_anomalies_total": nb_anomalies_total,
        "score": score,
        "verdict": verdict,
        "grade": grade,
        "justification": "justification du verdict",
        "anomalies_par_type": {"structure": 0, "statistique": 0, "coherence": 0, "realisme": 0},
        "anomalies_par_severite": {"critical": 0, "high": 0, "medium": 0, "low": 0},
        **extra,
    }


# ---------------------------------------------------------------------------
# generer_rapport_fichier
# ---------------------------------------------------------------------------


def test_generer_rapport_fichier_contient_les_infos_cles():
    verdict = _verdict(score=0.42, verdict="REJECTED", grade="F", nb_anomalies_total=1)
    rapport = generer_rapport_fichier(verdict, [_anomalie(severite="critical")])

    assert NOM_FICHIER_TEST in rapport
    assert "REJECTED" in rapport or "Rejeté" in rapport
    assert "0.42" in rapport
    assert "critical" in rapport


def test_generer_rapport_fichier_aucune_anomalie():
    rapport = generer_rapport_fichier(_verdict(), [])
    assert "Aucune anomalie détectée" in rapport


def test_generer_rapport_fichier_trie_par_severite_critical_en_premier():
    anomalies = [_anomalie(severite="low", justification="issue mineure"), _anomalie(severite="critical", justification="issue bloquante")]
    rapport = generer_rapport_fichier(_verdict(), anomalies)

    assert rapport.index("issue bloquante") < rapport.index("issue mineure")


def test_generer_rapport_fichier_echappe_le_html():
    anomalie = _anomalie(justification="<script>alert('x')</script>")
    rapport = generer_rapport_fichier(_verdict(), [anomalie])

    assert "<script>" not in rapport
    assert "&lt;script&gt;" in rapport


def test_generer_rapport_fichier_est_un_document_html_valide_minimal():
    rapport = generer_rapport_fichier(_verdict(), [])
    assert rapport.startswith("<!doctype html>")
    assert "</html>" in rapport


# ---------------------------------------------------------------------------
# generer_rapport_consolide
# ---------------------------------------------------------------------------


def test_generer_rapport_consolide_liste_vide():
    rapport = generer_rapport_consolide([])
    assert "0 fichier" in rapport


def test_generer_rapport_consolide_repartition_des_verdicts():
    fichiers = [
        _verdict(fichier="a.xlsx", verdict="ACCEPTED"),
        _verdict(fichier="b.xlsx", verdict="WARNING"),
        _verdict(fichier="c.xlsx", verdict="REJECTED"),
        _verdict(fichier="d.xlsx", verdict="REJECTED"),
    ]
    rapport = generer_rapport_consolide(fichiers)

    assert "4 fichier" in rapport
    # 2 REJECTED, 1 WARNING, 1 ACCEPTED : les 4 noms de fichiers doivent apparaître dans le tableau détaillé
    for nom in ("a.xlsx", "b.xlsx", "c.xlsx", "d.xlsx"):
        assert nom in rapport


def test_generer_rapport_consolide_somme_anomalies_par_type_et_severite():
    fichiers = [
        _verdict(
            fichier="a.xlsx",
            anomalies_par_type={"structure": 2, "statistique": 0, "coherence": 0, "realisme": 1},
            anomalies_par_severite={"critical": 1, "high": 0, "medium": 2, "low": 0},
        ),
        _verdict(
            fichier="b.xlsx",
            anomalies_par_type={"structure": 0, "statistique": 3, "coherence": 0, "realisme": 0},
            anomalies_par_severite={"critical": 0, "high": 1, "medium": 0, "low": 0},
        ),
    ]
    rapport = generer_rapport_consolide(fichiers)

    # structure=2, statistique=3, realisme=1, critical=1, high=1, medium=2
    for valeur_attendue in ("2", "3", "1"):
        assert valeur_attendue in rapport  # présence grossière, la structure exacte est vérifiée ci-dessous
    assert rapport.count(">2<") >= 1
    assert rapport.count(">3<") >= 1


def test_generer_rapport_consolide_top_anomalies_frequentes_avec_detail():
    anomalie_recurrente = _anomalie(type_="realisme", colonnes=["Model"], justification="Volvo EX40 n'existe pas")
    fichiers = [
        _verdict(fichier="a.xlsx", anomalies=[anomalie_recurrente, anomalie_recurrente]),
        _verdict(fichier="b.xlsx", anomalies=[anomalie_recurrente]),
    ]
    rapport = generer_rapport_consolide(fichiers)

    assert "Volvo EX40" in rapport  # apostrophe échappée par html.escape (&#x27;), normal et attendu
    assert ">3<" in rapport  # 3 occurrences au total


def test_generer_rapport_consolide_sans_detail_anomalies_section_explicite():
    fichiers = [_verdict(fichier="a.xlsx")]  # pas de clé "anomalies"
    rapport = generer_rapport_consolide(fichiers)
    assert "non fourni" in rapport


def test_generer_rapport_consolide_evolution_par_mois_avec_donnees():
    fichiers = [
        _verdict(fichier="a.xlsx", verdict="ACCEPTED", dossier_mois="LOAD_LOCK_202603"),
        _verdict(fichier="b.xlsx", verdict="REJECTED", dossier_mois="LOAD_LOCK_202604"),
    ]
    rapport = generer_rapport_consolide(fichiers)

    assert "LOAD_LOCK_202603" in rapport
    assert "LOAD_LOCK_202604" in rapport


def test_generer_rapport_consolide_sans_dossier_mois_section_explicite():
    fichiers = [_verdict(fichier="a.xlsx")]  # pas de clé "dossier_mois"
    rapport = generer_rapport_consolide(fichiers)
    assert "Aucune information de mois disponible" in rapport


def test_generer_rapport_consolide_tri_fichiers_du_pire_au_meilleur():
    fichiers = [
        _verdict(fichier="bon.xlsx", score=0.98),
        _verdict(fichier="pire.xlsx", score=0.1),
        _verdict(fichier="moyen.xlsx", score=0.6),
    ]
    rapport = generer_rapport_consolide(fichiers)

    assert rapport.index("pire.xlsx") < rapport.index("moyen.xlsx") < rapport.index("bon.xlsx")


def test_generer_rapport_consolide_echappe_le_html():
    fichiers = [_verdict(fichier="<img src=x onerror=alert(1)>.xlsx")]
    rapport = generer_rapport_consolide(fichiers)
    assert "<img src=x" not in rapport
