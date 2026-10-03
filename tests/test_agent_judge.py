"""Tests Judge v2 : règles métier, couverture, consolidation et compatibilité."""
from copy import deepcopy
import json

import pytest

import config
from judge.agent_judge import analyser, _grade_from_score, _verdict_from_score
from reporting.agent_reporting import generer_rapport_fichier, generer_rapport_consolide

NOM = "test.xlsx"


def anomalie(ligne=0, severite="medium", type_="statistique", confiance=1.0, **extra):
    return {
        "fichier": NOM, "ligne": ligne, "colonnes": ["Version", "Drive"],
        "type": type_, "source": "llm" if type_ in {"coherence", "realisme"} else "stats",
        "severite": severite, "valeur": "ALLGRIP / 2WD",
        "justification": "La transmission intégrale ALLGRIP contredit la valeur 2WD.",
        "confiance": confiance, **extra,
    }


def test_aucune_anomalie():
    v = analyser(NOM, [], 180)
    assert (v["score"], v["verdict"], v["grade"]) == (100, "ACCEPTED", "A")
    assert v["pipeline_status"] == "COMPLETE"
    assert v["lignes_affectees"] == 0
    assert not v["blocking"]


def test_pipeline_incomplet_ne_produit_ni_verdict_ni_score():
    v = analyser(
        NOM,
        [],
        180,
        pipeline_status="INCOMPLETE",
        pipeline_reason="Réalisme non exécuté : quota LLM épuisé.",
    )

    assert v["pipeline_status"] == "INCOMPLETE"
    assert v["verdict"] is None
    assert v["score"] is None
    assert v["grade"] is None
    assert v["penalite"] is None
    assert "Analyse incomplète" in v["justification"]
    assert "quota LLM épuisé" in v["justification"]


def test_quelques_low():
    v = analyser(NOM, [anomalie(i, "low") for i in range(5)], 180)
    assert v["score"] == pytest.approx(99.7222)
    assert v["verdict"] == "ACCEPTED"


def test_high_isolee_sur_gros_dataset():
    v = analyser(NOM, [anomalie(10, "high")], 100_000)
    assert v["score"] == pytest.approx(99.9994)
    assert v["verdict"] == "ACCEPTED"


@pytest.mark.parametrize("nombre,score,verdict", [(30, 82, "WARNING"), (60, 64, "REJECTED")])
def test_high_sur_proportion_importante(nombre, score, verdict):
    v = analyser(NOM, [anomalie(i, "high") for i in range(nombre)], 100)
    assert v["score"] == score
    assert v["verdict"] == verdict


def test_critical_llm_non_bloquante():
    v = analyser(NOM, [anomalie(0, "critical", "coherence", blocking=True)], 10)
    assert v["score"] == 90
    assert v["verdict"] == "ACCEPTED"
    assert not v["blocking"]


@pytest.mark.parametrize("regle", ["fichier_vide", "fichier_illisible", "structure_inexploitable"])
def test_regle_bloquante_independante_du_score(regle):
    v = analyser(NOM, [anomalie(0, "low", "structure", regle=regle, blocking=True)], 1000)
    assert v["score"] > 90
    assert v["verdict"] == "REJECTED"
    assert v["blocking"] and v["blocking_reasons"]


def test_fichier_vide_historique_rejete_avec_score_independant():
    a = anomalie(0, "critical", "structure", colonnes=[],
                 justification="Le fichier ne contient aucune ligne ou aucune colonne exploitable.")
    v = analyser(NOM, [a], 0)
    assert v["blocking"] and v["verdict"] == "REJECTED"
    assert v["score"] == 100


def test_critical_structure_n_est_pas_automatiquement_bloquante():
    v = analyser(NOM, [anomalie(0, "critical", "structure", blocking=True)], 100)
    assert not v["blocking"]
    assert v["score"] == 99


def test_regle_bloquante_llm_ignoree():
    v = analyser(NOM, [anomalie(0, "critical", "realisme", regle="fichier_vide", blocking=True)], 100)
    assert not v["blocking"]


def test_deduplication_coherence_realisme_et_sources():
    a = anomalie(73, "high", "coherence", confiance=0.6)
    b = anomalie(73, "critical", "realisme", confiance=0.9, colonnes=["Drive", "Version"])
    v = analyser(NOM, [a, b], 180)
    assert v["anomalies_total_brutes"] == 2
    assert v["anomalies_uniques"] == v["nb_anomalies_total"] == 1
    c = v["anomalies_consolidees"][0]
    assert c["sources"] == ["coherence", "realisme"]
    assert c["severite"] == "critical" and c["confiance"] == 0.9
    assert v["score"] == pytest.approx(99.5)


