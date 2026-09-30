from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from azure.core import MatchConditions
from azure.core.exceptions import HttpResponseError, ResourceExistsError

from src.harvester import blob_stage
from src.harvester.core import PublicationError, canonical_json
from src.harvester.main import build_parser
from src.harvester.derive import EXTRACT_NAME, REPORT_NAME, extract_digest
from src.harvester.tests.test_derive import derived_run


ACCOUNT = "https://testaccount.blob.core.windows.net"
CONTAINER = "private-staging"


def test_stage_cli_requires_account_url_and_defaults_to_staged_runs() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["stage-blob", "--run-dir", "run"])
    args = parser.parse_args(
        ["stage-blob", "--run-dir", "run", "--account-url", ACCOUNT]
    )
    assert args.container == "staged-runs"
    assert args.credential_mode == "managed-identity"


class MemoryBlobStore:
    def __init__(self, *, public_access=None, forbidden=False, corrupt_download=False):
        self.objects: dict[str, bytes] = {}
        self.calls: list[tuple[str, dict]] = []
        self.public_access = public_access
        self.forbidden = forbidden
        self.corrupt_download = corrupt_download
        self.container = None

    def get_container_client(self, container):
        self.container = container
        return self

    def get_container_properties(self):
        if self.forbidden:
            raise HttpResponseError(message="Forbidden", response=Mock(status_code=403))
        return {"public_access": self.public_access}

    def get_blob_client(self, name):
        return MemoryBlob(self, name)


class MemoryBlob:
    def __init__(self, store, name):
        self.store = store
        self.name = name

    def upload_blob(self, data, **kwargs):
        if self.store.forbidden:
            raise HttpResponseError(message="Forbidden", response=Mock(status_code=403))
        if self.name in self.store.objects:
            raise ResourceExistsError(message="Blob already exists")
        assert kwargs["overwrite"] is False
        assert kwargs["if_none_match"] == "*"
        if hasattr(data, "read"):
            assert kwargs["max_concurrency"] == 1
            chunks = []
            while chunk := data.read(64 * 1024):
                chunks.append(chunk)
            content = b"".join(chunks)
            assert len(content) == kwargs["length"]
        else:
            content = data
        self.store.objects[self.name] = content
        self.store.calls.append((self.name, kwargs))
        return {"etag": hashlib.md5(content, usedforsecurity=False).hexdigest()}

    def download_blob(self, *, etag, match_condition, max_concurrency):
        assert match_condition == MatchConditions.IfNotModified
        assert max_concurrency == 1
        assert etag == hashlib.md5(
            self.store.objects[self.name], usedforsecurity=False
        ).hexdigest()
        content = self.store.objects[self.name]
        if self.store.corrupt_download:
            content += b"tampered"
        return Mock(chunks=lambda: (content[index:index + 11] for index in range(0, len(content), 11)))


def _validated_run(tmp_path: Path) -> Path:
    return derived_run(tmp_path)


def _stage_blob(run: Path, store: MemoryBlobStore):
    return blob_stage.stage_validated_run(
        run, account_url=ACCOUNT, container=CONTAINER, service_client=store,
        spec_path=run.parent / "spec.json",
    )


def test_stage_streams_named_artifacts_and_writes_receipt_last(tmp_path: Path, monkeypatch) -> None:
    run = _validated_run(tmp_path)
    store = MemoryBlobStore()
    monkeypatch.setattr(blob_stage, "_CHUNK_SIZE", 17)
    monkeypatch.setattr(blob_stage, "uuid4", lambda: Mock(hex="attempt"))
    rows_path = run / "canonical-rows.ndjson"
    original_read_bytes = Path.read_bytes

    def no_materialized_rows(path):
        if path == rows_path:
            raise AssertionError("Canonical rows must be streamed")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", no_materialized_rows)
    result = _stage_blob(run, store)
    prefix = "staging/test-snapshot/attempt/"
    assert result["status"] == "StagedOnly"
    assert store.container == CONTAINER
    names = (
        "canonical-rows.ndjson", "stage-manifest.json", "validation.json", EXTRACT_NAME, REPORT_NAME
    )
    assert list(store.objects) == [prefix + name for name in (*names, "receipt.json")]
    for name in names:
        with (run / name).open("rb") as handle:
            data = handle.read()
        assert store.objects[prefix + name] == data
        assert store.calls[names.index(name)][1]["metadata"]["sha256"] == hashlib.sha256(
            data
        ).hexdigest()
        assert json.loads(store.objects[prefix + "receipt.json"])["artifacts"][name]["etag"]
    receipt = json.loads(store.objects[prefix + "receipt.json"])
    extract = json.loads((run / EXTRACT_NAME).read_text(encoding="utf-8"))
    assert receipt["extractDigest"] == extract_digest(extract)
    assert receipt["extractSnapshotId"] == "test-snapshot-extract"
    assert receipt["rowCount"] == json.loads((run / "validation.json").read_text())["rowCount"]
    assert receipt["baselineSnapshotId"] is None
    assert "approval.json" not in store.objects
    assert all("current.json" not in name for name in store.objects)


