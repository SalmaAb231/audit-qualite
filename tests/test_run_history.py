"""Tests unitaires de l'historisation des runs, sans accès AWS."""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from history.run_history import (
    construire_cle_historique_s3,
    construire_document_run,
    construire_entree_fichier,
    construire_prefixe_runs_s3,
    ecrire_historique_local,
    generer_run_id,
)


DEBUT = datetime(2026, 10, 8, 10, 35, 12, tzinfo=timezone.utc)
FIN = datetime(2026, 10, 8, 10, 37, 44, 300000, tzinfo=timezone.utc)


def _entree(
    nom,
    verdict,
    score,
    grade,
    anomalies,
    pipeline_status="COMPLETE",
    quarantined=False,
):
    return construire_entree_fichier(
        filename=nom,
        load_lock="LOAD_LOCK_202603",
        source=Path("data") / "LOAD_LOCK_202603" / nom,
        verdict={
            "pipeline_status": pipeline_status,
            "verdict": verdict,
            "score": score,
            "grade": grade,
            "justification": f"Reason for {nom}",
            "anomalies_consolidees": anomalies,
        },
        quarantined=quarantined,
        report_html=Path("resultats") / "LOAD_LOCK_202603" / f"rapport_{Path(nom).stem}.html",
        verdict_json=Path("resultats") / "LOAD_LOCK_202603" / f"verdict_{Path(nom).stem}.json",
        quarantine_key=(f"quarantine/LOAD_LOCK_202603/{nom}" if quarantined else None),
    )


def test_run_id_format_stable_et_suffixe_unique():
    premier = generer_run_id(DEBUT)
    second = generer_run_id(DEBUT)

    assert re.fullmatch(r"20261008T103512Z_[0-9a-f]{8}", premier)
    assert premier != second


def test_document_run_agrege_statuts_scores_agents_severites_et_quarantaine():
    fichiers = [
        _entree("accepted.xlsx", "ACCEPTED", 95, "A", [{"type": "structure", "severite": "low"}]),
        _entree("warning.xlsx", "WARNING", 72, "C", [{"type": "statistique", "severite": "medium"}]),
        _entree(
            "rejected.xlsx",
            "REJECTED",
            38,
            "F",
            [
                {"type": "coherence", "severite": "high"},
                {"type": "realisme", "severite": "critical"},
            ],
            quarantined=True,
        ),
        _entree(
            "incomplete.xlsx",
            None,
            None,
            None,
            [{"type": "inattendu", "severite": "inconnue"}],
            pipeline_status="INCOMPLETE",
            quarantined=True,
        ),
    ]

    document = construire_document_run(
        run_id="20261008T103512Z_a1b2c3d4",
        started_at_utc=DEBUT,
        finished_at_utc=FIN,
        storage_mode="local",
        files_discovered=5,
        files=fichiers,
    )

    resume = document["summary"]
    assert document["schema_version"] == 1
    assert document["started_at_utc"] == "2026-10-08T10:35:12Z"
    assert document["finished_at_utc"] == "2026-10-08T10:37:44Z"
    assert document["duration_seconds"] == 152.3
    assert resume["files_discovered"] == 5
    assert resume["files_processed"] == 4
    assert (resume["accepted"], resume["warning"], resume["rejected"], resume["incomplete"]) == (1, 1, 1, 1)
    assert resume["quarantined"] == 2
    assert resume["average_score"] == 68.3333
    assert resume["total_anomalies"] == 5
    assert resume["anomalies_by_agent"] == {
        "structure": 1,
        "stats": 1,
        "coherence": 1,
        "realisme": 1,
        "unknown": 1,
    }
    assert resume["anomalies_by_severity"] == {
        "critical": 1,
        "high": 1,
        "medium": 1,
        "low": 1,
        "unknown": 1,
    }
    assert document["files"][3]["verdict"] is None
    assert document["files"][3]["score"] is None
    assert document["files"][3]["grade"] is None


