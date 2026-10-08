"""
Tests unitaires de orchestrateur.py.

Aucun appel réseau réel : un client LLM factice (repris du même motif que
tests/test_agent_realisme.py) simule agents.llm_client.LLMClient.appeler().
Les tests qui touchent le disque (résultats, rapports) utilisent tmp_path.

agents.agent_coherence fixe ses dossiers de résultats/checkpoints en
constantes de module (calculées à l'import, donc jamais rechargées si on
monkeypatch config.RESULTATS_DIR en cours de test) : une autouse fixture
redirige directement ces constantes vers tmp_path, pour qu'aucun test
n'écrive dans le vrai dossier resultats/coherence/. Une deuxième autouse
fixture désactive config.RECHERCHE_WEB_ACTIVEE (agents/agent_realisme.py) :
sans elle, un chunk Réalisme avec au moins une anomalie ferait un vrai
appel réseau ddgs pendant les tests.

Usage :
    python -m pytest tests/test_orchestrateur.py -v
"""

import hashlib
import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

import agents.agent_coherence as agent_coherence_module
import config
import orchestrateur
from agents.llm_client import AppelMetadata
from ingestion.local_loader import FichierInfo

NOM_FICHIER_TEST = "test.xlsx"


@pytest.fixture(autouse=True)
def _isoler_dossiers_coherence(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_coherence_module, "DOSSIER_RESULTATS", tmp_path / "coherence")
    monkeypatch.setattr(agent_coherence_module, "DOSSIER_CHECKPOINTS", tmp_path / "coherence" / "checkpoints")

    # Ces tests d'orchestration injectent des DataFrames et utilisent des
    # chemins volontairement fictifs : simule uniquement la lecture binaire
    # du fichier. Les tests dédiés à Cohérence couvrent le vrai SHA-256.
    monkeypatch.setattr(
        agent_coherence_module,
        "calculer_hash_fichier",
        lambda chemin: hashlib.sha256(str(chemin).encode("utf-8")).hexdigest(),
    )
    monkeypatch.setattr(agent_coherence_module, "PAUSE_ENTRE_CHUNKS_SECONDES", 0.0)
    monkeypatch.setattr(config, "HISTORY_DIR", tmp_path / "history")


@pytest.fixture(autouse=True)
def _desactiver_recherche_web_par_defaut(monkeypatch):
    # Sans ça, un chunk Réalisme avec au moins une anomalie déclenche un
    # vrai appel réseau ddgs (voir agents/agent_realisme.py) — jamais
    # souhaitable dans ces tests.
    monkeypatch.setattr(config, "RECHERCHE_WEB_ACTIVEE", False)
    monkeypatch.setattr(config, "STORAGE_MODE", "local")
    # Les tests injectent exclusivement des clients LLM factices : leur
    # résultat ne doit pas dépendre du compteur de quota réel du poste.
    # Les scénarios dédiés au quota épuisé remplacent explicitement cette
    # valeur par 0 dans le test concerné.
    monkeypatch.setattr(orchestrateur, "quota_restant", lambda: 45)


def _info(nom=NOM_FICHIER_TEST, dossier_mois="LOAD_LOCK_202603"):
    return FichierInfo(
        chemin=Path("peu_importe") / nom,
        nom=nom,
        dossier_mois=dossier_mois,
        taille_ko=1.0,
        date_modification=datetime(2026, 1, 1),
    )


class _ClientFactice:
    def __init__(self, reponses):
        self.reponses = list(reponses)
        self.appels = []

    def appeler(self, **kwargs):
        self.appels.append(kwargs)
        return self.reponses.pop(0)


class _ClientExplosif:
    """Client factice qui échoue si on l'appelle : preuve qu'aucun appel Réalisme n'a eu lieu."""

    def appeler(self, **kwargs):
        raise AssertionError("Réalisme n'aurait jamais dû être appelé ici")


