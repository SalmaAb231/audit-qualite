"""Tests locaux de l'identité et de la reprise des checkpoints Cohérence."""

import json
from pathlib import Path

import pandas as pd
import pytest

import agents.agent_coherence as module_coherence
import config
from agents.agent_coherence import AgentCoherence, construire_identite_checkpoint
from agents.llm_client import AppelMetadata


class _ClientFactice:
    def __init__(self, reponses):
        self.reponses = list(reponses)
        self.appels = []

    def appeler(self, **kwargs):
        self.appels.append(kwargs)
        if not self.reponses:
            raise AssertionError("Aucun nouvel appel LLM ne devait avoir lieu.")
        return self.reponses.pop(0)


@pytest.fixture(autouse=True)
def _isoler_checkpoints(tmp_path, monkeypatch):
    dossier_resultats = tmp_path / "resultats" / "coherence"
    monkeypatch.setattr(module_coherence, "DOSSIER_RESULTATS", dossier_resultats)
    monkeypatch.setattr(module_coherence, "DOSSIER_CHECKPOINTS", dossier_resultats / "checkpoints")
    monkeypatch.setattr(module_coherence, "PAUSE_ENTRE_CHUNKS_SECONDES", 0.0)
    monkeypatch.setattr(config, "CHUNK_SIZE", 500)


def _creer_fichier(chemin: Path, contenu: bytes) -> Path:
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_bytes(contenu)
    return chemin


def _reponse_vide(succes=True, erreur=None):
    reponse = {"anomalies": []} if succes else None
    return reponse, AppelMetadata(modele="modele-factice", succes=succes, erreur=erreur)


def _analyser(chemin: Path, client: _ClientFactice):
    df = pd.DataFrame({"A": [1, 2, 3]})
    return AgentCoherence(llm_client=client).analyser_fichier(
        chemin,
        df=df,
        anomalies_structure=[],
    )


def test_meme_nom_dossiers_differents_produit_deux_checkpoints(tmp_path):
    fichier_mois_1 = _creer_fichier(tmp_path / "month1" / "ventes.xlsx", b"contenu mois 1")
    fichier_mois_2 = _creer_fichier(tmp_path / "month2" / "ventes.xlsx", b"contenu mois 2")

    identite_1 = construire_identite_checkpoint(fichier_mois_1)
    identite_2 = construire_identite_checkpoint(fichier_mois_2)

    assert identite_1["source_path"] != identite_2["source_path"]
    assert identite_1["content_hash"] != identite_2["content_hash"]
    assert identite_1["checkpoint_id"] != identite_2["checkpoint_id"]
    assert not Path(identite_1["source_path"]).is_absolute()
    assert module_coherence._chemin_checkpoint(identite_1["checkpoint_id"]) != module_coherence._chemin_checkpoint(
        identite_2["checkpoint_id"]
    )


def test_meme_nom_et_meme_contenu_dossiers_differents_garde_deux_identites(tmp_path):
    fichier_mois_1 = _creer_fichier(tmp_path / "month1" / "ventes.xlsx", b"contenu identique")
    fichier_mois_2 = _creer_fichier(tmp_path / "month2" / "ventes.xlsx", b"contenu identique")

    identite_1 = construire_identite_checkpoint(fichier_mois_1)
    identite_2 = construire_identite_checkpoint(fichier_mois_2)

    assert identite_1["content_hash"] == identite_2["content_hash"]
    assert identite_1["source_path"] != identite_2["source_path"]
    assert identite_1["checkpoint_id"] != identite_2["checkpoint_id"]


def test_meme_chemin_contenu_modifie_invalide_ancien_checkpoint(tmp_path):
    fichier = _creer_fichier(tmp_path / "month1" / "ventes.xlsx", b"ancienne version")
    ancienne_identite = construire_identite_checkpoint(fichier)
    ancien_checkpoint = {
        **ancienne_identite,
        "chunks": {"0": {"metadata": {"succes": True}, "anomalies": [], "nb_lignes": 3}},
    }
    module_coherence._sauvegarder_checkpoint(ancien_checkpoint)

    fichier.write_bytes(b"nouvelle version")
    nouvelle_identite = construire_identite_checkpoint(fichier)

    assert nouvelle_identite["source_path"] == ancienne_identite["source_path"]
    assert nouvelle_identite["content_hash"] != ancienne_identite["content_hash"]
    assert nouvelle_identite["checkpoint_id"] != ancienne_identite["checkpoint_id"]
    assert module_coherence._charger_checkpoint(nouvelle_identite)["chunks"] == {}


