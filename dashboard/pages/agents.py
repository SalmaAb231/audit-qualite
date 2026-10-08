"""Contribution and coverage of the four agents implemented by the pipeline."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from dashboard.components import (
    format_optional_number,
    render_empty_state,
    render_page_header,
    status_badge_html,
)
from dashboard import charts
from dashboard.transformations import (
    AGENT_DISPLAY_LABELS,
    AGENT_ORDER,
    aggregate_agent_counts,
    normalize_agent_key,
)


def agent_card_totals(agents_df: pd.DataFrame) -> pd.Series:
    """Same known-detection projection used by Detection Sources."""
    return aggregate_agent_counts(agents_df)


def agent_coverage_metrics(agents_df: pd.DataFrame) -> dict[str, dict]:
    if agents_df.empty or "agent" not in agents_df:
        return {}
    data = agents_df.copy()
    data["_agent"] = data["agent"].map(normalize_agent_key)
    data["_status"] = data.get("status", pd.Series("UNKNOWN", index=data.index)).fillna("UNKNOWN").astype(str).str.upper()
    data.loc[~data["_status"].isin({"SUCCESS", "FAILED", "NOT_RUN", "UNKNOWN"}), "_status"] = "UNKNOWN"
    data["_count"] = pd.to_numeric(data.get("count"), errors="coerce")
    data["_timestamp"] = pd.to_datetime(data.get("timestamp"), errors="coerce", utc=True)

    metrics = {}
    for key in AGENT_ORDER:
        subset = data[data["_agent"] == key]
        if subset.empty:
            metrics[key] = {
                "status": "UNKNOWN",
                "historical_detections": None,
                "audit_records": 0,
                "records_with_status": 0,
                "successful": 0,
                "failed": 0,
                "not_run": 0,
                "unknown": 0,
                "metadata_coverage": None,
                "success_rate": None,
                "executed": 0,
            }
            continue
        successful = int((subset["_status"] == "SUCCESS").sum())
        failed = int((subset["_status"] == "FAILED").sum())
        not_run = int((subset["_status"] == "NOT_RUN").sum())
        unknown = int((subset["_status"] == "UNKNOWN").sum())
        audit_records = len(subset)
        records_with_status = successful + failed + not_run
        metadata_coverage = records_with_status / audit_records * 100 if audit_records else None
        executed_statuses = successful + failed
        success_rate = successful / executed_statuses * 100 if executed_statuses else None
        known_statuses = subset[subset["_status"] != "UNKNOWN"]
        latest_pool = known_statuses if not known_statuses.empty else subset
        latest = latest_pool.sort_values("_timestamp", na_position="first").iloc[-1]
        known = subset["_count"].sum(min_count=1)
        metrics[key] = {
            "status": latest["_status"],
            "historical_detections": None if pd.isna(known) else known,
            "audit_records": audit_records,
            "records_with_status": records_with_status,
            "successful": successful,
            "failed": failed,
            "not_run": not_run,
            "unknown": unknown,
            "metadata_coverage": metadata_coverage,
            "success_rate": success_rate,
            "executed": executed_statuses,
        }
    return metrics


def render(agents_df: pd.DataFrame, files_df: pd.DataFrame | None = None) -> None:
    render_page_header(
        "Agents",
        "Agent Contribution, status metadata coverage, and success rate across audit history.",
    )
    files_df = files_df if files_df is not None else pd.DataFrame()
    if agents_df.empty:
        if files_df.empty:
            render_empty_state("No agent contribution data", "Run an audit to populate agent-level detections.")
        else:
            st.info("Agent execution status is not available for these historical file records.")
        return

    totals = agent_card_totals(agents_df)
    coverage = agent_coverage_metrics(agents_df)

    st.subheader("Agent Contribution and Coverage")
    for column, key in zip(st.columns(4), AGENT_ORDER):
        label = AGENT_DISPLAY_LABELS[key]
        metrics = coverage[key]
        with column:
            with st.container(border=True):
                st.markdown(f"#### {label}")
                st.markdown('<div class="agent-status-label">Latest known status</div>', unsafe_allow_html=True)
                st.markdown(status_badge_html(metrics["status"]), unsafe_allow_html=True)
                st.metric("Historical detections", format_optional_number(metrics["historical_detections"]))
                st.metric("Executed audits", format_optional_number(metrics["executed"]))
                st.caption(
                    "Success rate: **"
                    + format_optional_number(metrics["success_rate"], 1, suffix="%")
                    + "**"
                )
                st.caption(
                    f"{metrics['successful']} successful · {metrics['failed']} failed"
                )
                st.caption(
                    f"Status metadata available for {metrics['records_with_status']}/"
                    f"{metrics['audit_records']} historical records · "
                    f"{metrics['not_run']} not run · {metrics['unknown']} unknown"
                )
                if key == "realisme" and metrics["failed"]:
                    st.warning("Realism failures require review; detection count may be unavailable.")

    known_labels = set(AGENT_DISPLAY_LABELS.values())
    unexpected = totals[~totals.index.isin(known_labels)]
    if not unexpected.empty:
        st.warning("History contains detection counters for an unknown agent key.")
        st.dataframe(unexpected.rename("Historical detections"), use_container_width=True)

    nonzero = totals.dropna()
    nonzero = nonzero[nonzero > 0]
    left, right = st.columns(2)
    with left:
        with st.container(border=True):
            st.markdown("#### Detections by Agent")
            st.caption("Historical detections across audit file records.")
            if nonzero.empty:
                st.info("No known non-zero agent detection total is available.")
            else:
                charts.show(charts.horizontal_bars(nonzero, value_title="Historical detections", color=charts.PURPLE))
    with right:
        with st.container(border=True):
            st.markdown("#### Agent Success Rate")
            st.caption("SUCCESS / (SUCCESS + FAILED). NOT_RUN and unknown metadata are excluded.")
            success = pd.Series(
                {
                    AGENT_DISPLAY_LABELS[key]: metrics["success_rate"]
                    for key, metrics in coverage.items()
                    if metrics["success_rate"] is not None
                },
                dtype="float64",
            )
            if success.empty:
                st.info("Success rate is not available for this history.")
            else:
                success_colors = {
                    str(label).upper(): charts.get_agent_success_color(value)
                    for label, value in success.items()
                }
                charts.show(
                    charts.horizontal_bars(
                        success,
                        value_title="Success rate (%)",
                        color_map=success_colors,
                    )
                )

    trend_data = agents_df.copy()
    if {"timestamp", "count", "agent"}.issubset(trend_data.columns):
        trend_data["Audit time"] = pd.to_datetime(trend_data["timestamp"], errors="coerce", utc=True)
        trend_data["Detections"] = pd.to_numeric(trend_data["count"], errors="coerce")
        trend_data["Agent"] = trend_data["agent"].map(
            lambda value: AGENT_DISPLAY_LABELS.get(normalize_agent_key(value), str(value).title())
        )
        trend_data = (
            trend_data.dropna(subset=["Audit time", "Detections"])
            .groupby(["Audit time", "Agent"], as_index=False)["Detections"]
            .sum()
        )
    else:
        trend_data = pd.DataFrame()
    if not trend_data.empty and trend_data["Audit time"].nunique() >= 2:
        st.markdown("### Agent Detections Over Time")
        st.caption("Persisted anomaly detections by agent for each audit time.")
        charts.show(
            charts.multi_line(
                trend_data,
                x="Audit time",
                y="Detections",
                group="Agent",
                value_title="Detections",
                colors={
                    "Structure": charts.GREEN,
                    "Statistics": charts.BLUE,
                    "Coherence": charts.PURPLE,
                    "Realism": charts.AMBER,
                },
            )
        )

    st.markdown("### Agent Details")
    detail_rows = []
    for key in AGENT_ORDER:
        metrics = coverage[key]
        detail_rows.append(
            {
                "Agent": AGENT_DISPLAY_LABELS[key],
                "Status": metrics["status"],
                "Audit Records": metrics["audit_records"],
                "Status Known": metrics["records_with_status"],
                "Success": metrics["successful"],
                "Failed": metrics["failed"],
                "Not Run": metrics["not_run"],
                "Unknown": metrics["unknown"],
                "Success Rate": format_optional_number(metrics["success_rate"], 1, suffix="%"),
            }
        )
    st.dataframe(detail_rows, hide_index=True, use_container_width=True)
