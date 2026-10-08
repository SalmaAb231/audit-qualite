"""Tests du quota OpenRouter local par fingerprint, sans appel réseau réel."""

import hashlib
import json
from datetime import date

import pytest

import config
import agents.llm_client as llm_client_module
from agents.llm_client import (
    ERREUR_JSON_INVALIDE,
    ERREUR_QUOTA_EPUISE,
    ERREUR_REPONSE_INVALIDE,
    ERREUR_RESEAU,
    ERREUR_UPSTREAM_RATE_LIMIT,
    LLMClient,
    _chemin_compteur_quotidien,
    _cle_cache,
    _ecrire_cache,
    _fingerprint_api_key,
    _incrementer_compteur_quotidien,
    _lire_compteur_quotidien,
    quota_restant,
)


KEY_A = "test-key-A"
KEY_B = "test-key-B"


@pytest.fixture(autouse=True)
def _isoler_cache_et_quota(monkeypatch, tmp_path):
    cache_dir = tmp_path / "cache_llm"
    monkeypatch.setattr(config, "CACHE_LLM_DIR", cache_dir)
    monkeypatch.setattr(config, "QUOTA_LLM_DIR", cache_dir / "quota")
    monkeypatch.setitem(config.RATE_LIMIT, "max_requetes_jour", 45)
    monkeypatch.setitem(config.RATE_LIMIT, "pause_entre_appels", 0)
    monkeypatch.setattr(LLMClient, "_dernier_appel_ts", None)
    monkeypatch.setattr(LLMClient, "_echecs_upstream", {})
    monkeypatch.setattr(LLMClient, "_circuits_ouverts_jusqua", {})
    monkeypatch.setattr(config, "MAX_LLM_ATTEMPTS_PER_CHUNK", 4)
    monkeypatch.setattr(config, "FREE_TIER_RETRY_BACKOFF_SECONDS", 0)


def _ecrire_compteur(api_key: str, compteur: int, jour: str | None = None):
    chemin = _chemin_compteur_quotidien(api_key)
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_text(
        json.dumps({"date": jour or date.today().isoformat(), "compteur": compteur}),
        encoding="utf-8",
    )
    return chemin


def _reponse_http_ok():
    class _Response:
        status_code = 200
        text = '{"choices": [{"message": {"content": "ok"}}]}'

        def json(self):
            return {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }

    return _Response()


def _response(status_code, text, body=None):
    class _Response:
        def __init__(self):
            self.status_code = status_code
            self.text = text

        def json(self):
            if isinstance(body, Exception):
                raise body
            return body

    return _Response()


def test_fingerprint_est_stable_distinct_et_ne_contient_pas_la_cle():
    fingerprint_a = _fingerprint_api_key(KEY_A)

    assert fingerprint_a == hashlib.sha256(KEY_A.encode("utf-8")).hexdigest()[:12]
    assert fingerprint_a == _fingerprint_api_key(KEY_A)
    assert fingerprint_a != _fingerprint_api_key(KEY_B)
    assert len(fingerprint_a) == 12
    assert set(fingerprint_a) <= set("0123456789abcdef")
    assert KEY_A not in fingerprint_a


def test_compteurs_de_deux_cles_sont_independants():
    _ecrire_compteur(KEY_A, 10)

    assert _lire_compteur_quotidien(KEY_A) == 10
    assert _lire_compteur_quotidien(KEY_B) == 0
    assert quota_restant(KEY_A) == 35
    assert quota_restant(KEY_B) == 45

    _incrementer_compteur_quotidien(KEY_A)

    assert _lire_compteur_quotidien(KEY_A) == 11
    assert _lire_compteur_quotidien(KEY_B) == 0


def test_nouveau_jour_fichier_absent_et_json_corrompu_valent_zero():
    _ecrire_compteur(KEY_A, 44, jour="1900-01-01")
    assert _lire_compteur_quotidien(KEY_A) == 0
    assert _lire_compteur_quotidien(KEY_B) == 0

    chemin_b = _chemin_compteur_quotidien(KEY_B)
    chemin_b.parent.mkdir(parents=True, exist_ok=True)
    chemin_b.write_text("{json-invalide", encoding="utf-8")
    assert _lire_compteur_quotidien(KEY_B) == 0


