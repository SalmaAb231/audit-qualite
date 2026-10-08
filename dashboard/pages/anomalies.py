"""Aggregate anomaly analysis page."""

from __future__ import annotations

import hashlib
import json

import pandas as pd
import streamlit as st

from dashboard import charts
from dashboard.anomaly_data import load_anomaly_details
from dashboard.artifacts import read_artifact_cached, source_row_context
from dashboard.components import (
    format_optional_number,
    render_empty_state,
    render_file_bar_chart,
    render_horizontal_bar_chart,
    render_artifact_access,
    render_info_grid,
    render_metric_card,
    render_page_header,
)
from dashboard.transformations import (
    aggregate_agent_counts,
    historical_detections_by_file,
    latest_anomalies_by_file,
    latest_records_by_file,
    unique_file_identity_count,
)
import config


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


def filter_anomaly_records(frame: pd.DataFrame, filters: dict) -> pd.DataFrame:
    result = frame.copy()
    for key in ("filename", "load_lock", "agent", "type", "severity", "scope", "run_id"):
        selected = filters.get(key) or []
        if selected and key in result:
            result = result[result[key].astype(str).isin(selected)]
    query = str(filters.get("search") or "").strip().casefold()
    if query and not result.empty:
        searchable = result[["description", "detected_value", "columns"]].fillna("").astype(str)
        mask = searchable.apply(lambda column: column.str.casefold().str.contains(query, regex=False)).any(axis=1)
        result = result[mask]
    return result


def _location_label(row: pd.Series) -> str:
    if not bool(row.get("location_available")):
        return "Unavailable"
    scope = str(row.get("scope") or "")
    index = row.get("row_index_0_based")
    if pd.notna(index):
        return f"{scope} · index {int(index)}"
    return scope or "Available"


def _artifact_signature(files_df: pd.DataFrame) -> str:
    values = [
        f"{row.get('run_id')}:{row.get('verdict_json')}"
        for _, row in files_df.iterrows()
    ]
    return hashlib.sha256("|".join(values).encode("utf-8")).hexdigest()[:20]