def test_tampered_rows_or_manifest_never_upload(tmp_path: Path) -> None:
    run = _validated_run(tmp_path)
    rows = run / "canonical-rows.ndjson"
    rows.write_bytes(rows.read_bytes() + b"{}\n")
    store = MemoryBlobStore()
    with pytest.raises(PublicationError, match="Canonical rows changed"):
        _stage_blob(run, store)
    assert not store.objects

    run = _validated_run(tmp_path / "second")
    manifest = run / "stage-manifest.json"
    value = json.loads(manifest.read_text())
    value["capturedAt"] = "2028-01-01"
    manifest.write_text(canonical_json(value), encoding="utf-8")
    with pytest.raises(PublicationError, match="does not bind"):
        _stage_blob(run, store)
    assert not store.objects


@pytest.mark.parametrize(
    "tamper",
    [
        lambda extract, report: extract["rates"].update({"aws.block.capacity.gb_month": "0.000001"}),
        lambda extract, report: extract["manifest"].update(sourceContentHash="0" * 64),
        lambda extract, report: extract["manifest"].update(validationStatus="Published"),
        lambda extract, report: extract["manifest"].update(publishingHuman="someone"),
        lambda extract, report: report.update(stageManifestDigest="0" * 64),
        lambda extract, report: report.update(sourceSnapshotId="other-snapshot"),
    ],
)
def test_unbound_rate_extract_never_uploads(tmp_path: Path, tamper) -> None:
    run = _validated_run(tmp_path)
    extract = json.loads((run / EXTRACT_NAME).read_text(encoding="utf-8"))
    report = json.loads((run / REPORT_NAME).read_text(encoding="utf-8"))
    tamper(extract, report)
    (run / EXTRACT_NAME).write_text(json.dumps(extract), encoding="utf-8")
    (run / REPORT_NAME).write_text(json.dumps(report), encoding="utf-8")
    store = MemoryBlobStore()
    with pytest.raises(PublicationError, match="Rate extract does not bind"):
        _stage_blob(run, store)
    assert not store.objects


def test_extract_and_report_rewritten_together_never_upload(tmp_path: Path) -> None:
    run = _validated_run(tmp_path)
    extract = json.loads((run / EXTRACT_NAME).read_text(encoding="utf-8"))
    report = json.loads((run / REPORT_NAME).read_text(encoding="utf-8"))
    extract["rates"]["aws.block.capacity.gb_month"] = "0.000001"
    report["extractDigest"] = extract_digest(extract)
    (run / EXTRACT_NAME).write_text(json.dumps(extract), encoding="utf-8")
    (run / REPORT_NAME).write_text(json.dumps(report), encoding="utf-8")
    store = MemoryBlobStore()
    with pytest.raises(PublicationError, match="fresh derivation"):
        _stage_blob(run, store)
    assert not store.objects


def test_stage_rederives_with_the_same_baseline_diff(tmp_path: Path) -> None:
    run = _validated_run(tmp_path)
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"rates": {"aws.block.capacity.gb_month": "1.000000"}}))
    from src.harvester.derive import derive_run

    derive_run(run, spec_path=tmp_path / "spec.json", previous_extract=baseline)
    store = MemoryBlobStore()
    with pytest.raises(PublicationError, match="fresh derivation"):
        _stage_blob(run, store)
    result = blob_stage.stage_validated_run(
        run, account_url=ACCOUNT, container=CONTAINER, service_client=store,
        spec_path=tmp_path / "spec.json", previous_extract=baseline,
    )
    assert result["status"] == "StagedOnly"


def test_missing_rate_extract_never_uploads(tmp_path: Path) -> None:
    run = _validated_run(tmp_path)
    (run / EXTRACT_NAME).unlink()
    store = MemoryBlobStore()
    with pytest.raises(PublicationError, match="Cannot read required artifact"):
        _stage_blob(run, store)
    assert not store.objects


