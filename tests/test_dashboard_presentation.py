import pandas as pd
import inspect
from pathlib import Path
from types import SimpleNamespace

from dashboard import charts, components
from dashboard.components import (
    active_filter_labels,
    estimate_chunk_count,
    estimate_max_llm_calls,
    format_decision_reason,
    format_short_run_id,
    format_user_reason,
    format_duration,
    format_optional_number,
    render_artifact_access,
    render_audit_configuration,
    render_global_filters,
    render_sidebar_environment,
    quota_risk_level,
    stored_value,
    utc_now_label,
)
from dashboard.pages import agents, anomalies, files, new_audit, overview, quarantine, runs
from dashboard.styles import CSS, inject_global_styles
from dashboard.transformations import (
    agent_display_label,
    build_agents_df,
    build_files_df,
    build_runs_df,
    calculate_kpis,
    dashboard_metrics,
    derive_run_status,
    display_columns,
    filtered_view_counts,
    filter_runs_by_view,
    historical_detections_by_file,
    latest_anomalies_by_file,
    unique_file_identity_count,
    time_series_state,
    utc_timeline_data,
)


def test_missing_values_are_not_rendered_as_zero():
    assert format_optional_number(None) == "N/A"
    assert format_optional_number(float("nan")) == "N/A"
    assert format_optional_number(0) == "0"


def test_scores_have_two_decimals():
    assert format_optional_number(91.2, 2) == "91.20"


def test_duration_is_human_readable():
    assert format_duration(0.25) == "250 ms"
    assert format_duration(75) == "1 min 15 s"


def test_short_run_ids_keep_time_and_unique_suffix():
    first = format_short_run_id("20261008T175200Z_a1b2c3d4")
    second = format_short_run_id("20261008T175200Z_f6e5d4c3")
    assert first != second
    assert first.startswith("17:52:00")
    assert "a1b2c3" in first
    assert "f6e5d4" in second


def test_run_status_keeps_zero_file_executions_visible():
    assert derive_run_status({}, {"files_processed": 0}) == "NO FILE PROCESSED"
    assert derive_run_status({}, {"files_processed": 2}) == "COMPLETED"
    assert derive_run_status({}, {}) == "N/A"


def test_timeline_requires_two_nonzero_points():
    assert time_series_state(pd.Series(dtype="float64")) == "missing"
    assert time_series_state(pd.Series([0, 0])) == "zero"
    assert time_series_state(pd.Series([2])) == "insufficient"
    assert time_series_state(pd.Series([1, 2])) == "ready"


def test_execution_and_completed_audit_counts_are_distinct():
    runs = pd.DataFrame(
        [
            {"files_processed": 0, "run_status": "NO FILE PROCESSED"},
            {"files_processed": 2, "run_status": "COMPLETED"},
        ]
    )
    files = pd.DataFrame(
        [
            {"status": "ACCEPTED", "score": 90, "anomalies_total": 0, "quarantined": False},
            {"status": "WARNING", "score": 75, "anomalies_total": 1, "quarantined": False},
        ]
    )
    kpis = calculate_kpis(runs, files)
    assert kpis["executions"] == 2
    assert kpis["completed_audits"] == 1
    assert kpis["empty_runs"] == 1


def test_agent_page_uses_contribution_terminology():
    source = inspect.getsource(agents)
    assert "Agent Contribution" in source
    assert "Agent Performance" not in source


def test_real_agent_keys_use_display_only_labels():
    assert agent_display_label("structure") == "Structure"
    assert agent_display_label("stats") == "Statistics"
    assert agent_display_label("statistics") == "Statistics"
    assert agent_display_label("coherence") == "Coherence"
    assert agent_display_label("realisme") == "Realism"
    assert agent_display_label("realism") == "Realism"


def test_agent_cards_and_detection_sources_share_identical_totals():
    frame = pd.DataFrame(
        [
            {"agent": "structure", "count": 3},
            {"agent": "statistics", "count": 13},
            {"agent": "coherence", "count": 30},
            {"agent": "realism", "count": 1},
        ]
    )
    pd.testing.assert_series_equal(
        agents.agent_card_totals(frame),
        anomalies.detection_source_totals(frame),
    )


