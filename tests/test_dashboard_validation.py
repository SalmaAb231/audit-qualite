from dashboard.validation import history_agent_coverage, history_metric_coverage, validate_history_run


def _run(summary=None, files=None):
    return {
        "run_id": "run-1",
        "summary": summary or {},
        "files": files or [],
    }


def test_history_validation_accepts_consistent_record():
    result = validate_history_run(
        _run(
            {
                "files_processed": 1,
                "accepted": 1,
                "warning": 0,
                "rejected": 0,
                "incomplete": 0,
                "total_anomalies": 2,
                "anomalies_by_agent": {"schema": 2},
                "anomalies_by_severity": {"high": 2},
                "quarantined": 0,
            },
            [{"decision": "ACCEPTED", "anomalies_total": 2, "quarantined": False}],
        )
    )
    assert result.status == "OK"
    assert result.issues == []


def test_history_validation_detects_file_count_mismatch():
    result = validate_history_run(
        _run({"files_processed": 2, "accepted": 1, "warning": 0, "rejected": 0, "incomplete": 1}, [{}])
    )
    assert result.status == "WARNING"
    assert any("files_processed" in issue for issue in result.issues)


def test_history_validation_detects_verdict_count_mismatch():
    result = validate_history_run(
        _run(
            {"files_processed": 1, "accepted": 0, "warning": 0, "rejected": 0, "incomplete": 0},
            [{}],
        )
    )
    assert any("verdict" in issue.lower() for issue in result.issues)


def test_history_validation_detects_anomaly_totals_mismatch():
    result = validate_history_run(
        _run(
            {
                "files_processed": 1,
                "accepted": 1,
                "warning": 0,
                "rejected": 0,
                "incomplete": 0,
                "total_anomalies": 3,
                "anomalies_by_agent": {"schema": 2},
                "anomalies_by_severity": {"high": 2},
            },
            [{"decision": "ACCEPTED", "anomalies_total": 2}],
        )
    )
    assert sum("anomal" in issue.lower() for issue in result.issues) >= 2


def test_history_validation_detects_quarantine_contradiction():
    result = validate_history_run(
        _run(
            {
                "files_processed": 1,
                "accepted": 1,
                "warning": 0,
                "rejected": 0,
                "incomplete": 0,
                "quarantined": 1,
            },
            [{"decision": "ACCEPTED", "quarantined": True}],
        )
    )
    assert any("quarant" in issue.lower() for issue in result.issues)


def test_history_validation_detects_agent_total_mismatch():
    result = validate_history_run(
        _run(
            {
                "files_processed": 1,
                "accepted": 1,
                "warning": 0,
                "rejected": 0,
                "incomplete": 0,
                "total_anomalies": 3,
                "anomalies_by_agent": {"structure": 1, "stats": 1},
            },
            [{"verdict": "ACCEPTED", "anomalies_total": 3, "quarantined": False}],
        )
    )
    assert any("agent counters" in issue for issue in result.issues)


def test_metric_coverage_is_partial_without_severity_or_decision_reason():
    coverage = history_metric_coverage(_run(files=[{"verdict": "REJECTED"}]))
    assert coverage.status == "Partial"
    assert coverage.missing == ["severity breakdown", "decision reason"]


def test_metric_coverage_is_complete_when_optional_metrics_are_stored():
    coverage = history_metric_coverage(
        _run(
            files=[
                {
                    "verdict": "REJECTED",
                    "anomalies_by_severity": {"high": 1},
                    "decision_reason": "Stored Judge explanation",
                }
            ]
        )
    )
    assert coverage.status == "Complete"


def test_agent_coverage_reports_partial_when_realism_failed():
    coverage = history_agent_coverage(
        _run(
            files=[
                {
                    "agent_statuses": {
                        "structure": {"status": "SUCCESS"},
                        "stats": {"status": "SUCCESS"},
                        "coherence": {"status": "SUCCESS"},
                        "realisme": {"status": "FAILED", "error": "quota"},
                    }
                }
            ]
        )
    )
    assert coverage.status == "PARTIAL"
    assert coverage.successful == 3
    assert coverage.failed == 1
    assert coverage.total == 4
    assert coverage.agent_statuses["realisme"] == "FAILED"
