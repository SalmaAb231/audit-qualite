"""Executive overview page."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from dashboard import charts
from dashboard.components import (
    format_datetime,
    format_decision_reason,
    format_duration,
    format_optional_number,
    format_short_run_id,
    is_missing,
    render_empty_state,
    render_info_grid,
    render_metric_card,
    render_page_header,
    render_timeline_notice,
)
from dashboard.transformations import (
    aggregate_agent_counts,
    audit_outcome_counts as _audit_outcome_counts,
    dashboard_metrics,
    latest_audit_record,
    time_series_state,
)


def audit_outcome_counts(files_df: pd.DataFrame) -> pd.Series:
    """Compatibility facade returning the canonical four-outcome Series."""
    return pd.Series(_audit_outcome_counts(files_df), dtype="int64")


def _metric_grid(metrics: dict) -> None:
    items = [
        ("Audit Records", format_optional_number(metrics["audit_records"]), "Historical file records"),
        ("Unique Files", format_optional_number(metrics["unique_files"]), "Distinct source identities"),
        ("Executions", format_optional_number(metrics["executions"]), "Pipeline runs"),
        ("Avg. Quality Score", format_optional_number(metrics["average_quality_score"], 2), "Valid numeric scores"),
        ("Quarantine Events", format_optional_number(metrics["quarantine_events"]), "Historical events"),
        ("Historical Detections", format_optional_number(metrics["historical_detections"]), "Across audit records"),
    ]
    for column, (label, value, helper) in zip(st.columns(6), items):
        with column:
            render_metric_card(label, value, helper=helper)


def _quality_timeline(files_df: pd.DataFrame) -> pd.Series:
    if files_df.empty or not {"timestamp", "score"}.issubset(files_df.columns):
        return pd.Series(dtype="float64")
    data = files_df.assign(
        _timestamp=pd.to_datetime(files_df["timestamp"], errors="coerce", utc=True),
        _score=pd.to_numeric(files_df["score"], errors="coerce"),
    ).dropna(subset=["_timestamp", "_score"])
    return data.groupby("_timestamp")["_score"].mean().sort_index()


def _severity_totals(severity_df: pd.DataFrame) -> pd.Series:
    if severity_df.empty or not {"severity", "count"}.issubset(severity_df.columns):
        return pd.Series(dtype="float64")
    data = severity_df.assign(_count=pd.to_numeric(severity_df["count"], errors="coerce"))
    totals = data.groupby("severity")["_count"].sum(min_count=1).dropna()
    totals.index = totals.index.map(lambda value: str(value).title())
    return totals[totals > 0]


def _severity_summary(record: pd.Series, severity_df: pd.DataFrame) -> str | None:
    if severity_df.empty or not {"severity", "count"}.issubset(severity_df.columns):
        return None
    mask = pd.Series(True, index=severity_df.index)
    for column in ("run_id", "filename", "load_lock"):
        if column in severity_df:
            mask &= severity_df[column].eq(record.get(column))
    selected = severity_df.loc[mask].copy()
    if selected.empty:
        return None
    selected["_count"] = pd.to_numeric(selected["count"], errors="coerce")
    totals = selected.groupby(selected["severity"].astype(str).str.upper())["_count"].sum(min_count=1)
    parts = [
        f"{int(totals[level])} {level}"
        for level in ("CRITICAL", "HIGH", "MEDIUM", "LOW")
        if level in totals and pd.notna(totals[level]) and totals[level] > 0
    ]
    return " · ".join(parts) if parts else None


def render(
    runs_df: pd.DataFrame,
    files_df: pd.DataFrame,
    agents_df: pd.DataFrame | None = None,
    severity_df: pd.DataFrame | None = None,
) -> None:
    agents_df = agents_df if agents_df is not None else pd.DataFrame()
    severity_df = severity_df if severity_df is not None else pd.DataFrame()
    render_page_header("Overview", "Audit activity, quality outcomes, and detection signals at a glance.")

    metrics = dashboard_metrics(runs_df, files_df)
    _metric_grid(metrics)

    if runs_df.empty:
        render_empty_state("No execution history", "Run an audit to populate operational history.")
        return

    latest = latest_audit_record(files_df)
    if latest is not None:
        st.markdown("### Latest Audit")
        latest_run = runs_df[runs_df["run_id"].eq(latest.get("run_id"))]
        duration = latest_run.iloc[0].get("duration_seconds") if not latest_run.empty else None
        filename = latest.get("filename")
        outcome = latest.get("status")
        latest_items = [
            ("File", "—" if is_missing(filename) else str(filename)),
            ("Outcome", "—" if is_missing(outcome) else str(outcome)),
            ("Score", "—" if is_missing(latest.get("score")) else format_optional_number(latest.get("score"), 2)),
            ("Anomalies", format_optional_number(latest.get("anomalies_total"))),
            ("Duration", format_duration(duration)),
            ("Audit time", format_datetime(latest.get("timestamp"))),
        ]
        severity_summary = _severity_summary(latest, severity_df)
        if severity_summary:
            latest_items.append(("Severity summary", severity_summary))
        if not is_missing(outcome) and str(outcome).upper() == "INCOMPLETE":
            latest_items.append(
                (
                    "Reason",
                    format_decision_reason(
                        latest.get("decision_reason"),
                        latest.get("pipeline_status"),
                        latest.get("verdict"),
                    ),
                )
            )
        with st.container(border=True):
            render_info_grid(latest_items)

    left, right = st.columns(2)
    with left:
        with st.container(border=True):
            st.markdown("#### Audit Outcomes")
            st.caption("One outcome per historical audit file record.")
            if files_df.empty:
                st.info("No audit outcome is available for the selected view.")
            else:
                charts.show(charts.donut(audit_outcome_counts(files_df), center_label="audit records"))
    with right:
        with st.container(border=True):
            st.markdown("#### Quality Score Over Time")
            st.caption("Mean valid score by audit timestamp; incomplete records are excluded.")
            quality = _quality_timeline(files_df)
            state = time_series_state(quality)
            if state == "ready":
                charts.show(charts.line_series(quality, value_title="Quality score", color=charts.GREEN, decimals=2))
            else:
                render_timeline_notice(state)

    left, right = st.columns(2)
    with left:
        with st.container(border=True):
            st.markdown("#### Anomalies by Severity")
            st.caption("Historical detections across all selected audit records.")
            severity = _severity_totals(severity_df)
            if severity.empty:
                st.info("Severity breakdown is not available for this history.")
            else:
                ordered = severity.reindex(
                    [label for label in ("Critical", "High", "Medium", "Low") if label in severity.index]
                )
                charts.show(
                    charts.horizontal_bars(
                        ordered,
                        value_title="Historical detections",
                        color_map=charts.SEVERITY_COLORS,
                        preserve_order=True,
                    )
                )
    with right:
        with st.container(border=True):
            st.markdown("#### Agent Contribution")
            st.caption("Known historical detections; missing counters are not treated as zero.")
            agent_totals = aggregate_agent_counts(agents_df).dropna()
            agent_totals = agent_totals[agent_totals > 0]
            if agent_totals.empty:
                st.info("Agent detection totals are not available for this history.")
            else:
                charts.show(charts.horizontal_bars(agent_totals, value_title="Historical detections", color=charts.PURPLE))

    st.markdown("### Recent Activity")
    recent = runs_df.sort_values("started_at", ascending=False, na_position="last").head(5).copy()
    if recent.empty:
        st.info("No recent execution is available.")
    else:
        recent["Run"] = recent["run_id"].map(format_short_run_id)
        recent["Started"] = recent["started_at"].map(format_datetime)
        recent["Execution Status"] = recent["run_status"].fillna("N/A")
        recent["Files"] = pd.to_numeric(recent["files_processed"], errors="coerce")
        recent["Duration"] = recent["duration_seconds"].map(format_duration)
        st.dataframe(
            recent[["Run", "Started", "Duration", "Files", "Execution Status"]],
            hide_index=True,
            use_container_width=True,
        )

    st.markdown("### Attention Required")
    rejected = metrics["rejected"]
    incomplete = metrics["incomplete"]
    quarantined = metrics["quarantine_events"]
    if rejected + incomplete + quarantined == 0:
        st.success("No rejected, incomplete, or historically quarantined audit record requires attention.")
    else:
        st.warning(
            f"{rejected} rejected, {incomplete} incomplete, and {quarantined} historical quarantine event(s). "
            "Review the Files and Quarantine pages for record-level details."
        )
