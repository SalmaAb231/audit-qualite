"""
Tests unitaires du module agents/agent_realisme.py.

Aucun test n'effectue d'appel réseau réel : un client LLM factice
(_ClientFactice) simule LLMClient.appeler() pour ne jamais consommer le
quota gratuit partagé du projet (config.RATE_LIMIT["max_requetes_jour"]).

Usage :
    python -m pytest tests/test_agent_realisme.py -v
"""

import pandas as pd

import config
from agents.agent_realisme import _decouper_en_chunks, analyser
from agents.llm_client import AppelMetadata

NOM_FICHIER_TEST = "test.xlsx"

CHAMPS_CONTRAT = {
    "fichier", "ligne", "colonnes", "type", "severite",
    "source", "valeur", "justification", "confiance",
}


def _anomalie_brute(ligne=0, type_="realisme", source="llm", colonnes=None, severite="high", confiance=0.8):
    return {
        "fichier": "peu importe.xlsx",
        "ligne": ligne,
        "colonnes": colonnes or ["Puissance"],
        "type": type_,
        "severite": severite,
        "source": source,
        "valeur": "900 ch",
        "justification": "Une Fiat Panda à 900 ch n'existe pas sur le marché réel.",
        "confiance": confiance,
    }


class _ClientFactice:
    """Client LLM factice : retourne les réponses fournies dans l'ordre, sans appel réseau."""

    def __init__(self, reponses):
        self.reponses = list(reponses)
        self.appels = []

    def appeler(self, **kwargs):
        self.appels.append(kwargs)
        return self.reponses.pop(0)


class _ClientExplosif:
    """Client LLM factice qui lève systématiquement une exception."""

    def appeler(self, **kwargs):
        raise RuntimeError("panne réseau simulée")


def _df_simple(n=3):
    return pd.DataFrame({"Modele": [f"Voiture{i}" for i in range(n)], "Puissance": [100 + i for i in range(n)]})


# ---------------------------------------------------------------------------
# _decouper_en_chunks
# ---------------------------------------------------------------------------


def test_decouper_en_chunks_taille_exacte():
    chunks = _decouper_en_chunks(_df_simple(6), 2)
    assert [len(c) for c in chunks] == [2, 2, 2]


def test_decouper_en_chunks_preserve_index_original():
    chunks = _decouper_en_chunks(_df_simple(6), 4)
    assert list(chunks[1].index) == [4, 5]


def test_decouper_en_chunks_taille_nulle_un_seul_chunk():
    chunks = _decouper_en_chunks(_df_simple(5), 0)
    assert len(chunks) == 1
    assert len(chunks[0]) == 5


def test_decouper_en_chunks_plus_petit_que_le_df():
    chunks = _decouper_en_chunks(_df_simple(3), 100)
    assert len(chunks) == 1
    assert len(chunks[0]) == 3


# ---------------------------------------------------------------------------
# analyser
# ---------------------------------------------------------------------------


def test_analyser_df_vide_aucun_appel():
    client = _ClientFactice(reponses=[])
    assert analyser(NOM_FICHIER_TEST, pd.DataFrame(), client=client) == []
    assert client.appels == []


def test_analyser_un_chunk_detecte_et_reecrit_type_source():
    reponse = {"anomalies": [_anomalie_brute(ligne=1, type_="coherence", source="llm")]}
    client = _ClientFactice(reponses=[(reponse, AppelMetadata(modele="m", succes=True))])

    anomalies = analyser(NOM_FICHIER_TEST, _df_simple(3), client=client)

    assert len(anomalies) == 1
    assert anomalies[0]["type"] == "realisme"
    assert anomalies[0]["source"] == "llm"
    assert anomalies[0]["fichier"] == NOM_FICHIER_TEST
    assert anomalies[0]["ligne"] == 1


def test_analyser_reponse_liste_directe():
    reponse = [_anomalie_brute()]
    client = _ClientFactice(reponses=[(reponse, AppelMetadata(modele="m", succes=True))])

    anomalies = analyser(NOM_FICHIER_TEST, _df_simple(2), client=client)
    assert len(anomalies) == 1