def test_chemin_cache_s3_reproduit_arborescence_de_la_cle(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    loader = object.__new__(orchestrateur.S3Loader)

    chemin = loader.chemin_cache("input/LOAD_LOCK_202603/test.xlsx")

    assert chemin == tmp_path / "cache" / "input" / "LOAD_LOCK_202603" / "test.xlsx"


def test_cle_rapport_s3_utilise_uniquement_des_slashs(monkeypatch):
    monkeypatch.setattr(config, "S3_REPORTS_PREFIX", "reports\\")

    cle = orchestrateur._construire_cle_rapport_s3(
        "LOAD_LOCK_202603", "rapport_test.html"
    )

    assert cle == "reports/LOAD_LOCK_202603/rapport_test.html"
    assert "\\" not in cle


def test_cle_quarantaine_s3_utilise_uniquement_des_slashs(monkeypatch):
    monkeypatch.setattr(config, "S3_QUARANTINE_PREFIX", "quarantine\\")

    cle = orchestrateur._construire_cle_quarantaine_s3(
        "LOAD_LOCK_202603", "fichier.xlsx"
    )

    assert cle == "quarantine/LOAD_LOCK_202603/fichier.xlsx"
    assert "\\" not in cle


@pytest.mark.parametrize(
    "verdict,pipeline_status,attendu",
    [
        ("ACCEPTED", "COMPLETE", False),
        ("WARNING", "COMPLETE", False),
        ("REJECTED", "COMPLETE", True),
        (None, "INCOMPLETE", True),
    ],
)
def test_statuts_eligibles_a_la_quarantaine(verdict, pipeline_status, attendu):
    assert orchestrateur._doit_mettre_en_quarantaine(
        {"verdict": verdict, "pipeline_status": pipeline_status}
    ) is attendu


def _anomalie_realisme(ligne=0, severite="high"):
    return {
        "fichier": NOM_FICHIER_TEST,
        "ligne": ligne,
        "colonnes": ["Prix"],
        "type": "realisme",
        "severite": severite,
        "source": "llm",
        "valeur": "peu importe",
        "justification": "justification factice",
        "confiance": 0.8,
    }


# ---------------------------------------------------------------------------
# _lignes_suspectes
# ---------------------------------------------------------------------------


def test_lignes_suspectes_inclut_les_lignes_pointees_par_une_anomalie():
    df = pd.DataFrame({"A": range(20)})
    anomalies = [{"ligne": 7}, {"ligne": 13}]

    lot = orchestrateur._lignes_suspectes(df, anomalies, random_state=0)

    assert 7 in lot.index
    assert 13 in lot.index


def test_lignes_suspectes_inclut_la_ligne_zero():
    # ligne=0 est ambigu par convention (portée colonne OU vraie ligne 0,
    # voir docs/contrat_anomalie.md) : on l'inclut quand même, plutôt que de
    # risquer de manquer une vraie anomalie sur la première ligne du fichier
    # (raté observé en test réel, voir regles_orchestrateur.md).
    df = pd.DataFrame({"A": range(20)})
    anomalies = [{"ligne": 0}]

    lot = orchestrateur._lignes_suspectes(df, anomalies, random_state=0)

    assert 0 in lot.index


def test_lignes_suspectes_ajoute_un_echantillon_de_controle(monkeypatch):
    monkeypatch.setattr(config, "TAUX_ECHANTILLON_CONTROLE", 0.5)
    df = pd.DataFrame({"A": range(20)})

    lot = orchestrateur._lignes_suspectes(df, [], random_state=0)

    assert 0 < len(lot) <= len(df)


def test_lignes_suspectes_sans_echantillon_de_controle(monkeypatch):
    monkeypatch.setattr(config, "TAUX_ECHANTILLON_CONTROLE", 0.0)
    df = pd.DataFrame({"A": range(20)})
    anomalies = [{"ligne": 5}]

    lot = orchestrateur._lignes_suspectes(df, anomalies, random_state=0)

    assert list(lot.index) == [5]


# ---------------------------------------------------------------------------
# pipeline_fichier
# ---------------------------------------------------------------------------


def test_pipeline_fichier_court_circuite_sur_critical_structure():
    df_vide = pd.DataFrame()  # verifier_fichier_vide -> critical -> fichier_exploitable() == False

    verdict = orchestrateur.pipeline_fichier(
        _info(), df_vide, client=_ClientExplosif(), coherence_client=_ClientExplosif()
    )

    assert verdict["verdict"] == "REJECTED"
    assert verdict["dossier_mois"] == "LOAD_LOCK_202603"
    assert verdict["anomalies"]


def test_pipeline_fichier_flux_complet_integre_les_anomalies_realisme(monkeypatch):
    monkeypatch.setattr(config, "TAUX_ECHANTILLON_CONTROLE", 0.0)  # déterministe : uniquement les lignes suspectes explicites

    # Une ligne dupliquée (index 2) donne une anomalie structure avec ligne=2 != 0,
    # donc _lignes_suspectes sélectionne bien au moins cette ligne : garantit que
    # analyser_realisme reçoit un lot non vide et appelle le client factice.
    # Cohérence reçoit directement df + anomalies_structure (plus de rechargement,
    # voir AgentCoherence.analyser_fichier) : elle est bien invoquée, avec un
    # client factice qui répond "aucune anomalie".
    df = pd.DataFrame({"Modele": ["A", "B", "A", "C", "D"], "Prix": [1, 2, 1, 3, 4]})

    reponse = ({"anomalies": [_anomalie_realisme(ligne=2)]}, AppelMetadata(modele="m", succes=True))
    client = _ClientFactice(reponses=[reponse])
    coherence_client = _ClientFactice(reponses=[({"anomalies": []}, AppelMetadata(modele="m", succes=True))])

    verdict = orchestrateur.pipeline_fichier(
        _info(), df, client=client, coherence_client=coherence_client, random_state=0
    )

    assert len(client.appels) == 1
    assert verdict["anomalies_par_type"]["realisme"] == 1
    assert verdict["dossier_mois"] == "LOAD_LOCK_202603"
    assert any(a["type"] == "realisme" for a in verdict["anomalies"])


def test_pipeline_fichier_integre_les_anomalies_coherence(monkeypatch):
    # Stats et Cohérence tournent en parallèle sur tout le fichier ; leurs
    # anomalies combinées alimentent _lignes_suspectes et le verdict final.
    # Aucun mock de LocalLoader.charger nécessaire ici : Cohérence reçoit
    # directement le df déjà chargé, elle ne recharge plus rien elle-même.
    monkeypatch.setattr(config, "TAUX_ECHANTILLON_CONTROLE", 0.0)
    df = pd.DataFrame({"Modele": ["A", "B", "C", "D", "E"], "Prix": [10, 20, 30, 40, 50]})

    anomalie_coherence = {
        "fichier": NOM_FICHIER_TEST, "ligne": 3, "colonnes": ["Modele", "Prix"], "type": "coherence",
        "severite": "high", "source": "llm", "valeur": "peu importe",
        "justification": "incohérence factice", "confiance": 0.85,
    }
    coherence_client = _ClientFactice(
        reponses=[({"anomalies": [anomalie_coherence]}, AppelMetadata(modele="m", succes=True))]
    )
    realisme_client = _ClientFactice(reponses=[({"anomalies": []}, AppelMetadata(modele="m", succes=True))])

    verdict = orchestrateur.pipeline_fichier(
        _info(), df, client=realisme_client, coherence_client=coherence_client, random_state=0
    )

    assert len(coherence_client.appels) == 1
    assert verdict["anomalies_par_type"]["coherence"] == 1
    assert any(a["type"] == "coherence" and a["ligne"] == 3 for a in verdict["anomalies"])


def test_pipeline_fichier_ne_recharge_pas_le_fichier_pour_coherence(monkeypatch):
    # Preuve directe de la correction : si Cohérence essayait de recharger
    # le fichier depuis le disque (comme avant), ceci lèverait.
    def _charger_qui_explose(self, chemin):
        raise AssertionError("Cohérence n'aurait pas dû recharger le fichier depuis le disque")

    monkeypatch.setattr(orchestrateur.LocalLoader, "charger", _charger_qui_explose)
    monkeypatch.setattr(config, "TAUX_ECHANTILLON_CONTROLE", 0.0)

    df = pd.DataFrame({"Modele": ["A", "B", "C"], "Prix": [10, 20, 30]})
    coherence_client = _ClientFactice(reponses=[({"anomalies": []}, AppelMetadata(modele="m", succes=True))])
    realisme_client = _ClientFactice(reponses=[])  # aucune ligne suspecte attendue -> jamais appelé

    verdict = orchestrateur.pipeline_fichier(
        _info(), df, client=realisme_client, coherence_client=coherence_client, random_state=0
    )

    assert verdict["verdict"] in {"ACCEPTED", "WARNING", "REJECTED"}
    assert len(coherence_client.appels) == 1


def test_analyser_coherence_separe_cache_local_et_cle_s3(monkeypatch, tmp_path):
    appels = {}

    class _AgentCoherenceFactice:
        def __init__(self, llm_client=None):
            appels["client"] = llm_client

        def analyser_fichier(self, chemin, **kwargs):
            appels["chemin"] = chemin
            appels.update(kwargs)
            return {"anomalies": [], "status": "SUCCESS"}

    monkeypatch.setattr(orchestrateur, "AgentCoherence", _AgentCoherenceFactice)
    chemin_cache = tmp_path / "input" / "LOAD_LOCK_202603" / NOM_FICHIER_TEST
    cle_s3 = f"input/LOAD_LOCK_202603/{NOM_FICHIER_TEST}"
    df = pd.DataFrame({"A": [1]})

    resultat = orchestrateur._analyser_coherence(
        _info(),
        df,
        [],
        llm_client="client-factice",
        chemin_fichier=chemin_cache,
        checkpoint_source_path=cle_s3,
    )

    assert resultat["status"] == "SUCCESS"
    assert appels["chemin"] == chemin_cache
    assert appels["df"] is df
    assert appels["anomalies_structure"] == []
    assert appels["checkpoint_source_path"] == cle_s3


def test_judge_recoit_nombre_lignes_et_consolide_les_deux_agents(monkeypatch):
    monkeypatch.setattr(config, "TAUX_ECHANTILLON_CONTROLE", 0.0)
    df = pd.DataFrame({"Version": ["A", "B", "C", "D", "E"], "Drive": ["2WD"] * 5})
    anomalie = {
        "fichier": NOM_FICHIER_TEST, "ligne": 3, "colonnes": ["Version", "Drive"],
        "type": "coherence", "severite": "high", "source": "llm", "confiance": 1.0,
        "valeur": "ALLGRIP / 2WD", "justification": "La transmission ALLGRIP contredit 2WD.",
    }
    coherence = _ClientFactice([({"anomalies": [anomalie]}, AppelMetadata(modele="m", succes=True))])
    realisme = _ClientFactice([({"anomalies": [{**anomalie, "type": "realisme"}]}, AppelMetadata(modele="m", succes=True))])
    verdict = orchestrateur.pipeline_fichier(_info(), df, client=realisme, coherence_client=coherence)
    assert verdict["nb_lignes_total"] == 5
    assert verdict["anomalies_total_brutes"] == 2
    assert verdict["anomalies_uniques"] == 1
    assert verdict["anomalies"] == verdict["anomalies_consolidees"]
    assert verdict["anomalies"][0]["sources"] == ["coherence", "realisme"]
    assert verdict["score"] == 88 and verdict["verdict"] == "WARNING"


def test_quota_epuise_avant_realisme_produit_pipeline_incomplet(monkeypatch):
    monkeypatch.setattr(config, "TAUX_ECHANTILLON_CONTROLE", 1.0)
    monkeypatch.setattr(orchestrateur, "quota_restant", lambda: 0)
    df = pd.DataFrame({"Modele": ["A", "B", "C"], "Prix": [10, 20, 30]})
    coherence = _ClientFactice([({"anomalies": []}, AppelMetadata(modele="m", succes=True))])

    verdict = orchestrateur.pipeline_fichier(
        _info(), df, client=_ClientExplosif(), coherence_client=coherence
    )

    assert verdict["pipeline_status"] == "INCOMPLETE"
    assert verdict["verdict"] is None
    assert verdict["score"] is None
    assert verdict["grade"] is None
    assert verdict["etapes_pipeline"]["coherence"]["status"] == "SUCCESS"
    assert verdict["etapes_pipeline"]["realisme"]["status"] == "FAILED"
    assert "quota LLM épuisé" in verdict["pipeline_reason"]


def test_echec_realisme_conserve_les_anomalies_connues_des_autres_agents(monkeypatch):
    structure = [{"type": "structure", "severite": "low"}]
    stats = [{"type": "statistique", "severite": "medium"}]
    coherence = [{"type": "coherence", "severite": "high"}]
    anomalies_recues = []

    monkeypatch.setattr(orchestrateur, "analyser_structure", lambda *args, **kwargs: structure)
    monkeypatch.setattr(orchestrateur, "fichier_exploitable", lambda *args, **kwargs: True)
    monkeypatch.setattr(orchestrateur, "analyser_stats", lambda *args, **kwargs: stats)
    monkeypatch.setattr(
        orchestrateur,
        "_analyser_coherence",
        lambda *args, **kwargs: {
            "anomalies": coherence,
            "status": "SUCCESS",
            "analyse_complete": True,
            "error": None,
        },
    )
    monkeypatch.setattr(orchestrateur, "_lignes_suspectes", lambda df, *args, **kwargs: df)
    monkeypatch.setattr(orchestrateur, "quota_restant", lambda: 10)
    monkeypatch.setattr(
        orchestrateur,
        "analyser_realisme",
        lambda *args, **kwargs: {
            "anomalies": [],
            "status": "FAILED",
            "analyse_complete": False,
            "error": "réponse JSON invalide",
        },
    )

    def _judge(_nom, anomalies, **kwargs):
        anomalies_recues.extend(anomalies)
        return {
            "pipeline_status": kwargs["pipeline_status"],
            "pipeline_reason": kwargs["pipeline_reason"],
            "verdict": None,
            "score": None,
            "grade": None,
            "anomalies": anomalies,
            "anomalies_consolidees": anomalies,
        }

    monkeypatch.setattr(orchestrateur, "analyser_judge", _judge)
    verdict = orchestrateur.pipeline_fichier(_info(), pd.DataFrame({"A": [1]}))

    assert verdict["pipeline_status"] == "INCOMPLETE"
    assert verdict["verdict"] is None
    assert anomalies_recues == structure + stats + coherence
    assert verdict["etapes_pipeline"]["realisme"]["status"] == "FAILED"


def test_quota_epuise_pendant_coherence_remonte_partial(monkeypatch):
    monkeypatch.setattr(config, "CHUNK_SIZE", 1)
    monkeypatch.setattr(config, "TAUX_ECHANTILLON_CONTROLE", 0.0)
    monkeypatch.setattr(orchestrateur, "quota_restant", lambda: 0)
    df = pd.DataFrame({"Modele": ["A", "B", "C"], "Prix": [10, 20, 30]})
    coherence = _ClientFactice([
        ({"anomalies": []}, AppelMetadata(modele="m", succes=True)),
        ({"anomalies": []}, AppelMetadata(modele="m", succes=True)),
        (None, AppelMetadata(modele="m", succes=False, erreur="quota exhausted")),
    ])

    verdict = orchestrateur.pipeline_fichier(
        _info(), df, client=_ClientFactice([]), coherence_client=coherence
    )

    etape = verdict["etapes_pipeline"]["coherence"]
    assert verdict["pipeline_status"] == "INCOMPLETE"
    assert verdict["verdict"] is None and verdict["score"] is None
    assert etape["status"] == "PARTIAL"
    assert etape["metadata"]["nb_chunks_succes"] == 2
    assert etape["metadata"]["nb_chunks_echec"] == 1


def test_orchestrateur_interdit_le_second_sampling_realisme(monkeypatch):
    monkeypatch.setattr(config, "SEUIL_FICHIER_GEANT", 4)
    monkeypatch.setattr(config, "MAX_LIGNES_PAR_FICHIER", 4)
    monkeypatch.setattr(config, "TAUX_ECHANTILLON_CONTROLE", 0.0)
    monkeypatch.setattr(orchestrateur, "quota_restant", lambda: 45)
    df = pd.DataFrame({"A": range(10)})
    anomalies_stats = [
        {
            "fichier": NOM_FICHIER_TEST,
            "ligne": i,
            "colonnes": ["A"],
            "type": "statistique",
            "severite": "low",
            "source": "stats",
            "valeur": str(i),
            "justification": f"ligne suspecte {i}",
            "confiance": 1.0,
        }
        for i in df.index
    ]
    monkeypatch.setattr(orchestrateur, "analyser_stats", lambda nom, donnees: anomalies_stats)
    coherence = _ClientFactice([({"anomalies": []}, AppelMetadata(modele="m", succes=True))])
    lignes_recues = []

    def _realisme_factice(nom_fichier, lot, client=None, echantillonner=True):
        lignes_recues.extend(lot.index.tolist())
        assert echantillonner is False
        return {
            "anomalies": [], "status": "SUCCESS", "analyse_complete": True,
            "chunks_total": 1, "chunks_success": 1, "chunks_failed": 0,
            "nb_lignes_recues": len(lot), "nb_lignes_analysees": len(lot),
            "nb_lignes_echec": 0, "error": None,
        }

    monkeypatch.setattr(orchestrateur, "analyser_realisme", _realisme_factice)

    verdict = orchestrateur.pipeline_fichier(
        _info(), df, client=_ClientFactice([]), coherence_client=coherence
    )

    assert lignes_recues == list(df.index)
    assert verdict["pipeline_status"] == "COMPLETE"


# ---------------------------------------------------------------------------
# main() : garde-fou de quota et écriture des résultats
# ---------------------------------------------------------------------------


def test_fichiers_a_traiter_combine_noms_et_load_lock():
    infos = [
        _info(nom="a.xlsx", dossier_mois="LOAD_LOCK_202603"),
        _info(nom="a.xlsx", dossier_mois="LOAD_LOCK_202604"),
        _info(nom="b.xlsx", dossier_mois="LOAD_LOCK_202603"),
    ]

    class _LoaderFactice:
        def lister_fichiers(self):
            return infos

    selection = orchestrateur._fichiers_a_traiter(
        _LoaderFactice(), ["a.xlsx"], "LOAD_LOCK_202603"
    )

    assert selection == [infos[0]]


@pytest.mark.parametrize(
    ("noms", "dossier_mois", "noms_attendus"),
    [
        (["a.xlsx"], "LOAD_LOCK_202603", ["a.xlsx"]),
        (["a.xlsx", "b.xlsx"], "LOAD_LOCK_202603", ["a.xlsx", "b.xlsx"]),
        (None, "LOAD_LOCK_202603", ["a.xlsx", "b.xlsx"]),
    ],
)
def test_executer_audit_profile_uniquement_la_selection_du_loader_s3(
    monkeypatch,
    noms,
    dossier_mois,
    noms_attendus,
):
    infos = [
        _info(nom="a.xlsx", dossier_mois="LOAD_LOCK_202603"),
        _info(nom="b.xlsx", dossier_mois="LOAD_LOCK_202603"),
        _info(nom="hors_scope.xlsx", dossier_mois="LOAD_LOCK_202604"),
    ]

    class _LoaderS3Factice:
        def __init__(self):
            self.listings = 0

        def lister_fichiers(self):
            self.listings += 1
            return infos

    loader = _LoaderS3Factice()
    appels_inventaire = []
    monkeypatch.setattr(config, "STORAGE_MODE", "s3")
    monkeypatch.setattr(orchestrateur, "creer_loader", lambda: loader)
    monkeypatch.setattr(orchestrateur, "quota_restant", lambda: 0)
    monkeypatch.setattr(
        orchestrateur,
        "construire_inventaire",
        lambda *, loader, fichiers: appels_inventaire.append((loader, list(fichiers))),
    )
    monkeypatch.setattr(
        orchestrateur,
        "_finaliser_historique_run",
        lambda *args, **kwargs: {"local_path": None, "s3_key": None, "uploaded": None},
    )

    resultat = orchestrateur.executer_audit(
        noms_fichiers=noms,
        dossier_mois=dossier_mois,
    )

    assert loader.listings == 1
    assert len(appels_inventaire) == 1
    assert appels_inventaire[0][0] is loader
    assert [info.nom for info in appels_inventaire[0][1]] == noms_attendus
    assert resultat["files_discovered"] == len(noms_attendus)


def test_main_arrete_proprement_si_quota_deja_epuise(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "RESULTATS_DIR", tmp_path)
    monkeypatch.setattr(orchestrateur, "construire_inventaire", lambda *args, **kwargs: pd.DataFrame())
    monkeypatch.setattr(orchestrateur, "quota_restant", lambda: 0)
    monkeypatch.setattr(orchestrateur.LocalLoader, "lister_fichiers", lambda self: [_info()])
    monkeypatch.setattr(
        orchestrateur, "pipeline_fichier",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("pipeline_fichier n'aurait pas dû être appelé")),
    )

    verdicts = orchestrateur.main()

    assert verdicts == []
    historiques = list((tmp_path / "history" / "runs").rglob("*.json"))
    assert len(historiques) == 1


