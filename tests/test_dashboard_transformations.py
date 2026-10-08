"""Tests des DataFrames et agrégations du dashboard."""

from datetime import date

from dashboard.transformations import (
    apply_file_filters,
    build_dataframes,
    calculate_kpis,
    filter_counter_df,
)


def _history():
    return [
        {
            "run_id": "run-1",
            "started_at_utc": "2026-10-08T10:00:00Z",
            "finished_at_utc": "2026-10-08T10:01:00Z",
            "duration_seconds": 60,
            "storage_mode": "local",
            "summary": {
                "files_discovered": 2,
                "files_processed": 2,
                "accepted": 1,
                "warning": 0,
                "rejected": 0,
                "incomplete": 1,
                "quarantined": 1,
                "average_score": 91,
                "total_anomalies": 3,
            },
            "files": [
                {
                    "filename": "ok.xlsx",
                    "load_lock": "LOAD_LOCK_202603",
                    "source": "data/LOAD_LOCK_202603/ok.xlsx",
                    "pipeline_status": "COMPLETE",
                    "verdict": "ACCEPTED",
                    "score": 91,
                    "grade": "A",
                    "anomalies_total": 1,
                    "decision_reason": "Stored verdict reason",
                    "anomalies_by_agent": {"structure": 1, "stats": 0},
                    "anomalies_by_severity": {"low": 1},
                    "quarantined": False,
                },
                {
                    "filename": "incomplete.xlsx",
                    "load_lock": "LOAD_LOCK_202604",
                    "source": "data/LOAD_LOCK_202604/incomplete.xlsx",
                    "pipeline_status": "INCOMPLETE",
                    "verdict": None,
                    "score": None,
                    "grade": None,
                    "anomalies_total": 2,
                    "anomalies_by_agent": {"coherence": 2},
                    "anomalies_by_severity": {"high": 2},
                    "quarantined": True,
                    "quarantine_key": "quarantine/LOAD_LOCK_202604/incomplete.xlsx",
                },
            ],
        }
    ]


def test_dataframes_scores_none_statuts_quarantaine_et_compteurs():
    runs_df, files_df, agents_df, severity_df = build_dataframes(_history())

    assert len(runs_df) == 1
    assert runs_df.iloc[0]["files_processed"] == 2
    assert runs_df.iloc[0]["average_score"] == 91
    assert len(files_df) == 2
    assert files_df.loc[files_df["filename"] == "incomplete.xlsx", "status"].item() == "INCOMPLETE"
    assert files_df["score"].mean() == 91
    assert agents_df.groupby("agent")["count"].sum().to_dict()["coherence"] == 2
    assert severity_df.groupby("severity")["count"].sum().to_dict()["high"] == 2
    assert files_df.loc[files_df["filename"] == "ok.xlsx", "decision_reason"].item() == "Stored verdict reason"

    kpis = calculate_kpis(runs_df, files_df)
    assert kpis == {
        "executions": 1,
        "completed_audits": 1,
        "empty_runs": 0,
        "total_files": 2,
        "accepted": 1,
        "warning": 0,
        "rejected": 0,
        "incomplete": 1,
        "quarantined": 1,
        "average_score": 91.0,
        "total_anomalies": 3,
    }


def test_filtres_sont_combinables_et_robustes():
    _, files_df, agents_df, _ = build_dataframes(_history())
    filtres = {
        "load_lock": ["LOAD_LOCK_202603"],
        "verdict": ["ACCEPTED"],
        "pipeline_status": ["COMPLETE"],
        "grade": ["A"],
        "filename": "OK",
        "date_start": date(2026, 10, 8),
        "date_end": date(2026, 10, 8),
    }

    resultat = apply_file_filters(files_df, filtres)
    agents_filtres = filter_counter_df(agents_df, resultat)

    assert resultat["filename"].tolist() == ["ok.xlsx"]
    assert set(agents_filtres["filename"]) == {"ok.xlsx"}
    assert apply_file_filters(files_df, {"colonne_absente": ["x"]}).shape == files_df.shape


def test_history_none_produit_des_dataframes_vides():
    dataframes = build_dataframes(None)
    assert all(df.empty for df in dataframes)


def test_verdict_counts_distinguent_warning_rejected_et_incomplete():
    history = _history()
    history[0]["files"].extend(
        [
            {
                "filename": "warning.xlsx",
                "load_lock": "LOAD_LOCK_202603",
                "pipeline_status": "COMPLETE",
                "verdict": "WARNING",
                "score": 75,
                "grade": "C",
                "anomalies_total": 1,
                "quarantined": False,
            },
            {
                "filename": "rejected.xlsx",
                "load_lock": "LOAD_LOCK_202603",
                "pipeline_status": "COMPLETE",
                "verdict": "REJECTED",
                "score": 40,
                "grade": "F",
                "anomalies_total": 4,
                "quarantined": True,
            },
        ]
    )
    runs_df, files_df, _, _ = build_dataframes(history)

    kpis = calculate_kpis(runs_df, files_df)

    assert (kpis["accepted"], kpis["warning"], kpis["rejected"], kpis["incomplete"]) == (1, 1, 1, 1)
    assert kpis["quarantined"] == 2


def test_kpis_overview_un_run_un_fichier_rejected_ignore_score_none():
    history = _history()
    history[0]["files"] = [
        {
            "filename": "rejected.xlsx",
            "load_lock": "LOAD_LOCK_202603",
            "pipeline_status": "COMPLETE",
            "verdict": "REJECTED",
            "score": None,
            "anomalies_total": 4,
            "quarantined": True,
        }
    ]

    runs_df, files_df, _, _ = build_dataframes(history)
    kpis = calculate_kpis(runs_df, files_df)

    assert kpis["executions"] == 1
    assert kpis["completed_audits"] == 1
    assert kpis["empty_runs"] == 0
    assert kpis["total_files"] == 1
    assert kpis["rejected"] == 1
    assert kpis["quarantined"] == 1
    assert kpis["average_score"] is None
    assert kpis["total_anomalies"] == 4
