import json

import pandas as pd

import config
from dashboard.anomaly_data import load_anomaly_details
from dashboard.artifacts import ArtifactReadResult, read_artifact, read_json_artifact
from dashboard.pages.anomalies import filter_anomaly_records
from dashboard.transformations import latest_records_by_file


def test_local_artifact_is_read_from_an_allowed_root(tmp_path, monkeypatch):
    results = tmp_path / "results"
    results.mkdir()
    artifact = results / "verdict.json"
    artifact.write_text('{"verdict":"ACCEPTED"}', encoding="utf-8")
    monkeypatch.setattr(config, "RESULTATS_DIR", results)

    result = read_artifact(artifact, "local")

    assert result.status == "AVAILABLE"
    assert result.payload == b'{"verdict":"ACCEPTED"}'


def test_local_artifact_outside_authorized_roots_is_not_read(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside" / "secret.txt"
    allowed.mkdir()
    outside.parent.mkdir()
    outside.write_text("secret", encoding="utf-8")
    monkeypatch.setattr(config, "BASE_DIR", allowed)
    monkeypatch.setattr(config, "RESULTATS_DIR", allowed / "results")
    monkeypatch.setattr(config, "DATA_DIR", allowed / "data")
    monkeypatch.setattr(config, "CACHE_DIR", allowed / "cache")

    result = read_artifact(outside, "local")

    assert result.status == "NOT AVAILABLE"
    assert result.payload is None


def test_s3_artifact_uses_exact_persisted_key_with_fake_loader():
    class FakeLoader:
        bucket = "bucket-test"

        def __init__(self):
            self.keys = []

        def lire_objet(self, key):
            self.keys.append(key)
            return b"payload"

    loader = FakeLoader()
    result = read_artifact("reports/runs/run-1/report.html", "s3", s3_loader=loader)

    assert result.status == "AVAILABLE"
    assert loader.keys == ["reports/runs/run-1/report.html"]


def test_s3_nosuchkey_and_accessdenied_are_non_blocking():
    class FakeLoader:
        bucket = "bucket-test"

        def __init__(self, message):
            self.message = message

        def lire_objet(self, key):
            raise RuntimeError(self.message)

    for message in ("NoSuchKey", "AccessDenied"):
        result = read_artifact("reports/missing.html", "s3", s3_loader=FakeLoader(message))
        assert result.status == "ERROR"
        assert result.payload is None
        assert "could not be read" in result.error


def test_invalid_json_is_an_explicit_non_blocking_error():
    document, result = read_json_artifact(
        "verdict.json",
        "local",
        reader=lambda *_: ArtifactReadResult("AVAILABLE", "verdict.json", b"not-json"),
    )

    assert document is None
    assert result.status == "ERROR"
    assert "Invalid JSON" in result.error


def test_anomaly_details_load_from_verdict_and_report_legacy_coverage():
    files = pd.DataFrame(
        [
            {
                "run_id": "run-1",
                "timestamp": "2026-10-08T12:00:00Z",
                "storage_mode": "s3",
                "filename": "a.xlsx",
                "load_lock": "LOCK",
                "source": "input/LOCK/a.xlsx",
                "pipeline_status": "COMPLETE",
                "verdict": "WARNING",
                "verdict_json": "reports/runs/run-1/verdict_a.json",
            },
            {
                "run_id": "legacy",
                "filename": "legacy.xlsx",
                "verdict_json": None,
            },
        ]
    )
    verdict = {
        "anomalies": [
            {
                "anomaly_id": "a-1",
                "type": "coherence",
                "severite": "high",
                "confiance": 0.9,
                "columns": ["price"],
                "description": "Negative price",
                "location": {
                    "scope": "CELL",
                    "available": True,
                    "precision": "EXACT",
                    "row_index_0_based": 2,
                    "row_number_1_based": 3,
                    "row_indices_0_based": [2],
                },
            }
        ]
    }
    loaded = load_anomaly_details(
        files,
        reader=lambda *_: ArtifactReadResult(
            "AVAILABLE", "reports/runs/run-1/verdict_a.json", json.dumps(verdict).encode()
        ),
    )

    assert loaded.loaded_records == 1
    assert loaded.referenced_records == 1
    assert loaded.legacy_records == 1
    assert loaded.anomalies.iloc[0]["row_index_0_based"] == 2
    assert loaded.anomalies.iloc[0]["source_reference"] == "input/LOCK/a.xlsx"


def test_reaudit_keeps_rows_and_report_association_for_both_runs():
    files = pd.DataFrame(
        [
            {
                "run_id": "run-a",
                "timestamp": "2026-10-08T10:00:00Z",
                "storage_mode": "s3",
                "filename": "file.xlsx",
                "load_lock": "LOCK",
                "source": "input/LOCK/file.xlsx",
                "verdict_json": "reports/runs/run-a/verdict_file.json",
                "report_html": "reports/runs/run-a/report_file.html",
            },
            {
                "run_id": "run-b",
                "timestamp": "2026-10-08T11:00:00Z",
                "storage_mode": "s3",
                "filename": "file.xlsx",
                "load_lock": "LOCK",
                "source": "input/LOCK/file.xlsx",
                "verdict_json": "reports/runs/run-b/verdict_file.json",
                "report_html": "reports/runs/run-b/report_file.html",
            },
        ]
    )

    def reader(reference, _mode):
        run_id = "run-a" if "/run-a/" in reference else "run-b"
        row = 100 if run_id == "run-a" else 150
        payload = {
            "anomalies": [
                {
                    "anomaly_id": f"{run_id}-anomaly",
                    "run_id": run_id,
                    "agent": "realisme",
                    "severite": "high",
                    "description": run_id,
                    "location": {
                        "scope": "CELL",
                        "available": True,
                        "precision": "EXACT",
                        "location_status": "exact",
                        "row_index_0_based": row,
                        "row_indices_0_based": [row],
                    },
                }
            ]
        }
        return ArtifactReadResult("AVAILABLE", reference, json.dumps(payload).encode())

    full = load_anomaly_details(files, reader=reader).anomalies
    latest = load_anomaly_details(latest_records_by_file(files), reader=reader).anomalies

    assert list(full["row_index_0_based"]) == [100, 150]
    assert list(full["report_html"]) == [
        "reports/runs/run-a/report_file.html",
        "reports/runs/run-b/report_file.html",
    ]
    assert list(latest["run_id"]) == ["run-b"]
    assert list(latest["row_index_0_based"]) == [150]


def test_anomaly_explorer_filters_file_agent_severity_scope_and_run():
    frame = pd.DataFrame(
        [
            {"filename": "a.xlsx", "agent": "stats", "severity": "low", "scope": "COLUMN", "run_id": "a", "description": "one", "detected_value": 1, "columns": ["x"]},
            {"filename": "b.xlsx", "agent": "realisme", "severity": "high", "scope": "CELL", "run_id": "b", "description": "two", "detected_value": 2, "columns": ["y"]},
        ]
    )
    filtered = filter_anomaly_records(
        frame,
        {
            "filename": ["b.xlsx"],
            "agent": ["realisme"],
            "severity": ["high"],
            "scope": ["CELL"],
            "run_id": ["b"],
        },
    )

    assert list(filtered["filename"]) == ["b.xlsx"]
