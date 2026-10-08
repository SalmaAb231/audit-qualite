"""Tests du chargement history sans accès AWS réel."""

import json

import pytest
from botocore.exceptions import ClientError

from dashboard.data_loader import (
    history_runs_prefix,
    load_history_runs_local,
    load_history_runs_s3,
)
from ingestion.s3_client import S3Loader, S3OperationError


def _run(run_id="run-1", files=None, summary=None):
    return {
        "run_id": run_id,
        "started_at_utc": "2026-10-08T10:00:00Z",
        "summary": {} if summary is None else summary,
        "files": [] if files is None else files,
    }


def test_history_local_valide_vide_invalide_et_plusieurs_runs(tmp_path):
    history_dir = tmp_path / "history"
    dossier = history_dir / "runs" / "2026" / "10" / "08"
    dossier.mkdir(parents=True)
    (dossier / "a.json").write_text(json.dumps(_run("run-a")), encoding="utf-8")
    (dossier / "b.json").write_text(
        json.dumps(_run("run-b", files=[{"filename": "a.xlsx"}])),
        encoding="utf-8",
    )
    (dossier / "broken.json").write_text("{pas du json", encoding="utf-8")

    resultat = load_history_runs_local(history_dir)

    runs_par_id = {run["run_id"]: run for run in resultat.runs}
    assert set(runs_par_id) == {"run-a", "run-b"}
    assert runs_par_id["run-b"]["files"] == [{"filename": "a.xlsx"}]
    assert len(resultat.warnings) == 1


def test_history_local_absent_est_un_resultat_vide(tmp_path):
    resultat = load_history_runs_local(tmp_path / "absent")
    assert resultat.runs == []
    assert resultat.warnings == []


def test_history_local_normalise_les_donnees_none(tmp_path):
    dossier = tmp_path / "history" / "runs"
    dossier.mkdir(parents=True)
    (dossier / "none.json").write_text(
        json.dumps({"run_id": "run-none", "summary": None, "files": None}),
        encoding="utf-8",
    )

    resultat = load_history_runs_local(tmp_path / "history")

    assert resultat.runs[0]["summary"] == {}
    assert resultat.runs[0]["files"] == []


class _FakeS3Loader:
    def __init__(self, objets):
        self.objets = objets
        self.prefixe_recu = None

    def lister_cles(self, prefixe):
        self.prefixe_recu = prefixe
        return list(self.objets)

    def lire_objet(self, cle):
        return self.objets[cle]


def test_history_s3_liste_une_fois_et_lit_les_json_sans_boto_reel():
    loader = _FakeS3Loader(
        {
            "history/runs/2026/10/08/a.json": json.dumps(_run("run-s3")).encode(),
            "history/runs/2026/10/08/invalide.json": b"not-json",
            "history/runs/readme.txt": b"ignore",
        }
    )

    resultat = load_history_runs_s3(loader, "history/")

    assert loader.prefixe_recu == "history/runs/"
    assert [run["run_id"] for run in resultat.runs] == ["run-s3"]
    assert len(resultat.warnings) == 1
    assert history_runs_prefix("history\\") == "history/runs/"


def test_erreur_listing_s3_reste_non_bloquante():
    class _LoaderEnErreur:
        def lister_cles(self, prefixe):
            raise RuntimeError("panne simulée")

    resultat = load_history_runs_s3(_LoaderEnErreur())

    assert resultat.runs == []
    assert resultat.warnings


def test_listing_s3_vide_retourne_un_resultat_vide_sans_warning():
    resultat = load_history_runs_s3(_FakeS3Loader({}), "history/")

    assert resultat.runs == []
    assert resultat.warnings == []


def test_access_denied_du_client_s3_ne_devient_pas_silencieusement_une_liste_vide():
    class _Paginator:
        def paginate(self, **kwargs):
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "denied"}},
                "ListObjectsV2",
            )

    class _Client:
        def get_paginator(self, operation):
            assert operation == "list_objects_v2"
            return _Paginator()

    loader = object.__new__(S3Loader)
    loader.bucket = "bucket-test"
    loader._s3 = _Client()

    with pytest.raises(S3OperationError, match="AccessDenied"):
        loader.lister_cles("history/runs/")

    resultat = load_history_runs_s3(loader, "history/")
    assert resultat.runs == []
    assert any("s3:ListBucket" in warning for warning in resultat.warnings)


def test_erreur_sur_un_objet_history_nempeche_pas_de_lire_les_autres():
    class _LoaderLecturePartielle(_FakeS3Loader):
        def lire_objet(self, cle):
            if cle.endswith("broken.json"):
                raise S3OperationError("AccessDenied")
            return self.objets[cle]

    loader = _LoaderLecturePartielle(
        {
            "history/runs/2026/10/08/broken.json": b"ignored",
            "history/runs/2026/10/08/valid.json": json.dumps(_run("run-valid")).encode(),
        }
    )

    resultat = load_history_runs_s3(loader, "history/")

    assert [run["run_id"] for run in resultat.runs] == ["run-valid"]
    assert len(resultat.warnings) == 1


def test_interface_s3_generique_liste_et_lit_avec_un_client_mocke():
    class _Body:
        def read(self):
            return b'{"run_id": "run-mock"}'

    class _Paginator:
        def paginate(self, **kwargs):
            assert kwargs == {"Bucket": "bucket-test", "Prefix": "history/runs/"}
            return [
                {
                    "Contents": [
                        {"Key": "history/runs/"},
                        {"Key": "history/runs/run-mock.json"},
                    ]
                }
            ]

    class _Client:
        def get_paginator(self, operation):
            assert operation == "list_objects_v2"
            return _Paginator()

        def get_object(self, **kwargs):
            assert kwargs == {"Bucket": "bucket-test", "Key": "history/runs/run-mock.json"}
            return {"Body": _Body()}

    loader = object.__new__(S3Loader)
    loader.bucket = "bucket-test"
    loader._s3 = _Client()

    assert loader.lister_cles("history/runs/") == ["history/runs/run-mock.json"]
    assert loader.lire_objet("history/runs/run-mock.json") == b'{"run_id": "run-mock"}'