def test_affected_file_distribution_uses_filenames_not_pandas_index():
    frame = pd.DataFrame(
        [
            {"filename": "first.xlsx", "anomalies_total": 39},
            {"filename": "second.xlsx", "anomalies_total": 8},
        ]
    )
    result = anomalies.affected_file_totals(frame)
    assert result.index.tolist() == ["first.xlsx", "second.xlsx"]


def test_chart_timestamp_labels_preserve_history_utc_time():
    series = pd.Series(
        [3, 4],
        index=pd.to_datetime(["2026-10-08T14:46:00Z", "2026-10-08T15:43:00Z"], utc=True),
    )
    result = utc_timeline_data(series, "Anomalies")
    assert result["Timestamp UTC"].tolist() == ["08 Oct 14:46", "08 Oct 15:43"]


def test_audit_button_depends_on_current_running_state_not_previous_completion():
    assert new_audit.audit_submit_disabled(True, "COMPLETED") is False
    assert new_audit.audit_submit_disabled(True, "RUNNING") is True
    assert new_audit.audit_submit_disabled(False, "IDLE") is True


def test_missing_stored_text_uses_not_stored():
    assert stored_value(None) == "Not stored"
    assert stored_value("") == "Not stored"
    assert stored_value("Judge explanation") == "Judge explanation"


def test_decision_reason_is_displayed_when_history_contains_it():
    assert files.displayed_decision_reason(
        pd.Series({"decision_reason": "Stored Judge explanation"})
    ) == "Stored Judge explanation"


def test_missing_severity_has_one_explicit_message():
    assert anomalies.severity_availability_message(pd.DataFrame()) == (
        "Severity breakdown is not available for this historical record."
    )


def test_filtered_counts_keep_loaded_and_displayed_counts_distinct():
    counts = filtered_view_counts(
        pd.DataFrame(index=range(2)),
        pd.DataFrame(index=range(5)),
        pd.DataFrame(index=range(2)),
        pd.DataFrame(index=range(2)),
    )
    assert counts == {
        "displayed_executions": 2,
        "loaded_executions": 5,
        "displayed_files": 2,
        "loaded_files": 2,
    }


def test_internal_headers_are_mapped_to_user_labels():
    visible = display_columns(
        pd.DataFrame(columns=["filename", "load_lock", "status", "anomalies_total"])
    )
    assert visible.columns.tolist() == ["Filename", "LOAD_LOCK", "Audit Outcome", "Anomalies"]


def test_incomplete_pipeline_does_not_create_a_judge_verdict():
    frame = build_files_df(
        [
            {
                "run_id": "run-1",
                "started_at_utc": "2026-10-08T14:00:00Z",
                "files": [
                    {
                        "filename": "failed.xlsx",
                        "pipeline_status": "INCOMPLETE",
                        "verdict": "REJECTED",
                        "score": 12,
                    }
                ],
            }
        ]
    )
    assert frame.loc[0, "pipeline_status"] == "INCOMPLETE"
    assert pd.isna(frame.loc[0, "verdict"])
    assert pd.isna(frame.loc[0, "score"])


def test_completed_execution_can_contain_an_incomplete_file():
    history = [
        {
            "run_id": "run-1",
            "started_at_utc": "2026-10-08T14:00:00Z",
            "summary": {
                "files_processed": 1,
                "accepted": 0,
                "warning": 0,
                "rejected": 0,
                "incomplete": 1,
            },
            "files": [{"filename": "failed.xlsx", "pipeline_status": "INCOMPLETE"}],
        }
    ]
    run_frame = build_runs_df(history)
    file_frame = build_files_df(history)
    assert run_frame.loc[0, "run_status"] == "COMPLETED"
    assert run_frame.loc[0, "incomplete"] == 1
    assert file_frame.loc[0, "pipeline_status"] == "INCOMPLETE"
    assert pd.isna(file_frame.loc[0, "verdict"])
    assert runs.format_stop_reason("COMPLETED", None) == "—"


def test_runs_presentation_separates_execution_status_from_audit_outcomes():
    source = inspect.getsource(runs)
    assert 'table["Execution Status"]' in source
    assert 'st.markdown("#### Audit Outcomes")' in source


def test_runs_table_exposes_incomplete_column():
    assert 'table["Incomplete"]' in inspect.getsource(runs)