def test_main_traite_un_fichier_et_ecrit_les_resultats(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "RESULTATS_DIR", tmp_path)
    monkeypatch.setattr(orchestrateur, "construire_inventaire", lambda *args, **kwargs: pd.DataFrame())
    monkeypatch.setattr(config, "TAUX_ECHANTILLON_CONTROLE", 0.0)
    monkeypatch.setattr(orchestrateur, "quota_restant", lambda: 45)

    info = _info(nom="petit.xlsx", dossier_mois="LOAD_LOCK_202604")
    df = pd.DataFrame({"A": [1, 2, 3]})

    monkeypatch.setattr(orchestrateur.LocalLoader, "lister_fichiers", lambda self: [info])
    monkeypatch.setattr(orchestrateur.LocalLoader, "charger", lambda self, chemin: df)

    # Ce mock de charger() sert à main() (qui charge le fichier une fois) ;
    # Cohérence reçoit ensuite ce même df directement, sans recharger.
    coherence_client = _ClientFactice(reponses=[({"anomalies": []}, AppelMetadata(modele="m", succes=True))])

    verdicts = orchestrateur.main(client=_ClientFactice(reponses=[]), coherence_client=coherence_client)

    assert len(verdicts) == 1
    dossier_sortie = tmp_path / "LOAD_LOCK_202604"
    assert (dossier_sortie / "verdict_petit.json").exists()
    assert (dossier_sortie / "rapport_petit.html").exists()
    assert (tmp_path / "rapport_consolide.html").exists()
    assert len(list((tmp_path / "history" / "runs").rglob("*.json"))) == 1


