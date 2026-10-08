"""Execution history page."""

from __future__ import annotations

import json
from typing import Any

import pandas as pd
import streamlit as st

from dashboard.components import (
    display_value,
    format_datetime,
    format_duration,
    format_optional_number,
    format_short_reason,
    format_short_run_id,
    is_missing,
    render_empty_state,
    render_info_grid,
    render_page_header,
    status_badge_html,
)
from dashboard.transformations import display_columns
from dashboard.validation import (
    history_agent_coverage,
    history_metric_coverage,
    validate_history_run,
)


def _run_payload(row: pd.Series, run_files: pd.DataFrame) -> dict[str, Any]:
    raw = row.get("raw_history")
    if isinstance(raw, dict):
        return raw
    summary_fields = (
        "files_processed",
        "accepted",
        "warning",
        "rejected",
        "incomplete",
        "quarantined",
        "total_anomalies",
        "anomalies_by_agent",
        "anomalies_by_severity",
    )
    summary = {name: row.get(name) for name in summary_fields if name in row and not is_missing(row.get(name))}
    files = run_files.to_dict("records") if not run_files.empty else []
    return {"run_id": row.get("run_id"), "summary": summary, "files": files}


def _health_badge(status: str) -> str:
    return status_badge_html(status)


def format_stop_reason(status: Any, reason: Any) -> str:
    no_reason = is_missing(reason) or str(reason).strip().lower() in {
        "",
        "not stored",
        "none",
        "null",
        "nan",
        "n/a",
    }
    if str(status or "").upper() == "COMPLETED" and no_reason:
        return "—"
    return "—" if no_reason else str(reason)


