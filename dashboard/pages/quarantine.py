"""Historical quarantine page."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from dashboard.components import (
    display_value,
    format_datetime,
    format_optional_number,
    format_short_reason,
    format_user_reason,
    render_empty_state,
    render_artifact_access,
    render_info_grid,
    render_page_header,
    style_status_columns,
)
from dashboard.transformations import quarantine_metrics


def quarantine_history_counts(files_df: pd.DataFrame) -> tuple[int, int, int]:
    """Retourne événements, fichiers uniques et enregistrements au statut connu."""
    metrics = quarantine_metrics(files_df)
    return metrics["events"], metrics["unique_files"], metrics["known_records"]


def render(files_df: pd.DataFrame, agents_df: pd.DataFrame | None = None) -> None:
    agents_df = agents_df if agents_df is not None else pd.DataFrame()
    render_page_header(
        "Quarantine",
        "Historical quarantine information recorded at audit time; this is not the current S3 quarantine state.",
    )
    st.subheader("Historical Quarantine Events")
    if files_df.empty or "quarantined" not in files_df:
        render_empty_state("No quarantine history", "No historical quarantine flag is available for this selection.")
        return

    known = files_df["quarantined"].notna()
    quarantined = files_df.loc[known & files_df["quarantined"].fillna(False).astype(bool)].copy()
    events_count, unique_files_count, known_count = quarantine_history_counts(files_df)
    columns = st.columns(3)
    columns[0].metric("Quarantine events", format_optional_number(events_count))
    columns[1].metric(
        "Unique quarantined files",
        format_optional_number(unique_files_count),
    )
    columns[2].metric("Records with known status", format_optional_number(known_count))

    if quarantined.empty:
        st.success("No historical quarantine event is recorded in the selected view.")
        return

    st.subheader("Quarantine Event Records")
    visible = pd.DataFrame(index=quarantined.index)
    visible["Date"] = quarantined.get("timestamp", pd.Series(index=quarantined.index)).map(format_datetime)
    visible["Filename"] = quarantined.get("filename", pd.Series(index=quarantined.index)).map(display_value)
    visible["LOAD_LOCK"] = quarantined.get("load_lock", pd.Series(index=quarantined.index)).map(display_value)
    visible["Pipeline Status"] = quarantined.get(
        "pipeline_status", pd.Series(index=quarantined.index)
    ).map(display_value)
    visible["Judge Verdict"] = quarantined.get(
        "verdict", pd.Series(index=quarantined.index)
    ).map(display_value)
    reason = quarantined.get(
        "decision_reason",
        quarantined.get(
            "quarantine_reason",
            quarantined.get("reason", pd.Series(index=quarantined.index)),
        ),
    )
    visible["Short Reason"] = [
        format_short_reason(value, pipeline, verdict)
        for value, pipeline, verdict in zip(
            reason,
            quarantined.get("pipeline_status", pd.Series(index=quarantined.index)),
            quarantined.get("verdict", pd.Series(index=quarantined.index)),
        )
    ]
    key_column = next(
        (name for name in ("quarantine_key", "quarantine_s3_key") if name in quarantined),
        None,
    )
    st.dataframe(
        style_status_columns(visible, ["Pipeline Status", "Judge Verdict"]),
        hide_index=True,
        use_container_width=True,
    )

    st.subheader("Quarantine Event Details")
    selected = st.selectbox(
        "Select a quarantine event",
        list(quarantined.index),
        format_func=lambda index: (
            f"{display_value(quarantined.loc[index].get('filename'))} — "
            f"{format_datetime(quarantined.loc[index].get('timestamp'))}"
        ),
    )
    record = quarantined.loc[selected]
    reason_value = record.get("decision_reason")
    if pd.isna(reason_value) or not str(reason_value).strip():
        reason_value = record.get("quarantine_reason")
    summary_tab, artifacts_tab, agents_tab, technical_tab = st.tabs(
        ["Summary", "Artifacts", "Agent Results", "Technical Details"]
    )
    with summary_tab:
        render_info_grid(
            [
                ("File", display_value(record.get("filename"))),
                ("Pipeline Status", display_value(record.get("pipeline_status"))),
                ("Judge Verdict", display_value(record.get("verdict"))),
                ("Anomalies", format_optional_number(record.get("anomalies_total"))),
                (
                    "Reason",
                    format_user_reason(
                        reason_value,
                        record.get("pipeline_status"),
                        record.get("verdict"),
                    ),
                ),
                ("Quarantine object", display_value(record.get(key_column)) if key_column else "N/A"),
            ]
        )
    with artifacts_tab:
        source_column, quarantine_column, report_column = st.columns(3)
        with source_column:
            render_artifact_access(
                "Original source",
                record.get("source"),
                mime_type="application/octet-stream",
                download_label="Download original source",
                storage_mode=record.get("storage_mode") or "local",
                key=f"quarantine_source_{record.get('run_id')}_{selected}",
            )
        with quarantine_column:
            render_artifact_access(
                "Quarantine copy",
                record.get(key_column) if key_column else None,
                mime_type="application/octet-stream",
                download_label="Download quarantine copy",
                storage_mode=record.get("storage_mode") or "local",
                key=f"quarantine_copy_{record.get('run_id')}_{selected}",
            )
        with report_column:
            render_artifact_access(
                "Associated audit report",
                record.get("report_html"),
                mime_type="text/html",
                download_label="Download audit report",
                storage_mode=record.get("storage_mode") or "local",
                artifact_kind="html",
                action_label="View audit report",
                key=f"quarantine_report_{record.get('run_id')}_{selected}",
                expected_run_id=record.get("run_id"),
            )
    with agents_tab:
        if agents_df.empty:
            st.info("Agent execution metadata is not available for this historical event.")
        else:
            mask = pd.Series(True, index=agents_df.index)
            for column in ("run_id", "filename", "load_lock"):
                if column in agents_df:
                    mask &= agents_df[column].eq(record.get(column))
            agent_records = agents_df.loc[mask, ["agent", "status", "count", "error"]].copy()
            if agent_records.empty:
                st.info("Agent execution metadata is not available for this historical event.")
            else:
                client_agent_records = agent_records[["agent", "status", "count"]].copy()
                client_agent_records.columns = ["Agent", "Status", "Detections"]
                st.dataframe(
                    style_status_columns(client_agent_records, ["Status"]),
                    hide_index=True,
                    use_container_width=True,
                )
    with technical_tab:
        st.dataframe(
            pd.DataFrame(
                {
                    "Field": list(record.index),
                    "Stored value": ["Not stored" if pd.isna(value) else str(value) for value in record.values],
                }
            ),
            hide_index=True,
            use_container_width=True,
        )
        if not agents_df.empty:
            technical_agents = agents_df.loc[mask, ["agent", "status", "error"]].copy()
            technical_agents = technical_agents[technical_agents["error"].notna()]
            if not technical_agents.empty:
                technical_agents.columns = ["Agent", "Status", "Technical error"]
                st.dataframe(technical_agents, hide_index=True, use_container_width=True)
    technical = pd.DataFrame(
        {
            "Date": visible["Date"],
            "Filename": visible["Filename"],
            "Technical reason": reason.map(lambda value: "N/A" if pd.isna(value) else str(value)),
        }
    )
    with st.expander("All event reasons"):
        st.dataframe(technical, hide_index=True, use_container_width=True)
    st.caption("Entries reflect persisted audit history and may differ from the current object state in S3.")