def test_main_ignore_un_fichier_dont_le_chargement_echoue(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "RESULTATS_DIR", tmp_path)
    monkeypatch.setattr(orchestrateur, "construire_inventaire", lambda *args, **kwargs: pd.DataFrame())
    monkeypatch.setattr(orchestrateur, "quota_restant", lambda: 45)
    monkeypatch.setattr(orchestrateur.LocalLoader, "lister_fichiers", lambda self: [_info()])
    monkeypatch.setattr(orchestrateur.LocalLoader, "charger", lambda self, chemin: None)

    verdicts = orchestrateur.main()

    assert verdicts == []


def test_main_continue_apres_lechec_dun_fichier(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "RESULTATS_DIR", tmp_path)
    monkeypatch.setattr(orchestrateur, "construire_inventaire", lambda *args, **kwargs: pd.DataFrame())
    monkeypatch.setattr(orchestrateur, "quota_restant", lambda: 45)

    infos = [_info(nom="casse.xlsx"), _info(nom="ok.xlsx")]
    monkeypatch.setattr(orchestrateur.LocalLoader, "lister_fichiers", lambda self: infos)
    monkeypatch.setattr(orchestrateur.LocalLoader, "charger", lambda self, chemin: pd.DataFrame({"A": [1, 2, 3]}))

    appels = {"n": 0}
    run_ids = []

    def _run_id_unique(moment):
        run_ids.append(moment)
        return "20261008T103512Z_a1b2c3d4"

    def _pipeline_qui_casse_une_fois(info, df, client=None, coherence_client=None, random_state=None):
        appels["n"] += 1
        if info.nom == "casse.xlsx":
            raise RuntimeError("panne simulée")
        return {
            "fichier": info.nom, "nb_anomalies_total": 0, "score": 1.0, "verdict": "ACCEPTED", "grade": "A",
            "justification": "ok", "anomalies_par_type": {}, "anomalies_par_severite": {},
            "dossier_mois": info.dossier_mois, "anomalies": [],
        }

    monkeypatch.setattr(orchestrateur, "pipeline_fichier", _pipeline_qui_casse_une_fois)
    monkeypatch.setattr(orchestrateur, "generer_run_id", _run_id_unique)

    verdicts = orchestrateur.main()

    assert appels["n"] == 2
    assert len(run_ids) == 1
    assert len(verdicts) == 1
    assert verdicts[0]["fichier"] == "ok.xlsx"
    assert len(list((tmp_path / "history" / "runs").rglob("*.json"))) == 1


