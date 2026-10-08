"""Page d'action New Audit."""

from __future__ import annotations

import streamlit as st
from loguru import logger

import config
from dashboard.audit_service import (
    AuditServiceError,
    group_files_by_load_lock,
    run_full_load_lock,
    run_selected_files,
    run_single_file,
)
from dashboard.components import (
    display_value,
    format_bytes,
    render_audit_configuration,
    render_empty_state,
    render_info_grid,
    render_metric_card,
    render_page_header,
    render_scope_cards,
    render_section_header,
    utc_now_label,
)
from dashboard.data_loader import find_run, load_history_runs


def audit_completion_message(summary: dict | None) -> tuple[str, str]:
    """Return user-facing execution feedback without conflating it with outcomes."""
    summary = summary if isinstance(summary, dict) else {}
    try:
        incomplete = int(summary.get("incomplete") or 0)
    except (TypeError, ValueError):
        incomplete = 0
    if incomplete > 0:
        noun = "result is" if incomplete == 1 else "results are"
        return (
            f"Execution completed. {incomplete} audit {noun} INCOMPLETE. "
            "History refreshed successfully.",
            "warning",
        )
    return "Audit execution completed successfully. History refreshed successfully.", "success"


def current_audit_status() -> str:
    job = st.session_state.get("audit_job") or {}
    is_running = st.session_state.get("audit_in_progress") is True or job.get("state") == "RUNNING"
    return "RUNNING" if is_running else str(job.get("state") or "IDLE").upper()


def audit_submit_disabled(
    has_valid_selection: bool,
    current_status: str,
    *,
    confirmation_required: bool = False,
    confirmed: bool = False,
) -> bool:
    return (
        not has_valid_selection
        or str(current_status).upper() == "RUNNING"
        or (confirmation_required and not confirmed)
    )


def _reload_history_run(run_id: str) -> tuple[dict | None, list[str]]:
    """Invalide le cache avant de relire le run depuis la source de vérité."""
    st.cache_data.clear()
    resultat = load_history_runs(
        storage_mode=config.STORAGE_MODE,
        history_dir=config.HISTORY_DIR,
        history_prefix=config.S3_HISTORY_PREFIX,
    )
    return find_run(resultat.runs, run_id), resultat.warnings


def history_refresh_feedback(document: dict | None, storage_mode: str) -> tuple[str, str]:
    """Retourne un message honnête selon la présence effective du nouveau run."""
    if document is not None:
        message, level = audit_completion_message(document.get("summary"))
        return level, message
    if storage_mode.lower() == "s3":
        return "warning", "Audit completed, but the new run could not yet be loaded from S3 history."
    return "warning", "Audit completed, but the new run could not yet be loaded from history."


def _go_to_run(run_id: str) -> None:
    """Callback exécuté avant le rerun Streamlit, donc avant le widget navigation."""
    st.session_state.selected_run_id = run_id
    st.session_state.navigation = "Runs"


def _execute(operation, *args, audit_context: dict | None = None) -> None:
    if st.session_state.get("audit_in_progress"):
        st.warning("An audit is already running in this session.")
        return

    contexte = audit_context or {}
    st.session_state.audit_in_progress = True
    st.session_state.audit_job = {
        "run_id": None,
        "selected_files": list(contexte.get("selected_files") or []),
        "load_lock": contexte.get("load_lock"),
        "started_at": utc_now_label(),
        "state": "RUNNING",
        "latest_result": None,
    }
    audit_termine = False
    try:
        st.info(
            "Running on "
            f"{contexte.get('load_lock') or '(no LOAD_LOCK)'} · "
            f"{contexte.get('files', 'N/A')} file(s) · {config.STORAGE_MODE.upper()}"
        )
        with st.spinner("Running Data Quality Audit... This may take several minutes."):
            resultat = operation(*args)
        run_id = resultat["run_id"]
        document, warnings = _reload_history_run(run_id)
        st.session_state.last_audit = {
            "run_id": run_id,
            "history": document,
            "warnings": warnings,
            "history_path": resultat.get("history_path"),
            "history_key": resultat.get("history_key"),
            "history_uploaded": resultat.get("history_uploaded"),
        }
        st.session_state.audit_job.update(
            {"run_id": run_id, "state": "COMPLETED", "latest_result": document}
        )
        st.session_state.last_refresh = utc_now_label()
        audit_termine = True
    except AuditServiceError as erreur:
        st.session_state.last_audit_error = str(erreur)
        st.session_state.audit_job.update({"state": "FAILED", "error": str(erreur)})
    except Exception as erreur:
        logger.error(f"Erreur UI inattendue pendant l'audit : {erreur}", exc_info=True)
        st.session_state.last_audit_error = (
            "An unexpected error occurred. Check the server logs for details."
        )
        st.session_state.audit_job.update(
            {"state": "FAILED", "error": st.session_state.last_audit_error}
        )
    finally:
        st.session_state.audit_in_progress = False
    if audit_termine:
        # Le rerun reconstruit aussi les DataFrames et les compteurs de sidebar.
        st.rerun()


