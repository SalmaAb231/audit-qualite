"""Projection dashboard des anomalies détaillées persistées dans les verdicts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import pandas as pd

from dashboard.artifacts import ArtifactReadResult, read_json_artifact


ANOMALY_COLUMNS = [
    "anomaly_id",
    "run_id",
    "timestamp",
    "filename",
    "load_lock",
    "pipeline_status",
    "verdict",
    "agent",
    "type",
    "severity",
    "confidence",
    "scope",
    "row_index_0_based",
    "row_number_1_based",
    "physical_row_number_1_based",
    "columns",
    "cell_reference",
    "detected_value",
    "expected",
    "rule",
    "description",
    "source_reference",
    "sheet_name",
    "location_available",
    "location_precision",
    "location_status",
    "location_reason",
    "row_indices_0_based",
    "known_row_indices_0_based",
    "storage_mode",
    "verdict_json",
    "report_html",
]


@dataclass
class AnomalyLoadResult:
    anomalies: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=ANOMALY_COLUMNS))
    referenced_records: int = 0
    loaded_records: int = 0
    legacy_records: int = 0
    errors: list[str] = field(default_factory=list)


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _row_from_anomaly(anomaly: dict, record: pd.Series, ordinal: int) -> dict:
    location = anomaly.get("location") if isinstance(anomaly.get("location"), dict) else {}
    columns = anomaly.get("columns", anomaly.get("colonnes", []))
    if isinstance(columns, str):
        columns = [columns]
    if not isinstance(columns, list):
        columns = []
    agents = anomaly.get("agents")
    agent = anomaly.get("agent")
    if not agent and isinstance(agents, list):
        agent = ", ".join(str(value) for value in agents)
    return {
        "anomaly_id": anomaly.get("anomaly_id") or f"legacy-{record.get('run_id')}-{ordinal}",
        "run_id": anomaly.get("run_id") or record.get("run_id"),
        "timestamp": anomaly.get("audit_timestamp_utc") or record.get("timestamp"),
        "filename": anomaly.get("filename") or anomaly.get("fichier") or record.get("filename"),
        "load_lock": anomaly.get("load_lock") or record.get("load_lock"),
        "pipeline_status": record.get("pipeline_status"),
        "verdict": record.get("verdict"),
        "agent": agent or anomaly.get("type"),
        "type": anomaly.get("issue_type") or anomaly.get("type"),
        "severity": anomaly.get("severite") or anomaly.get("severity"),
        "confidence": anomaly.get("confiance", anomaly.get("confidence")),
        "scope": location.get("scope") or "UNAVAILABLE",
        "row_index_0_based": location.get("row_index_0_based"),
        "row_number_1_based": location.get("row_number_1_based"),
        "physical_row_number_1_based": location.get("physical_row_number_1_based"),
        "columns": columns,
        "cell_reference": location.get("cell_reference"),
        "detected_value": anomaly.get("detected_value", anomaly.get("valeur")),
        "expected": anomaly.get("expected", anomaly.get("expected_value")),
        "rule": anomaly.get("rule", anomaly.get("regle")),
        "description": anomaly.get("description", anomaly.get("justification")),
        "source_reference": anomaly.get("source_reference") or record.get("source"),
        "sheet_name": location.get("sheet_name"),
        "location_available": location.get("available", False),
        "location_precision": location.get("precision") or "UNAVAILABLE",
        "location_status": location.get("location_status") or (
            "exact" if location.get("available") else "unavailable"
        ),
        "location_reason": location.get("unavailable_reason"),
        "row_indices_0_based": _list(location.get("row_indices_0_based")),
        "known_row_indices_0_based": _list(location.get("known_row_indices_0_based")),
        "storage_mode": record.get("storage_mode") or "local",
        "verdict_json": record.get("verdict_json"),
        "report_html": record.get("report_html"),
    }


def load_anomaly_details(
    files_df: pd.DataFrame,
    *,
    reader: Callable[[str, str], ArtifactReadResult] | None = None,
) -> AnomalyLoadResult:
    result = AnomalyLoadResult()
    rows = []
    for _, record in files_df.iterrows():
        reference = record.get("verdict_json")
        if reference is None or pd.isna(reference) or not str(reference).strip():
            result.legacy_records += 1
            continue
        result.referenced_records += 1
        document, read_result = read_json_artifact(
            reference,
            record.get("storage_mode") or "local",
            reader=reader,
        )
        if document is None:
            result.errors.append(
                f"{record.get('filename')}: {read_result.error or read_result.status}"
            )
            continue
        result.loaded_records += 1
        anomalies = document.get("anomalies_consolidees")
        if not isinstance(anomalies, list):
            anomalies = document.get("anomalies")
        if not isinstance(anomalies, list):
            result.legacy_records += 1
            continue
        for ordinal, anomaly in enumerate(anomalies):
            if isinstance(anomaly, dict):
                rows.append(_row_from_anomaly(anomaly, record, ordinal))
    frame = pd.DataFrame(rows, columns=ANOMALY_COLUMNS)
    if not frame.empty:
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce", utc=True)
        frame["confidence"] = pd.to_numeric(frame["confidence"], errors="coerce")
        for column in ("row_index_0_based", "row_number_1_based", "physical_row_number_1_based"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce").astype("Int64")
    result.anomalies = frame
    return result