def test_main_plusieurs_fichiers_cree_un_seul_historique(monkeypatch, tmp_path):
    infos = [_info(nom="a.xlsx"), _info(nom="b.xlsx")]
    df = pd.DataFrame({"A": [1]})

    class _LoaderLocalFactice:
        def lister_fichiers(self):
            return infos

        def charger(self, chemin):
            return df

    def _pipeline_factice(info, donnees, **kwargs):
        return {
            "fichier": info.nom,
            "dossier_mois": info.dossier_mois,
            "pipeline_status": "COMPLETE",
            "verdict": "ACCEPTED",
            "score": 100,
            "grade": "A",
            "anomalies": [],
        }

    monkeypatch.setattr(config, "STORAGE_MODE", "local")
    monkeypatch.setattr(config, "RESULTATS_DIR", tmp_path)
    monkeypatch.setattr(orchestrateur, "creer_loader", lambda: _LoaderLocalFactice())
    monkeypatch.setattr(orchestrateur, "construire_inventaire", lambda *args, **kwargs: pd.DataFrame())
    monkeypatch.setattr(orchestrateur, "quota_restant", lambda: 45)
    monkeypatch.setattr(orchestrateur, "pipeline_fichier", _pipeline_factice)

    verdicts = orchestrateur.main()

    historiques = list((tmp_path / "history" / "runs").rglob("*.json"))
    assert len(verdicts) == 2
    assert len(historiques) == 1
    document = json.loads(historiques[0].read_text(encoding="utf-8"))
    assert document["summary"]["files_discovered"] == 2
    assert document["summary"]["files_processed"] == 2
    assert len(document["files"]) == 2


