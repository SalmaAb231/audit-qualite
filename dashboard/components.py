"""Reusable presentation components for the Streamlit dashboard."""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape
import hashlib
import math
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

import pandas as pd
import streamlit as st
import streamlit.components.v1 as st_components

from dashboard import charts
from dashboard.artifacts import (
    artifact_reference_status,
    read_artifact_cached,
    read_json_artifact,
    resolve_local_artifact,
    mime_type_for_reference,
    is_versioned_run_reference,
)
import config


# Kept only as an import-compatible constant; the professional UI no longer renders pictograms.
STATUS_ICONS: dict[str, str] = {}
FILTER_STATE_KEYS = (
    "filter_load_lock",
    "filter_verdict",
    "filter_pipeline_status",
    "filter_grade",
    "filter_filename",
    "filter_date_range",
)


def is_missing(value: Any) -> bool:
    """Return True for absent scalar values without confusing zero with missing."""
    if value is None:
        return True
    try:
        missing = pd.isna(value)
    except (TypeError, ValueError):
        return False
    try:
        return bool(missing)
    except (TypeError, ValueError):
        return False


def format_optional_number(
    value: Any,
    decimals: int = 0,
    *,
    suffix: str = "",
) -> str:
    if is_missing(value):
        return "N/A"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(number):
        return "N/A"
    rendered = f"{number:,.0f}" if decimals == 0 else f"{number:,.{decimals}f}"
    return f"{rendered}{suffix}"


def format_duration(value: Any) -> str:
    if is_missing(value):
        return "N/A"
    try:
        seconds = max(0.0, float(value))
    except (TypeError, ValueError):
        return "N/A"
    if not math.isfinite(seconds):
        return "N/A"
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, remaining = divmod(int(round(seconds)), 60)
    if minutes < 60:
        return f"{minutes} min {remaining:02d} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min"


def format_datetime(value: Any) -> str:
    if is_missing(value):
        return "N/A"
    try:
        parsed = pd.to_datetime(value, utc=True)
    except (TypeError, ValueError):
        return str(value)
    if pd.isna(parsed):
        return "N/A"
    return parsed.strftime("%d %b %Y, %H:%M UTC")


def format_number(value: Any, decimals: int = 0) -> str:
    return format_optional_number(value, decimals)


def format_score(value: Any) -> str:
    return format_optional_number(value, 2)


def format_timestamp(value: Any) -> str:
    return format_datetime(value)


def format_bytes(value: Any) -> str:
    if is_missing(value):
        return "N/A"
    try:
        size = float(value)
    except (TypeError, ValueError):
        return str(value)
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if abs(size) < 1024 or unit == units[-1]:
            decimals = 0 if unit == "B" else 1
            return f"{size:.{decimals}f} {unit}"
        size /= 1024
    return "N/A"


def display_value(value: Any, *, decimals: int | None = None) -> str:
    if decimals is not None:
        return format_optional_number(value, decimals)
    return "N/A" if is_missing(value) else str(value)


def stored_value(value: Any) -> str:
    return "Not stored" if is_missing(value) or str(value).strip() == "" else str(value)


def format_short_run_id(run_id: Any) -> str:
    """Keep the execution time and a unique suffix in compact run labels."""
    if is_missing(run_id) or not str(run_id).strip():
        return "—"
    value = str(run_id).strip()
    match = re.match(r"^\d{8}T(?P<time>\d{6})Z[_-](?P<suffix>[A-Za-z0-9]+)", value)
    if match:
        raw_time = match.group("time")
        shown_time = f"{raw_time[:2]}:{raw_time[2:4]}:{raw_time[4:6]}"
        return f"{shown_time} · {match.group('suffix')[:6]}"
    if len(value) <= 20:
        return value
    return f"{value[:10]}…{value[-6:]}"