def test_failed_agent_count_is_na_but_successful_counts_are_preserved():
    frame = build_agents_df(
        [
            {
                "run_id": "run-1",
                "started_at_utc": "2026-10-08T14:00:00Z",
                "files": [
                    {
                        "filename": "file.xlsx",
                        "anomalies_by_agent": {"structure": 3, "realisme": 0},
                        "agent_statuses": {
                            "structure": {"status": "SUCCESS"},
                            "stats": {"status": "SUCCESS"},
                            "coherence": {"status": "SUCCESS"},
                            "realisme": {"status": "FAILED", "error": "quota"},
                        },
                    }
                ],
            }
        ]
    )
    structure = frame.loc[frame["agent"] == "structure"].iloc[0]
    realism = frame.loc[frame["agent"] == "realisme"].iloc[0]
    assert structure["count"] == 3
    assert structure["status"] == "SUCCESS"
    assert pd.isna(realism["count"])
    assert realism["status"] == "FAILED"


def test_latest_file_totals_do_not_sum_repeated_historical_audits():
    frame = pd.DataFrame(
        [
            {"filename": "repeat.xlsx", "timestamp": "2026-10-08T10:00:00Z", "anomalies_total": 8},
            {"filename": "repeat.xlsx", "timestamp": "2026-10-08T11:00:00Z", "anomalies_total": 2},
        ]
    )
    assert anomalies.affected_file_totals(frame).loc["repeat.xlsx"] == 10
    assert anomalies.latest_file_anomaly_totals(frame).loc["repeat.xlsx"] == 2


def test_three_repeated_audits_are_117_cumulative_and_39_latest():
    frame = pd.DataFrame(
        [
            {
                "source": "s3://bucket/data/LOAD_LOCK_202603/repeat.xlsx",
                "filename": "repeat.xlsx",
                "timestamp": f"2026-10-08T1{hour}:00:00Z",
                "anomalies_total": 39,
            }
            for hour in range(3)
        ]
    )
    assert historical_detections_by_file(frame).loc["repeat.xlsx"] == 117
    assert latest_anomalies_by_file(frame).loc["repeat.xlsx"] == 39


def test_unique_files_impacted_uses_source_then_load_lock_filename_identity():
    frame = pd.DataFrame(
        [
            {"source": "s3://bucket/a.xlsx", "filename": "a.xlsx", "load_lock": "L1", "anomalies_total": 3},
            {"source": "s3://bucket/a.xlsx", "filename": "a.xlsx", "load_lock": "L1", "anomalies_total": 2},
            {"source": None, "filename": "b.xlsx", "load_lock": "L2", "anomalies_total": 1},
            {"source": None, "filename": "clean.xlsx", "load_lock": "L3", "anomalies_total": 0},
        ]
    )
    assert unique_file_identity_count(frame, impacted_only=True) == 2


def test_audit_chart_uses_independent_markers_and_business_colors():
    frame = pd.DataFrame(
        [
            {
                "timestamp": f"2026-10-08T1{index}:00:00Z",
                "filename": f"file-{index}.xlsx",
                "load_lock": "LOAD_LOCK_1",
                "anomalies_total": index + 1,
                "pipeline_status": "INCOMPLETE" if outcome == "INCOMPLETE" else "COMPLETE",
                "verdict": None if outcome == "INCOMPLETE" else outcome,
                "status": outcome,
                "score": None if outcome == "INCOMPLETE" else 80 + index,
            }
            for index, outcome in enumerate(("ACCEPTED", "WARNING", "REJECTED", "INCOMPLETE"))
        ]
    )
    figure = charts.anomalies_per_audit(frame)
    assert all(trace.mode == "markers" for trace in figure.data)
    colors = {trace.name.upper(): trace.marker.color for trace in figure.data}
    assert colors == charts.OUTCOME_COLORS