def test_deux_problemes_differents_meme_ligne():
    a = anomalie(73, "high", "coherence", probleme_id="transmission")
    b = anomalie(73, "high", "realisme", probleme_id="puissance")
    v = analyser(NOM, [a, b], 100)
    assert v["anomalies_uniques"] == 2
    assert v["lignes_affectees"] == 1
    assert v["score"] == pytest.approx(99.4)


def test_meme_colonnes_valeur_mais_diagnostics_opposes_non_fusionnes():
    a = anomalie(justification="Le prix est trop haut pour cette version du véhicule.")
    b = anomalie(justification="Le prix est trop bas pour cette version du véhicule.")
    assert analyser(NOM, [a, b], 100)["anomalies_uniques"] == 2


def test_colonnes_proches_avec_probleme_id_et_colonnes_distinctes():
    a = anomalie(type_="coherence", colonnes=["A", "B", "C", "D"], probleme_id="transmission")
    b = anomalie(type_="realisme", colonnes=["A", "B", "C", "D", "E"], probleme_id="transmission")
    assert analyser(NOM, [a, b], 100)["anomalies_uniques"] == 1
    b["colonnes"] = ["A", "Prix"]
    assert analyser(NOM, [a, b], 100)["anomalies_uniques"] == 2


def test_deduplication_ne_traverse_pas_fichiers_lignes_ou_portees():
    a = anomalie()
    autres = [anomalie(fichier="autre.xlsx"), anomalie(ligne=1), anomalie(portee="colonne")]
    assert analyser(NOM, [a, *autres], 100)["anomalies_uniques"] == 4


def test_confiance_llm_reduit_impact():
    faible = analyser(NOM, [anomalie(0, "critical", "coherence", 0.2)], 10)
    forte = analyser(NOM, [anomalie(0, "critical", "coherence", 1.0)], 10)
    assert faible["score"] == 98 and forte["score"] == 90
    assert faible["score"] > forte["score"]


def test_confiance_stats_ne_reduit_pas_regle_deterministe():
    assert analyser(NOM, [anomalie(0, "high", confiance=0.01)], 10)["score"] == 94


def test_meme_anomalie_10_lignes_et_100000_lignes():
    assert analyser(NOM, [anomalie(0, "high")], 10)["score"] == 94
    assert analyser(NOM, [anomalie(0, "high")], 100_000)["score"] == pytest.approx(99.9994)


def test_normalisation_et_entree_non_mutee():
    brut = {"ligne": "3", "colonnes": "Prix", "severity": "HIGH",
            "type": "realism", "confidence": "0.5"}
    original = deepcopy(brut)
    v = analyser(NOM, [brut], 10)
    a = v["anomalies_consolidees"][0]
    assert brut == original
    assert a["fichier"] == NOM and a["ligne"] == 3 and a["colonnes"] == ["Prix"]
    assert a["severite"] == "high" and a["type"] == "realisme"
    assert a["confiance"] == 0.5 and a["justification"]


@pytest.mark.parametrize("confiance,attendue", [(None, 1), ("bad", 1), (float("nan"), 1),
                                             (float("inf"), 1), (-1, 0), (5, 1)])
def test_confiance_manquante_invalide_ou_hors_bornes(confiance, attendue):
    a = analyser(NOM, [anomalie(type_="coherence", confiance=confiance)], 10)["anomalies_consolidees"][0]
    assert a["confiance"] == attendue


def test_indices_explicitement_fournis_comptes_une_fois():
    a = anomalie(0, "high", lignes=[0, 1, 2])
    b = anomalie(2, "critical", "coherence")
    v = analyser(NOM, [a, b], 10)
    assert v["lignes_affectees"] == 3
    assert v["score"] == 78  # 2 x 0.6 + 1 x 1.0, sur 10 lignes
    assert not v["couverture_estimee"]


def test_agregats_anciens_preservent_compte_et_signalent_estimation():
    a = anomalie(0, valeur="36 valeur(s) hors de l'intervalle attendu")
    v = analyser(NOM, [a], 180)
    assert v["lignes_affectees"] == 36
    assert v["taux_lignes_affectees"] == 0.2
    assert v["score"] == 95 and v["couverture_estimee"]