def render(
    runs_df: pd.DataFrame,
    files_df: pd.DataFrame | None = None,
    raw_runs: list[dict] | None = None,
) -> None:
    render_page_header("Runs", "Execution history, completion state, and persisted-record consistency.")
    files_df = files_df if files_df is not None else pd.DataFrame()
    raw_by_id = {
        run.get("run_id"): run
        for run in (raw_runs or [])
        if isinstance(run, dict) and run.get("run_id")
    }
    if runs_df.empty:
        render_empty_state("No execution history", "No audit execution is available for the selected view.")
        return

    table = pd.DataFrame(index=runs_df.index)
    table["Date"] = runs_df.get("started_at", pd.Series(index=runs_df.index)).map(format_datetime)
    table["Run ID"] = runs_df.get("run_id", pd.Series(index=runs_df.index)).map(
        format_short_run_id
    )
    table["Execution Status"] = runs_df.get("run_status", pd.Series(index=runs_df.index)).map(display_value)
    table["Audit File Records"] = runs_df.get("files_processed", pd.Series(index=runs_df.index)).map(format_optional_number)
    table["Accepted"] = runs_df.get("accepted", pd.Series(index=runs_df.index)).map(format_optional_number)
    table["Warning"] = runs_df.get("warning", pd.Series(index=runs_df.index)).map(format_optional_number)
    table["Rejected"] = runs_df.get("rejected", pd.Series(index=runs_df.index)).map(format_optional_number)
    table["Incomplete"] = runs_df.get("incomplete", pd.Series(index=runs_df.index)).map(format_optional_number)
    table["Anomalies"] = runs_df.get("total_anomalies", pd.Series(index=runs_df.index)).map(format_optional_number)
    table["Duration"] = runs_df.get("duration_seconds", pd.Series(index=runs_df.index)).map(format_duration)

    health = []
    for _, row in runs_df.iterrows():
        run_id = row.get("run_id")
        related = files_df[files_df["run_id"] == run_id] if "run_id" in files_df else pd.DataFrame()
        payload = raw_by_id.get(run_id) or _run_payload(row, related)
        health.append(validate_history_run(payload).status)
    table["Record Consistency"] = health
    st.dataframe(table, hide_index=True, use_container_width=True)
    st.caption(
        "Execution Status describes whether the pipeline run finished. "
        "Audit Outcomes describe the quality-analysis results of processed files."
    )

    st.subheader("Run Details")
    choices = list(runs_df.index)
    selected = st.selectbox(
        "Select an execution",
        choices,
        format_func=lambda index: f"{format_short_run_id(runs_df.loc[index].get('run_id'))} — "
        f"{format_datetime(runs_df.loc[index].get('started_at'))} — "
        f"{display_value(runs_df.loc[index].get('run_status'))}",
    )
    row = runs_df.loc[selected]
    run_id = row.get("run_id")
    related = files_df[files_df["run_id"] == run_id] if "run_id" in files_df else pd.DataFrame()
    raw_payload = raw_by_id.get(run_id)
    payload = raw_payload or _run_payload(row, related)
    validation = validate_history_run(payload)
    coverage = history_metric_coverage(payload)
    agent_coverage = history_agent_coverage(payload)

    st.markdown("#### Execution Status")
    st.markdown(status_badge_html(row.get("run_status")), unsafe_allow_html=True)
    render_info_grid(
        [
            ("Run ID", display_value(run_id)),
            ("Started", format_datetime(row.get("started_at"))),
            ("Duration", format_duration(row.get("duration_seconds"))),
            ("Files processed", format_optional_number(row.get("files_processed"))),
            ("Stop reason", format_stop_reason(row.get("run_status"), row.get("stop_reason"))),
        ]
    )

    st.markdown("#### Audit Outcomes")
    render_info_grid(
        [
            ("Accepted", format_optional_number(row.get("accepted"))),
            ("Warning", format_optional_number(row.get("warning"))),
            ("Rejected", format_optional_number(row.get("rejected"))),
            ("Incomplete", format_optional_number(row.get("incomplete"))),
        ]
    )

    st.markdown("#### Record Consistency")
    st.markdown(_health_badge(validation.status), unsafe_allow_html=True)
    if validation.issues:
        st.warning("Persisted history contains inconsistencies. Displayed values have not been corrected automatically.")
        for issue in validation.issues:
            st.caption(f"- {issue}")
    else:
        st.caption("No consistency issue was detected in the available persisted fields.")

    st.markdown("#### Record Completeness")
    st.markdown(status_badge_html(coverage.status), unsafe_allow_html=True)
    if coverage.missing:
        st.caption("Optional historical metrics not stored: " + ", ".join(coverage.missing) + ".")
    elif coverage.status == "N/A":
        st.caption("Metric coverage does not apply because this execution processed no file.")
    else:
        st.caption("Severity and decision reason are available for every processed file record.")

    st.markdown("#### Agent Analysis Coverage")
    st.markdown(status_badge_html(agent_coverage.status), unsafe_allow_html=True)
    if agent_coverage.total:
        st.caption(
            f"Agents successful: {agent_coverage.successful}/{agent_coverage.total} · "
            f"Failed: {agent_coverage.failed} · Not run: {agent_coverage.not_run} · "
            f"Unknown: {agent_coverage.unknown}"
        )
        labels = {
            "structure": "Structure",
            "stats": "Statistics",
            "coherence": "Coherence",
            "realisme": "Realism",
        }
        render_info_grid(
            [(labels[key], value) for key, value in agent_coverage.agent_statuses.items()]
        )
    else:
        st.caption("Agent coverage does not apply because this execution processed no file.")

    if row.get("run_status") == "NO FILE PROCESSED" or related.empty:
        st.info("This execution is retained in history even though no file was processed.")
    else:
        st.markdown("#### Processed Files")
        columns = [
            name
            for name in (
                "filename",
                "load_lock",
                "pipeline_status",
                "verdict",
                "score",
                "anomalies_total",
                "decision_reason",
            )
            if name in related
        ]
        prepared = related[columns].copy()
        if "decision_reason" in prepared:
            prepared["decision_reason"] = prepared.apply(
                lambda item: format_short_reason(
                    item.get("decision_reason"),
                    item.get("pipeline_status"),
                    item.get("verdict"),
                ),
                axis=1,
            )
        visible = display_columns(prepared)
        for column in ("Filename", "LOAD_LOCK", "Pipeline Status", "Judge Verdict"):
            if column in visible:
                visible[column] = visible[column].map(display_value)
        if "Score" in visible:
            visible["Score"] = visible["Score"].map(lambda value: format_optional_number(value, 2))
        st.dataframe(visible, hide_index=True, use_container_width=True)

    with st.expander("Technical details"):
        raw = raw_payload or row.get("raw_history")
        if isinstance(raw, dict):
            st.json(raw)
        else:
            technical = {key: value for key, value in row.to_dict().items() if not is_missing(value)}
            st.code(json.dumps(technical, default=str, indent=2), language="json")