def format_user_reason(
    value: Any,
    pipeline_status: Any = None,
    judge_verdict: Any = None,
) -> str:
    """Render a concise reason without contradicting the persisted audit state."""
    if is_missing(value) or not str(value).strip():
        return "N/A"
    original = str(value).strip()
    text = original.lower()
    pipeline = status_label(pipeline_status)
    verdict = status_label(judge_verdict)
    is_incomplete = pipeline == "INCOMPLETE" or verdict == "N/A"
    technical_failure = any(
        marker in text
        for marker in (
            "quota",
            "budget global",
            "retry budget",
            "http 429",
            "rate limit",
            "rate-limited",
            "json invalide",
            "invalid json",
            "non extractible",
            "failed",
            "incomplete",
            "incomplet",
        )
    )
    if technical_failure and not is_incomplete and pipeline == "COMPLETE":
        return "N/A"
    agent = "Realism" if "realisme" in text or "réalisme" in text or "realism" in text else None
    if "quota llm" in text or ("quota" in text and "local" in text):
        prefix = f"{agent} agent" if agent else "LLM agent"
        return f"{prefix} was not executed: local LLM quota exhausted."
    if "budget global" in text or "retry budget" in text or "budget" in text and "tentative" in text:
        prefix = f"{agent} analysis" if agent else "LLM analysis"
        return f"{prefix} unavailable after the LLM retry budget was exhausted."
    if "http 429" in text or "rate limit" in text or "rate-limited" in text:
        prefix = f"{agent} agent" if agent else "LLM agent"
        return f"{prefix} failed: OpenRouter rate limit."
    if "json invalide" in text or "invalid json" in text or "non extractible" in text:
        prefix = f"{agent} agent" if agent else "LLM agent"
        return f"{prefix} failed: invalid model response."
    if "failed" in text or "incomplete" in text or "incomplet" in text:
        return "Audit incomplete — one or more agents failed."
    first_sentence = original.split(". ", 1)[0].strip()
    if len(first_sentence) < len(original):
        first_sentence += "."
    return first_sentence if len(first_sentence) <= 160 else f"{first_sentence[:157].rstrip()}..."


def format_decision_reason(
    value: Any,
    pipeline_status: Any = None,
    judge_verdict: Any = None,
) -> str:
    """Business-facing decision reason; missing persisted Judge text stays neutral."""
    if is_missing(value) or not str(value).strip():
        if status_label(pipeline_status) == "INCOMPLETE":
            return "Audit incomplete — one or more agents failed."
        return "—"
    rendered = format_user_reason(value, pipeline_status, judge_verdict)
    return "—" if rendered == "N/A" else rendered


def format_short_reason(
    value: Any,
    pipeline_status: Any = None,
    judge_verdict: Any = None,
    *,
    maximum_length: int = 72,
) -> str:
    """Compact table reason while leaving the full persisted value in details."""
    rendered = format_decision_reason(value, pipeline_status, judge_verdict)
    lower = rendered.lower()
    if "realism" in lower and any(token in lower for token in ("unavailable", "failed", "not executed")):
        return "Realism analysis unavailable"
    if len(rendered) <= maximum_length:
        return rendered
    return f"{rendered[: maximum_length - 1].rstrip()}…"


def status_label(status: Any) -> str:
    if is_missing(status):
        return "N/A"
    return str(status).replace("_", " ").strip().upper()


def status_badge_html(status: Any) -> str:
    label = status_label(status)
    css_status = label.lower().replace(" ", "-")
    return f'<span class="status-badge status-{escape(css_status)}">{escape(label)}</span>'


def render_status_badge(status: Any) -> None:
    st.markdown(status_badge_html(status), unsafe_allow_html=True)


