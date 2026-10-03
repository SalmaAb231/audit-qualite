"""
Tests unitaires du module agents/agent_realisme.py.

Aucun test n'effectue d'appel réseau réel : un client LLM factice
(_ClientFactice) simule LLMClient.appeler() pour ne jamais consommer le
quota gratuit partagé du projet (config.RATE_LIMIT["max_requetes_jour"]).
config.RECHERCHE_WEB_ACTIVEE est désactivée par défaut (fixture autouse) pour
qu'aucun test n'appelle réellement DuckDuckGo ni ne consomme un deuxième
appel LLM de vérification par accident ; les tests dédiés à cette fonction
la réactivent explicitement et simulent `DDGS`.

Usage :
    python -m pytest tests/test_agent_realisme.py -v
"""

import pandas as pd
import pytest

import config
from agents.agent_realisme import (
    _construire_requete_recherche,
    _decouper_en_chunks,
    _rechercher_web,
    _verifier_avec_recherche_web,
    analyser,
    analyser_avec_statut,
)
from agents.llm_client import AppelMetadata

NOM_FICHIER_TEST = "test.xlsx"


@pytest.fixture(autouse=True)
def _desactiver_recherche_web_par_defaut(monkeypatch):
    monkeypatch.setattr(config, "RECHERCHE_WEB_ACTIVEE", False)

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


def test_analyser_avec_statut_succes_sans_anomalie_est_complet():
    client = _ClientFactice(reponses=[({"anomalies": []}, AppelMetadata(modele="m", succes=True))])

    resultat = analyser_avec_statut(NOM_FICHIER_TEST, _df_simple(2), client=client)

    assert resultat["anomalies"] == []
    assert resultat["status"] == "SUCCESS"
    assert resultat["analyse_complete"] is True
    assert resultat["chunks_success"] == 1
    assert resultat["chunks_failed"] == 0


def test_analyser_avec_statut_echec_llm_nest_pas_un_succes_vide():
    client = _ClientFactice(reponses=[(None, AppelMetadata(modele="m", succes=False, erreur="timeout"))])

    resultat = analyser_avec_statut(NOM_FICHIER_TEST, _df_simple(2), client=client)

    assert resultat["anomalies"] == []
    assert resultat["status"] == "FAILED"
    assert resultat["analyse_complete"] is False
    assert resultat["chunks_failed"] == 1
    assert resultat["error"] == "timeout"


def test_analyser_avec_statut_exception_llm_est_failed():
    resultat = analyser_avec_statut(NOM_FICHIER_TEST, _df_simple(2), client=_ClientExplosif())

    assert resultat["status"] == "FAILED"
    assert resultat["analyse_complete"] is False
    assert "panne réseau simulée" in resultat["error"]


def test_analyser_avec_statut_multi_chunks_partiel(monkeypatch):
    monkeypatch.setattr(config, "CHUNK_SIZE", 1)
    client = _ClientFactice(reponses=[
        ({"anomalies": []}, AppelMetadata(modele="m", succes=True)),
        ({"anomalies": []}, AppelMetadata(modele="m", succes=True)),
        (None, AppelMetadata(modele="m", succes=False, erreur="quota exhausted")),
    ])

    resultat = analyser_avec_statut(NOM_FICHIER_TEST, _df_simple(3), client=client)

    assert resultat["status"] == "PARTIAL"
    assert resultat["analyse_complete"] is False
    assert resultat["chunks_total"] == 3
    assert resultat["chunks_success"] == 2
    assert resultat["chunks_failed"] == 1


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


def test_analyser_sous_le_seuil_geant_ne_tronque_pas(monkeypatch):
    # Règle alignée sur agents/agent_coherence.py : sous SEUIL_FICHIER_GEANT,
    # AUCUNE troncature, même si le fichier dépasse MAX_LIGNES_PAR_FICHIER.
    # 10 lignes, CHUNK_SIZE=4 -> 3 chunks, aucune ligne perdue.
    monkeypatch.setattr(config, "MAX_LIGNES_PAR_FICHIER", 4)
    monkeypatch.setattr(config, "CHUNK_SIZE", 4)
    client = _ClientFactice(reponses=[
        ({"anomalies": []}, AppelMetadata(modele="m", succes=True)),
        ({"anomalies": []}, AppelMetadata(modele="m", succes=True)),
        ({"anomalies": []}, AppelMetadata(modele="m", succes=True)),
    ])

    analyser(NOM_FICHIER_TEST, _df_simple(10), client=client)

    assert len(client.appels) == 3