def test_reponse_cachee_ne_consomme_pas_le_quota(monkeypatch):
    cle_cache = _cle_cache("system", "user", 0.0, 10, True)
    _ecrire_cache(cle_cache, "model-test", '{"ok": true}', 1, 1, None)
    monkeypatch.setattr(
        llm_client_module.requests,
        "post",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("aucun réseau attendu")),
    )

    reponse, metadata = LLMClient(api_key=KEY_A).appeler(
        "system",
        "user",
        model="model-test",
        temperature=0.0,
        max_tokens=10,
        json_attendu=True,
    )

    assert reponse == {"ok": True}
    assert metadata.depuis_cache is True
    assert _lire_compteur_quotidien(KEY_A) == 0


def test_tentative_reseau_increment_uniquement_la_cle_du_client(monkeypatch):
    monkeypatch.setattr(llm_client_module.requests, "post", lambda *args, **kwargs: _reponse_http_ok())
    client = LLMClient(api_key=KEY_A)

    resultat = client._tenter("model-test", "system", "user", 0.0, 10, False)

    assert resultat["succes"] is True
    assert _lire_compteur_quotidien(KEY_A) == 1
    assert _lire_compteur_quotidien(KEY_B) == 0


def test_deux_instances_utilisent_deux_fichiers_compteur(monkeypatch):
    monkeypatch.setattr(llm_client_module.requests, "post", lambda *args, **kwargs: _reponse_http_ok())
    client_a = LLMClient(api_key=KEY_A)
    client_b = LLMClient(api_key=KEY_B)

    client_a._tenter("model-test", "system-a", "user-a", 0.0, 10, False)
    client_b._tenter("model-test", "system-b", "user-b", 0.0, 10, False)

    assert _lire_compteur_quotidien(client_a.api_key) == 1
    assert _lire_compteur_quotidien(client_b.api_key) == 1
    assert _chemin_compteur_quotidien(KEY_A) != _chemin_compteur_quotidien(KEY_B)


