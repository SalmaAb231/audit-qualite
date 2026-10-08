"""Audited files page."""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from dashboard.components import (
    display_value,
    format_bytes,
    format_datetime,
    format_decision_reason,
    format_optional_number,
    friendly_file_label,
    is_missing,
    render_artifact_references,
    render_empty_state,
    render_info_grid,
    render_page_header,
)


def _first(record: pd.Series, *names: str) -> Any:
    for name in names:
        value = record.get(name)
        if not is_missing(value):
            return value
    return None


def _detail_pairs(record: pd.Series, fields: list[tuple[str, tuple[str, ...]]]) -> list[tuple[str, Any]]:
    pairs: list[tuple[str, Any]] = []
    for label, names in fields:
        value = _first(record, *names)
        if value is not None:
            pairs.append((label, value))
    return pairs


def severity_for_file(record: pd.Series, severity_df: pd.DataFrame) -> pd.Series:
    if severity_df.empty or "severity" not in severity_df or "count" not in severity_df:
        return pd.Series(dtype="float64")
    mask = pd.Series(True, index=severity_df.index)
    for column in ("run_id", "filename", "load_lock"):
        if column in severity_df and column in record:
            mask &= severity_df[column].eq(record.get(column))
    selected = severity_df.loc[mask].copy()
    if selected.empty:
        return pd.Series(dtype="float64")
    selected["_count"] = pd.to_numeric(selected["count"], errors="coerce")
    totals = selected.groupby("severity")["_count"].sum(min_count=1)
    order = [value for value in ("critical", "high", "medium", "low", "unknown") if value in totals]
    totals = totals.reindex(order)
    totals.index = [str(value).title() for value in totals.index]
    return totals


def displayed_decision_reason(record: pd.Series) -> str:
    return format_decision_reason(
        _first(record, "decision_reason", "reason", "verdict_reason"),
        record.get("pipeline_status"),
        record.get("verdict"),
    )


def render(
    files_df: pd.DataFrame,
    agents_df: pd.DataFrame | None = None,
    severity_df: pd.DataFrame | None = None,
) -> None:
    agents_df = agents_df if agents_df is not None else pd.DataFrame()
    severity_df = severity_df if severity_df is not None else pd.DataFrame()
    render_page_header("Files", "Inspect audited files, verdicts, detections, and stored artifact references.")
    if files_df.empty:
        render_empty_state("No audited files", "The selected view contains no processed file.")
        return

    table = files_df.copy()
    table["Date"] = table.get("timestamp", pd.Series(index=table.index)).map(format_datetime)
    table["Filename"] = table.get("filename", pd.Series(index=table.index)).map(display_value)
    table["LOAD_LOCK"] = table.get("load_lock", pd.Series(index=table.index)).map(display_value)
    table["Pipeline Status"] = table.get("pipeline_status", pd.Series(index=table.index)).map(display_value)
    table["Judge Verdict"] = table.get("verdict", pd.Series(index=table.index)).map(display_value)
    table["Score"] = table.get("score", pd.Series(index=table.index)).map(lambda value: format_optional_number(value, 2))
    table["Anomalies"] = table.get("anomalies_total", pd.Series(index=table.index)).map(format_optional_number)
    table["Quarantined"] = table.get("quarantined", pd.Series(index=table.index)).map(
        lambda value: "N/A" if is_missing(value) else ("Yes" if bool(value) else "No")
    )
    st.dataframe(
        table[["Date", "Filename", "LOAD_LOCK", "Pipeline Status", "Judge Verdict", "Score", "Anomalies", "Quarantined"]],
        hide_index=True,
        use_container_width=True,
    )

    st.subheader("File Details")
    choices = list(files_df.index)
    selected = st.selectbox(
        "Select an audited file",
        choices,
        format_func=lambda index: friendly_file_label(
            files_df.loc[index].get("filename"),
            files_df.loc[index].get("load_lock"),
            files_df.loc[index].get("timestamp"),
        ),
    )
    record = files_df.loc[selected]

    metadata = _detail_pairs(
        record,
        [
            ("Run ID", ("run_id",)),
            ("LOAD_LOCK", ("load_lock",)),
            ("Audit date", ("timestamp",)),
            ("File size", ("size", "size_bytes")),
            ("Source", ("source", "storage_mode")),
        ],
    )
    metadata = [
        (
            label,
            format_datetime(value)
            if label == "Audit date"
            else format_bytes(value)
            if label == "File size"
            else value,
        )
        for label, value in metadata
    ]
    artifact_items = [
        ("Report", _first(record, "report_key", "report_s3_key", "report_html")),
        ("Verdict JSON", _first(record, "verdict_key", "verdict_s3_key", "verdict_json")),
        ("Quarantine object", _first(record, "quarantine_key", "quarantine_s3_key")),
    ]
    mask = pd.Series(True, index=agents_df.index)
    for column in ("run_id", "filename", "load_lock"):
        if column in agents_df:
            mask &= agents_df[column].eq(record.get(column))
    selected_agents = agents_df.loc[mask].copy() if not agents_df.empty else pd.DataFrame()

    summary_tab, detections_tab, agents_tab, metadata_tab, artifacts_tab, technical_tab = st.tabs(
        ["Summary", "Detections", "Agent Results", "Metadata", "Artifacts", "Technical Details"]
    )
    with summary_tab:
        render_info_grid(
            [
                ("Pipeline Status", display_value(record.get("pipeline_status"))),
                ("Judge Verdict", display_value(record.get("verdict"))),
                ("Score", format_optional_number(record.get("score"), 2)),
                ("Anomalies", format_optional_number(record.get("anomalies_total"))),
                ("Quarantined", "N/A" if is_missing(record.get("quarantined")) else ("Yes" if record.get("quarantined") else "No")),
                ("Decision reason", displayed_decision_reason(record)),
            ]
        )
    with detections_tab:
        severity = severity_for_file(record, severity_df)
        if severity.empty:
            st.info("Severity breakdown is not available for this historical record.")
        else:
            nonzero = severity[severity > 0]
            if nonzero.empty:
                st.success("No anomaly was recorded in the available severity counters.")
            else:
                render_info_grid([(label, format_optional_number(value)) for label, value in nonzero.items()])
    with agents_tab:
        if selected_agents.empty:
            st.info("Agent execution metadata is not available for this historical record.")
        else:
            view = selected_agents[["agent", "status", "count"]].copy()
            view.columns = ["Agent", "Execution Status", "Detections"]
            view["Detections"] = view["Detections"].map(format_optional_number)
            st.dataframe(view, hide_index=True, use_container_width=True)
    with metadata_tab:
        if metadata:
            render_info_grid(metadata)
        else:
            st.info("No additional metadata was stored for this record.")
    with artifacts_tab:
        if any(not is_missing(value) for _, value in artifact_items):
            render_artifact_references(artifact_items)
        else:
            st.info("No artifact reference was stored for this record.")
    with technical_tab:
        st.caption("Raw persisted values are shown here for diagnosis; they do not override the audit outcome.")
        technical_reason = _first(record, "decision_reason", "reason", "verdict_reason")
        st.code(str(technical_reason) if not is_missing(technical_reason) else "Not stored", language="text")
        raw_details = pd.DataFrame(
            {
                "Field": list(record.index),
                "Stored value": ["Not stored" if is_missing(value) else str(value) for value in record.values],
            }
        )
        st.dataframe(raw_details, hide_index=True, use_container_width=True)