def _render_last_result() -> None:
    erreur = st.session_state.pop("last_audit_error", None)
    if erreur:
        st.error(erreur)

    dernier = st.session_state.get("last_audit")
    if not dernier:
        return

    render_section_header("Latest Audit Result")
    with st.container(border=True):
        document = dernier.get("history")
        niveau, message = history_refresh_feedback(document, config.STORAGE_MODE)
        getattr(st, niveau)(message)
        render_info_grid(
            [
                ("Run ID", dernier["run_id"]),
                ("History", "Available" if document is not None else "Unavailable"),
            ]
        )
        if document:
            resume = document.get("summary") or {}
            colonnes = st.columns(5)
            valeurs = [
                ("Processed", "files_processed", "▤", "primary"),
                ("Accepted", "accepted", "✓", "success"),
                ("Warning", "warning", "!", "warning"),
                ("Rejected", "rejected", "×", "danger"),
                ("Incomplete", "incomplete", "•", "neutral"),
            ]
            for colonne, (libelle, cle, icon, tone) in zip(colonnes, valeurs):
                with colonne:
                    render_metric_card(libelle, display_value(resume.get(cle)), icon, tone)
        else:
            if dernier.get("history_uploaded") is False:
                st.error("History upload to S3 failed. Check the server upload logs and s3:PutObject permission.")
        for avertissement in dernier.get("warnings") or []:
            st.warning(avertissement)
        st.button(
            "View Run Results",
            key="view_last_run",
            on_click=_go_to_run,
            args=(dernier["run_id"],),
            disabled=document is None,
        )