def test_agent_audit_chart_does_not_connect_different_files():
    points = pd.DataFrame(
        [
            {
                "audit_time": "2026-10-08T10:00:00Z",
                "agent": "Structure",
                "detections": 3,
                "filename": "a.xlsx",
                "load_lock": "L1",
                "agent_status": "SUCCESS",
                "pipeline_status": "COMPLETE",
                "verdict": "ACCEPTED",
            },
            {
                "audit_time": "2026-10-08T11:00:00Z",
                "agent": "Structure",
                "detections": 8,
                "filename": "b.xlsx",
                "load_lock": "L2",
                "agent_status": "SUCCESS",
                "pipeline_status": "COMPLETE",
                "verdict": "WARNING",
            },
        ]
    )
    figure = charts.agent_detections_by_audit(points)
    assert all(trace.mode == "markers" for trace in figure.data)


def test_latest_file_values_use_load_lock_as_part_of_fallback_identity():
    frame = pd.DataFrame(
        [
            {
                "load_lock": "LOAD_LOCK_1",
                "filename": "same.xlsx",
                "timestamp": "2026-10-08T10:00:00Z",
                "anomalies_total": 8,
            },
            {
                "load_lock": "LOAD_LOCK_1",
                "filename": "same.xlsx",
                "timestamp": "2026-10-08T11:00:00Z",
                "anomalies_total": 2,
            },
            {
                "load_lock": "LOAD_LOCK_2",
                "filename": "same.xlsx",
                "timestamp": "2026-10-08T12:00:00Z",
                "anomalies_total": 5,
            },
        ]
    )
    latest = latest_anomalies_by_file(frame)
    assert latest.to_dict() == {
        "LOAD_LOCK_2 / same.xlsx": 5.0,
        "LOAD_LOCK_1 / same.xlsx": 2.0,
    }


def test_quarantine_events_and_unique_files_are_distinct():
    frame = pd.DataFrame(
        [
            {"filename": "repeat.xlsx", "quarantined": True},
            {"filename": "repeat.xlsx", "quarantined": True},
            {"filename": "clean.xlsx", "quarantined": False},
        ]
    )
    assert quarantine.quarantine_history_counts(frame) == (2, 1, 3)


def test_runs_view_filter_never_removes_persisted_rows():
    frame = pd.DataFrame(
        [
            {"run_id": "processed", "files_processed": 2},
            {"run_id": "empty", "files_processed": 0},
            {"run_id": "unknown", "files_processed": None},
        ]
    )
    processed = filter_runs_by_view(frame, "Processed runs")
    restored = filter_runs_by_view(frame, "All runs")
    assert processed["run_id"].tolist() == ["processed"]
    assert restored["run_id"].tolist() == ["processed", "empty", "unknown"]
    assert len(frame) == 3


def test_llm_call_estimate_and_active_filter_labels():
    assert estimate_max_llm_calls(3, attempts_per_chunk=4) == 24
    labels = active_filter_labels(
        {"pipeline_status": ["INCOMPLETE"], "verdict": [], "filename": "sales"}
    )
    assert labels == ["Pipeline Status: INCOMPLETE", "Filename: sales"]


def test_audit_outcomes_keep_all_categories_in_fixed_order():
    frame = pd.DataFrame(
        {"status": ["WARNING", "REJECTED", "INCOMPLETE", "INCOMPLETE"]}
    )
    counts = overview.audit_outcome_counts(frame)
    assert counts.index.tolist() == ["ACCEPTED", "WARNING", "REJECTED", "INCOMPLETE"]
    assert counts.to_dict() == {
        "ACCEPTED": 0,
        "WARNING": 1,
        "REJECTED": 1,
        "INCOMPLETE": 2,
    }


def test_user_reason_is_short_and_technical_reason_is_not_mutated():
    technical = (
        "Realism FAILED: HTTP 429 provider temporarily rate-limited upstream; "
        "model=nvidia/example:free"
    )
    assert format_user_reason(technical) == "Realism agent failed: OpenRouter rate limit."
    record = pd.Series({"decision_reason": technical})
    assert files.displayed_decision_reason(record) == "Realism agent failed: OpenRouter rate limit."
    assert record["decision_reason"] == technical


def test_accepted_record_without_persisted_reason_displays_dash():
    assert format_decision_reason(None, "COMPLETE", "ACCEPTED") == "—"


