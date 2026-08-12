"""
Tests unitaires de orchestrateur.py.

Aucun appel réseau réel : un client LLM factice (repris du même motif que
tests/test_agent_realisme.py) simule agents.llm_client.LLMClient.appeler().
Les tests qui touchent le disque (résultats, rapports) utilisent tmp_path.

Usage :
    python -m pytest tests/test_orchestrateur.py -v
"""

from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

import config
import orchestrateur
from agents.llm_client import AppelMetadata
from ingestion.local_loader import FichierInfo

NOM_FICHIER_TEST = "test.xlsx"


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

    verdict = orchestrateur.pipeline_fichier(_info(), df_vide, client=_ClientExplosif())

    assert verdict["verdict"] == "REJECTED"
    assert verdict["dossier_mois"] == "LOAD_LOCK_202603"
    assert verdict["anomalies"]


def test_pipeline_fichier_flux_complet_integre_les_anomalies_realisme(monkeypatch):
    monkeypatch.setattr(config, "TAUX_ECHANTILLON_CONTROLE", 0.0)  # déterministe : uniquement les lignes suspectes explicites

    # Une ligne dupliquée (index 2) donne une anomalie structure avec ligne=2 != 0,
    # donc _lignes_suspectes sélectionne bien au moins cette ligne : garantit que
    # analyser_realisme reçoit un lot non vide et appelle le client factice.
    df = pd.DataFrame({"Modele": ["A", "B", "A", "C", "D"], "Prix": [1, 2, 1, 3, 4]})

    reponse = ({"anomalies": [_anomalie_realisme(ligne=2)]}, AppelMetadata(modele="m", succes=True))
    client = _ClientFactice(reponses=[reponse])

    verdict = orchestrateur.pipeline_fichier(_info(), df, client=client, random_state=0)

    assert len(client.appels) == 1
    assert verdict["anomalies_par_type"]["realisme"] == 1
    assert verdict["dossier_mois"] == "LOAD_LOCK_202603"
    assert any(a["type"] == "realisme" for a in verdict["anomalies"])


# ---------------------------------------------------------------------------
# main() : garde-fou de quota et écriture des résultats
# ---------------------------------------------------------------------------


def test_main_arrete_proprement_si_quota_deja_epuise(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "RESULTATS_DIR", tmp_path)
    monkeypatch.setattr(orchestrateur, "quota_restant", lambda: 0)
    monkeypatch.setattr(orchestrateur.LocalLoader, "lister_fichiers", lambda self: [_info()])
    monkeypatch.setattr(
        orchestrateur, "pipeline_fichier",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("pipeline_fichier n'aurait pas dû être appelé")),
    )

    verdicts = orchestrateur.main()

    assert verdicts == []
    assert not list(tmp_path.iterdir())


def test_main_traite_un_fichier_et_ecrit_les_resultats(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "RESULTATS_DIR", tmp_path)
    monkeypatch.setattr(config, "TAUX_ECHANTILLON_CONTROLE", 0.0)
    monkeypatch.setattr(orchestrateur, "quota_restant", lambda: 45)

    info = _info(nom="petit.xlsx", dossier_mois="LOAD_LOCK_202604")
    df = pd.DataFrame({"A": [1, 2, 3]})

    monkeypatch.setattr(orchestrateur.LocalLoader, "lister_fichiers", lambda self: [info])
    monkeypatch.setattr(orchestrateur.LocalLoader, "charger", lambda self, chemin: df)

    verdicts = orchestrateur.main(client=_ClientFactice(reponses=[]))

    assert len(verdicts) == 1
    dossier_sortie = tmp_path / "LOAD_LOCK_202604"
    assert (dossier_sortie / "verdict_petit.json").exists()
    assert (dossier_sortie / "rapport_petit.html").exists()
    assert (tmp_path / "rapport_consolide.html").exists()


def test_main_ignore_un_fichier_dont_le_chargement_echoue(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "RESULTATS_DIR", tmp_path)
    monkeypatch.setattr(orchestrateur, "quota_restant", lambda: 45)
    monkeypatch.setattr(orchestrateur.LocalLoader, "lister_fichiers", lambda self: [_info()])
    monkeypatch.setattr(orchestrateur.LocalLoader, "charger", lambda self, chemin: None)

    verdicts = orchestrateur.main()

    assert verdicts == []


def test_main_continue_apres_lechec_dun_fichier(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "RESULTATS_DIR", tmp_path)
    monkeypatch.setattr(orchestrateur, "quota_restant", lambda: 45)

    infos = [_info(nom="casse.xlsx"), _info(nom="ok.xlsx")]
    monkeypatch.setattr(orchestrateur.LocalLoader, "lister_fichiers", lambda self: infos)
    monkeypatch.setattr(orchestrateur.LocalLoader, "charger", lambda self, chemin: pd.DataFrame({"A": [1, 2, 3]}))

    appels = {"n": 0}

    def _pipeline_qui_casse_une_fois(info, df, client=None, random_state=None):
        appels["n"] += 1
        if info.nom == "casse.xlsx":
            raise RuntimeError("panne simulée")
        return {
            "fichier": info.nom, "nb_anomalies_total": 0, "score": 1.0, "verdict": "ACCEPTED", "grade": "A",
            "justification": "ok", "anomalies_par_type": {}, "anomalies_par_severite": {},
            "dossier_mois": info.dossier_mois, "anomalies": [],
        }

    monkeypatch.setattr(orchestrateur, "pipeline_fichier", _pipeline_qui_casse_une_fois)

    verdicts = orchestrateur.main()

    assert appels["n"] == 2
    assert len(verdicts) == 1
    assert verdicts[0]["fichier"] == "ok.xlsx"