def render(available_files: list[dict]) -> None:
    render_page_header("New Audit", "Configure and launch a Data Quality audit.")
    st.warning("Action page: launching an audit executes the pipeline and may call OpenRouter LLMs.")

    if not available_files:
        render_empty_state(
            "No Supported Input Files",
            "Add a supported file to the active LOCAL or S3 input source before starting an audit.",
        )
        _render_last_result()
        return

    groupes = group_files_by_load_lock(available_files)
    load_locks = sorted(groupes)
    render_section_header("Audit Scope", "Choose one file, a selection, or an entire LOAD_LOCK folder.")
    mode = st.radio(
        "Audit scope",
        ["Single File", "Multiple Files", "Full LOAD_LOCK Folder"],
        horizontal=True,
        key="audit_scope",
        label_visibility="collapsed",
    )
    render_scope_cards(mode)

    render_section_header("Configuration")
    if mode == "Single File":
        with st.form("single_audit_form"):
            load_lock = st.selectbox(
                "LOAD_LOCK", load_locks, format_func=lambda value: value or "(no LOAD_LOCK)"
            )
            noms = [fichier["filename"] for fichier in groupes[load_lock]]
            filename = st.selectbox("Filename", noms)
            selected_info = [
                fichier for fichier in groupes[load_lock] if fichier.get("filename") == filename
            ]
            plan = render_audit_configuration(
                config.STORAGE_MODE, load_lock, mode, 1, selected_info
            )
            quota_confirmation = True
            if plan["insufficient"]:
                quota_confirmation = st.checkbox(
                    "I understand that the local allowance may be insufficient and the audit may be INCOMPLETE."
                )
            submitted = st.form_submit_button(
                "Run Data Quality Audit",
                type="primary",
                disabled=audit_submit_disabled(
                    bool(filename),
                    current_audit_status(),
                    confirmation_required=plan["insufficient"],
                    confirmed=quota_confirmation,
                ),
            )
        if submitted:
            _execute(
                run_single_file,
                filename,
                load_lock,
                audit_context={"load_lock": load_lock, "files": 1, "selected_files": [filename]},
            )

    elif mode == "Multiple Files":
        with st.form("multiple_audit_form"):
            load_lock = st.selectbox(
                "LOAD_LOCK", load_locks, format_func=lambda value: value or "(no LOAD_LOCK)"
            )
            noms = [fichier["filename"] for fichier in groupes[load_lock]]
            selection = st.multiselect("Files", noms)
            selected_info = [
                fichier for fichier in groupes[load_lock] if fichier.get("filename") in selection
            ]
            plan = render_audit_configuration(
                config.STORAGE_MODE, load_lock, mode, len(selection), selected_info
            )
            st.info(
                f"{len(selection)} file(s) will be analysed. This audit may perform multiple "
                "OpenRouter LLM calls and take several minutes."
            )
            confirmation_label = "I confirm that I want to run this multi-file audit."
            if plan["insufficient"]:
                confirmation_label += " I understand that the local allowance may be insufficient."
            confirmation = st.checkbox(confirmation_label)
            submitted = st.form_submit_button(
                "Run Data Quality Audit",
                type="primary",
                disabled=(
                    audit_submit_disabled(
                        bool(selection),
                        current_audit_status(),
                        confirmation_required=True,
                        confirmed=confirmation,
                    )
                ),
            )
        if submitted:
            _execute(
                run_selected_files,
                selection,
                load_lock,
                audit_context={"load_lock": load_lock, "files": len(selection), "selected_files": selection},
            )

    else:
        with st.form("folder_audit_form"):
            load_lock = st.selectbox(
                "LOAD_LOCK", load_locks, format_func=lambda value: value or "(no LOAD_LOCK)"
            )
            noms = [fichier["filename"] for fichier in groupes[load_lock]]
            plan = render_audit_configuration(
                config.STORAGE_MODE, load_lock, mode, len(noms), groupes[load_lock]
            )
            apercu = [
                {
                    "Filename": fichier.get("filename"),
                    "Size": (
                        f"{fichier['size_kb']:.1f} KB"
                        if isinstance(fichier.get("size_kb"), (int, float))
                        else format_bytes(fichier.get("size_bytes") or fichier.get("size"))
                    ),
                }
                for fichier in groupes[load_lock][:50]
            ]
            st.dataframe(
                apercu,
                use_container_width=True,
                hide_index=True,
            )
            if len(noms) > 50:
                st.caption(f"Preview limited to 50 of {len(noms)} files.")
            st.warning(
                f"You are about to analyse {len(noms)} file(s). This audit may perform multiple "
                "OpenRouter LLM calls. Check your quota before continuing."
            )
            confirmation_label = "I confirm that I want to audit this entire LOAD_LOCK."
            if plan["insufficient"]:
                confirmation_label += " I understand that the local allowance may be insufficient."
            confirmation = st.checkbox(confirmation_label)
            submitted = st.form_submit_button(
                "Run Data Quality Audit",
                type="primary",
                disabled=(
                    audit_submit_disabled(
                        bool(noms),
                        current_audit_status(),
                        confirmation_required=True,
                        confirmed=confirmation,
                    )
                ),
            )
        if submitted:
            _execute(
                run_full_load_lock,
                load_lock,
                audit_context={"load_lock": load_lock, "files": len(noms), "selected_files": noms},
            )

    _render_last_result()
