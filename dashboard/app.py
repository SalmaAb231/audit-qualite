"""Point d'entrée Streamlit du Data Quality Audit Dashboard."""

from __future__ import annotations

import sys
from html import escape
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import streamlit as st

st.set_page_config(
    page_title="Data Quality Audit Dashboard",
    page_icon=None,
    layout="wide",
)

import config
from dashboard.audit_service import AuditServiceError, list_available_files
from dashboard.components import (
    render_app_header,
    render_filtered_view_summary,
    render_global_filters,
    render_sidebar_brand,
    status_badge_html,
    utc_now_label,
)
from dashboard.data_loader import history_runs_prefix, load_history_runs
from dashboard.pages import agents, anomalies, files, new_audit, overview, quarantine, runs
from dashboard.styles import inject_global_styles
from dashboard.transformations import (
    apply_file_filters,
    build_dataframes,
    filter_counter_df,
    filter_runs_df,
    filtered_view_counts,
    has_active_filters,
)


@st.cache_data(ttl=300, show_spinner=False)
def _cached_history(storage_mode: str, history_dir: str, bucket: str, prefix: str):
    del bucket  # Fait partie de la clé de cache lorsque la configuration change.
    resultat = load_history_runs(
        storage_mode=storage_mode,
        history_dir=Path(history_dir),
        history_prefix=prefix,
    )
    return resultat.runs, resultat.warnings


@st.cache_data(ttl=300, show_spinner=False)
def _cached_dataframes(raw_runs: list[dict]):
    return build_dataframes(raw_runs)


@st.cache_data(ttl=120, show_spinner=False)
def _cached_available_files(storage_mode: str, data_dir: str, bucket: str, prefix: str):
    del storage_mode, data_dir, bucket, prefix
    return list_available_files()


inject_global_styles()

st.session_state.setdefault("last_refresh", utc_now_label())
st.session_state.setdefault("navigation", "Overview")
render_app_header(config.STORAGE_MODE, st.session_state.last_refresh)

raw_runs, load_warnings = _cached_history(
    config.STORAGE_MODE,
    str(config.HISTORY_DIR),
    config.S3_BUCKET,
    config.S3_HISTORY_PREFIX,
)
runs_df, files_df, agents_df, severity_df = _cached_dataframes(raw_runs)

pages = ["Overview", "New Audit", "Files", "Anomalies", "Agents", "Quarantine", "Runs"]
navigation_labels = {
    "Overview": "Overview",
    "New Audit": "New Audit",
    "Files": "Files",
    "Anomalies": "Anomalies",
    "Agents": "Agents",
    "Quarantine": "Quarantine",
    "Runs": "Runs",
}
render_sidebar_brand()
st.sidebar.markdown('<div class="dq-sidebar-section">NAVIGATION</div>', unsafe_allow_html=True)
page = st.sidebar.radio(
    "Navigation",
    pages,
    key="navigation",
    format_func=lambda value: navigation_labels[value],
    label_visibility="collapsed",
)

history_location = (
    history_runs_prefix(config.S3_HISTORY_PREFIX)
    if config.STORAGE_MODE == "s3"
    else (config.HISTORY_DIR / "runs").as_posix()
)
history_source_label = "S3 history" if config.STORAGE_MODE == "s3" else "Local history"
st.sidebar.markdown('<div class="dq-sidebar-section">ENVIRONMENT</div>', unsafe_allow_html=True)
st.sidebar.markdown(status_badge_html(config.STORAGE_MODE), unsafe_allow_html=True)
st.sidebar.caption(f"Last update: {st.session_state.last_refresh}")

filtres = None
if page != "New Audit":
    filtres = render_global_filters(files_df)

with st.sidebar.expander("System status", expanded=False):
    st.caption(f"Executions loaded: {len(runs_df)}")
    st.caption(f"Audit records loaded: {len(files_df)}")
    st.markdown(
        f'<div class="sidebar-data-line" title="{escape(history_location)}">'
        f'History source: {escape(history_source_label)}</div>',
        unsafe_allow_html=True,
    )
    if st.button("Refresh data", use_container_width=True, key="refresh_dashboard_data"):
        st.cache_data.clear()
        st.session_state.last_refresh = utc_now_label()
        st.rerun()

audit_job = st.session_state.get("audit_job") or {}
if audit_job.get("state") == "RUNNING":
    selected = ", ".join(audit_job.get("selected_files") or [])
    st.info(f"Audit running for {selected or 'the selected scope'}. Started {audit_job.get('started_at', 'N/A')}.")
elif audit_job.get("state") == "FAILED":
    st.error("The latest audit attempt failed. Open New Audit for details.")

for avertissement in load_warnings:
    st.warning(avertissement)

if page == "New Audit":
    try:
        available = _cached_available_files(
            config.STORAGE_MODE,
            str(config.DATA_DIR),
            config.S3_BUCKET,
            config.S3_INPUT_PREFIX,
        )
    except AuditServiceError as erreur:
        st.error(str(erreur))
        available = []
    new_audit.render(available)
else:
    assert filtres is not None
    filtered_files = apply_file_filters(files_df, filtres)
    filtered_runs = filter_runs_df(runs_df, filtered_files, filtres)
    filtered_agents = filter_counter_df(agents_df, filtered_files)
    filtered_severity = filter_counter_df(severity_df, filtered_files)
    render_filtered_view_summary(
        filtered_view_counts(filtered_runs, runs_df, filtered_files, files_df),
        has_active_filters(filtres),
        filtres,
    )
    run_ids = set(filtered_runs["run_id"].dropna()) if not filtered_runs.empty else set()
    filtered_raw_runs = [run for run in raw_runs if run.get("run_id") in run_ids]

    if page == "Overview":
        overview.render(filtered_runs, filtered_files, filtered_agents, filtered_severity)
    elif page == "Files":
        files.render(filtered_files, filtered_agents, filtered_severity)
    elif page == "Anomalies":
        anomalies.render(filtered_files, filtered_agents, filtered_severity)
    elif page == "Agents":
        agents.render(filtered_agents, filtered_files)
    elif page == "Quarantine":
        quarantine.render(filtered_files, filtered_agents)
    elif page == "Runs":
        runs.render(filtered_runs, filtered_files, filtered_raw_runs)