def test_analyser_aucune_anomalie_reponse_vide():
    reponse = {"anomalies": []}
    client = _ClientFactice(reponses=[(reponse, AppelMetadata(modele="m", succes=True))])
    assert analyser(NOM_FICHIER_TEST, _df_simple(2), client=client) == []


def test_analyser_appel_echoue_ne_bloque_pas():
    client = _ClientFactice(reponses=[(None, AppelMetadata(modele="m", succes=False, erreur="HTTP 429"))])
    assert analyser(NOM_FICHIER_TEST, _df_simple(2), client=client) == []


def test_analyser_appel_leve_une_exception_ne_bloque_pas():
    assert analyser(NOM_FICHIER_TEST, _df_simple(2), client=_ClientExplosif()) == []


def test_analyser_multi_chunks_agrege(monkeypatch):
    monkeypatch.setattr(config, "CHUNK_SIZE", 2)
    reponses = [
        ({"anomalies": [_anomalie_brute(ligne=0)]}, AppelMetadata(modele="m", succes=True)),
        ({"anomalies": [_anomalie_brute(ligne=2)]}, AppelMetadata(modele="m", succes=True)),
        ({"anomalies": []}, AppelMetadata(modele="m", succes=True)),
    ]
    client = _ClientFactice(reponses=reponses)

    anomalies = analyser(NOM_FICHIER_TEST, _df_simple(5), client=client)

    assert len(client.appels) == 3
    assert len(anomalies) == 2
    assert {a["ligne"] for a in anomalies} == {0, 2}


def test_analyser_respecte_max_lignes_par_fichier(monkeypatch):
    # Sans troncature, 10 lignes avec CHUNK_SIZE=4 -> 3 chunks. Avec
    # MAX_LIGNES_PAR_FICHIER=4, un seul chunk est envoyé : la troncature a
    # bien eu lieu avant le découpage.
    monkeypatch.setattr(config, "MAX_LIGNES_PAR_FICHIER", 4)
    monkeypatch.setattr(config, "CHUNK_SIZE", 4)
    client = _ClientFactice(reponses=[({"anomalies": []}, AppelMetadata(modele="m", succes=True))])

    analyser(NOM_FICHIER_TEST, _df_simple(10), client=client)

    assert len(client.appels) == 1


def test_analyser_appelle_avec_les_bons_parametres_config():
    client = _ClientFactice(reponses=[({"anomalies": []}, AppelMetadata(modele="m", succes=True))])
    analyser(NOM_FICHIER_TEST, _df_simple(2), client=client)

    appel = client.appels[0]
    assert appel["model"] == config.MODELES["realisme"]
    assert appel["temperature"] == config.TEMPERATURES["realisme"]
    assert appel["max_tokens"] == config.MAX_TOKENS["realisme"]
    assert appel["json_attendu"] is True


def test_analyser_rejette_anomalie_hors_contrat():
    reponse_invalide = {"anomalies": [{"colonnes": ["Puissance"]}]}  # champs requis manquants
    client = _ClientFactice(reponses=[(reponse_invalide, AppelMetadata(modele="m", succes=True))])
    assert analyser(NOM_FICHIER_TEST, _df_simple(2), client=client) == []


# ---------------------------------------------------------------------------
# Conformité au contrat commun (docs/contrat_anomalie.md)
# ---------------------------------------------------------------------------


def test_toutes_les_anomalies_respectent_le_contrat():
    reponse = {"anomalies": [_anomalie_brute(ligne=0), _anomalie_brute(ligne=1, severite="low", confiance=0.4)]}
    client = _ClientFactice(reponses=[(reponse, AppelMetadata(modele="m", succes=True))])

    anomalies = analyser(NOM_FICHIER_TEST, _df_simple(2), client=client)
    assert anomalies

    for anomalie in anomalies:
        assert set(anomalie.keys()) == CHAMPS_CONTRAT
        assert anomalie["type"] == "realisme"
        assert anomalie["source"] == "llm"
        assert isinstance(anomalie["ligne"], int)
        assert isinstance(anomalie["colonnes"], list)
        assert anomalie["severite"] in {"critical", "high", "medium", "low"}
        assert isinstance(anomalie["confiance"], float)
        assert 0.0 <= anomalie["confiance"] <= 1.0
