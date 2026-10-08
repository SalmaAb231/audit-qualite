"""Aggregate anomaly analysis page."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from dashboard import charts
from dashboard.components import (
    format_optional_number,
    render_empty_state,
    render_file_bar_chart,
    render_horizontal_bar_chart,
    render_page_header,
    render_timeline_notice,
)
from dashboard.transformations import (
    aggregate_agent_counts,
    historical_detections_by_file,
    latest_anomalies_by_file,
    time_series_state,
)


def _numeric_total(frame: pd.DataFrame, column: str):
    if frame.empty or column not in frame:
        return None
    return pd.to_numeric(frame[column], errors="coerce").sum(min_count=1)


def detection_source_totals(agents_df: pd.DataFrame) -> pd.Series:
    return aggregate_agent_counts(agents_df)


def severity_totals(severity_df: pd.DataFrame) -> pd.Series:
    if severity_df.empty or "severity" not in severity_df or "count" not in severity_df:
        return pd.Series(dtype="float64")
    data = severity_df.assign(_count=pd.to_numeric(severity_df["count"], errors="coerce"))
    totals = data.groupby("severity")["_count"].sum(min_count=1)
    order = [value for value in ("critical", "high", "medium", "low", "unknown") if value in totals]
    order.extend(value for value in totals.index if value not in order)
    totals = totals.reindex(order)
    totals.index = [str(value).title() for value in totals.index]
    return totals


def severity_availability_message(severity_df: pd.DataFrame) -> str | None:
    totals = severity_totals(severity_df)
    if totals.empty:
        return "Severity breakdown is not available for this historical record."
    if not (totals > 0).any():
        return "No anomaly was recorded in the available severity counters."
    return None


def affected_file_totals(files_df: pd.DataFrame) -> pd.Series:
    return historical_detections_by_file(files_df)


def latest_file_anomaly_totals(files_df: pd.DataFrame) -> pd.Series:
    return latest_anomalies_by_file(files_df)


def render(
    files_df: pd.DataFrame,
    agents_df: pd.DataFrame | None = None,
    severity_df: pd.DataFrame | None = None,
) -> None:
    agents_df = agents_df if agents_df is not None else pd.DataFrame()
    severity_df = severity_df if severity_df is not None else pd.DataFrame()
    render_page_header(
        "Anomalies",
        "Aggregate detection signals from audit history; this view does not represent live source state.",
    )
    if files_df.empty and agents_df.empty:
        render_empty_state("No anomaly data", "No processed file or agent detection is available for this selection.")
        return

    total = _numeric_total(files_df, "anomalies_total")
    known = files_df.get("anomalies_total", pd.Series(dtype="float64")).notna().sum()
    anomaly_values = pd.to_numeric(
        files_df.get("anomalies_total", pd.Series(dtype="float64")), errors="coerce"
    )
    impacted = int((anomaly_values > 0).sum())

    st.subheader("Summary")
    columns = st.columns(3)
    columns[0].metric("Historical anomaly detections", format_optional_number(total))
    columns[1].metric("Audit file records impacted", format_optional_number(impacted if known else None))
    columns[2].metric("Records with known totals", format_optional_number(known))
    if total is None or pd.isna(total):
        st.info("Anomaly totals are not available in the selected history records.")
    elif float(total) == 0:
        st.success("No anomaly was recorded in the selected history records.")
    elif files_df["filename"].duplicated().any():
        st.caption("Totals sum detections across audit file records; repeated audits are not deduplicated.")

    st.subheader("Detection Sources")
    agents = detection_source_totals(agents_df).dropna()
    agents = agents[agents > 0]
    if agents.empty:
        st.info("No non-zero agent detection total is available.")
    else:
        render_horizontal_bar_chart(
            agents,
            title="Historical detections by agent across audit records",
            category_title="Agent",
            value_title="Historical detections",
        )

    st.subheader("Severity")
    severities = severity_totals(severity_df)
    severity_message = severity_availability_message(severity_df)
    if severity_message:
        st.info(severity_message)
    else:
        nonzero_severities = severities[severities > 0]
        st.caption("Historical detections by severity across audit records.")
        charts.show(
            charts.horizontal_bars(
                nonzero_severities,
                value_title="Historical detections",
                color_map=charts.SEVERITY_COLORS,
                preserve_order=True,
            )
        )

    st.subheader("Latest Anomalies by File")
    latest = latest_file_anomaly_totals(files_df)
    if latest.empty:
        st.info("No latest per-file anomaly value is available.")
    else:
        render_file_bar_chart(
            latest,
            title="Latest anomaly count for each file",
            subtitle="Anomaly count from each file's latest audit record.",
            value_title="Latest audit anomalies",
            color="#475569",
        )

    st.subheader("Anomalies per Audit Over Time")
    st.caption("Number of anomaly detections recorded for each historical audit file record.")
    timeline_values = pd.to_numeric(
        files_df.get("anomalies_total", pd.Series(dtype="float64")), errors="coerce"
    )
    valid_timestamps = pd.to_datetime(
        files_df.get("timestamp", pd.Series(index=files_df.index, dtype="object")),
        errors="coerce",
        utc=True,
    )
    state = time_series_state(timeline_values[valid_timestamps.notna()])
    if state == "ready":
        charts.show(charts.anomalies_per_audit(files_df))
    else:
        render_timeline_notice(state)

    with st.expander("Historical cumulative analysis", expanded=False):
        st.markdown("#### Cumulative Historical Detections by File")
        affected = affected_file_totals(files_df)
        if affected.empty:
            st.info("No affected file is available.")
        else:
            render_file_bar_chart(
                affected,
                title="Cumulative historical detections",
                subtitle=(
                    "Sum of anomaly detections across all audit records. "
                    "Repeated audits are included; this is not the current anomaly count."
                ),
                value_title="Cumulative detections",
            )