def test_main_s3_transmet_cache_local_et_cle_source(monkeypatch, tmp_path):
    cle_s3 = "input/LOAD_LOCK_202603/test.xlsx"
    info = _info()
    info.chemin = cle_s3
    df = pd.DataFrame({"A": [1]})
    chemin_cache = tmp_path / "cache" / "input" / "LOAD_LOCK_202603" / "test.xlsx"

    class _S3LoaderFactice:
        def __init__(self):
            self.uploads = []

        def lister_fichiers(self):
            return [info]

        def charger(self, cle):
            assert cle == cle_s3
            return df

        def chemin_cache(self, cle):
            assert cle == cle_s3
            return chemin_cache

        def uploader(self, chemin_local, cle):
            self.uploads.append((Path(chemin_local), cle))
            return True

    recus = {}

    def _pipeline_factice(info_recu, df_recu, **kwargs):
        recus.update(kwargs)
        assert info_recu is info
        assert df_recu is df
        return {
            "fichier": info.nom,
            "dossier_mois": info.dossier_mois,
            "verdict": "ACCEPTED",
            "pipeline_status": "COMPLETE",
            "anomalies": [],
        }

    loader = _S3LoaderFactice()
    monkeypatch.setattr(config, "STORAGE_MODE", "s3")
    monkeypatch.setattr(config, "RESULTATS_DIR", tmp_path)
    monkeypatch.setattr(orchestrateur, "creer_loader", lambda: loader)
    monkeypatch.setattr(orchestrateur, "construire_inventaire", lambda *args, **kwargs: pd.DataFrame())
    monkeypatch.setattr(orchestrateur, "quota_restant", lambda: 45)
    monkeypatch.setattr(orchestrateur, "pipeline_fichier", _pipeline_factice)

    verdicts = orchestrateur.main()

    assert len(verdicts) == 1
    assert recus["chemin_fichier_coherence"] == chemin_cache
    assert recus["checkpoint_source_path"] == cle_s3
    assert loader.uploads[:3] == [
        (
            tmp_path / "LOAD_LOCK_202603" / "rapport_test.html",
            "reports/LOAD_LOCK_202603/rapport_test.html",
        ),
        (
            tmp_path / "LOAD_LOCK_202603" / "verdict_test.json",
            "reports/LOAD_LOCK_202603/verdict_test.json",
        ),
        (tmp_path / "rapport_consolide.html", "reports/rapport_consolide.html"),
    ]
    assert len(loader.uploads) == 4
    uploads_history = [upload for upload in loader.uploads if upload[1].startswith("history/runs/")]
    assert len(uploads_history) == 1
    assert uploads_history[0][0].is_file()


