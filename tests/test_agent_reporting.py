"""
Tests unitaires du module reporting/agent_reporting.py.

100% déterministe (pure mise en forme HTML, aucun appel LLM) : les tests
vérifient le contenu texte généré plutôt que d'analyser le HTML avec un
parseur, pour rester simples et rapides.

Usage :
    python -m pytest tests/test_agent_reporting.py -v
"""

from copy import deepcopy

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


def test_generer_rapport_fichier_affiche_analyse_incomplete_sans_score():
    verdict = _verdict(
        score=None,
        verdict=None,
        grade=None,
        pipeline_status="INCOMPLETE",
        pipeline_reason="quota LLM épuisé",
        justification="Analyse incomplète : aucun verdict qualité ni score n'est produit.",
    )

    rapport = generer_rapport_fichier(verdict, [])

    assert "Analyse incomplète" in rapport
    assert 'class="badge badge-INCOMPLETE"' in rapport
    assert 'class="badge badge-ACCEPTED"' not in rapport
    assert '<div class="valeur">None</div>' not in rapport


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


def test_generer_rapport_consolide_accepte_un_pipeline_incomplet():
    fichiers = [
        _verdict(
            fichier="incomplet.xlsx",
            score=None,
            verdict=None,
            grade=None,
            pipeline_status="INCOMPLETE",
        ),
        _verdict(fichier="complet.xlsx", score=95, verdict="ACCEPTED"),
    ]

    rapport = generer_rapport_consolide(fichiers)

    assert "Analyse incomplète" in rapport
    assert "incomplet.xlsx" in rapport
    assert "complet.xlsx" in rapport


def _verdict_multiagents():
    originaux = [_anomalie(type_="coherence"), _anomalie(type_="realisme")]
    probleme = {**originaux[1], "sources": ["coherence", "realisme"],
                "nb_signalements": 2, "signalements_originaux": originaux}
    return _verdict(score=73.4474, grade="C", verdict="WARNING", score_echelle=100,
                    nb_signalements_bruts=2, nb_problemes_consolides=1,
                    nb_anomalies_total=1, lignes_affectees=1, couverture_estimee=True,
                    anomalies_consolidees=[probleme], anomalies_par_type={"realisme": 1})


def _carte(valeur, libelle):
    return f'<div class="valeur">{valeur}</div><div class="libelle">{libelle}</div>'


def test_resume_et_tableau_preservent_provenance_multiagents():
    v = _verdict_multiagents()
    avant = deepcopy(v)
    rapport = generer_rapport_fichier(v, [])
    for valeur, libelle in [(2, "Signalements bruts"), (1, "Problèmes consolidés"),
                            (1, "Lignes affectées"), (73.4474, "Score /100")]:
        assert _carte(valeur, libelle) in rapport
    assert '<th>Type principal</th><th>Sources</th><th>Signalements</th>' in rapport
    assert '<td>realisme</td><td>coherence, realisme</td><td>2</td>' in rapport
    assert 'Couverture estimée : oui.' in rapport
    assert '<div class="libelle">Anomalies</div>' not in rapport
    assert v == avant


def test_consolide_repartit_originaux_sans_attribuer_fusion_au_type_principal():
    v = _verdict_multiagents()
    rapport = generer_rapport_consolide([v])
    bruts = rapport.split('<h2>Signalements bruts par agent</h2>')[1].split('<h2>Problèmes consolidés</h2>')[0]
    assert _carte(1, "coherence") in bruts
    assert _carte(1, "realisme") in bruts
    assert _carte(2, "Total des signalements bruts") in bruts
    assert _carte(1, "Total des problèmes consolidés") in rapport
    assert _carte(1, "Problèmes confirmés par plusieurs agents") in rapport
    assert '<th>Signalements bruts</th><th>Problèmes consolidés</th><th>Lignes affectées</th>' in rapport
    assert '<td>C</td><td>73.4474</td><td>2</td><td>1</td><td>1</td>' in rapport
    assert 'Anomalies par type' not in rapport


def test_compteurs_consolides_additionnent_plusieurs_fichiers():
    rapport = generer_rapport_consolide([_verdict_multiagents(), _verdict_multiagents()])
    assert _carte(2, "coherence") in rapport
    assert _carte(2, "realisme") in rapport
    assert _carte(4, "Total des signalements bruts") in rapport
    assert _carte(2, "Total des problèmes consolidés") in rapport
    assert _carte(2, "Problèmes confirmés par plusieurs agents") in rapport


def test_sources_et_nombre_signalements_sont_echappes():
    v = _verdict_multiagents()
    a = v['anomalies_consolidees'][0]
    a['sources'] = ['<script>source</script>']
    a['nb_signalements'] = '<img src=x>'
    rapport = generer_rapport_fichier(v, [])
    assert '<script>' not in rapport and '<img src=x>' not in rapport
    assert '&lt;script&gt;source&lt;/script&gt;' in rapport
    assert '&lt;img src=x&gt;' in rapport


def test_origines_manquantes_ne_deviennent_pas_de_faux_compteurs_bruts():
    v = _verdict_multiagents()
    del v['anomalies_consolidees'][0]['signalements_originaux']
    rapport = generer_rapport_consolide([v])
    assert 'Répartition complète indisponible' in rapport
    assert 'Type principal des problèmes consolidés' in rapport
    assert _carte(1, 'Problèmes confirmés par plusieurs agents') in rapport


def test_reporting_incomplete_preserve_compteurs_et_score_absent():
    v = _verdict_multiagents()
    v.update(pipeline_status='INCOMPLETE', score=None, grade=None, verdict=None)
    avant = deepcopy(v)
    for rapport in (generer_rapport_fichier(v, []), generer_rapport_consolide([v])):
        assert 'badge-INCOMPLETE' in rapport
        assert '73.4474' not in rapport
        assert 'coherence' in rapport and 'realisme' in rapport
    assert v == avant