def test_agregats_sans_index_ne_sont_pas_additionnes_aveuglement():
    a = anomalie(None, "high", nb_lignes_affectees=30, colonnes=["A"])
    b = anomalie(None, "medium", nb_lignes_affectees=50, colonnes=["B"])
    v = analyser(NOM, [a, b], 100)
    assert v["lignes_affectees"] == 50
    assert v["score"] == 77  # 30 x .6 + 20 x .25
    assert v["couverture_estimee"]


def test_nan_colonne_historique_et_anomalie_sur_index_zero():
    a = anomalie(0, "medium", "structure", colonnes=["CO2"],
                 valeur="99.4% de valeurs manquantes (179/180)",
                 justification="La colonne 'CO2' a plus de 90% de ses valeurs manquantes (99.4%).")
    b = anomalie(0, "critical", "coherence", colonnes=["Drive"])
    v = analyser(NOM, [a, b], 180)
    assert v["lignes_affectees"] == 179
    assert v["score"] == pytest.approx(74.7222)
    assert v["couverture_estimee"]


def test_ancienne_signature_sans_nombre_lignes_est_explicitement_provisoire():
    v = analyser(NOM, [anomalie(99, "low")])
    assert v["nb_lignes_total"] is None and v["score_provisoire"]
    assert v["verdict"] == "WARNING"
    assert analyser(NOM, None)["score"] == 100


def test_signalements_non_objets_non_silencieux():
    v = analyser(NOM, [None, "invalide"], 100)
    assert v["anomalies_invalides"] == 2
    assert v["verdict"] == "WARNING"


def test_indices_invalides_ne_sortent_pas_de_la_taille_dataset():
    v = analyser(NOM, [anomalie(1000, "high")], 10)
    assert v["couverture_estimee"]
    assert 0 <= v["lignes_affectees"] <= 10


@pytest.mark.parametrize("nombre", [-1, 1.5, True, "invalide"])
def test_nombre_lignes_invalide(nombre):
    with pytest.raises(ValueError):
        analyser(NOM, [], nombre)


@pytest.mark.parametrize("score,grade", [(100, "A"), (90, "A"), (89.9999, "B"),
                                        (80, "B"), (79.9999, "C"), (70, "C"),
                                        (69.9999, "D"), (60, "D"), (59.9999, "F"), (0, "F")])
def test_grades(score, grade):
    assert _grade_from_score(score) == grade


@pytest.mark.parametrize("score,verdict", [(100, "ACCEPTED"), (90, "ACCEPTED"),
                                          (89.9999, "WARNING"), (70, "WARNING"),
                                          (69.9999, "REJECTED"), (0, "REJECTED")])
def test_seuils(score, verdict):
    assert _verdict_from_score(score) == verdict
    assert _verdict_from_score(score, blocking=True) == "REJECTED"


def test_configuration_judge_independante_des_agents(monkeypatch):
    monkeypatch.setattr(config, "JUDGE_POIDS_SEVERITE", {**config.JUDGE_POIDS_SEVERITE, "high": 0.2})
    monkeypatch.setattr(config, "JUDGE_SEUIL_ACCEPTED", 99)
    v = analyser(NOM, [anomalie(0, "high")], 10)
    assert v["score"] == 98 and v["verdict"] == "WARNING"
    assert config.PONDERATION_SEVERITE["high"] == 0.7


def test_entree_permutee_sortie_identique_et_json_serialisable():
    anomalies = [anomalie(3, "high", "coherence"), anomalie(3, "critical", "realisme"),
                 anomalie(2, "low", colonnes=["Prix"])]
    a = analyser(NOM, anomalies, 100)
    b = analyser(NOM, list(reversed(anomalies)), 100)
    assert a == b
    json.dumps(a, allow_nan=False)


def test_sortie_compatible_et_rapports_utilisent_anomalies_consolidees():
    anomalies = [anomalie(73, "high", "coherence"), anomalie(73, "high", "realisme")]
    v = analyser(NOM, anomalies, 180)
    requis = {"score", "grade", "verdict", "blocking", "blocking_reasons", "anomalies_total_brutes",
              "anomalies_uniques", "anomalies_par_severite", "anomalies_par_type", "lignes_affectees",
              "taux_lignes_affectees", "anomalies_consolidees", "nb_anomalies_total", "justification"}
    assert requis <= v.keys()
    rapport = generer_rapport_fichier(v, anomalies)
    assert "Score /100" in rapport
    assert rapport.count("ALLGRIP / 2WD") == 1
    v["anomalies"] = anomalies  # une ancienne liste brute ne doit pas remplacer le résultat v2
    consolide = generer_rapport_consolide([v])
    assert ">1</td>" in consolide