def test_analyser_echantillonne_les_fichiers_geants(monkeypatch):
    # Au-dessus de SEUIL_FICHIER_GEANT, échantillonné à MAX_LIGNES_PAR_FICHIER
    # lignes avant le découpage en chunks : 10 lignes -> 4 -> 1 seul chunk.
    monkeypatch.setattr(config, "SEUIL_FICHIER_GEANT", 4)
    monkeypatch.setattr(config, "MAX_LIGNES_PAR_FICHIER", 4)
    monkeypatch.setattr(config, "CHUNK_SIZE", 4)
    client = _ClientFactice(reponses=[({"anomalies": []}, AppelMetadata(modele="m", succes=True))])

    analyser(NOM_FICHIER_TEST, _df_simple(10), client=client)

    assert len(client.appels) == 1


def test_analyser_echantillon_fichier_geant_reproductible(monkeypatch):
    # Même seed que l'échantillonnage réel (SEED_ECHANTILLONNAGE) : deux
    # analyses du même fichier géant doivent envoyer les mêmes lignes.
    monkeypatch.setattr(config, "SEUIL_FICHIER_GEANT", 4)
    monkeypatch.setattr(config, "MAX_LIGNES_PAR_FICHIER", 4)
    monkeypatch.setattr(config, "CHUNK_SIZE", 100)

    prompts = []
    for _ in range(2):
        client = _ClientFactice(reponses=[({"anomalies": []}, AppelMetadata(modele="m", succes=True))])
        analyser(NOM_FICHIER_TEST, _df_simple(20), client=client)
        prompts.append(client.appels[0]["user_prompt"])

    assert prompts[0] == prompts[1]


def test_analyser_sans_echantillonnage_preserve_toutes_les_lignes_recues(monkeypatch):
    import agents.agent_realisme as agent_realisme_module

    monkeypatch.setattr(config, "SEUIL_FICHIER_GEANT", 4)
    monkeypatch.setattr(config, "MAX_LIGNES_PAR_FICHIER", 4)
    monkeypatch.setattr(config, "CHUNK_SIZE", 3)
    df = _df_simple(10)
    df.index = range(100, 110)
    index_analyses = []

    def _chunk_factice(nom_fichier, chunk, client, num_chunk, nb_chunks):
        index_analyses.extend(chunk.index.tolist())
        return {"anomalies": [], "succes": True, "error": None, "nb_lignes": len(chunk)}

    monkeypatch.setattr(agent_realisme_module, "_analyser_chunk", _chunk_factice)

    resultat = analyser_avec_statut(
        NOM_FICHIER_TEST, df, client=_ClientFactice([]), echantillonner=False
    )

    assert index_analyses == list(df.index)
    assert resultat["nb_lignes_recues"] == resultat["nb_lignes_analysees"] == len(df)
    assert resultat["status"] == "SUCCESS"


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


# ---------------------------------------------------------------------------
# Vérification web (_construire_requete_recherche, _rechercher_web,
# _verifier_avec_recherche_web)
# ---------------------------------------------------------------------------


class _DDGSFactice:
    """Simule ddgs.DDGS() : retourne des résultats fixes ou lève, sans réseau."""

    def __init__(self, resultats=None, leve=False):
        self.resultats = resultats if resultats is not None else [{"title": "T", "body": "B"}]
        self.leve = leve
        self.requetes = []

    def text(self, requete, max_results=3):
        self.requetes.append(requete)
        if self.leve:
            raise RuntimeError("panne réseau ddgs simulée")
        return self.resultats


def test_construire_requete_recherche_combine_valeur_et_colonnes():
    anomalie = _anomalie_brute(colonnes=["Model", "version"])
    anomalie["valeur"] = "EX40"
    assert _construire_requete_recherche(anomalie) == "EX40 Model version"


def test_rechercher_web_retourne_vide_sur_echec(monkeypatch):
    import agents.agent_realisme as agent_realisme_module
    monkeypatch.setattr(agent_realisme_module, "DDGS", lambda: _DDGSFactice(leve=True))
    assert _rechercher_web("peu importe") == ""


def test_rechercher_web_retourne_vide_si_requete_vide():
    assert _rechercher_web("   ") == ""