def test_entree_fichier_reference_les_resultats_et_serialise_path():
    entree = _entree(
        "rejected.xlsx",
        "REJECTED",
        40,
        "F",
        [{"type": "coherence", "severite": "high"}],
        quarantined=True,
    )

    assert entree["source"] == "data/LOAD_LOCK_202603/rejected.xlsx"
    assert entree["report_html"].endswith("rapport_rejected.html")
    assert entree["verdict_json"].endswith("verdict_rejected.json")
    assert entree["quarantine_key"] == "quarantine/LOAD_LOCK_202603/rejected.xlsx"
    assert entree["decision_reason"] == "Reason for rejected.xlsx"
    assert entree["artifacts"]["report_html"] == entree["report_html"]
    assert entree["artifacts"]["verdict_json"] == entree["verdict_json"]
    assert entree["artifacts"]["anomalies_json"] is None
    assert "anomalies" not in entree
    json.dumps(entree, ensure_ascii=False)


def test_document_run_stocke_la_reference_du_rapport_consolide_sans_casser_legacy():
    document = construire_document_run(
        run_id="run-versioned",
        started_at_utc=DEBUT,
        finished_at_utc=FIN,
        storage_mode="s3",
        files_discovered=0,
        files=[],
        artifacts={"consolidated_report_html": "reports/runs/run-versioned/rapport_consolide.html"},
    )
    legacy = construire_document_run(
        run_id="run-legacy",
        started_at_utc=DEBUT,
        finished_at_utc=FIN,
        storage_mode="local",
        files_discovered=0,
        files=[],
    )

    assert document["artifacts"]["consolidated_report_html"].startswith("reports/runs/run-versioned/")
    assert "artifacts" not in legacy


def test_entree_fichier_normalise_uniquement_les_alias_agents_connus():
    entree = _entree(
        "aliases.xlsx",
        "WARNING",
        70,
        "C",
        [
            {"type": "statistics", "severite": "medium"},
            {"type": "realism", "severite": "high"},
        ],
    )

    assert entree["anomalies_by_agent"]["stats"] == 1
    assert entree["anomalies_by_agent"]["realisme"] == 1


def test_entree_fichier_persiste_les_statuts_agents_sans_inventer_zero():
    entree = construire_entree_fichier(
        filename="partial.xlsx",
        load_lock="LOAD_LOCK_202603",
        source="input/partial.xlsx",
        verdict={
            "pipeline_status": "INCOMPLETE",
            "verdict": None,
            "anomalies_consolidees": [{"type": "coherence", "severite": "high"}],
            "etapes_pipeline": {
                "structure": {"status": "SUCCESS"},
                "stats": {"status": "SUCCESS"},
                "coherence": {"status": "SUCCESS"},
                "realisme": {"status": "FAILED", "error": "quota"},
            },
        },
        quarantined=True,
    )

    assert entree["anomalies_by_agent"]["coherence"] == 1
    assert entree["agent_statuses"]["coherence"]["status"] == "SUCCESS"
    assert entree["agent_statuses"]["realisme"] == {
        "status": "FAILED",
        "error": "quota",
    }


def test_historique_local_et_cle_s3_suivent_la_meme_date_utc(tmp_path):
    document = construire_document_run(
        run_id="20261008T103512Z_a1b2c3d4",
        started_at_utc=DEBUT,
        finished_at_utc=FIN,
        storage_mode="s3",
        files_discovered=0,
        files=[],
    )

    chemin = ecrire_historique_local(document, tmp_path / "history", DEBUT)
    cle = construire_cle_historique_s3(
        "history\\",
        DEBUT,
        document["run_id"],
    )

    assert chemin == tmp_path / "history" / "runs" / "2026" / "10" / "08" / "20261008T103512Z_a1b2c3d4.json"
    assert json.loads(chemin.read_text(encoding="utf-8")) == document
    assert cle == "history/runs/2026/10/08/20261008T103512Z_a1b2c3d4.json"
    assert "\\" not in cle


def test_prefixe_runs_ne_double_pas_runs_et_reste_compatible_avec_la_cle_writer():
    for prefixe_configure in ("history/", "history/runs/", "history\\runs\\"):
        prefixe_reader = construire_prefixe_runs_s3(prefixe_configure)
        cle_writer = construire_cle_historique_s3(prefixe_configure, DEBUT, "run")

        assert prefixe_reader == "history/runs/"
        assert cle_writer.startswith(prefixe_reader)
        assert "runs/runs" not in cle_writer
        assert "\\" not in cle_writer