def test_exemple_180_lignes():
    severites = ["critical"] * 2 + ["high"] * 4 + ["medium"] * 3 + ["low"] * 5
    v = analyser(NOM, [anomalie(i, s) for i, s in enumerate(severites)], 180)
    assert v["lignes_affectees"] == 14
    assert v["taux_lignes_affectees"] == pytest.approx(14 / 180, abs=1e-6)
    assert v["penalite"] == pytest.approx(3.1389)
    assert v["score"] == pytest.approx(96.8611)
    assert v["grade"] == "A" and v["verdict"] == "ACCEPTED"


def test_meme_probleme_statistique_et_llm_conserve_provenance():
    a = anomalie(3, "high", "statistique")
    b = anomalie(3, "high", "coherence")
    v = analyser(NOM, [a, b], 100)
    assert v["anomalies_uniques"] == 1
    assert v["anomalies"][0]["sources"] == ["coherence", "statistique"]


def test_entree_scalaire_invalide_ne_plante_pas():
    assert analyser(NOM, 42, 100)["anomalies_invalides"] == 1


def test_cli_lit_taille_et_ecrit_verdict_consolide(tmp_path, monkeypatch, capsys):
    import judge.agent_judge as judge
    entree = tmp_path / "entree.json"
    entree.write_text(json.dumps({"fichier": NOM, "nb_lignes_totales": 180,
                                 "anomalies": [anomalie(73, "high", "coherence"),
                                               anomalie(73, "high", "realisme")]}), encoding="utf-8")
    monkeypatch.setattr(config, "RESULTATS_DIR", tmp_path)
    monkeypatch.setattr(judge.sys, "argv", ["judge", str(entree)])
    judge.main()
    v = json.loads((tmp_path / "verdict_entree.json").read_text(encoding="utf-8"))
    assert v["nb_lignes_total"] == 180 and v["anomalies_uniques"] == 1
    assert v["score"] == pytest.approx(99.6667)
    assert "ACCEPTED" in capsys.readouterr().out


def signalements_puissance():
    return [
        anomalie(5, "critical", "coherence", 0.98,
                 colonnes=["PowerKW", "PowerPS"], valeur="PowerKW=60, PowerPS=5700",
                 justification="60 kW correspond à ~81 ch (PS), 5700 PS est physiquement impossible pour ce véhicule ; 5700 est vraisemblablement un régime moteur placé dans la mauvaise colonne."),
        anomalie(5, "critical", "realisme", 0.99,
                 colonnes=["PowerPS"], valeur="5700",
                 justification="Une puissance de 5700 ch (PS) est physiquement impossible pour un moteur 1.2 L hybride (attendu ~80-90 ch)."),
    ]


def test_puissance_inclusion_colonnes_et_tracabilite():
    brutes = signalements_puissance()
    avant = deepcopy(brutes)
    v = analyser(NOM, brutes, 38)
    assert v["nb_signalements_bruts"] == 2
    assert v["nb_problemes_consolides"] == 1
    a = v["anomalies"][0]
    assert a["colonnes"] == ["PowerKW", "PowerPS"]
    assert a["severite"] == "critical" and a["confiance"] == 0.99
    assert a["sources"] == ["coherence", "realisme"] and a["nb_signalements"] == 2
    assert sorted(a["signalements_originaux"], key=lambda x: x["type"]) == avant
    a["signalements_originaux"][0]["colonnes"].append("Test")
    assert brutes == avant


def test_prix_multicolonnes_et_valeur_simple():
    brutes = [
        anomalie(7, "high", "coherence", 0.95,
                 colonnes=["Duration", "ListPrice", "Mileage", "Price"],
                 valeur="Price='37.299,-', ListPrice='€ 37.299,-', Duration=nan, Mileage=nan",
                 justification="La colonne Price contient le prix catalogue (identique à ListPrice) au lieu d'un loyer mensuel ; Duration et Mileage sont vides."),
        anomalie(7, "high", "realisme", 0.9, colonnes=["Price"], valeur="37.299,-",
                 justification="Le prix affiché (37 299 €) correspond au prix catalogue, pas à un loyer mensuel de location longue durée."),
    ]
    v = analyser(NOM, brutes, 38)
    assert v["nb_problemes_consolides"] == 1
    assert v["anomalies"][0]["colonnes"] == ["Duration", "ListPrice", "Mileage", "Price"]


def test_co2_et_prix_meme_ligne_restent_distincts():
    brutes = [anomalie(5, "high", "coherence", colonnes=["CO2"], problem_family="emission"),
              anomalie(5, "high", "realisme", colonnes=["Price"], problem_family="prix")]
    v = analyser(NOM, brutes, 38)
    assert v["nb_problemes_consolides"] == 2 and v["lignes_affectees"] == 1