def test_rechercher_web_formate_les_resultats(monkeypatch):
    import agents.agent_realisme as agent_realisme_module
    monkeypatch.setattr(
        agent_realisme_module, "DDGS",
        lambda: _DDGSFactice(resultats=[{"title": "Volvo EX40", "body": "modèle électrique 2026"}]),
    )
    resultat = _rechercher_web("Volvo EX40")
    assert "Volvo EX40" in resultat
    assert "modèle électrique 2026" in resultat


def test_verifier_avec_recherche_web_desactivee_ne_fait_rien(monkeypatch):
    monkeypatch.setattr(config, "RECHERCHE_WEB_ACTIVEE", False)
    anomalies = [_anomalie_brute()]
    client = _ClientExplosif()  # preuve qu'il n'est jamais appelé
    assert _verifier_avec_recherche_web(NOM_FICHIER_TEST, anomalies, client) == anomalies


def test_verifier_avec_recherche_web_sans_resultat_conserve_les_anomalies(monkeypatch):
    import agents.agent_realisme as agent_realisme_module
    monkeypatch.setattr(config, "RECHERCHE_WEB_ACTIVEE", True)
    monkeypatch.setattr(agent_realisme_module, "PAUSE_ENTRE_RECHERCHES_SECONDES", 0.0)
    monkeypatch.setattr(agent_realisme_module, "DDGS", lambda: _DDGSFactice(resultats=[]))

    anomalies = [_anomalie_brute()]
    client = _ClientExplosif()  # aucun résultat -> jamais appelé

    assert _verifier_avec_recherche_web(NOM_FICHIER_TEST, anomalies, client) == anomalies


def test_verifier_avec_recherche_web_retire_une_anomalie_contredite(monkeypatch):
    import agents.agent_realisme as agent_realisme_module
    monkeypatch.setattr(config, "RECHERCHE_WEB_ACTIVEE", True)
    monkeypatch.setattr(agent_realisme_module, "PAUSE_ENTRE_RECHERCHES_SECONDES", 0.0)
    monkeypatch.setattr(agent_realisme_module, "DDGS", lambda: _DDGSFactice())

    anomalies = [_anomalie_brute(ligne=0), _anomalie_brute(ligne=1)]
    reponse_verif = {"verifications": [
        {"index": 0, "confirmee": False, "raison": "Le web montre que ce modèle existe."},
        {"index": 1, "confirmee": True, "raison": "Toujours invraisemblable."},
    ]}
    client = _ClientFactice(reponses=[(reponse_verif, AppelMetadata(modele="m", succes=True))])

    resultat = _verifier_avec_recherche_web(NOM_FICHIER_TEST, anomalies, client)

    assert len(resultat) == 1
    assert resultat[0]["ligne"] == 1
    assert len(client.appels) == 1


def test_verifier_avec_recherche_web_echec_llm_conserve_les_anomalies(monkeypatch):
    import agents.agent_realisme as agent_realisme_module
    monkeypatch.setattr(config, "RECHERCHE_WEB_ACTIVEE", True)
    monkeypatch.setattr(agent_realisme_module, "PAUSE_ENTRE_RECHERCHES_SECONDES", 0.0)
    monkeypatch.setattr(agent_realisme_module, "DDGS", lambda: _DDGSFactice())

    anomalies = [_anomalie_brute()]
    assert _verifier_avec_recherche_web(NOM_FICHIER_TEST, anomalies, _ClientExplosif()) == anomalies


def test_analyser_appelle_la_verification_web_si_activee(monkeypatch):
    import agents.agent_realisme as agent_realisme_module
    monkeypatch.setattr(config, "RECHERCHE_WEB_ACTIVEE", True)
    monkeypatch.setattr(agent_realisme_module, "PAUSE_ENTRE_RECHERCHES_SECONDES", 0.0)
    monkeypatch.setattr(agent_realisme_module, "DDGS", lambda: _DDGSFactice())

    reponse_analyse = ({"anomalies": [_anomalie_brute(ligne=0)]}, AppelMetadata(modele="m", succes=True))
    reponse_verif = ({"verifications": [{"index": 0, "confirmee": True, "raison": "ok"}]}, AppelMetadata(modele="m", succes=True))
    client = _ClientFactice(reponses=[reponse_analyse, reponse_verif])

    anomalies = analyser(NOM_FICHIER_TEST, _df_simple(2), client=client)

    assert len(client.appels) == 2  # 1 analyse + 1 vérification
    assert len(anomalies) == 1