def test_new_audit_completion_message_warns_for_incomplete_result():
    message, level = new_audit.audit_completion_message({"incomplete": 1})
    assert level == "warning"
    assert "Execution completed" in message
    assert "1 audit result is INCOMPLETE" in message
    assert "Audit completed successfully" not in message
    level, refreshed = new_audit.history_refresh_feedback(
        {"summary": {"incomplete": 1}}, "s3"
    )
    assert level == "warning"
    assert "INCOMPLETE" in refreshed


def test_agent_status_metadata_coverage_uses_all_audit_file_records():
    frame = pd.DataFrame(
        [
            {"agent": "structure", "status": "UNKNOWN", "count": 2, "timestamp": "2026-10-01T10:00:00Z"},
            {"agent": "structure", "status": "UNKNOWN", "count": 1, "timestamp": "2026-10-02T10:00:00Z"},
            {"agent": "structure", "status": "UNKNOWN", "count": 3, "timestamp": "2026-10-03T10:00:00Z"},
            {"agent": "structure", "status": "SUCCESS", "count": 1, "timestamp": "2026-10-04T10:00:00Z"},
        ]
    )
    metrics = agents.agent_coverage_metrics(frame)["structure"]
    assert metrics["historical_detections"] == 7
    assert metrics["audit_records"] == 4
    assert metrics["records_with_status"] == 1
    assert metrics["unknown"] == 3
    assert metrics["metadata_coverage"] == 25
    assert metrics["success_rate"] == 100


def test_missing_agent_status_history_is_unknown_not_success_or_failed():
    frame = build_agents_df(
        [
            {
                "run_id": "old-run",
                "started_at_utc": "2026-09-01T10:00:00Z",
                "files": [
                    {
                        "filename": "old.xlsx",
                        "anomalies_by_agent": {"structure": 2},
                    }
                ],
            }
        ]
    )
    structure = frame.loc[frame["agent"] == "structure"].iloc[0]
    assert structure["status"] == "UNKNOWN"
    metrics = agents.agent_coverage_metrics(frame)["structure"]
    assert metrics["records_with_status"] == 0
    assert metrics["successful"] == 0
    assert metrics["failed"] == 0


def test_realism_success_rate_is_50_percent_for_one_success_and_one_failure():
    frame = pd.DataFrame(
        [
            {"agent": "realisme", "status": "SUCCESS", "count": 1, "timestamp": "2026-10-08T10:00:00Z"},
            {"agent": "realisme", "status": "FAILED", "count": None, "timestamp": "2026-10-08T11:00:00Z"},
        ]
    )
    metrics = agents.agent_coverage_metrics(frame)["realisme"]
    assert metrics["success_rate"] == 50
    assert metrics["status"] == "FAILED"


def test_unknown_and_not_run_are_excluded_from_agent_success_rate():
    frame = pd.DataFrame(
        [
            {"agent": "structure", "status": "SUCCESS", "count": 1, "timestamp": "2026-10-08T10:00:00Z"},
            {"agent": "structure", "status": "FAILED", "count": None, "timestamp": "2026-10-08T11:00:00Z"},
            {"agent": "structure", "status": "NOT_RUN", "count": None, "timestamp": "2026-10-08T12:00:00Z"},
            {"agent": "structure", "status": "UNKNOWN", "count": None, "timestamp": "2026-10-08T13:00:00Z"},
        ]
    )
    metrics = agents.agent_coverage_metrics(frame)["structure"]
    assert metrics["success_rate"] == 50
    assert metrics["successful"] == 1
    assert metrics["failed"] == 1
    assert metrics["not_run"] == 1
    assert metrics["unknown"] == 1


def test_quota_risk_is_computed_locally_from_estimate_and_remaining_allowance():
    assert quota_risk_level(4, 13) == "LOW"
    assert quota_risk_level(8, 13) == "MEDIUM"
    assert quota_risk_level(12, 13) == "HIGH"
    assert quota_risk_level(14, 13) == "INSUFFICIENT"


def test_filter_chips_use_neutral_indigo_style():
    selector = '[data-testid="stSidebar"] [data-baseweb="tag"]'
    assert selector in CSS
    assert "#e8edff" in CSS


def test_primary_action_and_responsive_kpi_styles_are_present():
    assert 'button[kind="primary"]' in CSS
    assert "background: var(--dq-blue)" in CSS
    assert ".metric-grid" in CSS
    assert "repeat(3, minmax(0, 1fr))" in CSS


