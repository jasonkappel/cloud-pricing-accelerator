from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from azure.core import MatchConditions

from src.harvester.core import PublicationError, ValidationError, canonical_json
from src.harvester.published_admission import admit_published_blob
from src.harvester.snapshot import (
    _verify_approval_record,
    approve_snapshot,
    publish_snapshot,
    validate_snapshot,
)
from src.harvester.tests.test_harvester import APPROVAL_KEY, _approval_record, _approve, _stage


class Blob:
    def __init__(self, objects: dict[str, bytes], name: str, mutate_pointer: bool = False):
        self.objects = objects
        self.name = name
        self.mutate_pointer = mutate_pointer
        self.properties_reads = 0

    def get_blob_properties(self):
        self.properties_reads += 1
        if self.mutate_pointer and self.properties_reads > 1:
            return Mock(etag='"changed"', size=len(self.objects[self.name]))
        return Mock(etag='"original"', size=len(self.objects[self.name]))

    def download_blob(self, *, offset, length, etag, match_condition, max_concurrency, decompress):
        assert etag == '"original"'
        assert match_condition == MatchConditions.IfNotModified
        assert max_concurrency == 1
        assert decompress is False
        assert 0 <= length <= 4 * 1024 * 1024
        content = self.objects[self.name][offset:offset + length]
        return Mock(readall=lambda: content)


class Store:
    def __init__(self, pointer: dict, artifact: bytes, *, mutate_pointer: bool = False):
        self.objects = {
            "publication-control/current.json": canonical_json(pointer).encode(),
            f"published-pricebooks/{pointer['artifact']}": artifact,
        }
        self.mutate_pointer = mutate_pointer
        self.requested: list[str] = []

    def get_container_client(self, name):
        self.requested.append(name)
        return Container(self, name)

    def _pointer_blob(self, blob_name):
        if not hasattr(self, "_pointer"):
            self._pointer = Blob(
                self.objects, f"publication-control/{blob_name}",
                mutate_pointer=self.mutate_pointer,
            )
        return self._pointer


class Container:
    def __init__(self, store: Store, name: str):
        self.store = store
        self.name = name

    def get_blob_client(self, name: str):
        if self.name == "publication-control":
            return self.store._pointer_blob(name)
        return Blob(self.store.objects, f"{self.name}/{name}")


def _published(tmp_path: Path, *, mutable_pilot: bool = False):
    run = tmp_path / "run"
    _stage(run, "published-fixture")
    validation = validate_snapshot(run, previous_artifact=None, bootstrap=True)
    if mutable_pilot:
        sku_map = tmp_path / "pilot-skumap.json"
        sku_map.write_text('{"version":1}\n', encoding="utf-8")
        record = _approval_record(validation, sku_map)
        record.update(evidencePolicy="MutablePilot", nonProduction=True)
        record["signature"] = hmac.new(
            APPROVAL_KEY.encode(),
            canonical_json({key: value for key, value in record.items() if key != "signature"}).encode(),
            hashlib.sha256,
        ).hexdigest()
        approval_path = tmp_path / "pilot-approval.json"
        approval_path.write_text(canonical_json(record), encoding="utf-8")
        approve_snapshot(
            run, approval_record_path=approval_path, sku_map_path=sku_map,
            approval_key=APPROVAL_KEY,
        )
    else:
        _approve(run, tmp_path, validation)
    published = tmp_path / "published"
    pointer = publish_snapshot(
        run, store_dir=published, expected_pointer_hash=None,
        approval_key=APPROVAL_KEY,
    )
    return pointer, (published / pointer["artifact"]).read_bytes()


def _trusted_approval(record: dict) -> bool:
    _verify_approval_record(
        record,
        validation={
            field: record[field]
            for field in ("snapshotId", "contentHash", "coverageMatrixDigest", "scope", "stageManifestDigest")
        },
        sku_map_digest=record["skuMapDigest"],
        approval_key=APPROVAL_KEY,
    )
    return True


def _admit(tmp_path: Path, store: Store, **kwargs):
    return admit_published_blob(
        store,
        scratch_dir=tmp_path,
        verify_approval=_trusted_approval,
        verify_immutability=lambda container: container == "published-pricebooks",
        **kwargs,
    )


