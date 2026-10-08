"""Tests du refresh post-audit sans pipeline, AWS ni rendu Streamlit réel."""

from contextlib import nullcontext
from types import SimpleNamespace

from dashboard.pages import new_audit


def test_reload_post_audit_vide_le_cache_puis_retrouve_le_run(monkeypatch):
    appels = []
    run = {"run_id": "run-new", "summary": {}, "files": []}

    monkeypatch.setattr(new_audit.st.cache_data, "clear", lambda: appels.append("clear"))

    def _load_history_runs(**kwargs):
        appels.append("load")
        return SimpleNamespace(runs=[run], warnings=[])

    monkeypatch.setattr(new_audit, "load_history_runs", _load_history_runs)

    document, warnings = new_audit._reload_history_run("run-new")

    assert appels == ["clear", "load"]
    assert document == run
    assert warnings == []


def test_reload_post_audit_signale_un_run_absent(monkeypatch):
    monkeypatch.setattr(new_audit.st.cache_data, "clear", lambda: None)
    monkeypatch.setattr(
        new_audit,
        "load_history_runs",
        lambda **kwargs: SimpleNamespace(runs=[], warnings=[]),
    )

    document, _ = new_audit._reload_history_run("run-missing")
    niveau, message = new_audit.history_refresh_feedback(document, "s3")

    assert document is None
    assert niveau == "warning"
    assert message == "Audit completed, but the new run could not yet be loaded from S3 history."


def test_message_success_seulement_si_le_nouveau_run_est_present():
    niveau, message = new_audit.history_refresh_feedback({"run_id": "run-new"}, "s3")

    assert niveau == "success"
    assert message == "Audit execution completed successfully. History refreshed successfully."


def test_execute_respecte_pipeline_clear_reload_puis_rerun(monkeypatch):
    evenements = []

    class _Session(dict):
        __getattr__ = dict.__getitem__
        __setattr__ = dict.__setitem__

    faux_streamlit = SimpleNamespace(
        session_state=_Session(),
        cache_data=SimpleNamespace(clear=lambda: evenements.append("clear")),
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        spinner=lambda *args, **kwargs: nullcontext(),
        rerun=lambda: evenements.append("rerun"),
    )
    monkeypatch.setattr(new_audit, "st", faux_streamlit)
    monkeypatch.setattr(new_audit.config, "STORAGE_MODE", "s3")
    monkeypatch.setattr(
        new_audit,
        "load_history_runs",
        lambda **kwargs: (
            evenements.append("reload")
            or SimpleNamespace(runs=[{"run_id": "run-new"}], warnings=[])
        ),
    )

    def _operation():
        evenements.append("pipeline_complete")
        return {
            "run_id": "run-new",
            "history_path": "local.json",
            "history_key": "history/runs/2026/10/08/run-new.json",
            "history_uploaded": True,
        }

    new_audit._execute(_operation)

    assert evenements == ["pipeline_complete", "clear", "reload", "rerun"]
    assert faux_streamlit.session_state.last_audit["history"]["run_id"] == "run-new"


def test_execute_ne_relance_pas_un_audit_deja_running(monkeypatch):
    evenements = []

    class _Session(dict):
        __getattr__ = dict.__getitem__
        __setattr__ = dict.__setitem__

    faux_streamlit = SimpleNamespace(
        session_state=_Session(audit_in_progress=True),
        warning=lambda message: evenements.append(message),
    )
    monkeypatch.setattr(new_audit, "st", faux_streamlit)

    new_audit._execute(lambda: evenements.append("pipeline"))

    assert evenements == ["An audit is already running in this session."]


def test_current_status_survit_a_la_navigation_via_audit_job(monkeypatch):
    class _Session(dict):
        __getattr__ = dict.__getitem__
        __setattr__ = dict.__setitem__

    monkeypatch.setattr(
        new_audit,
        "st",
        SimpleNamespace(
            session_state=_Session(
                audit_in_progress=False,
                audit_job={"state": "RUNNING", "selected_files": ["file.xlsx"]},
            )
        ),
    )

    assert new_audit.current_audit_status() == "RUNNING"