def render_app_header(storage_mode: str = "local", last_refresh: str | None = None) -> None:
    mode = str(storage_mode or "local").upper()
    st.markdown(
        f"""
        <div class="app-header">
          <div>
            <div class="app-title">Data Quality Audit</div>
            <div class="app-subtitle">Operational monitoring and audit history</div>
          </div>
          <div class="app-header-meta">
            <span class="storage-badge" title="Current storage mode">{escape(mode)}</span>
            <span class="last-refresh">Last update: {escape(last_refresh or "N/A")}</span>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_page_header(title: str, subtitle: str = "", icon: str | None = None, **_: Any) -> None:
    # Compatibility with the former (icon, title, subtitle) signature.
    if icon is not None and len(str(title).strip()) <= 8:
        title, subtitle, icon = subtitle, str(icon), None
    del icon
    title = str(title).strip()
    while title and not title[0].isalnum():
        title = title[1:].lstrip()
    subtitle_html = f'<p class="page-subtitle">{escape(subtitle)}</p>' if subtitle else ""
    st.markdown(
        f'<div class="page-header"><h1>{escape(title)}</h1>{subtitle_html}</div>',
        unsafe_allow_html=True,
    )


def render_section_header(title: str, description: str = "") -> None:
    st.subheader(title)
    if description:
        st.caption(description)


def render_empty_state(title: str, description: str, icon: str | None = None, **_: Any) -> None:
    # Compatibility with the former (icon, title, description) signature.
    if icon is not None and len(str(title).strip()) <= 8:
        title, description, icon = description, str(icon), None
    del icon
    st.markdown(
        f"""
        <div class="empty-state">
          <div class="empty-state-title">{escape(title)}</div>
          <div class="empty-state-description">{escape(description)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_metric_card(
    label: str,
    value: Any,
    icon: str | None = None,
    helper: str | None = None,
    **_: Any,
) -> None:
    del icon
    if helper in {"primary", "success", "warning", "danger", "neutral"}:
        helper = None
    helper_html = f'<div class="metric-helper">{escape(helper)}</div>' if helper else ""
    st.markdown(
        f"""
        <div class="metric-card">
          <div class="metric-label">{escape(label)}</div>
          <div class="metric-value">{escape(display_value(value))}</div>
          {helper_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_metric_grid(items: Iterable[tuple[str, Any, str | None]]) -> None:
    """Responsive KPI grid used when fixed Streamlit columns become too narrow."""
    cards = []
    for label, value, helper in items:
        helper_html = f'<div class="metric-helper">{escape(str(helper))}</div>' if helper else ""
        cards.append(
            '<div class="metric-card metric-grid-card">'
            f'<div class="metric-label">{escape(str(label))}</div>'
            f'<div class="metric-value">{escape(display_value(value))}</div>'
            f'{helper_html}</div>'
        )
    st.markdown(f'<div class="metric-grid">{"".join(cards)}</div>', unsafe_allow_html=True)


def _metric_row(items: Iterable[tuple[str, Any]], columns: int = 5) -> None:
    items = list(items)
    for offset in range(0, len(items), columns):
        row = items[offset : offset + columns]
        for column, (label, value) in zip(st.columns(len(row)), row):
            with column:
                render_metric_card(label, value)


def render_kpis(kpis: Mapping[str, Any]) -> None:
    _metric_row(
        [
            ("Files Audited", format_optional_number(kpis.get("total_files"))),
            ("Accepted", format_optional_number(kpis.get("accepted"))),
            ("Warning", format_optional_number(kpis.get("warning"))),
            ("Rejected", format_optional_number(kpis.get("rejected"))),
            ("Average Score", format_optional_number(kpis.get("average_score"), 2)),
        ]
    )
    _metric_row(
        [
            ("Executions", format_optional_number(kpis.get("executions"))),
            ("Completed Audits", format_optional_number(kpis.get("completed_audits"))),
            ("No File Processed", format_optional_number(kpis.get("empty_runs"))),
            ("Incomplete", format_optional_number(kpis.get("incomplete"))),
            ("Quarantined", format_optional_number(kpis.get("quarantined"))),
            ("Historical Detections", format_optional_number(kpis.get("total_anomalies"))),
        ]
    )


def render_scope_cards(*args: Any, **kwargs: Any) -> None:
    """Deprecated compatibility shim; scope is now shown in the audit form."""
    del args, kwargs


def utc_now_label() -> str:
    return datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")


def render_sidebar_environment(
    storage_mode: str,
    runs_loaded: int,
    files_loaded: int,
    last_refresh: str,
    history_location: str,
    *,
    include_data_status: bool = True,
) -> None:
    st.sidebar.markdown('<div class="dq-sidebar-section">ENVIRONMENT</div>', unsafe_allow_html=True)
    st.sidebar.markdown(status_badge_html(storage_mode), unsafe_allow_html=True)
    if include_data_status:
        render_sidebar_data_status(runs_loaded, files_loaded, last_refresh, history_location)


def render_sidebar_data_status(
    runs_loaded: int,
    files_loaded: int,
    last_refresh: str,
    history_location: str,
) -> None:
    st.sidebar.markdown('<div class="dq-sidebar-section">DATA SOURCE STATUS</div>', unsafe_allow_html=True)
    st.sidebar.caption(f"Executions loaded: {runs_loaded}")
    st.sidebar.caption(f"Audited file records loaded: {files_loaded}")
    st.sidebar.caption(f"Last refresh: {last_refresh}")
    st.sidebar.caption(f"History source: {history_location}")


def _filter_options(files_df: pd.DataFrame, column: str) -> list[Any]:
    if files_df.empty or column not in files_df:
        return []
    return sorted(files_df[column].dropna().unique().tolist(), key=lambda value: str(value))


def _clear_filter_state() -> None:
    for key in FILTER_STATE_KEYS:
        st.session_state.pop(key, None)


def _stored_filters_are_active(files_df: pd.DataFrame) -> bool:
    for key in FILTER_STATE_KEYS[:-1]:
        value = st.session_state.get(key)
        if value not in (None, "", [], (), set()):
            return True
    selected_dates = st.session_state.get("filter_date_range")
    if selected_dates and not files_df.empty and "timestamp" in files_df:
        dates = files_df["timestamp"].dropna()
        if not dates.empty and len(selected_dates) == 2:
            return tuple(selected_dates) != (dates.min().date(), dates.max().date())
    return False


def render_global_filters(files_df: pd.DataFrame) -> dict[str, Any]:
    previously_active = _stored_filters_are_active(files_df)
    with st.sidebar.expander("Filters", expanded=previously_active):
        load_locks = st.multiselect(
            "LOAD_LOCK", _filter_options(files_df, "load_lock"), key="filter_load_lock"
        )
        verdicts = st.multiselect(
            "Judge Verdict", _filter_options(files_df, "verdict"), key="filter_verdict"
        )
        pipeline_statuses = st.multiselect(
            "Pipeline status",
            _filter_options(files_df, "pipeline_status"),
            key="filter_pipeline_status",
        )
        grades = st.multiselect("Grade", _filter_options(files_df, "grade"), key="filter_grade")
        filename = st.text_input("Filename contains", key="filter_filename")

        date_start = date_end = None
        if not files_df.empty and "timestamp" in files_df:
            dates = files_df["timestamp"].dropna()
            if not dates.empty:
                minimum_date = dates.min().date()
                maximum_date = dates.max().date()
                selected_dates = st.date_input(
                    "Audit date range",
                    value=(minimum_date, maximum_date),
                    key="filter_date_range",
                )
                if isinstance(selected_dates, (list, tuple)):
                    if len(selected_dates) > 0:
                        date_start = selected_dates[0]
                    if len(selected_dates) > 1:
                        date_end = selected_dates[1]
                if date_start == minimum_date and date_end == maximum_date:
                    date_start = date_end = None

    filters = {
        "load_lock": load_locks,
        "verdict": verdicts,
        "pipeline_status": pipeline_statuses,
        "grade": grades,
        "filename": filename,
        "date_start": date_start,
        "date_end": date_end,
    }
    active = bool(load_locks or verdicts or pipeline_statuses or grades or filename)
    active = active or date_start is not None or date_end is not None
    if active:
        with st.sidebar:
            st.button("Reset filters", on_click=_clear_filter_state, use_container_width=True)
    return filters


def render_audit_configuration(
    storage_mode: str,
    load_lock: Any,
    scope: str,
    file_count: int,
    selected_files: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    allowance: Any = None
    maximum: Any = None
    attempts_per_chunk = 4
    try:
        import config
        from agents.llm_client import quota_restant

        maximum = config.RATE_LIMIT.get("max_requetes_jour")
        allowance = quota_restant(getattr(config, "OPENROUTER_API_KEY", None))
        attempts_per_chunk = int(getattr(config, "MAX_LLM_ATTEMPTS_PER_CHUNK", 4))
    except (ImportError, OSError, TypeError, ValueError):
        pass

    estimated_chunks, exact_chunk_estimate = estimate_chunk_count(
        selected_files,
        file_count=file_count,
    )
    estimated_calls = estimate_max_llm_calls(estimated_chunks, attempts_per_chunk)

    items = [
        ("Storage", str(storage_mode).upper()),
        ("LOAD_LOCK", load_lock or "N/A"),
        ("Scope", scope),
        ("Selected files", file_count),
        ("Estimated LLM calls", f"≈ {estimated_calls}" if exact_chunk_estimate else f"≥ {estimated_calls}"),
    ]
    if allowance is not None:
        quota_label = f"{allowance} remaining / {maximum}" if maximum is not None else f"{allowance} remaining"
        items.append(("Daily local LLM allowance", quota_label))
        items.append(("Quota risk", quota_risk_level(estimated_calls, allowance)))
    render_info_grid(items)
    if exact_chunk_estimate:
        st.caption("Estimate uses stored row counts, configured chunk size, two LLM agents, and retry allowance.")
    else:
        st.caption(
            "Baseline estimate: row counts are unavailable before loading, so at least one chunk per file is used. "
            "Large files can require more calls. No LLM call is made for this estimate."
        )
    if allowance is not None and allowance < estimated_calls:
        st.warning(
            "The remaining local OpenRouter allowance is below this estimate. "
            "The audit can stop with an INCOMPLETE pipeline status if the allowance is exhausted."
        )
    return {
        "estimated_chunks": estimated_chunks,
        "estimated_calls": estimated_calls,
        "remaining_calls": allowance,
        "maximum_calls": maximum,
        "risk": quota_risk_level(estimated_calls, allowance) if allowance is not None else "UNKNOWN",
        "insufficient": allowance is not None and allowance < estimated_calls,
        "exact_chunk_estimate": exact_chunk_estimate,
    }


def estimate_chunk_count(
    selected_files: Iterable[Mapping[str, Any]] | None,
    *,
    file_count: int,
    chunk_size: int | None = None,
) -> tuple[int, bool]:
    """Estimate chunks from stored row counts, falling back honestly to one per file."""
    files = list(selected_files or [])
    try:
        if chunk_size is None:
            import config

            chunk_size = int(getattr(config, "CHUNK_SIZE", 0) or 0)
    except (ImportError, TypeError, ValueError):
        chunk_size = 0
    row_counts = [pd.to_numeric(item.get("row_count"), errors="coerce") for item in files]
    if files and chunk_size and all(not pd.isna(value) for value in row_counts):
        chunks = sum(max(1, math.ceil(float(value) / chunk_size)) for value in row_counts)
        return chunks, True
    return max(0, int(file_count)), False


def estimate_max_llm_calls(
    file_count: int,
    attempts_per_chunk: int = 4,
    llm_agents: int = 2,
) -> int:
    """Approximation de planification, sans lecture ni découpage des fichiers."""
    return max(0, int(file_count)) * max(0, int(attempts_per_chunk)) * max(0, int(llm_agents))


def quota_risk_level(estimated_calls: int, remaining_calls: int) -> str:
    estimated = max(0, int(estimated_calls))
    remaining = max(0, int(remaining_calls))
    if estimated > remaining:
        return "INSUFFICIENT"
    if remaining == 0 or estimated <= remaining * 0.5:
        return "LOW"
    if estimated <= remaining * 0.8:
        return "MEDIUM"
    return "HIGH"


def render_agent_card(
    name: str,
    historical_detections: Any,
    audit_records: Any = None,
    records_with_status: Any = None,
    successful_audits: Any = None,
    failed_audits: Any = None,
    metadata_coverage: Any = None,
    success_rate: Any = None,
    *,
    status: str = "UNKNOWN",
    not_run: Any = None,
    unknown: Any = None,
) -> None:
    with st.container(border=True):
        st.markdown(f"#### {escape(str(name))}")
        st.markdown(status_badge_html(status), unsafe_allow_html=True)
        first = st.columns(4)
        first[0].metric("Historical detections", format_optional_number(historical_detections))
        first[1].metric("Audit file records", format_optional_number(audit_records))
        first[2].metric("Records with status metadata", format_optional_number(records_with_status))
        first[3].metric(
            "Status metadata coverage",
            format_optional_number(metadata_coverage, 1, suffix="%"),
        )
        second = st.columns(4)
        second[0].metric("Successful", format_optional_number(successful_audits))
        second[1].metric("Failed", format_optional_number(failed_audits))
        second[2].metric("Not run", format_optional_number(not_run))
        second[3].metric(
            "Success rate (known executions)",
            format_optional_number(success_rate, 1, suffix="%"),
        )
        if unknown is not None and not is_missing(unknown) and int(unknown) > 0:
            st.caption(f"Status metadata unavailable for {int(unknown)} audit file record(s).")
        st.caption("Success rate uses executed statuses only (SUCCESS + FAILED); NOT_RUN is excluded.")


def render_sidebar_brand() -> None:
    st.sidebar.markdown(
        '<div class="sidebar-brand"><div class="sidebar-brand-title">Data Quality</div>'
        '<div class="sidebar-brand-subtitle">Audit dashboard</div></div>',
        unsafe_allow_html=True,
    )


def render_filtered_view_indicator(active: bool) -> None:
    if active:
        st.sidebar.caption("Filtered view — metrics and tables reflect the active filters.")


def active_filter_labels(filters: Mapping[str, Any] | None) -> list[str]:
    if not filters:
        return []
    labels = {
        "load_lock": "LOAD_LOCK",
        "verdict": "Judge Verdict",
        "pipeline_status": "Pipeline Status",
        "grade": "Grade",
        "filename": "Filename",
        "date_start": "Start date",
        "date_end": "End date",
    }
    result = []
    for key, label in labels.items():
        value = filters.get(key)
        if value in (None, "", [], ()):
            continue
        if isinstance(value, (list, tuple, set)):
            shown = ", ".join(str(item) for item in value)
        else:
            shown = str(value)
        result.append(f"{label}: {shown}")
    return result


def render_filtered_view_summary(
    counts: Mapping[str, int],
    active: bool,
    filters: Mapping[str, Any] | None = None,
) -> None:
    if not active:
        return
    details = active_filter_labels(filters)
    suffix = f" Active filters — {'; '.join(details)}." if details else ""
    st.info(
        "**Filtered view** — "
        f"{counts['displayed_executions']} of {counts['loaded_executions']} executions; "
        f"{counts['displayed_files']} of {counts['loaded_files']} audited files."
        f"{suffix}"
    )
    st.sidebar.markdown("**Filtered view**")
    st.sidebar.caption(
        f"{counts['displayed_executions']} of {counts['loaded_executions']} executions"
    )
    st.sidebar.caption(f"{counts['displayed_files']} of {counts['loaded_files']} audited files")


def _info_value_html(label: Any, value: Any) -> str:
    semantic_values = {
        "ACCEPTED", "WARNING", "REJECTED", "INCOMPLETE", "SUCCESS", "FAILED",
        "COMPLETE", "COMPLETED", "NOT RUN", "UNKNOWN", "NO FILE PROCESSED",
        "CRITICAL", "HIGH", "MEDIUM", "LOW",
    }
    if str(label).strip().lower() in {
        "status", "execution status", "pipeline status", "judge verdict", "outcome",
    } or status_label(value) in semantic_values:
        return status_badge_html(value)
    return escape(display_value(value))


def render_info_grid(items: Mapping[str, Any] | Iterable[tuple[str, Any]]) -> None:
    pairs = list(items.items()) if isinstance(items, Mapping) else list(items)
    if not pairs:
        return
    columns = st.columns(min(3, len(pairs)))
    for index, (label, value) in enumerate(pairs):
        css_class = "info-item"
        if str(label).strip().lower() == "quota risk":
            risk = str(value or "unknown").strip().lower()
            css_class += f" quota-risk quota-risk-{escape(risk)}"
        with columns[index % len(columns)]:
            st.markdown(
                f'<div class="{css_class}"><div class="info-label">{escape(str(label))}</div>'
                f'<div class="info-value">{_info_value_html(label, value)}</div></div>',
                unsafe_allow_html=True,
            )


def render_compact_info_grid(items: Iterable[tuple[str, Any]]) -> None:
    """Responsive compact detail grid, including semantic status badges."""
    cards = [
        '<div class="compact-info-item">'
        f'<div class="info-label">{escape(str(label))}</div>'
        f'<div class="compact-info-value">{_info_value_html(label, value)}</div>'
        '</div>'
        for label, value in items
    ]
    st.markdown(f'<div class="compact-info-grid">{"".join(cards)}</div>', unsafe_allow_html=True)


def status_cell_style(value: Any) -> str:
    """Subtle semantic cell styling for client-facing Streamlit tables."""
    label = status_label(value)
    palette = {
        "ACCEPTED": ("#eafbf1", "#067647"),
        "SUCCESS": ("#eafbf1", "#067647"),
        "COMPLETE": ("#eafbf1", "#067647"),
        "COMPLETED": ("#eafbf1", "#067647"),
        "WARNING": ("#fff7e6", "#a15c00"),
        "MEDIUM": ("#fff7e6", "#a15c00"),
        "HIGH": ("#fff2e8", "#c2410c"),
        "REJECTED": ("#fff0ef", "#b42318"),
        "FAILED": ("#fff0ef", "#b42318"),
        "CRITICAL": ("#fff0ef", "#b42318"),
        "INCOMPLETE": ("#f1f4f8", "#526177"),
        "NOT RUN": ("#f1f4f8", "#526177"),
        "UNKNOWN": ("#f1f4f8", "#526177"),
        "NO FILE PROCESSED": ("#f1f4f8", "#526177"),
        "LOW": ("#f1f4f8", "#526177"),
        "N/A": ("#f1f4f8", "#526177"),
    }
    background, color = palette.get(label, ("transparent", "inherit"))
    return (
        f"background-color: {background}; color: {color}; font-weight: 700; "
        "border-radius: 999px; text-align: center"
    )


def style_status_columns(frame: pd.DataFrame, columns: Iterable[str]):
    existing = [column for column in columns if column in frame.columns]
    if not existing:
        return frame
    return frame.style.map(status_cell_style, subset=existing)


def render_outcome_bar_chart(counts: pd.Series) -> None:
    charts.show(charts.donut(counts, center_label="audit records"))


def render_file_bar_chart(
    values: pd.Series,
    *,
    title: str,
    subtitle: str,
    value_title: str,
    color: str = "#4f46e5",
) -> None:
    """Horizontal file chart with one row per filename and dynamic height."""
    numeric = pd.to_numeric(values, errors="coerce").dropna().sort_values(ascending=False)
    if numeric.empty:
        st.info("No data available for the current selection.")
        return
    if title:
        st.markdown(f"**{title}**")
    if subtitle:
        st.caption(subtitle)
    charts.show(charts.horizontal_bars(numeric, value_title=value_title, color=color))


def render_timeline_notice(state: str) -> None:
    messages = {
        "missing": "Trend data is not available for the selected view.",
        "zero": "No detections were recorded for the selected view.",
        "insufficient": "At least two data points are required to display a trend.",
    }
    st.info(messages.get(state, "Trend data is not available."))


def render_horizontal_bar_chart(
    series: pd.Series,
    *,
    title: str,
    category_title: str,
    value_title: str,
    color: str = "#3157a4",
) -> None:
    del category_title
    if title:
        st.caption(title)
    charts.show(charts.horizontal_bars(series, value_title=value_title, color=color))


def render_verdict_chart(series: pd.Series) -> None:
    charts.show(
        charts.horizontal_bars(
            series,
            value_title="Audit file records",
            color_map=charts.OUTCOME_COLORS,
        )
    )


def render_utc_line_chart(series: pd.Series, *, title: str, value_title: str) -> None:
    if title:
        st.caption(title)
    charts.show(charts.line_series(series, value_title=value_title))


def local_artifact_path(reference: Any) -> Path | None:
    return resolve_local_artifact(reference)


def render_artifact_access(
    label: str,
    reference: Any,
    *,
    mime_type: str,
    download_label: str,
    storage_mode: str = "local",
    artifact_kind: str = "binary",
    action_label: str | None = None,
    key: str | None = None,
    expected_run_id: Any = None,
) -> bool:
    """Charge un artefact uniquement après l'action explicite de l'utilisateur."""
    st.markdown(f"#### {escape(label)}")
    status = artifact_reference_status(reference, storage_mode)
    status_slot = st.empty()

    def _show_status(value: str) -> None:
        status_class = {
            "AVAILABLE": "success",
            "REFERENCED": "warning",
            "NOT AVAILABLE": "unknown",
            "ERROR": "error",
        }.get(value, "unknown")
        status_slot.markdown(
            f'<span class="status-badge status-{status_class}">{escape(value)}</span>',
            unsafe_allow_html=True,
        )

    _show_status(status)
    if status == "NOT AVAILABLE":
        st.caption("No artifact reference was stored for this audit record.")
        return False
    if artifact_kind == "html" and expected_run_id and not is_versioned_run_reference(reference, expected_run_id):
        st.warning(
            "Historical report reference is not versioned by run. It may be a legacy latest alias."
        )

    digest = hashlib.sha256(f"{storage_mode}:{reference}:{label}".encode("utf-8")).hexdigest()[:16]
    state_key = key or f"artifact_loaded_{digest}"
    if st.button(
        action_label or ("Open report" if artifact_kind == "html" else f"Load {label.lower()}"),
        key=f"{state_key}_button",
        use_container_width=True,
    ):
        st.session_state[state_key] = True
    if not st.session_state.get(state_key):
        st.caption("Content is loaded on demand from the persisted reference.")
        return False

    result = read_artifact_cached(str(reference), str(storage_mode or "local"), config.S3_BUCKET)
    if result.status != "AVAILABLE" or result.payload is None:
        _show_status("ERROR" if result.status == "ERROR" else "NOT AVAILABLE")
        st.error(result.error or "The referenced artifact could not be loaded.")
        with st.expander("Technical reference"):
            st.code(str(reference), language="text")
        return False

    payload = result.payload
    _show_status("AVAILABLE")
    file_name = Path(str(reference).replace("\\", "/")).name or "artifact"
    if artifact_kind == "html":
        try:
            html_payload = payload.decode("utf-8")
        except UnicodeDecodeError:
            st.error("The report is not valid UTF-8 HTML.")
        else:
            st_components.html(html_payload, height=640, scrolling=True)
    elif artifact_kind == "json":
        document, parsed = read_json_artifact(
            reference,
            storage_mode,
            reader=lambda _ref, _mode: result,
        )
        if document is None:
            st.error(parsed.error or "The verdict JSON could not be decoded.")
        else:
            anomalies = document.get("anomalies_consolidees", document.get("anomalies", []))
            anomalies = anomalies if isinstance(anomalies, list) else []
            render_info_grid(
                [
                    ("Pipeline status", display_value(document.get("pipeline_status"))),
                    ("Judge verdict", display_value(document.get("verdict"))),
                    ("Score", format_optional_number(document.get("score"), 2)),
                    ("Grade", display_value(document.get("grade"))),
                    ("Detailed anomalies", len(anomalies)),
                    (
                        "Decision reason",
                        display_value(document.get("decision_reason") or document.get("justification")),
                    ),
                ]
            )
            with st.expander("Raw verdict JSON"):
                st.json(document)
    st.download_button(
        download_label,
        data=payload,
        file_name=file_name,
        mime=mime_type_for_reference(reference, mime_type),
        key=f"{state_key}_download",
        use_container_width=True,
    )
    return True


def render_artifact_references(items: Iterable[tuple[str, Any]]) -> None:
    """Backward-compatible compact artifact status renderer."""
    for label, value in items:
        render_artifact_access(
            label,
            value,
            mime_type="application/octet-stream",
            download_label=f"Download {label.lower()}",
        )


def friendly_file_label(filename: Any, load_lock: Any, timestamp: Any) -> str:
    parts = [display_value(filename)]
    if not is_missing(load_lock):
        parts.append(str(load_lock))
    if not is_missing(timestamp):
        try:
            parsed = pd.to_datetime(timestamp, utc=True)
            parts.append(parsed.strftime("%d %b %H:%M"))
        except (TypeError, ValueError):
            pass
    return " — ".join(parts)