def test_failed_validation_and_public_container_fail_closed(tmp_path: Path) -> None:
    run = _validated_run(tmp_path)
    validation_path = run / "validation.json"
    validation = json.loads(validation_path.read_text())
    validation["validationStatus"] = "Failed"
    validation_path.write_text(canonical_json(validation), encoding="utf-8")
    store = MemoryBlobStore()
    with pytest.raises(PublicationError, match="Validated"):
        _stage_blob(run, store)
    assert not store.objects
    validation_path.write_text(canonical_json({**validation, "validationStatus": "Validated"}))
    with pytest.raises(PublicationError, match="private"):
        _stage_blob(run, MemoryBlobStore(public_access="blob"))


def test_retry_uses_new_attempt_without_overwriting(tmp_path: Path, monkeypatch) -> None:
    run = _validated_run(tmp_path)
    store = MemoryBlobStore()
    attempts = iter(("first", "second"))
    monkeypatch.setattr(blob_stage, "uuid4", lambda: Mock(hex=next(attempts)))
    store.objects["staging/test-snapshot/first/canonical-rows.ndjson"] = b"existing"
    with pytest.raises(PublicationError, match="already exist"):
        _stage_blob(run, store)
    result = _stage_blob(run, store)
    assert result["prefix"] == "staging/test-snapshot/second/"
    assert store.objects["staging/test-snapshot/first/canonical-rows.ndjson"] == b"existing"
    assert "staging/test-snapshot/first/receipt.json" not in store.objects


def test_remote_corruption_never_creates_receipt(tmp_path: Path) -> None:
    store = MemoryBlobStore(corrupt_download=True)
    with pytest.raises(PublicationError, match="remote verification"):
        _stage_blob(_validated_run(tmp_path), store)
    assert not any(name.endswith("receipt.json") for name in store.objects)


def test_missing_blob_role_is_explicit_failure(tmp_path: Path) -> None:
    run = _validated_run(tmp_path)
    store = MemoryBlobStore(forbidden=True)
    with pytest.raises(PublicationError, match="Forbidden"):
        _stage_blob(run, store)
    assert not store.objects


@pytest.mark.parametrize(
    "account", ["http://testaccount.blob.core.windows.net", "https://evil.example",
                "https://testaccount.blob.core.windows.net/?sig=secret"]
)
def test_rejects_untrusted_account_endpoint(tmp_path: Path, account: str) -> None:
    store = MemoryBlobStore()
    with pytest.raises(PublicationError, match="account URL"):
        blob_stage.stage_validated_run(
            tmp_path, account_url=account, container=CONTAINER, service_client=store
        )
    assert store.container is None


@pytest.mark.parametrize("container", ["publication-control", "published-pricebooks"])
def test_stage_rejects_publication_containers(tmp_path: Path, container: str) -> None:
    store = MemoryBlobStore()
    with pytest.raises(PublicationError, match="publication containers"):
        blob_stage.stage_validated_run(
            tmp_path, account_url=ACCOUNT, container=container, service_client=store
        )
    assert store.container is None


def test_explicit_credential_selection(tmp_path: Path, monkeypatch) -> None:
    run = _validated_run(tmp_path)
    store = MemoryBlobStore()
    local = Mock()
    managed = Mock()
    monkeypatch.setattr(blob_stage, "DefaultAzureCredential", local)
    monkeypatch.setattr(blob_stage, "ManagedIdentityCredential", managed)

    class Service:
        def __init__(self, account_url, *, credential):
            assert account_url == ACCOUNT
            self.credential = credential

        def __enter__(self):
            return store

        def __exit__(self, *args):
            return None

    monkeypatch.setattr(blob_stage, "BlobServiceClient", Service)
    local_result = blob_stage.stage_validated_run(
        run, account_url=ACCOUNT, container=CONTAINER, credential_mode="local",
        spec_path=run.parent / "spec.json",
    )
    local.assert_called_once_with()
    managed.assert_not_called()
    managed_result = blob_stage.stage_validated_run(
        run, account_url=ACCOUNT, container=CONTAINER,
        managed_identity_client_id="identity-id", spec_path=run.parent / "spec.json",
    )
    assert managed_result["prefix"] != local_result["prefix"]
    managed.assert_called_once_with(client_id="identity-id")
