"""Transformations pandas pures pour les pages de monitoring."""

from __future__ import annotations

import pandas as pd


RUN_COLUMNS = [
    "run_id", "started_at", "finished_at", "duration_seconds", "storage_mode",
    "files_discovered", "files_processed", "accepted", "warning", "rejected",
    "incomplete", "quarantined", "average_score", "total_anomalies",
    "run_status", "stop_reason", "consolidated_report_html",
]
FILE_COLUMNS = [
    "run_id", "timestamp", "storage_mode", "filename", "load_lock", "source", "pipeline_status",
    "verdict", "status", "score", "grade", "anomalies_total", "quarantined",
    "decision_reason", "report_html", "verdict_json", "quarantine_key",
]
AGENT_COLUMNS = [
    "run_id", "timestamp", "filename", "load_lock", "agent", "count", "status", "error"
]
SEVERITY_COLUMNS = ["run_id", "timestamp", "filename", "load_lock", "severity", "count"]

AGENT_ORDER = ("structure", "stats", "coherence", "realisme")
AGENT_KEY_ALIASES = {
    "structure": "structure",
    "stats": "stats",
    "statistics": "stats",
    "statistique": "stats",
    "coherence": "coherence",
    "cohérence": "coherence",
    "realisme": "realisme",
    "réalisme": "realisme",
    "realism": "realisme",
}
AGENT_DISPLAY_LABELS = {
    "structure": "Structure",
    "stats": "Statistics",
    "coherence": "Coherence",
    "realisme": "Realism",
}
DISPLAY_COLUMNS = {
    "filename": "Filename",
    "load_lock": "LOAD_LOCK",
    "status": "Audit Outcome",
    "verdict": "Judge Verdict",
    "pipeline_status": "Pipeline Status",
    "score": "Score",
    "anomalies_total": "Anomalies",
    "quarantined": "Quarantined",
    "decision_reason": "Reason",
}


def _mapping(value) -> dict:
    return value if isinstance(value, dict) else {}


def _files(value) -> list:
    return value if isinstance(value, list) else []


def effective_status(pipeline_status, verdict) -> str:
    if str(pipeline_status or "").upper() == "INCOMPLETE":
        return "INCOMPLETE"
    return str(verdict or "N/A").upper()


def normalize_agent_key(value) -> str:
    key = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return AGENT_KEY_ALIASES.get(key, key)


def agent_display_label(value) -> str:
    canonical = normalize_agent_key(value)
    return AGENT_DISPLAY_LABELS.get(canonical, canonical.replace("_", " ").title() or "Unknown")


def aggregate_agent_counts(agents_df: pd.DataFrame) -> pd.Series:
    """Aggregate persisted agent counters with display-only label normalization."""
    if agents_df.empty or "agent" not in agents_df or "count" not in agents_df:
        return pd.Series(dtype="float64")
    data = agents_df.assign(
        _agent=agents_df["agent"].map(normalize_agent_key),
        _count=pd.to_numeric(agents_df["count"], errors="coerce"),
    )
    totals = data.groupby("_agent")["_count"].sum(min_count=1)
    ordered = [key for key in AGENT_ORDER if key in totals.index]
    ordered.extend(key for key in totals.index if key not in ordered)
    totals = totals.reindex(ordered)
    totals.index = [agent_display_label(key) for key in totals.index]
    return totals


def agent_file_counts(agents_df: pd.DataFrame) -> pd.Series:
    if agents_df.empty or "agent" not in agents_df or "count" not in agents_df:
        return pd.Series(dtype="int64")
    data = agents_df.assign(
        _agent=agents_df["agent"].map(normalize_agent_key),
        _count=pd.to_numeric(agents_df["count"], errors="coerce"),
    )
    data = data[data["_count"] > 0]
    identity = [column for column in ("run_id", "filename", "load_lock") if column in data]
    if not identity:
        return pd.Series(dtype="int64")
    counts = data.drop_duplicates(["_agent", *identity]).groupby("_agent").size()
    counts.index = [agent_display_label(key) for key in counts.index]
    return counts