def _render_anomaly_explorer(files_df: pd.DataFrame) -> None:
    st.subheader("Anomaly Explorer")
    st.caption(
        "Detailed records are read from persisted verdict JSON artifacts only on request. "
        "Row indices are 0-based; data-row numbers are 1-based."
    )
    mode = st.radio(
        "Audit mode",
        ["Latest audit per file", "Full history"],
        horizontal=True,
        key="anomaly_audit_mode",
        help="Latest keeps one most recent audit per source file; Full history includes repeated audits.",
    )
    scoped_files = latest_records_by_file(files_df) if mode == "Latest audit per file" else files_df
    signature = _artifact_signature(scoped_files)
    state_key = "anomaly_explorer_signature"
    if st.button("Load detailed anomaly records", type="primary", key="load_anomaly_explorer"):
        st.session_state[state_key] = signature
    if st.session_state.get(state_key) != signature:
        st.info("Load the detailed artifacts to filter, inspect, and export anomaly records.")
        return

    loaded = load_anomaly_details(scoped_files)
    st.caption(
        f"Detailed coverage: {loaded.loaded_records}/{loaded.referenced_records} referenced verdict artifact(s) loaded."
    )
    if loaded.legacy_records:
        st.warning(
            f"{loaded.legacy_records} historical record(s) do not expose a detailed verdict artifact. "
            "Aggregate counters remain available, but detailed locations are unavailable."
        )
    for error in loaded.errors:
        st.warning(error)
    frame = loaded.anomalies
    if frame.empty:
        st.info("No detailed anomaly record is available for the selected history.")
        return

    filter_columns = st.columns(3)
    filters = {}
    definitions = [
        ("filename", "File", "Source filename stored with the audit record."),
        ("load_lock", "LOAD_LOCK", "Selected ingestion batch."),
        ("agent", "Agent", "Pipeline detector that reported the issue."),
        ("type", "Type", "Persisted anomaly category."),
        ("severity", "Severity", "Persisted severity; it is not recalculated here."),
        ("scope", "Scope", "CELL, ROW, COLUMN, FILE, or MULTI_ROW location scope."),
        ("run_id", "Audit / Run ID", "Immutable pipeline execution identifier."),
    ]
    for index, (key, label, help_text) in enumerate(definitions):
        options = sorted(value for value in frame[key].dropna().astype(str).unique() if value)
        with filter_columns[index % 3]:
            filters[key] = st.multiselect(label, options, key=f"anomaly_{key}", help=help_text)
    filters["search"] = st.text_input(
        "Search description or value",
        key="anomaly_text_search",
        help="Case-insensitive search in the persisted description, detected value, and columns.",
    )
    filtered = filter_anomaly_records(frame, filters)
    st.caption(f"Showing {len(filtered)} of {len(frame)} detailed anomaly record(s).")
    st.caption("Row is the physical CSV/Excel row when that derived mapping is proven; otherwise it is blank.")
    if filtered.empty:
        st.info("No anomaly matches the current explorer filters.")
        return

    table = pd.DataFrame(index=filtered.index)
    table["Date"] = filtered["timestamp"].dt.strftime("%d %b %Y %H:%M UTC")
    table["File"] = filtered["filename"]
    table["LOAD_LOCK"] = filtered["load_lock"]
    table["Agent"] = filtered["agent"]
    table["Type"] = filtered["type"]
    table["Severity"] = filtered["severity"].astype(str).str.upper()
    table["Scope"] = filtered["scope"]
    table["Sheet"] = filtered["sheet_name"]
    table["Row"] = filtered["physical_row_number_1_based"]
    table["Column"] = filtered["columns"].map(
        lambda values: ", ".join(values) if isinstance(values, list) and values else None
    )
    table["Cell"] = filtered["cell_reference"]
    table["Issue"] = filtered["description"].fillna("N/A").map(
        lambda value: value if len(str(value)) <= 140 else str(value)[:137] + "..."
    )
    table["Confidence"] = filtered["confidence"]
    non_empty_columns = [
        column for column in table.columns
        if not table[column].isna().all() and not table[column].astype(str).str.strip().isin({"", "None", "nan", "<NA>"}).all()
    ]
    st.dataframe(table[non_empty_columns], hide_index=True, use_container_width=True)

    export = filtered.copy()
    for column in ("columns", "row_indices_0_based", "known_row_indices_0_based"):
        export[column] = export[column].map(lambda value: json.dumps(value, ensure_ascii=False))
    st.download_button(
        "Export filtered anomaly details (CSV)",
        data=export.to_csv(index=False).encode("utf-8-sig"),
        file_name="anomaly_details.csv",
        mime="text/csv",
        use_container_width=False,
    )

    st.markdown("#### Anomaly Details")
    choices = list(filtered.index)
    selected = st.selectbox(
        "Select an anomaly",
        choices,
        format_func=lambda idx: (
            f"{filtered.loc[idx, 'filename']} — {filtered.loc[idx, 'severity']} — "
            f"{str(filtered.loc[idx, 'description'])[:90]}"
        ),
        key="selected_anomaly_detail",
    )
    record = filtered.loc[selected]
    render_info_grid(
        [
            ("Audit time", record.get("timestamp")),
            ("Run ID", record.get("run_id") or "N/A"),
            ("Filename", record.get("filename") or "N/A"),
            ("LOAD_LOCK", record.get("load_lock") or "N/A"),
            ("Agent", record.get("agent") or "N/A"),
            ("Severity", record.get("severity") or "N/A"),
            ("Type", record.get("type") or "N/A"),
            ("Scope", record.get("scope") or "N/A"),
        ]
    )
    location = {
        "Scope": record.get("scope"),
        "Row index (0-based)": record.get("row_index_0_based"),
        "Data row (1-based)": record.get("row_number_1_based"),
        "Physical CSV/Excel row": record.get("physical_row_number_1_based"),
        "Columns": record.get("columns"),
        "Sheet": record.get("sheet_name") or "Not persisted",
        "Cell": record.get("cell_reference") or "N/A",
        "Precision": record.get("location_precision"),
        "Location reliability": record.get("location_status"),
        "Reason unavailable": record.get("location_reason") or "N/A",
    }
    st.json(location)
    st.write(record.get("description") or "No description persisted.")
    st.code(str(record.get("detected_value") or "N/A"), language="text")
    st.caption(f"Expected / Rule: {record.get('expected') or record.get('rule') or 'N/A'}")

    report_column, source_column = st.columns(2)
    with report_column:
        render_artifact_access(
            "Audit report",
            record.get("report_html"),
            mime_type="text/html",
            download_label="Download audit report",
            storage_mode=record.get("storage_mode") or "local",
            artifact_kind="html",
            action_label="View audit report",
            key=f"anomaly_report_{record.get('anomaly_id')}",
            expected_run_id=record.get("run_id"),
        )
    with source_column:
        render_artifact_access(
            "Source file",
            record.get("source_reference"),
            mime_type="application/octet-stream",
            download_label="Download source file",
            storage_mode=record.get("storage_mode") or "local",
            artifact_kind="binary",
            key=f"anomaly_source_{record.get('anomaly_id')}",
        )

    exact_rows = record.get("row_indices_0_based")
    exact_rows = exact_rows if isinstance(exact_rows, list) else []
    if exact_rows and bool(record.get("location_available")):
        if st.button("Show source row context", key=f"source_context_{record.get('anomaly_id')}"):
            source = record.get("source_reference")
            if source is None or pd.isna(source) or not str(source).strip():
                st.warning("No source reference was persisted for this anomaly.")
            else:
                result = read_artifact_cached(
                    str(source), str(record.get("storage_mode") or "local"), config.S3_BUCKET
                )
                if result.status != "AVAILABLE" or result.payload is None:
                    st.error(result.error or "The source file could not be loaded.")
                else:
                    try:
                        context = source_row_context(result.payload, str(record.get("filename")), exact_rows)
                    except Exception as exc:
                        st.error(f"Source row context is unavailable: {exc}")
                    else:
                        if context.empty:
                            st.warning("The persisted row index is outside the current source file.")
                        else:
                            st.dataframe(context, hide_index=True, use_container_width=True)
    else:
        st.info("Exact source-row context is unavailable for this FILE/COLUMN or partially located anomaly.")


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
    unique_impacted = (
        unique_file_identity_count(files_df, impacted_only=True) if known else None
    )

    st.subheader("Summary")
    columns = st.columns(3)
    with columns[0]:
        render_metric_card(
            "Historical anomaly detections",
            format_optional_number(total),
            helper="Sum across historical audit records; repeated audits are included.",
        )
    with columns[1]:
        render_metric_card(
            "Audit file records impacted",
            format_optional_number(impacted if known else None),
            helper="Historical audit records with at least one anomaly.",
        )
    with columns[2]:
        render_metric_card(
            "Unique files impacted",
            format_optional_number(unique_impacted),
            helper="Distinct source files represented in the selected history.",
        )
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

    st.subheader("Anomalies per Audit")
    st.caption("Each point represents one historical audit record.")
    timeline_values = pd.to_numeric(
        files_df.get("anomalies_total", pd.Series(dtype="float64")), errors="coerce"
    )
    valid_timestamps = pd.to_datetime(
        files_df.get("timestamp", pd.Series(index=files_df.index, dtype="object")),
        errors="coerce",
        utc=True,
    )
    valid_points = timeline_values.notna() & valid_timestamps.notna()
    if valid_points.any():
        charts.show(charts.anomalies_per_audit(files_df))
    else:
        st.info("Per-audit anomaly values are not available for the selected history.")

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

    _render_anomaly_explorer(files_df)
