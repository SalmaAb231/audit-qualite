from history.anomaly_traceability import enrich_anomaly, enrich_verdict


BASE = {
    "fichier": "cars.xlsx",
    "ligne": 0,
    "colonnes": ["price"],
    "type": "coherence",
    "severite": "high",
    "source": "llm",
    "valeur": "-1",
    "justification": "Negative price",
    "confiance": 0.9,
}


def _enrich(anomaly, source_metadata=None):
    if source_metadata is None:
        source_metadata = {
            "sheet_name": "Vehicles",
            "columns_in_source_order": ["model", "price"],
            "row_count": 1000,
            "header_rows": 1,
            "row_mapping_status": "exact",
        }
    return enrich_anomaly(
        anomaly,
        run_id="20261008T120000Z_aaaaaaaa",
        audit_timestamp_utc="2026-10-08T12:00:00Z",
        load_lock="LOAD_LOCK_202610",
        filename="cars.xlsx",
        source_reference="input/LOAD_LOCK_202610/cars.xlsx",
        ordinal=0,
        source_metadata=source_metadata,
    )


def test_exact_llm_cell_keeps_zero_based_and_documents_display_rows():
    anomaly = _enrich(BASE)

    assert anomaly["location"] == {
        "scope": "CELL",
        "available": True,
        "precision": "EXACT",
        "location_status": "exact",
        "row_index_0_based": 0,
        "row_number_1_based": 1,
        "physical_row_number_1_based": 2,
        "physical_row_status": "derived",
        "row_indices_0_based": [0],
        "known_row_indices_0_based": [],
        "affected_row_count": None,
        "columns": ["price"],
        "column": "price",
        "sheet_name": "Vehicles",
        "cell_reference": "B2",
        "unavailable_reason": None,
        "basis": "pandas source index (0-based); data row number is index+1; physical row uses the loader sidecar",
    }


def test_legacy_deterministic_zero_never_becomes_a_fabricated_cell():
    anomaly = _enrich({**BASE, "type": "statistique", "source": "stats"})

    assert anomaly["location"]["available"] is False
    assert anomaly["location"]["precision"] == "PARTIAL"
    assert anomaly["location"]["row_index_0_based"] is None
    assert "ambiguous line=0" in anomaly["location"]["unavailable_reason"]


def test_multi_row_exact_indices_are_preserved_without_sheet_invention():
    anomaly = _enrich(
        {**BASE, "ligne": 4, "lignes": [4, 8, 10], "colonnes": []},
        source_metadata={},
    )

    assert anomaly["location"]["scope"] == "MULTI_ROW"
    assert anomaly["location"]["row_indices_0_based"] == [4, 8, 10]
    assert anomaly["location"]["sheet_name"] is None


def test_physical_row_and_cell_are_unavailable_without_loader_mapping_proof():
    anomaly = _enrich(BASE, source_metadata={})

    assert anomaly["location"]["row_index_0_based"] == 0
    assert anomaly["location"]["physical_row_number_1_based"] is None
    assert anomaly["location"]["physical_row_status"] == "unavailable"
    assert anomaly["location"]["cell_reference"] is None


def test_verdict_enrichment_preserves_business_decision_and_counts():
    verdict = {
        "verdict": "WARNING",
        "score": 88,
        "grade": "B",
        "nb_problemes_consolides": 1,
        "anomalies": [BASE],
        "anomalies_consolidees": [BASE],
    }

    enriched = enrich_verdict(
        verdict,
        run_id="run-1",
        audit_timestamp_utc="2026-10-08T12:00:00Z",
        load_lock="LOCK",
        filename="cars.xlsx",
        source_reference="input/LOCK/cars.xlsx",
    )

    assert (enriched["verdict"], enriched["score"], enriched["grade"]) == ("WARNING", 88, "B")
    assert enriched["nb_problemes_consolides"] == 1
    assert enriched["anomalies"] == enriched["anomalies_consolidees"]
    assert enriched["anomalies"][0]["run_id"] == "run-1"
    assert verdict["anomalies"][0].get("run_id") is None


def test_file_and_column_scopes_do_not_invent_rows_or_cells():
    file_level = _enrich({**BASE, "ligne": None, "colonnes": [], "portee": "fichier"})
    column_level = _enrich({**BASE, "ligne": None, "portee": "colonne"})

    assert file_level["location"]["scope"] == "FILE"
    assert file_level["location"]["row_index_0_based"] is None
    assert column_level["location"]["scope"] == "COLUMN"
    assert column_level["location"]["column"] == "price"
    assert column_level["location"]["cell_reference"] is None


def test_agent_severity_and_optional_expected_rule_are_preserved():
    anomaly = _enrich({**BASE, "expected": ">= 0", "rule_id": "positive_price"})

    assert anomaly["agent"] == "coherence"
    assert anomaly["severite"] == "high"
    assert anomaly["expected"] == ">= 0"
    assert anomaly["rule"] == "positive_price"