def test_meme_chemin_meme_contenu_retrouve_la_meme_identite(tmp_path):
    fichier = _creer_fichier(tmp_path / "month1" / "ventes.xlsx", b"contenu stable")

    identite_1 = construire_identite_checkpoint(fichier)
    identite_2 = construire_identite_checkpoint(fichier)

    assert identite_1 == identite_2


def test_meme_fichier_version_checkpoint_differente_change_identite(tmp_path):
    fichier = _creer_fichier(tmp_path / "month1" / "ventes.xlsx", b"contenu stable")

    identite_v1_a = construire_identite_checkpoint(fichier, checkpoint_version="coherence-v1")
    identite_v1_b = construire_identite_checkpoint(fichier, checkpoint_version="coherence-v1")
    identite_v2 = construire_identite_checkpoint(fichier, checkpoint_version="coherence-v2")

    assert identite_v1_a == identite_v1_b
    assert identite_v1_a["source_path"] == identite_v2["source_path"]
    assert identite_v1_a["content_hash"] == identite_v2["content_hash"]
    assert identite_v1_a["checkpoint_version"] == "coherence-v1"
    assert identite_v2["checkpoint_version"] == "coherence-v2"
    assert identite_v1_a["checkpoint_id"] != identite_v2["checkpoint_id"]


def test_chunk_reussi_est_reutilise_sans_nouvel_appel(tmp_path):
    fichier = _creer_fichier(tmp_path / "month1" / "ventes.xlsx", b"contenu stable")
    premier_client = _ClientFactice([_reponse_vide(succes=True)])
    second_client = _ClientFactice([])

    premier_resultat = _analyser(fichier, premier_client)
    second_resultat = _analyser(fichier, second_client)

    assert len(premier_client.appels) == 1
    assert second_client.appels == []
    assert premier_resultat["status"] == second_resultat["status"] == "SUCCESS"
    assert premier_resultat["analyse_complete"] is True

    identite = construire_identite_checkpoint(fichier)
    checkpoint = json.loads(
        module_coherence._chemin_checkpoint(identite["checkpoint_id"]).read_text(encoding="utf-8")
    )
    assert checkpoint["source_path"] == identite["source_path"]
    assert checkpoint["content_hash"] == identite["content_hash"]
    assert checkpoint["checkpoint_version"] == module_coherence.CHECKPOINT_VERSION
    assert checkpoint["checkpoint_id"] == identite["checkpoint_id"]
    assert checkpoint["chunks"]["0"]["metadata"]["succes"] is True


def test_chunk_echoue_est_retente_au_lancement_suivant(tmp_path):
    fichier = _creer_fichier(tmp_path / "month1" / "ventes.xlsx", b"contenu stable")
    premier_client = _ClientFactice([_reponse_vide(succes=False, erreur="HTTP 429")])
    second_client = _ClientFactice([_reponse_vide(succes=True)])

    premier_resultat = _analyser(fichier, premier_client)
    second_resultat = _analyser(fichier, second_client)

    assert len(premier_client.appels) == 1
    assert premier_resultat["metadata"]["nb_chunks_echec"] == 1
    assert len(second_client.appels) == 1
    assert second_resultat["metadata"]["nb_chunks_echec"] == 0

    identite = construire_identite_checkpoint(fichier)
    checkpoint = module_coherence._charger_checkpoint(identite)
    assert checkpoint["chunks"]["0"]["metadata"]["succes"] is True


def test_analyse_multi_chunks_partielle_remonte_les_compteurs(tmp_path, monkeypatch):
    fichier = _creer_fichier(tmp_path / "month1" / "ventes.xlsx", b"contenu stable")
    monkeypatch.setattr(config, "CHUNK_SIZE", 1)
    client = _ClientFactice([
        _reponse_vide(succes=True),
        _reponse_vide(succes=True),
        _reponse_vide(succes=False, erreur="quota exhausted"),
    ])

    resultat = _analyser(fichier, client)

    assert resultat["status"] == "PARTIAL"
    assert resultat["analyse_complete"] is False
    assert resultat["metadata"]["nb_chunks"] == 3
    assert resultat["metadata"]["nb_chunks_succes"] == 2
    assert resultat["metadata"]["nb_chunks_echec"] == 1
    assert "quota exhausted" in resultat["error"]
