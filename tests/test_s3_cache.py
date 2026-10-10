"""Régressions du cache S3 ; aucun client AWS ni appel réseau réel."""

from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json

from botocore.exceptions import ClientError
import pytest

import config
from agents.agent_coherence import construire_identite_checkpoint
from ingestion.local_loader import LocalLoader
from ingestion.s3_client import S3Loader


KEY = "input/LOAD_LOCK_202603/vehicles.csv"


class FakeS3:
    def __init__(self):
        self.payload = b"Model,Price\nA,100\n"
        self.version_id = None
        self.downloads = 0
        self.requests = []
        self.fail_download = False
        self.change_during_download = False
        self.truncate = False

    def head_object(self, **kwargs):
        metadata = {
            "ContentLength": len(self.payload),
            "ETag": '"' + hashlib.md5(self.payload).hexdigest() + '"',
            "LastModified": datetime(2026, 10, 10, tzinfo=timezone.utc),
        }
        if self.version_id is not None:
            metadata["VersionId"] = self.version_id
        return metadata

    def get_object(self, **kwargs):
        self.downloads += 1
        self.requests.append(kwargs)
        if self.fail_download:
            raise OSError("simulated download failure")
        if self.change_during_download:
            self.payload = self.payload.replace(b"200", b"300").replace(b"100", b"300")
        metadata = self.head_object()
        if kwargs["IfMatch"] != metadata["ETag"]:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "GetObject")
        metadata["Body"] = BytesIO(self.payload[:-2] if self.truncate else self.payload)
        return metadata


@pytest.fixture
def loader(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    instance = object.__new__(S3Loader)
    instance.bucket = "example-bucket"
    instance.prefix = "input/"
    instance._s3 = FakeS3()
    instance._loader_local = LocalLoader(tmp_path / "cache")
    return instance


def test_unchanged_object_reuses_verified_cache(loader):
    assert loader.charger(KEY)["Price"].tolist() == [100]
    assert loader.charger(KEY)["Price"].tolist() == [100]
    assert loader._s3.downloads == 1


def test_same_size_replacement_refreshes_data_and_checkpoint_identity(loader):
    loader.charger(KEY)
    before = construire_identite_checkpoint(loader.chemin_cache(KEY), KEY)
    loader._s3.payload = loader._s3.payload.replace(b"100", b"200")

    assert loader.charger(KEY)["Price"].tolist() == [200]
    after = construire_identite_checkpoint(loader.chemin_cache(KEY), KEY)

    assert before["source_path"] == after["source_path"]
    assert before["checkpoint_id"] != after["checkpoint_id"]
    assert loader._s3.downloads == 2


@pytest.mark.parametrize("metadata_text", [None, "invalid-json", "[]"])
def test_legacy_or_invalid_sidecar_forces_download(loader, metadata_text):
    path = loader.chemin_cache(KEY)
    path.parent.mkdir(parents=True)
    path.write_bytes(loader._s3.payload.replace(b"100", b"900"))
    if metadata_text is not None:
        path.with_name(path.name + ".s3-cache.json").write_text(metadata_text)

    assert loader.charger(KEY)["Price"].tolist() == [100]
    assert loader._s3.downloads == 1


def test_same_size_local_corruption_forces_download(loader):
    loader.charger(KEY)
    path = loader.chemin_cache(KEY)
    path.write_bytes(loader._s3.payload.replace(b"100", b"900"))

    assert loader.charger(KEY)["Price"].tolist() == [100]
    assert loader._s3.downloads == 2


def test_bucket_change_does_not_reuse_other_bucket_cache(loader):
    loader.charger(KEY)
    loader.bucket = "another-example-bucket"
    loader.charger(KEY)
    assert loader._s3.downloads == 2


def test_version_change_invalidates_cache_without_extra_iam_permission(loader):
    loader._s3.version_id = "version-one"
    loader.charger(KEY)
    assert "VersionId" not in loader._s3.requests[-1]
    loader._s3.version_id = "version-two"
    loader.charger(KEY)
    path = loader.chemin_cache(KEY)
    metadata = json.loads(path.with_name(path.name + ".s3-cache.json").read_text())
    assert metadata["identity"]["version_id"] == "version-two"
    assert loader._s3.downloads == 2


def test_version_race_with_same_etag_never_updates_cache(loader, monkeypatch):
    loader._s3.version_id = "version-one"
    loader.charger(KEY)
    path = loader.chemin_cache(KEY)
    sidecar = path.with_name(path.name + ".s3-cache.json")
    old_metadata = sidecar.read_bytes()
    loader._s3.version_id = "version-two"
    original_get = loader._s3.get_object

    def changed_version(**kwargs):
        loader._s3.version_id = "version-three"
        return original_get(**kwargs)

    monkeypatch.setattr(loader._s3, "get_object", changed_version)

    assert loader.charger(KEY) is None
    assert sidecar.read_bytes() == old_metadata


@pytest.mark.parametrize("failure", ["fail_download", "change_during_download", "truncate"])
def test_failed_refresh_preserves_cache_but_never_returns_stale_data(loader, failure):
    loader.charger(KEY)
    path = loader.chemin_cache(KEY)
    old_payload = path.read_bytes()
    sidecar = path.with_name(path.name + ".s3-cache.json")
    old_metadata = json.loads(sidecar.read_text())
    loader._s3.payload = old_payload.replace(b"100", b"200")
    setattr(loader._s3, failure, True)

    assert loader.charger(KEY) is None
    assert path.read_bytes() == old_payload
    assert json.loads(sidecar.read_text()) == old_metadata
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name, sidecar.name]