def test_published_admission_checks_full_artifact_and_pointer(tmp_path: Path) -> None:
    pointer, artifact = _published(tmp_path)
    store = Store(pointer, artifact)
    before = set(tmp_path.iterdir())
    result = _admit(tmp_path, store)
    assert result["snapshotId"] == pointer["snapshotId"]
    assert result["contentHash"] == pointer["contentHash"]
    assert result["rowCount"] > 0
    assert store.requested == ["publication-control", "published-pricebooks"]
    assert set(tmp_path.iterdir()) == before


def test_missing_authority_fails_before_access(tmp_path: Path) -> None:
    store = Mock()
    with pytest.raises(PublicationError, match="trusted approval"):
        admit_published_blob(
            store, scratch_dir=tmp_path,
            verify_approval=None, verify_immutability=lambda _: True,
        )
    store.get_container_client.assert_not_called()
    with pytest.raises(PublicationError, match="no verified WORM"):
        admit_published_blob(
            store, scratch_dir=tmp_path,
            verify_approval=_trusted_approval, verify_immutability=None,
        )
    with pytest.raises(PublicationError, match="no verified WORM policy"):
        admit_published_blob(
            store, scratch_dir=tmp_path,
            verify_approval=_trusted_approval, verify_immutability=lambda _: False,
        )
    store.get_container_client.assert_not_called()


def test_tampered_artifact_fails_closed(tmp_path: Path) -> None:
    pointer, artifact = _published(tmp_path)
    with pytest.raises(ValidationError, match="Normalized price row is missing"):
        _admit(tmp_path, Store(pointer, artifact + b"{}\n"))


def test_swapped_pointer_and_pointer_race_fail_closed(tmp_path: Path) -> None:
    pointer, artifact = _published(tmp_path)
    with pytest.raises(PublicationError, match="does not bind"):
        _admit(tmp_path, Store({**pointer, "contentHash": "0" * 64}, artifact))
    with pytest.raises(PublicationError, match="changed during admission"):
        _admit(tmp_path, Store(pointer, artifact, mutate_pointer=True))


def test_bounded_downloads_and_worm_failure(tmp_path: Path, monkeypatch) -> None:
    pointer, artifact = _published(tmp_path)
    store = Store(pointer, artifact)
    original_download = Blob.download_blob

    def forbid_oversized_artifact(self, **kwargs):
        if self.name.startswith("published-pricebooks/"):
            raise AssertionError("Oversized artifact must not be downloaded.")
        return original_download(self, **kwargs)

    monkeypatch.setattr(Blob, "download_blob", forbid_oversized_artifact)
    before = set(tmp_path.iterdir())
    with pytest.raises(PublicationError, match="size limit"):
        _admit(tmp_path, store, max_artifact_bytes=len(artifact) - 1)
    assert set(tmp_path.iterdir()) == before
    store.objects["publication-control/current.json"] = b" " * 8193
    with pytest.raises(PublicationError, match="pointer exceeds"):
        _admit(tmp_path, store)
    with pytest.raises(PublicationError, match="WORM not configured"):
        admit_published_blob(
            store,
            scratch_dir=tmp_path,
            verify_approval=_trusted_approval,
            verify_immutability=lambda _: (_ for _ in ()).throw(
                PublicationError("WORM not configured")
            ),
        )


def test_unverified_approval_cannot_admit(tmp_path: Path) -> None:
    pointer, artifact = _published(tmp_path)
    with pytest.raises(PublicationError, match="approval was not verified"):
        admit_published_blob(
            Store(pointer, artifact),
            scratch_dir=tmp_path,
            verify_approval=lambda _: False,
            verify_immutability=lambda _: True,
        )


def test_one_person_may_hold_both_sign_off_roles(tmp_path: Path) -> None:
    # A two-person maker-checker is deferred to v2; the signed record still names both roles.
    run = tmp_path / "run"
    _stage(run, "published-fixture")
    validation = validate_snapshot(run, previous_artifact=None, bootstrap=True)
    sku_map = tmp_path / "skumap.json"
    sku_map.write_text("{}\n", encoding="utf-8")
    record = _approval_record(validation, sku_map, approver_id="same-id", reviewer_id="same-id")
    approval_path = tmp_path / "approval.json"
    approval_path.write_text(canonical_json(record), encoding="utf-8")
    approve_snapshot(
        run, approval_record_path=approval_path, sku_map_path=sku_map, approval_key=APPROVAL_KEY,
    )
    published = tmp_path / "published"
    pointer = publish_snapshot(
        run, store_dir=published, expected_pointer_hash=None, approval_key=APPROVAL_KEY,
    )
    artifact = (published / pointer["artifact"]).read_bytes()
    admitted = admit_published_blob(
        Store(pointer, artifact),
        scratch_dir=tmp_path,
        verify_approval=_trusted_approval,
        verify_immutability=lambda _: True,
    )
    manifest = json.loads(artifact.split(b"\n", 1)[0])["manifest"]
    assert manifest["skuMapReviewerId"] == manifest["publishingHumanId"] == "same-id"
    assert admitted is not None


