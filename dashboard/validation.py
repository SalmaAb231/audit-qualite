"""Validation non bloquante de la cohérence des documents history."""

from __future__ import annotations

from dataclasses import dataclass, field

from loguru import logger


@dataclass(frozen=True)
class HistoryValidation:
    run_id: str
    issues: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        return "OK" if not self.issues else "WARNING"


@dataclass(frozen=True)
class MetricCoverage:
    status: str
    missing: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AgentCoverage:
    status: str
    successful: int = 0
    failed: int = 0
    not_run: int = 0
    unknown: int = 0
    total: int = 0
    agent_statuses: dict[str, str] = field(default_factory=dict)


def history_agent_coverage(run: dict) -> AgentCoverage:
    expected = ("structure", "stats", "coherence", "realisme")
    files = run.get("files") if isinstance(run.get("files"), list) else []
    if not files:
        return AgentCoverage(status="N/A")

    counters = {"SUCCESS": 0, "FAILED": 0, "NOT_RUN": 0, "UNKNOWN": 0}
    statuses_by_agent = {agent: [] for agent in expected}
    for file in files:
        statuses = file.get("agent_statuses") if isinstance(file, dict) else {}
        statuses = statuses if isinstance(statuses, dict) else {}
        for agent in expected:
            details = statuses.get(agent)
            details = details if isinstance(details, dict) else {}
            status = str(details.get("status") or "UNKNOWN").upper()
            if status not in counters:
                status = "FAILED" if status == "PARTIAL" else "UNKNOWN"
            counters[status] += 1
            statuses_by_agent[agent].append(status)

    total = len(files) * len(expected)
    complete = counters["SUCCESS"] == total
    aggregate_statuses = {}
    for agent, statuses in statuses_by_agent.items():
        if "FAILED" in statuses:
            aggregate_statuses[agent] = "FAILED"
        elif "NOT_RUN" in statuses:
            aggregate_statuses[agent] = "NOT_RUN"
        elif statuses and all(status == "SUCCESS" for status in statuses):
            aggregate_statuses[agent] = "SUCCESS"
        else:
            aggregate_statuses[agent] = "UNKNOWN"
    return AgentCoverage(
        status="COMPLETE" if complete else "PARTIAL",
        successful=counters["SUCCESS"],
        failed=counters["FAILED"],
        not_run=counters["NOT_RUN"],
        unknown=counters["UNKNOWN"],
        total=total,
        agent_statuses=aggregate_statuses,
    )


def history_metric_coverage(run: dict) -> MetricCoverage:
    """Describe optional metric availability without treating gaps as inconsistency."""
    files = run.get("files") if isinstance(run.get("files"), list) else []
    if not files:
        return MetricCoverage(status="N/A", missing=[])

    missing: list[str] = []
    if any(not isinstance(file, dict) or not isinstance(file.get("anomalies_by_severity"), dict) for file in files):
        missing.append("severity breakdown")
    if any(
        not isinstance(file, dict)
        or not str(file.get("decision_reason") or file.get("reason") or "").strip()
        for file in files
    ):
        missing.append("decision reason")
    return MetricCoverage(status="Complete" if not missing else "Partial", missing=missing)


def _nombre(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _champ_manquant(run_id: str, field: str, issues: list[str]) -> None:
    message = f"Missing history field {field} for run {run_id}"
    logger.warning(message)
    issues.append(message)


def validate_history_run(run: dict) -> HistoryValidation:
    """Retourne les incohérences constatées sans modifier le document."""
    run_id = str(run.get("run_id") or "N/A")
    issues: list[str] = []
    summary = run.get("summary") if isinstance(run.get("summary"), dict) else {}
    files = run.get("files") if isinstance(run.get("files"), list) else []

    if "files_processed" not in summary:
        _champ_manquant(run_id, "summary.files_processed", issues)
    else:
        files_processed = _nombre(summary.get("files_processed"))
        if files_processed is not None and files_processed != len(files):
            issues.append(
                f"files_processed={files_processed:g} but files contains {len(files)} item(s)"
            )

    verdict_fields = ("accepted", "warning", "rejected", "incomplete")
    if "files_processed" in summary and all(field in summary for field in verdict_fields):
        values = [_nombre(summary.get(field)) for field in verdict_fields]
        files_processed = _nombre(summary.get("files_processed"))
        if files_processed is not None and all(value is not None for value in values):
            verdict_total = sum(values)
            if verdict_total != files_processed:
                issues.append(
                    f"verdict counts sum to {verdict_total:g} but files_processed={files_processed:g}"
                )

    total_anomalies = _nombre(summary.get("total_anomalies"))
    if "total_anomalies" not in summary:
        _champ_manquant(run_id, "summary.total_anomalies", issues)
    elif all(isinstance(file, dict) and _nombre(file.get("anomalies_total")) is not None for file in files):
        files_total = sum(file["anomalies_total"] for file in files)
        if total_anomalies is not None and files_total != total_anomalies:
            issues.append(
                f"total_anomalies={total_anomalies:g} but file anomaly counts sum to {files_total:g}"
            )

    for field, label in (
        ("anomalies_by_agent", "agent counters"),
        ("anomalies_by_severity", "severity counters"),
    ):
        counters = summary.get(field)
        if isinstance(counters, dict) and all(_nombre(value) is not None for value in counters.values()):
            counter_total = sum(counters.values())
            if total_anomalies is not None and counter_total != total_anomalies:
                issues.append(
                    f"total_anomalies={total_anomalies:g} but {label} sum to {counter_total:g}"
                )

    if "quarantined" in summary:
        quarantined = _nombre(summary.get("quarantined"))
        known_flags = [file.get("quarantined") for file in files if isinstance(file, dict)]
        if quarantined is not None and all(isinstance(flag, bool) for flag in known_flags):
            actual = sum(flag is True for flag in known_flags)
            if actual != quarantined:
                issues.append(
                    f"quarantined={quarantined:g} but files contain {actual} quarantine event(s)"
                )

    for index, file in enumerate(files):
        if not isinstance(file, dict) or "quarantined" not in file:
            continue
        status = (
            "INCOMPLETE"
            if str(file.get("pipeline_status") or "").upper() == "INCOMPLETE"
            else str(file.get("verdict") or "").upper()
        )
        expected = status in {"REJECTED", "INCOMPLETE"}
        if isinstance(file.get("quarantined"), bool) and file["quarantined"] != expected:
            name = file.get("filename") or f"files[{index}]"
            issues.append(
                f"{name}: status={status or 'N/A'} conflicts with quarantined={file['quarantined']}"
            )

    for issue in issues:
        if not issue.startswith("Missing history field"):
            logger.warning(f"History consistency warning for run {run_id}: {issue}")
    return HistoryValidation(run_id=run_id, issues=issues)
