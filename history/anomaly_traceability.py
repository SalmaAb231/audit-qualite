"""Enrichissement non métier des anomalies pour leur traçabilité.

Ce module intervient après le Judge. Il ne détecte, ne filtre, ne fusionne et
ne requalifie aucune anomalie : il ajoute seulement le contexte d'audit et une
localisation explicite à la structure déjà produite.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openpyxl.utils import get_column_letter


AGENT_PAR_TYPE = {
    "structure": "structure",
    "statistique": "stats",
    "statistics": "stats",
    "coherence": "coherence",
    "cohérence": "coherence",
    "realisme": "realisme",
    "réalisme": "realisme",
    "realism": "realisme",
}


def _iso_utc(value: datetime | str) -> str:
    if isinstance(value, str):
        return value
    moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number >= 0 else None


def _columns(anomaly: dict) -> list[str]:
    values = anomaly.get("colonnes") or anomaly.get("columns") or []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple, set)):
        return []
    return list(dict.fromkeys(str(value) for value in values if str(value).strip()))


def _rows(anomaly: dict) -> list[int]:
    values = anomaly.get("lignes") or []
    if not isinstance(values, (list, tuple, set)):
        values = []
    rows = {row for value in values if (row := _integer(value)) is not None}
    line = _integer(anomaly.get("ligne"))
    if line is not None:
        rows.add(line)
    return sorted(rows)


def _originals(anomaly: dict) -> list[dict]:
    values = anomaly.get("signalements_originaux")
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, dict)]


def _location(anomaly: dict, extension: str, source_metadata: dict | None = None) -> dict:
    """Décrit uniquement ce que les détecteurs ont effectivement conservé."""
    columns = _columns(anomaly)
    rows = _rows(anomaly)
    affected = _integer(anomaly.get("nb_lignes_affectees"))
    scope_hint = str(anomaly.get("portee") or "").strip().casefold()
    anomaly_type = str(anomaly.get("type") or "").strip().casefold()
    source = str(anomaly.get("source") or "").strip().casefold()
    metadata = source_metadata if isinstance(source_metadata, dict) else {}
    original_has_scope = any(original.get("portee") for original in _originals(anomaly))

    exact_rows = True
    reason = None
    if scope_hint == "fichier":
        scope, rows = "FILE", []
    elif scope_hint == "feuille":
        scope, rows = "SHEET", []
    elif scope_hint == "colonne":
        scope, rows = "COLUMN", []
    elif len(rows) > 1:
        scope = "MULTI_ROW"
    elif affected is not None and affected > len(rows):
        scope = "MULTI_ROW"
        exact_rows = False
        reason = "Only a representative row and an affected-row count were persisted."
    elif rows:
        scope = "CELL" if len(columns) == 1 else "ROW"
    elif columns:
        scope = "COLUMN"
    else:
        scope = "FILE"

    # The historical contract used line=0 both for the real first row and for
    # aggregate structure/statistics findings. LLM prompts explicitly preserve
    # the source DataFrame index, so their zero is exact. For legacy deterministic
    # findings without an explicit scope, never pretend that zero is a cell.
    ambiguous_zero = (
        rows == [0]
        and source == "stats"
        and anomaly_type in {"structure", "statistique", "statistics"}
        and not original_has_scope
    )
    if ambiguous_zero:
        if anomaly_type == "structure" and columns:
            scope, rows = "COLUMN", []
            exact_rows = True
            reason = None
        else:
            exact_rows = False
            reason = (
                "Legacy deterministic finding used the ambiguous line=0 convention; "
                "an exact source row was not persisted."
            )

    row_count = _integer(metadata.get("row_count"))
    if rows and row_count is not None and any(row >= row_count for row in rows):
        exact_rows = False
        reason = "Persisted row index is outside the source row count captured by the loader."

    row = rows[0] if len(rows) == 1 and exact_rows else None
    mapping_exact = metadata.get("row_mapping_status") == "exact"
    header_rows = _integer(metadata.get("header_rows"))
    physical_offset = header_rows + 1 if mapping_exact and header_rows is not None else None
    physical_row = row + physical_offset if row is not None and physical_offset is not None else None
    sheet_name = metadata.get("sheet_name") or anomaly.get("sheet_name")
    ordered_columns = metadata.get("columns_in_source_order")
    ordered_columns = ordered_columns if isinstance(ordered_columns, list) else []
    column_positions = [index for index, name in enumerate(ordered_columns) if name == columns[0]] if len(columns) == 1 else []
    cell_reference = None
    if sheet_name and physical_row is not None and len(column_positions) == 1:
        cell_reference = f"{get_column_letter(column_positions[0] + 1)}{physical_row}"
    return {
        "scope": scope,
        "available": bool(
            scope in {"FILE", "SHEET", "COLUMN"}
            or (scope in {"ROW", "CELL", "MULTI_ROW"} and rows and exact_rows)
        ),
        "precision": "EXACT" if exact_rows else "PARTIAL",
        "location_status": "exact" if exact_rows else "unavailable",
        "row_index_0_based": row,
        "row_number_1_based": row + 1 if row is not None else None,
        "physical_row_number_1_based": physical_row,
        "physical_row_status": "derived" if physical_row is not None else "unavailable",
        "row_indices_0_based": rows if exact_rows else [],
        "known_row_indices_0_based": rows if not exact_rows else [],
        "affected_row_count": affected,
        "columns": columns,
        "column": columns[0] if len(columns) == 1 else None,
        "sheet_name": sheet_name,
        "cell_reference": cell_reference,
        "unavailable_reason": reason,
        "basis": (
            "pandas source index (0-based); data row number is index+1; physical row uses the loader sidecar"
            if row is not None and mapping_exact
            else None
        ),
    }


def enrich_anomaly(
    anomaly: dict,
    *,
    run_id: str,
    audit_timestamp_utc: datetime | str,
    load_lock: str | None,
    filename: str,
    source_reference: str | Path | None,
    ordinal: int,
    source_metadata: dict | None = None,
) -> dict:
    enriched = deepcopy(anomaly)
    source = None if source_reference is None else str(source_reference)
    extension = Path(filename).suffix
    identity = json.dumps(
        [run_id, load_lock, filename, ordinal, anomaly],
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    anomaly_type = str(anomaly.get("type") or "unknown").casefold()
    sources = anomaly.get("sources") if isinstance(anomaly.get("sources"), list) else []
    agents = list(
        dict.fromkeys(
            AGENT_PAR_TYPE.get(str(value).casefold(), str(value).casefold())
            for value in [anomaly_type, *sources]
            if value
        )
    )
    enriched.update(
        {
            "anomaly_id": hashlib.sha256(identity).hexdigest()[:20],
            "run_id": run_id,
            "audit_timestamp_utc": _iso_utc(audit_timestamp_utc),
            "load_lock": load_lock,
            "filename": filename,
            "source_reference": source,
            "agent": AGENT_PAR_TYPE.get(anomaly_type, anomaly_type),
            "agents": agents,
            "issue_type": anomaly.get("type"),
            "description": anomaly.get("justification"),
            "message": anomaly.get("message") or anomaly.get("justification"),
            "detected_value": anomaly.get("valeur"),
            "observed_value": anomaly.get("observed_value", anomaly.get("valeur")),
            "expected": anomaly.get("expected", anomaly.get("expected_value", anomaly.get("expected_range"))),
            "rule": anomaly.get("rule") or anomaly.get("regle") or anomaly.get("rule_id"),
            "columns": _columns(anomaly),
            "location": _location(anomaly, extension, source_metadata),
        }
    )
    return enriched


def enrich_verdict(
    verdict: dict,
    *,
    run_id: str,
    audit_timestamp_utc: datetime | str,
    load_lock: str | None,
    filename: str,
    source_reference: str | Path | None,
    source_metadata: dict | None = None,
) -> dict:
    """Retourne une copie traçable sans modifier la décision du Judge."""
    enriched = deepcopy(verdict)
    anomalies = verdict.get("anomalies_consolidees")
    if not isinstance(anomalies, list):
        anomalies = verdict.get("anomalies")
    anomalies = anomalies if isinstance(anomalies, list) else []
    detailed = [
        enrich_anomaly(
            anomaly,
            run_id=run_id,
            audit_timestamp_utc=audit_timestamp_utc,
            load_lock=load_lock,
            filename=filename,
            source_reference=source_reference,
            ordinal=index,
            source_metadata=source_metadata,
        )
        for index, anomaly in enumerate(anomalies)
        if isinstance(anomaly, dict)
    ]
    enriched["anomalies"] = detailed
    enriched["anomalies_consolidees"] = detailed
    enriched["traceability"] = {
        "schema_version": 1,
        "run_id": run_id,
        "audit_timestamp_utc": _iso_utc(audit_timestamp_utc),
        "load_lock": load_lock,
        "filename": filename,
        "source_reference": None if source_reference is None else str(source_reference),
        "row_convention": "pandas index 0-based; first data row is 1; physical CSV/Excel row is 2",
        "sheet_name_available": any(
            anomaly.get("location", {}).get("sheet_name") for anomaly in detailed
        ),
        "source_metadata": {
            "sheet_name": (source_metadata or {}).get("sheet_name"),
            "columns_in_source_order": (source_metadata or {}).get("columns_in_source_order"),
            "row_count": (source_metadata or {}).get("row_count"),
            "header_rows": (source_metadata or {}).get("header_rows"),
            "row_mapping_status": (source_metadata or {}).get("row_mapping_status", "unavailable"),
        },
    }
    return enriched