def test_main_local_ne_tente_aucun_upload(monkeypatch, tmp_path):
    info = _info(nom="local.xlsx", dossier_mois="LOAD_LOCK_LOCAL")
    df = pd.DataFrame({"A": [1]})

    class _LocalLoaderFactice:
        def __init__(self):
            self.nb_uploads = 0

        def lister_fichiers(self):
            return [info]

        def charger(self, chemin):
            return df

        def uploader(self, *args):
            self.nb_uploads += 1
            raise AssertionError("Aucun upload ne doit être tenté en mode local")

    loader = _LocalLoaderFactice()
    monkeypatch.setattr(config, "STORAGE_MODE", "local")
    monkeypatch.setattr(config, "RESULTATS_DIR", tmp_path)
    monkeypatch.setattr(orchestrateur, "creer_loader", lambda: loader)
    monkeypatch.setattr(orchestrateur, "construire_inventaire", lambda *args, **kwargs: pd.DataFrame())
    monkeypatch.setattr(orchestrateur, "quota_restant", lambda: 45)
    monkeypatch.setattr(
        orchestrateur,
        "pipeline_fichier",
        lambda *args, **kwargs: {
            "fichier": info.nom,
            "dossier_mois": info.dossier_mois,
            "verdict": "REJECTED",
            "score": 100,
            "grade": "A",
            "anomalies": [],
        },
    )

    verdicts = orchestrateur.main()

    assert len(verdicts) == 1
    assert loader.nb_uploads == 0
    assert (tmp_path / "LOAD_LOCK_LOCAL" / "rapport_local.html").is_file()
    assert (tmp_path / "LOAD_LOCK_LOCAL" / "verdict_local.json").is_file()
    assert (tmp_path / "rapport_consolide.html").is_file()
    assert len(list((tmp_path / "history" / "runs").rglob("*.json"))) == 1


def test_echec_upload_s3_ne_supprime_pas_les_resultats_locaux(monkeypatch, tmp_path):
    info = _info()
    info.chemin = "input/LOAD_LOCK_202603/test.xlsx"
    df = pd.DataFrame({"A": [1]})
    chemin_cache = tmp_path / "cache" / "input" / "LOAD_LOCK_202603" / "test.xlsx"
    chemin_cache.parent.mkdir(parents=True)
    chemin_cache.write_bytes(b"source en cache")

    class _S3LoaderEnEchec:
        def __init__(self):
            self.uploads = []

        def lister_fichiers(self):
            return [info]

        def charger(self, cle):
            return df

        def chemin_cache(self, cle):
            return chemin_cache

        def uploader(self, chemin_local, cle):
            self.uploads.append((Path(chemin_local), cle))
            return False

    loader = _S3LoaderEnEchec()
    monkeypatch.setattr(config, "STORAGE_MODE", "s3")
    monkeypatch.setattr(config, "RESULTATS_DIR", tmp_path)
    monkeypatch.setattr(orchestrateur, "creer_loader", lambda: loader)
    monkeypatch.setattr(orchestrateur, "construire_inventaire", lambda *args, **kwargs: pd.DataFrame())
    monkeypatch.setattr(orchestrateur, "quota_restant", lambda: 45)
    monkeypatch.setattr(
        orchestrateur,
        "pipeline_fichier",
        lambda *args, **kwargs: {
            "fichier": info.nom,
            "dossier_mois": info.dossier_mois,
            "verdict": "REJECTED",
            "pipeline_status": "COMPLETE",
            "score": 100,
            "grade": "A",
            "anomalies": [],
        },
    )

    verdicts = orchestrateur.main()

    assert len(verdicts) == 1
    assert len(loader.uploads) == 5
    assert loader.uploads[2] == (
        chemin_cache,
        "quarantine/LOAD_LOCK_202603/test.xlsx",
    )
    assert chemin_cache.is_file()
    assert (tmp_path / "LOAD_LOCK_202603" / "rapport_test.html").is_file()
    assert (tmp_path / "LOAD_LOCK_202603" / "verdict_test.json").is_file()
    assert (tmp_path / "rapport_consolide.html").is_file()
    assert len(list((tmp_path / "history" / "runs").rglob("*.json"))) == 1