def display_columns(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.rename(columns={key: value for key, value in DISPLAY_COLUMNS.items() if key in frame})


def utc_timeline_data(series: pd.Series, value_label: str) -> pd.DataFrame:
    """Build stable UTC labels so browser timezone conversion cannot shift chart axes."""
    if series.empty:
        return pd.DataFrame(columns=["Timestamp UTC", value_label])
    timestamps = pd.to_datetime(series.index, errors="coerce", utc=True)
    values = pd.to_numeric(series.array, errors="coerce")
    data = pd.DataFrame({"_timestamp": timestamps, value_label: values}).dropna()
    data = data.sort_values("_timestamp")
    data["Timestamp UTC"] = data["_timestamp"].dt.strftime("%d %b %H:%M")
    return data[["Timestamp UTC", value_label]]


def filtered_view_counts(
    filtered_runs: pd.DataFrame,
    all_runs: pd.DataFrame,
    filtered_files: pd.DataFrame,
    all_files: pd.DataFrame,
) -> dict[str, int]:
    return {
        "displayed_executions": len(filtered_runs),
        "loaded_executions": len(all_runs),
        "displayed_files": len(filtered_files),
        "loaded_files": len(all_files),
    }


def derive_run_status(run: dict, summary: dict) -> str:
    explicit = run.get("run_status") or summary.get("run_status")
    if explicit:
        return str(explicit).upper()
    files_processed = pd.to_numeric(summary.get("files_processed"), errors="coerce")
    if pd.isna(files_processed):
        return "N/A"
    return "NO FILE PROCESSED" if files_processed == 0 else "COMPLETED"


def build_runs_df(runs: list[dict] | None) -> pd.DataFrame:
    lignes = []
    for run in runs or []:
        if not isinstance(run, dict):
            continue
        resume = _mapping(run.get("summary"))
        artifacts = _mapping(run.get("artifacts"))
        lignes.append(
            {
                "run_id": run.get("run_id"),
                "started_at": run.get("started_at_utc"),
                "finished_at": run.get("finished_at_utc"),
                "duration_seconds": run.get("duration_seconds"),
                "storage_mode": run.get("storage_mode"),
                **{colonne: resume.get(colonne) for colonne in RUN_COLUMNS[5:]},
                "run_status": derive_run_status(run, resume),
                "stop_reason": run.get("stop_reason") or resume.get("stop_reason") or resume.get("pipeline_reason"),
                "consolidated_report_html": artifacts.get("consolidated_report_html")
                or run.get("consolidated_report_html"),
            }
        )
    df = pd.DataFrame(lignes, columns=RUN_COLUMNS)
    for colonne in ("started_at", "finished_at"):
        df[colonne] = pd.to_datetime(df[colonne], errors="coerce", utc=True)
    for colonne in RUN_COLUMNS[2:]:
        if colonne not in {"storage_mode", "finished_at", "run_status", "stop_reason", "consolidated_report_html"}:
            df[colonne] = pd.to_numeric(df[colonne], errors="coerce")
    return df


def _file_records(runs: list[dict] | None):
    for run in runs or []:
        if not isinstance(run, dict):
            continue
        timestamp = run.get("started_at_utc")
        run_id = run.get("run_id")
        storage_mode = run.get("storage_mode") or "local"
        for fichier in _files(run.get("files")):
            if isinstance(fichier, dict):
                yield run_id, timestamp, storage_mode, fichier


def build_files_df(runs: list[dict] | None) -> pd.DataFrame:
    lignes = []
    for run_id, timestamp, storage_mode, fichier in _file_records(runs):
        pipeline_status = fichier.get("pipeline_status")
        artifacts = _mapping(fichier.get("artifacts"))
        incomplet = str(pipeline_status or "").upper() == "INCOMPLETE"
        verdict = None if incomplet else fichier.get("verdict")
        lignes.append(
            {
                "run_id": run_id,
                "timestamp": timestamp,
                "storage_mode": storage_mode,
                "filename": fichier.get("filename"),
                "load_lock": fichier.get("load_lock"),
                "source": fichier.get("source") or artifacts.get("source"),
                "pipeline_status": pipeline_status,
                "verdict": verdict,
                "status": effective_status(pipeline_status, verdict),
                "score": None if incomplet else fichier.get("score"),
                "grade": None if incomplet else fichier.get("grade"),
                "anomalies_total": fichier.get("anomalies_total"),
                "quarantined": fichier.get("quarantined") if isinstance(fichier.get("quarantined"), bool) else pd.NA,
                "decision_reason": fichier.get("decision_reason") or fichier.get("reason"),
                "report_html": fichier.get("report_html") or artifacts.get("report_html"),
                "verdict_json": fichier.get("verdict_json") or artifacts.get("verdict_json"),
                "quarantine_key": fichier.get("quarantine_key") or artifacts.get("quarantine"),
            }
        )
    df = pd.DataFrame(lignes, columns=FILE_COLUMNS)
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
    df["score"] = pd.to_numeric(df["score"], errors="coerce")
    df["anomalies_total"] = pd.to_numeric(df["anomalies_total"], errors="coerce")
    df["quarantined"] = df["quarantined"].astype("boolean")
    return df


def _build_counter_df(runs, field: str, name: str, columns: list[str]) -> pd.DataFrame:
    lignes = []
    for run_id, timestamp, _storage_mode, fichier in _file_records(runs):
        for cle, nombre in _mapping(fichier.get(field)).items():
            lignes.append(
                {
                    "run_id": run_id,
                    "timestamp": timestamp,
                    "filename": fichier.get("filename"),
                    "load_lock": fichier.get("load_lock"),
                    name: str(cle),
                    "count": nombre,
                }
            )
    df = pd.DataFrame(lignes, columns=columns)
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
    df["count"] = pd.to_numeric(df["count"], errors="coerce")
    return df


def build_agents_df(runs: list[dict] | None) -> pd.DataFrame:
    lignes = []
    for run_id, timestamp, _storage_mode, fichier in _file_records(runs):
        compteurs = {}
        for cle, valeur in _mapping(fichier.get("anomalies_by_agent")).items():
            agent = normalize_agent_key(cle)
            nombre = pd.to_numeric(valeur, errors="coerce")
            if not pd.isna(nombre):
                compteurs[agent] = compteurs.get(agent, 0) + nombre
        statuts = {
            normalize_agent_key(cle): valeur
            for cle, valeur in _mapping(fichier.get("agent_statuses")).items()
        }
        cles = list(dict.fromkeys([*AGENT_ORDER, *compteurs.keys(), *statuts.keys()]))
        for agent in cles:
            details = statuts.get(agent)
            details = details if isinstance(details, dict) else {}
            statut = str(details.get("status") or "UNKNOWN").upper()
            if statut not in {"SUCCESS", "FAILED", "NOT_RUN", "UNKNOWN"}:
                statut = "FAILED" if statut == "PARTIAL" else "UNKNOWN"
            nombre = pd.to_numeric(compteurs.get(agent), errors="coerce")
            if statut in {"FAILED", "NOT_RUN"} and (pd.isna(nombre) or nombre == 0):
                nombre = pd.NA
            lignes.append(
                {
                    "run_id": run_id,
                    "timestamp": timestamp,
                    "filename": fichier.get("filename"),
                    "load_lock": fichier.get("load_lock"),
                    "agent": agent,
                    "count": nombre,
                    "status": statut,
                    "error": details.get("error"),
                }
            )
    df = pd.DataFrame(lignes, columns=AGENT_COLUMNS)
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
    df["count"] = pd.to_numeric(df["count"], errors="coerce")
    return df


def build_severity_df(runs: list[dict] | None) -> pd.DataFrame:
    return _build_counter_df(runs, "anomalies_by_severity", "severity", SEVERITY_COLUMNS)


def build_dataframes(runs: list[dict] | None):
    return (
        build_runs_df(runs),
        build_files_df(runs),
        build_agents_df(runs),
        build_severity_df(runs),
    )


def has_active_filters(filters: dict | None) -> bool:
    return any(value not in (None, "", [], (), set()) for value in (filters or {}).values())


def apply_file_filters(files_df: pd.DataFrame, filters: dict | None) -> pd.DataFrame:
    if files_df.empty or not filters:
        return files_df.copy()
    df = files_df.copy()
    correspondances = {
        "load_lock": "load_lock",
        "verdict": "verdict",
        "pipeline_status": "pipeline_status",
        "grade": "grade",
    }
    for filtre, colonne in correspondances.items():
        valeurs = filters.get(filtre)
        if valeurs and colonne in df.columns:
            valeurs = valeurs if isinstance(valeurs, (list, tuple, set)) else [valeurs]
            df = df[df[colonne].isin(valeurs)]

    recherche = str(filters.get("filename") or "").strip()
    if recherche and "filename" in df.columns:
        df = df[df["filename"].fillna("").str.contains(recherche, case=False, regex=False)]

    if "timestamp" in df.columns:
        debut = filters.get("date_start")
        fin = filters.get("date_end")
        dates = df["timestamp"].dt.date
        if debut:
            df = df[dates >= debut]
        if fin:
            df = df[dates <= fin]
    return df


def filter_counter_df(counter_df: pd.DataFrame, filtered_files_df: pd.DataFrame) -> pd.DataFrame:
    if counter_df.empty or filtered_files_df.empty:
        return counter_df.iloc[0:0].copy()
    identifiants = set(
        zip(filtered_files_df["run_id"], filtered_files_df["filename"], filtered_files_df["load_lock"])
    )
    masque = [
        (run_id, filename, load_lock) in identifiants
        for run_id, filename, load_lock in zip(
            counter_df["run_id"], counter_df["filename"], counter_df["load_lock"]
        )
    ]
    return counter_df.loc[masque].copy()


def filter_runs_df(runs_df: pd.DataFrame, files_df: pd.DataFrame, filters: dict | None) -> pd.DataFrame:
    if not has_active_filters(filters):
        return runs_df.copy()
    run_ids = set(files_df["run_id"].dropna()) if not files_df.empty else set()
    return runs_df[runs_df["run_id"].isin(run_ids)].copy()


def _file_identity(row: pd.Series) -> str:
    """Stable identity for a physical/source file across repeated audits."""
    raw_source = row.get("source")
    source = "" if pd.isna(raw_source) else str(raw_source).strip()
    if source and source.lower() not in {"nan", "none", "n/a"}:
        return source
    load_lock = row.get("load_lock")
    filename = row.get("filename")
    if not pd.isna(filename) and str(filename).strip():
        return f"{'' if pd.isna(load_lock) else load_lock}::{filename}"
    return f"record::{row.name}"


def unique_file_identity_count(files_df: pd.DataFrame, *, impacted_only: bool = False) -> int:
    """Count distinct source identities without deduplicating the underlying history."""
    if files_df.empty:
        return 0
    selected = files_df.copy()
    if impacted_only:
        if "anomalies_total" not in selected:
            return 0
        anomalies = pd.to_numeric(selected.get("anomalies_total"), errors="coerce")
        selected = selected.loc[anomalies > 0]
    if selected.empty:
        return 0
    return int(selected.apply(_file_identity, axis=1).nunique())


def latest_records_by_file(files_df: pd.DataFrame) -> pd.DataFrame:
    """Return only the most recent audit record for each file identity."""
    if files_df.empty:
        return files_df.copy()
    latest = files_df.copy()
    latest["_file_identity"] = latest.apply(_file_identity, axis=1)
    if "timestamp" in latest:
        latest = latest.sort_values("timestamp", na_position="first")
    return latest.drop_duplicates("_file_identity", keep="last").drop(columns="_file_identity")


def filter_runs_by_view(runs_df: pd.DataFrame, view: str) -> pd.DataFrame:
    """Filter the Runs page only; persisted history is never mutated or removed."""
    if runs_df.empty or str(view).strip().lower() == "all runs":
        return runs_df.copy()
    if "files_processed" not in runs_df:
        return runs_df.iloc[0:0].copy()
    processed = pd.to_numeric(runs_df.get("files_processed"), errors="coerce")
    return runs_df.loc[processed > 0].copy()


def agent_audit_points(agents_df: pd.DataFrame, files_df: pd.DataFrame) -> pd.DataFrame:
    """Presentation records for one persisted agent count in one audit file record."""
    columns = [
        "audit_time", "agent", "detections", "filename", "load_lock",
        "agent_status", "pipeline_status", "verdict",
    ]
    if agents_df.empty or not {"timestamp", "agent", "count"}.issubset(agents_df.columns):
        return pd.DataFrame(columns=columns)

    points = agents_df.copy()
    points["audit_time"] = pd.to_datetime(points["timestamp"], errors="coerce", utc=True)
    points["detections"] = pd.to_numeric(points["count"], errors="coerce")
    points["agent"] = points["agent"].map(agent_display_label)
    points["agent_status"] = points.get(
        "status", pd.Series("UNKNOWN", index=points.index)
    ).fillna("UNKNOWN")

    join_keys = [
        column for column in ("run_id", "filename", "load_lock")
        if column in points.columns and column in files_df.columns
    ]
    context_columns = [
        column for column in ("pipeline_status", "verdict") if column in files_df.columns
    ]
    if join_keys and context_columns:
        context = files_df[join_keys + context_columns].drop_duplicates(join_keys, keep="last")
        points = points.merge(context, on=join_keys, how="left", suffixes=("", "_audit"))

    for column in ("filename", "load_lock", "pipeline_status", "verdict"):
        if column not in points:
            points[column] = pd.NA
    points = points.dropna(subset=["audit_time", "detections"])
    return points[columns]


def latest_audit_record(files_df: pd.DataFrame) -> pd.Series | None:
    """Most recent audit file record in the current filtered population."""
    if files_df.empty:
        return None
    data = files_df.copy()
    if "timestamp" in data:
        timestamps = pd.to_datetime(data["timestamp"], errors="coerce", utc=True)
        if timestamps.notna().any():
            return data.loc[timestamps.idxmax()]
    return data.iloc[-1]


def historical_detections_by_file(files_df: pd.DataFrame, limit: int = 10) -> pd.Series:
    """Cumulative detections across records; repeated audits intentionally remain included."""
    if files_df.empty or not {"filename", "anomalies_total"}.issubset(files_df.columns):
        return pd.Series(dtype="float64")
    totals = files_df.assign(
        _count=pd.to_numeric(files_df["anomalies_total"], errors="coerce")
    ).groupby("filename")["_count"].sum(min_count=1).dropna()
    return totals[totals > 0].sort_values(ascending=False).head(limit)


def latest_anomalies_by_file(files_df: pd.DataFrame, limit: int = 10) -> pd.Series:
    """Latest anomaly value per stable file identity, never a historical sum."""
    latest = latest_records_by_file(files_df)
    if latest.empty or not {"filename", "anomalies_total"}.issubset(latest.columns):
        return pd.Series(dtype="float64")
    latest = latest.assign(_count=pd.to_numeric(latest["anomalies_total"], errors="coerce"))
    latest = latest.dropna(subset=["filename", "_count"])
    if latest.empty:
        return pd.Series(dtype="float64")
    filenames = latest["filename"].astype(str)
    duplicate_names = filenames.duplicated(keep=False)
    labels = filenames.copy()
    if duplicate_names.any():
        load_locks = latest.get("load_lock", pd.Series("", index=latest.index)).fillna("").astype(str)
        labels.loc[duplicate_names] = load_locks.loc[duplicate_names] + " / " + filenames.loc[duplicate_names]
    result = pd.Series(latest["_count"].to_numpy(), index=labels, dtype="float64")
    return result.sort_values(ascending=False).head(limit)


def audit_outcome_counts(files_df: pd.DataFrame) -> dict[str, int]:
    """Historical audit-record outcomes, in the canonical display order."""
    if files_df.empty:
        counts = pd.Series(dtype="int64")
    else:
        counts = files_df["status"].fillna("INCOMPLETE").value_counts()
    return {
        "ACCEPTED": int(counts.get("ACCEPTED", 0)),
        "WARNING": int(counts.get("WARNING", 0)),
        "REJECTED": int(counts.get("REJECTED", 0)),
        "INCOMPLETE": int(counts.get("INCOMPLETE", 0)),
    }


def quarantine_metrics(files_df: pd.DataFrame) -> dict[str, int]:
    """Historical quarantine events, distinct file identities, and known flags."""
    if files_df.empty or "quarantined" not in files_df:
        return {"events": 0, "unique_files": 0, "known_records": 0}
    known = files_df["quarantined"].notna()
    events = files_df.loc[known & files_df["quarantined"].fillna(False).astype(bool)].copy()
    unique_files = int(events.apply(_file_identity, axis=1).nunique()) if not events.empty else 0
    return {
        "events": int(len(events)),
        "unique_files": unique_files,
        "known_records": int(known.sum()),
    }


def dashboard_metrics(runs_df: pd.DataFrame, files_df: pd.DataFrame) -> dict:
    """Single source of truth for dashboard-wide business metrics."""
    outcomes = audit_outcome_counts(files_df)
    scores = (
        pd.to_numeric(files_df.get("score"), errors="coerce")
        if not files_df.empty
        else pd.Series(dtype="float64")
    )
    anomalies = (
        pd.to_numeric(files_df.get("anomalies_total"), errors="coerce")
        if not files_df.empty
        else pd.Series(dtype="float64")
    )
    files_processed = (
        pd.to_numeric(runs_df.get("files_processed"), errors="coerce")
        if not runs_df.empty
        else pd.Series(dtype="float64")
    )
    latest = latest_records_by_file(files_df)
    quarantine = quarantine_metrics(files_df)
    return {
        "audit_records": int(len(files_df)),
        "unique_files": int(len(latest)),
        "executions": int(len(runs_df)),
        "completed_runs": int((files_processed > 0).sum()),
        "no_file_processed": int((files_processed == 0).sum()),
        "average_quality_score": None if scores.dropna().empty else round(float(scores.mean()), 2),
        "historical_detections": None if anomalies.dropna().empty else int(anomalies.sum()),
        "quarantine_events": quarantine["events"],
        "unique_quarantined_files": quarantine["unique_files"],
        "accepted": outcomes["ACCEPTED"],
        "warning": outcomes["WARNING"],
        "rejected": outcomes["REJECTED"],
        "incomplete": outcomes["INCOMPLETE"],
    }


def calculate_kpis(runs_df: pd.DataFrame, files_df: pd.DataFrame) -> dict:
    """Backward-compatible KPI facade used by existing pages and tests."""
    metrics = dashboard_metrics(runs_df, files_df)
    return {
        "executions": metrics["executions"],
        "completed_audits": metrics["completed_runs"],
        "empty_runs": metrics["no_file_processed"],
        "total_files": metrics["audit_records"],
        "accepted": metrics["accepted"],
        "warning": metrics["warning"],
        "rejected": metrics["rejected"],
        "incomplete": metrics["incomplete"],
        "quarantined": metrics["quarantine_events"],
        "average_score": metrics["average_quality_score"],
        "total_anomalies": metrics["historical_detections"],
    }


def time_series_state(series: pd.Series, minimum_points: int = 2) -> str:
    """État d'une série avant rendu : missing, zero, insufficient ou ready."""
    values = pd.to_numeric(series, errors="coerce").dropna()
    if values.empty:
        return "missing"
    if (values == 0).all():
        return "zero"
    if len(values) < minimum_points:
        return "insufficient"
    return "ready"
