"""Lecture sûre, différée et mise en cache des artefacts d'audit."""

from __future__ import annotations

import json
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any, Callable

import pandas as pd
import streamlit as st
from loguru import logger

import config
from ingestion.s3_client import S3Loader


@dataclass(frozen=True)
class ArtifactReadResult:
    status: str
    reference: str | None = None
    payload: bytes | None = None
    error: str | None = None


def mime_type_for_reference(reference: Any, fallback: str = "application/octet-stream") -> str:
    suffix = Path(str(reference or "")).suffix.lower()
    return {
        ".html": "text/html",
        ".htm": "text/html",
        ".json": "application/json",
        ".csv": "text/csv",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xls": "application/vnd.ms-excel",
    }.get(suffix, fallback)


def is_versioned_run_reference(reference: Any, run_id: Any) -> bool:
    if reference is None or run_id is None:
        return False
    normalized = "/" + str(reference).replace("\\", "/").strip("/") + "/"
    marker = f"/runs/{str(run_id).strip('/')}/"
    return marker in normalized


def artifact_reference_status(reference: Any, storage_mode: str) -> str:
    if reference is None or not str(reference).strip():
        return "NOT AVAILABLE"
    if str(storage_mode or "local").lower() == "s3":
        return "REFERENCED"
    return "AVAILABLE" if resolve_local_artifact(reference) is not None else "NOT AVAILABLE"


def _allowed_local_roots() -> list[Path]:
    # Never authorize the whole project root: a forged history reference must
    # not make files such as .env downloadable from the dashboard.
    roots = [config.RESULTATS_DIR, config.DATA_DIR, config.CACHE_DIR]
    result = []
    for root in roots:
        try:
            resolved = Path(root).resolve()
        except OSError:
            continue
        if resolved not in result:
            result.append(resolved)
    return result


def resolve_local_artifact(reference: Any) -> Path | None:
    """Résout une référence historique sans autoriser une lecture arbitraire."""
    if reference is None or not str(reference).strip():
        return None
    candidate = Path(str(reference))
    if not candidate.is_absolute():
        candidate = config.BASE_DIR / candidate
    try:
        resolved = candidate.resolve(strict=False)
        allowed = any(resolved == root or root in resolved.parents for root in _allowed_local_roots())
        return resolved if allowed and resolved.is_file() else None
    except OSError:
        return None


def _valid_s3_key(reference: Any) -> str | None:
    if reference is None:
        return None
    key = str(reference).replace("\\", "/").strip("/")
    if not key or key.startswith("s3://"):
        return None
    parts = PurePosixPath(key).parts
    if any(part in {"", ".", ".."} for part in parts):
        return None
    normalized = PurePosixPath(*parts).as_posix()
    allowed_prefixes = [
        str(prefix).replace("\\", "/").strip("/") + "/"
        for prefix in (
            config.S3_INPUT_PREFIX,
            config.S3_REPORTS_PREFIX,
            config.S3_QUARANTINE_PREFIX,
            config.S3_HISTORY_PREFIX,
        )
        if str(prefix).strip("/\\")
    ]
    if allowed_prefixes and not any(normalized.startswith(prefix) for prefix in allowed_prefixes):
        return None
    return normalized


def _short_s3_error(error: Exception) -> str:
    text = str(error)
    code = next(
        (candidate for candidate in ("NoSuchKey", "AccessDenied", "NoCredentials") if candidate in text),
        None,
    )
    suffix = f" ({code})" if code else ""
    return f"The referenced S3 artifact could not be read{suffix}."


def read_artifact(
    reference: Any,
    storage_mode: str,
    *,
    s3_loader=None,
) -> ArtifactReadResult:
    """Lit une référence persistée; aucune clé n'est reconstruite."""
    if reference is None or not str(reference).strip():
        return ArtifactReadResult("NOT AVAILABLE")
    mode = str(storage_mode or "local").lower()
    if mode == "s3":
        key = _valid_s3_key(reference)
        if key is None:
            return ArtifactReadResult("ERROR", str(reference), error="Invalid persisted S3 object key.")
        loader = s3_loader or S3Loader()
        try:
            payload = loader.lire_objet(key)
        except Exception as exc:
            bucket = getattr(loader, "bucket", config.S3_BUCKET) or "<not configured>"
            logger.error(
                f"Artifact S3 read failed (bucket={bucket}, key={key}): {exc}",
                exc_info=True,
            )
            return ArtifactReadResult("ERROR", key, error=_short_s3_error(exc))
        if payload is None:
            return ArtifactReadResult("ERROR", key, error="The referenced S3 artifact returned no content.")
        return ArtifactReadResult("AVAILABLE", key, bytes(payload))

    path = resolve_local_artifact(reference)
    if path is None:
        return ArtifactReadResult("NOT AVAILABLE", str(reference), error="The referenced local file does not exist.")
    try:
        return ArtifactReadResult("AVAILABLE", str(path), path.read_bytes())
    except OSError as exc:
        logger.error(f"Local artifact read failed (path={path}): {exc}", exc_info=True)
        return ArtifactReadResult("ERROR", str(path), error="The referenced local artifact could not be read.")


@st.cache_data(ttl=config.ARTIFACT_CACHE_TTL_SECONDS, show_spinner=False)
def read_artifact_cached(
    reference: str,
    storage_mode: str,
    bucket: str = "",
) -> ArtifactReadResult:
    del bucket  # Cache invalidation when the configured bucket changes.
    return read_artifact(reference, storage_mode)


def read_json_artifact(
    reference: Any,
    storage_mode: str,
    *,
    reader: Callable[[str, str], ArtifactReadResult] | None = None,
) -> tuple[dict | None, ArtifactReadResult]:
    if reference is None or not str(reference).strip():
        result = ArtifactReadResult("NOT AVAILABLE")
        return None, result
    active_reader = reader or (
        lambda ref, mode: read_artifact_cached(ref, mode, config.S3_BUCKET)
    )
    result = active_reader(str(reference), str(storage_mode or "local"))
    if result.status != "AVAILABLE" or result.payload is None:
        return None, result
    try:
        decoded = json.loads(result.payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return None, ArtifactReadResult(
            "ERROR", result.reference, result.payload, f"Invalid JSON artifact: {exc}"
        )
    if not isinstance(decoded, dict):
        return None, ArtifactReadResult(
            "ERROR", result.reference, result.payload, "The JSON artifact is not an object."
        )
    return decoded, result


def source_row_context(payload: bytes, filename: str, row_indices: list[int]) -> pd.DataFrame:
    """Extrait les lignes demandées; n'invente jamais un index absent."""
    suffix = Path(filename).suffix.lower()
    stream = BytesIO(payload)
    if suffix in {".xlsx", ".xls"}:
        frame = pd.read_excel(stream)
    elif suffix == ".csv":
        frame = pd.read_csv(stream, sep=None, engine="python", encoding="utf-8")
    else:
        raise ValueError(f"Unsupported source format: {suffix or 'unknown'}")
    valid = sorted({index for index in row_indices if isinstance(index, int) and 0 <= index < len(frame)})
    if not valid:
        return pd.DataFrame(columns=frame.columns)
    return frame.iloc[valid].assign(_source_row_index_0_based=valid)