def test_staged_status_and_unexpected_artifact_name_are_rejected(tmp_path: Path) -> None:
    pointer, artifact = _published(tmp_path)
    lines = artifact.splitlines(keepends=True)
    header = json.loads(lines[0])
    header["manifest"]["validationStatus"] = "Validated"
    staged = f"{canonical_json(header)}\n".encode() + b"".join(lines[1:])
    with pytest.raises(ValidationError, match="not Published"):
        _admit(tmp_path, Store(pointer, staged))
    with pytest.raises(PublicationError, match="artifact name is invalid"):
        _admit(tmp_path, Store({**pointer, "artifact": "other.pricebook.ndjson"}, artifact))


def test_explicit_mutable_pilot_requires_label_and_storage_safeguards(tmp_path: Path) -> None:
    pointer, artifact = _published(tmp_path, mutable_pilot=True)
    store = Store(pointer, artifact)
    admitted = admit_published_blob(
        store,
        scratch_dir=tmp_path,
        verify_approval=_trusted_approval,
        verify_immutability=None,
        evidence_policy="mutable-pilot",
        verify_pilot_storage=lambda container: container == "published-pricebooks",
        verify_freshness=lambda snapshot: snapshot["snapshotId"] == pointer["snapshotId"],
    )
    assert admitted["evidencePolicy"] == "MutablePilot"
    with pytest.raises(PublicationError, match="cannot be admitted as WORM"):
        _admit(tmp_path, Store(pointer, artifact))
    with pytest.raises(PublicationError, match="verified versioning and soft delete"):
        admit_published_blob(
            Mock(),
            scratch_dir=tmp_path,
            verify_approval=_trusted_approval,
            verify_immutability=None,
            evidence_policy="mutable-pilot",
            verify_pilot_storage=lambda _: False,
            verify_freshness=lambda _: True,
        )
    with pytest.raises(PublicationError, match="independent freshness verifier"):
        admit_published_blob(
            Mock(), scratch_dir=tmp_path, verify_approval=_trusted_approval,
            verify_immutability=None, evidence_policy="mutable-pilot",
            verify_pilot_storage=lambda _: True,
        )
    with pytest.raises(PublicationError, match="independent freshness check"):
        admit_published_blob(
            Store(pointer, artifact), scratch_dir=tmp_path,
            verify_approval=_trusted_approval, verify_immutability=None,
            evidence_policy="mutable-pilot", verify_pilot_storage=lambda _: True,
            verify_freshness=lambda _: False,
        )


def test_mutable_pilot_cannot_admit_unlabeled_or_unsigned_artifact(tmp_path: Path) -> None:
    pointer, artifact = _published(tmp_path)
    with pytest.raises(PublicationError, match="non-production label"):
        admit_published_blob(
            Store(pointer, artifact),
            scratch_dir=tmp_path,
            verify_approval=_trusted_approval,
            verify_immutability=None,
            evidence_policy="mutable-pilot",
            verify_pilot_storage=lambda _: True,
            verify_freshness=lambda _: True,
        )
    with pytest.raises(PublicationError, match="approval was not verified"):
        admit_published_blob(
            Store(pointer, artifact),
            scratch_dir=tmp_path,
            verify_approval=lambda _: False,
            verify_immutability=None,
            evidence_policy="mutable-pilot",
            verify_pilot_storage=lambda _: True,
            verify_freshness=lambda _: True,
        )


def test_mutable_pilot_label_is_bound_to_signed_approval(tmp_path: Path) -> None:
    pointer, artifact = _published(tmp_path, mutable_pilot=True)
    staged_manifest = json.loads((tmp_path / "run" / "stage-manifest.json").read_text())
    assert "evidencePolicy" not in staged_manifest
    header, rows = artifact.split(b"\n", 1)
    altered_header = json.loads(header)
    altered_header["manifest"].update(evidencePolicy="WORM", nonProduction=False)
    tampered = canonical_json(altered_header).encode() + b"\n" + rows
    with pytest.raises(PublicationError, match="signature is invalid"):
        _admit(tmp_path, Store(pointer, tampered))