def test_quarantaine_rejected_copie_le_cache_sans_supprimer_input(monkeypatch, tmp_path):
    cle_input = "input/LOAD_LOCK_202603/fichier.xlsx"
    info = _info(nom="fichier.xlsx")
    info.chemin = cle_input
    chemin_cache = tmp_path / "cache" / cle_input
    chemin_cache.parent.mkdir(parents=True)
    chemin_cache.write_bytes(b"contenu source")

    class _LoaderFactice:
        def __init__(self):
            self.objets = {cle_input}
            self.uploads = []

        def chemin_cache(self, cle):
            assert cle == cle_input
            return chemin_cache

        def uploader(self, chemin_local, cle):
            self.uploads.append((Path(chemin_local), cle))
            self.objets.add(cle)
            return True

    loader = _LoaderFactice()
    monkeypatch.setattr(config, "STORAGE_MODE", "s3")

    succes = orchestrateur._mettre_en_quarantaine_s3(
        loader,
        info,
        {"verdict": "REJECTED", "pipeline_status": "COMPLETE"},
    )

    cle_quarantaine = "quarantine/LOAD_LOCK_202603/fichier.xlsx"
    assert succes is True
    assert loader.uploads == [(chemin_cache, cle_quarantaine)]
    assert cle_input in loader.objets
    assert cle_quarantaine in loader.objets
    assert chemin_cache.is_file()


def test_quarantaine_incomplete_est_copiee(monkeypatch, tmp_path):
    info = _info(nom="incomplet.xlsx")
    info.chemin = "input/LOAD_LOCK_202603/incomplet.xlsx"
    chemin_cache = tmp_path / "cache" / info.chemin
    chemin_cache.parent.mkdir(parents=True)
    chemin_cache.write_bytes(b"contenu source")
    uploads = []

    class _LoaderFactice:
        def chemin_cache(self, cle):
            return chemin_cache

        def uploader(self, chemin_local, cle):
            uploads.append((Path(chemin_local), cle))
            return True

    monkeypatch.setattr(config, "STORAGE_MODE", "s3")

    succes = orchestrateur._mettre_en_quarantaine_s3(
        _LoaderFactice(),
        info,
        {"verdict": None, "pipeline_status": "INCOMPLETE"},
    )

    assert succes is True
    assert uploads == [
        (chemin_cache, "quarantine/LOAD_LOCK_202603/incomplet.xlsx")
    ]


def test_exception_quarantaine_ne_remonte_pas_et_conserve_le_cache(monkeypatch, tmp_path):
    info = _info(nom="erreur.xlsx")
    info.chemin = "input/LOAD_LOCK_202603/erreur.xlsx"
    chemin_cache = tmp_path / "cache" / info.chemin
    chemin_cache.parent.mkdir(parents=True)
    chemin_cache.write_bytes(b"contenu source")

    class _LoaderExplosif:
        def chemin_cache(self, cle):
            return chemin_cache

        def uploader(self, chemin_local, cle):
            raise RuntimeError("panne S3 simulée")

    monkeypatch.setattr(config, "STORAGE_MODE", "s3")

    succes = orchestrateur._mettre_en_quarantaine_s3(
        _LoaderExplosif(),
        info,
        {"verdict": "REJECTED", "pipeline_status": "COMPLETE"},
    )

    assert succes is False
    assert chemin_cache.is_file()


def test_exception_upload_historique_ne_remonte_pas(monkeypatch, tmp_path):
    historique = tmp_path / "history.json"
    historique.write_text("{}", encoding="utf-8")

    class _LoaderExplosif:
        def uploader(self, chemin_local, cle):
            raise RuntimeError("panne S3 simulée")

    monkeypatch.setattr(config, "STORAGE_MODE", "s3")

    succes = orchestrateur._uploader_historique_s3(
        _LoaderExplosif(),
        historique,
        "history/runs/2026/10/08/run.json",
    )

    assert succes is False
    assert historique.is_file()


def test_finalisation_history_s3_retient_chemin_cle_et_succes_upload(monkeypatch, tmp_path):
    uploads = []

    class _LoaderFactice:
        def uploader(self, chemin_local, cle):
            uploads.append((chemin_local, cle))
            return True

    debut = datetime(2026, 10, 8, 10, 0, 0)
    monkeypatch.setattr(config, "STORAGE_MODE", "s3")
    monkeypatch.setattr(config, "HISTORY_DIR", tmp_path / "history")
    monkeypatch.setattr(config, "S3_HISTORY_PREFIX", "history/")

    persistance = orchestrateur._finaliser_historique_run(
        _LoaderFactice(),
        "20261008T100000Z_a1b2c3d4",
        debut,
        0,
        [],
    )

    assert persistance["local_path"].is_file()
    assert persistance["s3_key"] == "history/runs/2026/10/08/20261008T100000Z_a1b2c3d4.json"
    assert persistance["uploaded"] is True
    assert uploads == [(persistance["local_path"], persistance["s3_key"])]