def test_quota_sans_argument_utilise_la_cle_configuree(monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", KEY_A)
    _ecrire_compteur(KEY_A, 12)

    assert quota_restant() == 33


def test_ancien_compteur_global_est_ignore():
    ancien = config.CACHE_LLM_DIR / "compteur_quotidien.json"
    ancien.parent.mkdir(parents=True, exist_ok=True)
    ancien.write_text(
        json.dumps({"date": date.today().isoformat(), "compteur": 45}),
        encoding="utf-8",
    )

    assert quota_restant(KEY_A) == 45
    assert not _chemin_compteur_quotidien(KEY_A).exists()


def test_fichier_et_logs_quota_ne_contiennent_jamais_la_cle(monkeypatch):
    messages = []

    class _LoggerFactice:
        def info(self, message):
            messages.append(str(message))

        def debug(self, message):
            messages.append(str(message))

        def warning(self, message):
            messages.append(str(message))

        def error(self, message):
            messages.append(str(message))

    monkeypatch.setattr(llm_client_module, "logger", _LoggerFactice())
    _incrementer_compteur_quotidien(KEY_A)
    monkeypatch.setitem(config.RATE_LIMIT, "max_requetes_jour", 1)
    LLMClient(api_key=KEY_A)._tenter("model-test", "system", "user", 0.0, 10, False)

    chemin = _chemin_compteur_quotidien(KEY_A)
    assert KEY_A not in chemin.name
    assert KEY_A not in chemin.read_text(encoding="utf-8")
    assert KEY_A not in "\n".join(messages)
    assert _fingerprint_api_key(KEY_A) in "\n".join(messages)


def test_absence_de_cle_ne_cree_pas_de_fingerprint_ni_de_fichier(monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")
    monkeypatch.setattr(
        llm_client_module.requests,
        "post",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("aucun réseau attendu")),
    )

    assert quota_restant() == config.RATE_LIMIT["max_requetes_jour"]
    assert _chemin_compteur_quotidien() is None
    resultat = LLMClient()._tenter("model-test", "system", "user", 0.0, 10, False)
    assert resultat["retryable"] is False
    assert not config.QUOTA_LLM_DIR.exists()
    with pytest.raises(ValueError):
        _fingerprint_api_key("")


def test_429_upstream_passe_immediatement_au_modele_suivant(monkeypatch):
    appels = []

    def _post(*args, **kwargs):
        modele = kwargs["json"]["model"]
        appels.append(modele)
        if modele == "model-a":
            return _response(429, "Provider temporarily rate-limited upstream")
        return _response(
            200,
            "ok",
            {"choices": [{"message": {"content": '{"ok": true}'}}], "usage": {}},
        )

    monkeypatch.setattr(llm_client_module.requests, "post", _post)
    response, metadata = LLMClient(api_key=KEY_A).appeler(
        "system", "upstream", model=["model-a", "model-b"], json_attendu=True
    )

    assert response == {"ok": True}
    assert metadata.succes is True
    assert appels == ["model-a", "model-b"]


def test_429_quota_reel_arrete_la_cascade(monkeypatch):
    appels = []

    def _post(*args, **kwargs):
        appels.append(kwargs["json"]["model"])
        return _response(429, "Daily quota exhausted")

    monkeypatch.setattr(llm_client_module.requests, "post", _post)
    _, metadata = LLMClient(api_key=KEY_A).appeler(
        "system", "quota", model=["model-a", "model-b"], json_attendu=True
    )

    assert appels == ["model-a"]
    assert metadata.error_type == ERREUR_QUOTA_EPUISE


def test_http_200_sans_choices_a_un_seul_retry(monkeypatch):
    appels = []

    def _post(*args, **kwargs):
        modele = kwargs["json"]["model"]
        appels.append(modele)
        if modele == "model-a":
            return _response(200, "{}", {})
        return _response(
            200,
            "ok",
            {"choices": [{"message": {"content": '{"ok": true}'}}], "usage": {}},
        )

    monkeypatch.setattr(llm_client_module.requests, "post", _post)
    _, metadata = LLMClient(api_key=KEY_A).appeler(
        "system", "missing-choices", model=["model-a", "model-b"], json_attendu=True
    )

    assert metadata.succes is True
    assert appels == ["model-a", "model-a", "model-b"]


def test_json_invalide_respecte_le_budget_global(monkeypatch):
    appels = []

    def _post(*args, **kwargs):
        appels.append(kwargs["json"]["model"])
        return _response(
            200,
            "not-json",
            {"choices": [{"message": {"content": "not-json"}}], "usage": {}},
        )

    monkeypatch.setattr(llm_client_module.requests, "post", _post)
    _, metadata = LLMClient(api_key=KEY_A).appeler(
        "system", "invalid-json", model=["model-a", "model-b", "model-c"], json_attendu=True
    )

    assert len(appels) == config.MAX_LLM_ATTEMPTS_PER_CHUNK
    assert metadata.nb_tentatives == config.MAX_LLM_ATTEMPTS_PER_CHUNK
    assert metadata.budget_atteint is True
    assert metadata.error_type == ERREUR_JSON_INVALIDE


def test_budget_fourni_par_le_chunk_limite_aussi_la_cascade(monkeypatch):
    appels = []

    def _post(*args, **kwargs):
        appels.append(kwargs["json"]["model"])
        return _response(
            200,
            "not-json",
            {"choices": [{"message": {"content": "not-json"}}], "usage": {}},
        )

    monkeypatch.setattr(llm_client_module.requests, "post", _post)
    _, metadata = LLMClient(api_key=KEY_A).appeler(
        "system",
        "shared-chunk-budget",
        model=["model-a", "model-b"],
        json_attendu=True,
        attempt_budget=2,
    )

    assert len(appels) == 2
    assert metadata.nb_tentatives == 2
    assert metadata.budget_atteint is True


def test_timeout_a_un_seul_retry_avant_modele_suivant(monkeypatch):
    appels = []

    def _post(*args, **kwargs):
        modele = kwargs["json"]["model"]
        appels.append(modele)
        if modele == "model-a":
            raise llm_client_module.requests.exceptions.Timeout("timeout")
        return _response(
            200,
            "ok",
            {"choices": [{"message": {"content": '{"ok": true}'}}], "usage": {}},
        )

    monkeypatch.setattr(llm_client_module.requests, "post", _post)
    _, metadata = LLMClient(api_key=KEY_A).appeler(
        "system", "timeout", model=["model-a", "model-b"], json_attendu=True
    )

    assert metadata.succes is True
    assert appels == ["model-a", "model-a", "model-b"]


def test_circuit_breaker_ignore_modele_rate_limited_sur_appel_suivant(monkeypatch):
    appels = []

    def _post(*args, **kwargs):
        modele = kwargs["json"]["model"]
        appels.append(modele)
        if modele == "model-a":
            return _response(429, "upstream temporarily rate-limited")
        return _response(
            200,
            "ok",
            {"choices": [{"message": {"content": '{"ok": true}'}}], "usage": {}},
        )

    monkeypatch.setattr(llm_client_module.requests, "post", _post)
    client = LLMClient(api_key=KEY_A)
    client.appeler("system", "circuit-1", model=["model-a", "model-b"], json_attendu=True)
    client.appeler("system", "circuit-2", model=["model-a", "model-b"], json_attendu=True)
    client.appeler("system", "circuit-3", model=["model-a", "model-b"], json_attendu=True)

    assert appels == ["model-a", "model-b", "model-a", "model-b", "model-b"]