@pytest.mark.parametrize("champ", ["rule_id", "problem_id", "problem_family"])
def test_identifiant_prioritaire_sur_descriptions(champ):
    brutes = [anomalie(5, "high", "coherence", colonnes=["PowerKW", "PowerPS"],
                      justification="Diagnostic A", valeur="A", **{champ: "puissance"}),
              anomalie(5, "critical", "realisme", colonnes=["PowerPS"],
                      justification="Diagnostic B", valeur="B", **{champ: "puissance"})]
    assert analyser(NOM, brutes, 38)["nb_problemes_consolides"] == 1
    brutes[1][champ] = "autre probleme"
    assert analyser(NOM, brutes, 38)["nb_problemes_consolides"] == 2


def test_inclusion_seule_ne_prouve_pas_meme_probleme():
    a, b = signalements_puissance()
    b["justification"] = "La valeur est manquante après extraction."
    assert analyser(NOM, [a, b], 38)["nb_problemes_consolides"] == 2


def test_colonne_principale_commune_avec_famille():
    brutes = [anomalie(colonnes=["Price", "Duration"], main_column="Price", problem_family="prix"),
              anomalie(colonnes=["Price", "Mileage"], main_column="Price", problem_family="prix")]
    assert analyser(NOM, brutes, 38)["nb_problemes_consolides"] == 1


def test_colonne_principale_commune_deduite_des_signalements():
    brutes = [anomalie(colonnes=["Price", "Duration"], valeur="Price='500', Duration=36",
                      justification="Le loyer mensuel est incohérent."),
              anomalie(colonnes=["Price", "Mileage"], valeur="Price='500', Mileage=10000",
                      justification="Loyer mensuel incohérent pour cette offre.")]
    assert analyser(NOM, brutes, 38)["nb_problemes_consolides"] == 1


def test_anti_fusion_transitive_par_inclusion():
    # A inclus dans B, C inclus dans B, mais A et C disjoints.
    brutes = [anomalie(colonnes=["CO2"], problem_family="controle"),
              anomalie(colonnes=["CO2", "Price"], problem_family="controle"),
              anomalie(colonnes=["Price"], problem_family="controle")]
    v = analyser(NOM, brutes, 38)
    assert v["nb_problemes_consolides"] == 2
    assert sorted(a["nb_signalements"] for a in v["anomalies"]) == [1, 2]
    assert analyser(NOM, list(reversed(brutes)), 38) == v


def test_identifiants_contradictoires_non_masques_par_famille():
    brutes = [anomalie(problem_id="prix trop haut", problem_family="prix"),
              anomalie(problem_id="prix trop bas", problem_family="prix")]
    assert analyser(NOM, brutes, 38)["nb_problemes_consolides"] == 2


def test_consolidation_interagents_sans_double_penalite():
    a, b = signalements_puissance()
    v = analyser(NOM, [a, b], 38)
    assert v["score"] == analyser(NOM, [b], 38)["score"]
    assert v["penalite"] == round(100 * 0.99 / 38, 4)
    assert v["lignes_affectees"] == 1


def test_compteurs_et_justification_distinguent_signalements_problemes_lignes():
    brutes = signalements_puissance() + [anomalie(5, "medium", colonnes=["CO2"])]
    v = analyser(NOM, brutes, 38)
    assert (v["nb_signalements_bruts"], v["nb_problemes_consolides"], v["lignes_affectees"]) == (3, 2, 1)
    assert v["anomalies_total_brutes"] == 3
    assert v["anomalies_uniques"] == v["nb_anomalies_total"] == 2
    assert "2 problème(s) consolidé(s)" in v["justification"]
    assert "3 signalement(s) brut(s)" in v["justification"]
    assert "1 ligne(s) affectée(s)" in v["justification"]
    assert "unique" not in v["justification"]


def test_consolidation_preserve_incomplete_et_blocage_structure():
    brutes = signalements_puissance()
    incomplet = analyser(NOM, brutes, 38, pipeline_status="INCOMPLETE")
    assert all(incomplet[k] is None for k in ("score", "grade", "verdict", "penalite"))
    assert incomplet["nb_problemes_consolides"] == 1
    brutes.append(anomalie(0, "low", "structure", rule_id="structure_inexploitable"))
    bloque = analyser(NOM, brutes, 38)
    assert bloque["blocking"] and bloque["verdict"] == "REJECTED"