def test_missing_artifact_never_renders_a_download_button(monkeypatch):
    downloads = []
    monkeypatch.setattr(components.st, "markdown", lambda *args, **kwargs: None)
    monkeypatch.setattr(components.st, "caption", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        components.st,
        "download_button",
        lambda *args, **kwargs: downloads.append((args, kwargs)),
    )
    shown = render_artifact_access(
        "Report",
        None,
        mime_type="text/html",
        download_label="Download report",
    )
    assert shown is False
    assert downloads == []


def test_app_component_contract_remains_importable():
    assert callable(render_global_filters)
    assert callable(render_sidebar_environment)
    assert callable(render_audit_configuration)
    assert callable(utc_now_label)
    assert callable(inject_global_styles)


def test_complete_accepted_record_is_never_presented_as_incomplete_from_stale_text():
    reason = format_user_reason(
        "Realism FAILED: HTTP 429 from a previous technical attempt",
        pipeline_status="COMPLETE",
        judge_verdict="ACCEPTED",
    )
    assert reason == "N/A"
    assert "incomplete" not in reason.lower()
    displayed = files.displayed_decision_reason(
        pd.Series(
            {
                "pipeline_status": "COMPLETE",
                "verdict": "ACCEPTED",
                "decision_reason": "Realism FAILED: HTTP 429 from a previous technical attempt",
            }
        )
    )
    assert displayed == "—"


def test_dashboard_metrics_distinguish_history_records_from_unique_files():
    runs = pd.DataFrame([{"files_processed": 1}, {"files_processed": 1}])
    files = pd.DataFrame(
        [
            {
                "source": "s3://bucket/data/file.xlsx",
                "filename": "file.xlsx",
                "timestamp": "2026-10-08T10:00:00Z",
                "status": "WARNING",
                "score": 70,
                "anomalies_total": 8,
                "quarantined": False,
            },
            {
                "source": "s3://bucket/data/file.xlsx",
                "filename": "file.xlsx",
                "timestamp": "2026-10-08T11:00:00Z",
                "status": "ACCEPTED",
                "score": 90,
                "anomalies_total": 2,
                "quarantined": False,
            },
        ]
    )
    metrics = dashboard_metrics(runs, files)
    assert metrics["audit_records"] == 2
    assert metrics["unique_files"] == 1
    assert metrics["historical_detections"] == 10
    assert metrics["average_quality_score"] == 80


def test_llm_estimate_uses_row_counts_when_available_and_is_otherwise_a_minimum():
    assert estimate_chunk_count(
        [{"row_count": 250}, {"row_count": 10}], file_count=2, chunk_size=100
    ) == (4, True)
    assert estimate_chunk_count(
        [{"filename": "unknown.xlsx"}], file_count=1, chunk_size=100
    ) == (1, False)


def test_analytics_pages_do_not_invoke_an_llm_client_on_render():
    for module in (overview, files, anomalies, agents, quarantine, runs):
        source = inspect.getsource(module)
        assert "LLMClient(" not in source
        assert ".appeler(" not in source
        assert "S3Loader(" not in source
        assert "executer_audit(" not in source


def test_navigation_contract_and_secret_names_stay_out_of_client_pages():
    app_source = (Path(__file__).parents[1] / "dashboard" / "app.py").read_text(
        encoding="utf-8"
    )
    assert 'pages = ["Overview", "New Audit", "Files", "Anomalies", "Agents", "Quarantine", "Runs"]' in app_source
    client_source = "\n".join(
        inspect.getsource(module) for module in (overview, files, anomalies, agents, quarantine, runs)
    )
    assert "AWS_SECRET_ACCESS_KEY" not in client_source
    assert "OPENROUTER_API_KEY" not in client_source


def test_anomaly_explorer_uses_a_pre_render_navigation_callback(monkeypatch):
    session_state = SimpleNamespace(navigation="Files")
    monkeypatch.setattr(files, "st", SimpleNamespace(session_state=session_state))

    files._open_anomaly_explorer()

    assert session_state.navigation == "Anomalies"
    render_source = inspect.getsource(files.render)
    assert "on_click=_open_anomaly_explorer" in render_source
    assert "st.session_state.navigation" not in render_source
