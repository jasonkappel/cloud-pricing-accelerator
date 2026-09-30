import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient

from app import create_app
from app.approvals import (
    ApprovalConfigurationError,
    HmacApprovalSigner,
    KeyVaultApprovalSigner,
    StorageUnavailable,
    approval_settings,
    digest_of,
    reset_approval_backend,
    sign_record,
)
from app.pricing import pricing_engine

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from harvester.snapshot import _verify_approval_record  # noqa: E402

HMAC_KEY = "local-approval-test-key-0123456789abcdef"
SNAPSHOT = "trust-20270101a"
RUN = "0123456789abcdef0123456789abcdef"
REVIEWER = ("11111111-1111-1111-1111-111111111111", "Sample Reviewer", "SkuMapReviewer")
APPROVER = ("22222222-2222-2222-2222-222222222222", "Sample Approver", "SnapshotApprover")
ESTIMATOR = ("33333333-3333-3333-3333-333333333333", "Test Estimator", "Estimator")


@pytest.fixture(autouse=True)
def reset_backend():
    reset_approval_backend()
    yield
    reset_approval_backend()


@pytest.fixture
def local_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("PRICEBOOK_APPROVAL_MODE", "local")
    monkeypatch.setenv("PRICEBOOK_LOCAL_ROOT", str(tmp_path))
    monkeypatch.setenv("APPROVAL_HMAC_KEY", HMAC_KEY)
    return tmp_path


def as_user(monkeypatch: pytest.MonkeyPatch, oid: str, name: str, roles: str) -> None:
    monkeypatch.setenv("LOCAL_PRINCIPAL_OID", oid)
    monkeypatch.setenv("LOCAL_PRINCIPAL_NAME", name)
    monkeypatch.setenv("LOCAL_PRINCIPAL_ROLES", roles)


def _etag(data: bytes) -> str:
    return f'"{hashlib.sha256(data).hexdigest()[:32]}"'


def stage_run(
    root: Path,
    *,
    snapshot_id: str = SNAPSHOT,
    run_id: str = RUN,
    mutate: Callable[[dict[str, Any]], None] | None = None,
    rows: bytes | None = None,
) -> dict[str, Any]:
    rows = rows or b'{"rowId":"a"}\n{"rowId":"b"}\n'
    row_count = sum(1 for line in rows.splitlines() if line.strip())
    content_hash = hashlib.sha256(rows).hexdigest()
    scope = {"azureRegion": "eastus2", "awsRegion": "us-east-1"}
    manifest = {
        "snapshotId": snapshot_id,
        "capturedAt": "2027-01-01T00:00:00+00:00",
        "contentHash": content_hash,
        "rowCount": row_count,
        "scope": scope,
        "coverageMatrixDigest": "d" * 64,
        "validationStatus": "Validated",
        "publishingHuman": None,
        "skuMapReviewer": None,
        "skuMapDigest": None,
    }
    extract = {
        "manifest": {
            "snapshotId": f"extract-{snapshot_id}",
            "sourceSnapshotId": snapshot_id,
            "sourceContentHash": content_hash,
            "publishingHuman": None,
        },
        "rates": {"aws.ec2.m7i.large": "0.1008"},
    }
    files: dict[str, Any] = {
        "manifest": manifest,
        "validation": {
            "snapshotId": snapshot_id,
            "validatedAt": "2027-01-01T00:10:00+00:00",
            "validationStatus": "Validated",
            "failures": [],
            "coverage": {"checks": {"aws-ec2": True, "azure-vm": True}, "missing": []},
            "coverageMatrixDigest": "d" * 64,
            "scope": scope,
            "comparison": {"bootstrap": True, "added": 0, "retired": 0, "changed": 0,
                           "materialRateChangeCount": 0},
            "baselinePointerHash": None,
            "baselineSnapshotId": None,
            "contentHash": content_hash,
            "rowCount": row_count,
        },
        "extract": extract,
        "report": {
            "specVersion": "1",
            "sourceSnapshotId": snapshot_id,
            "sourceContentHash": content_hash,
            "extractSnapshotId": f"extract-{snapshot_id}",
            "rateCount": 1,
            "assumedRateCount": 0,
            "diff": {
                "baselineSnapshotId": "baseline-1",
                "baselineDigest": "e" * 64,
                "addedCount": 0,
                "removedCount": 0,
                "changedCount": 1,
                "unchangedCount": 0,
                "rates": [{
                    "rateKey": "aws.ec2.m7i.large", "assumed": False, "old": "0.0960",
                    "new": "0.1008", "change": "0.0048", "percentChange": "5.0000",
                    "status": "changed",
                }],
            },
        },
        "rows": rows,
    }
    files["validation"]["stageManifestDigest"] = digest_of(manifest)
    files["report"]["extractDigest"] = digest_of(extract)
    if mutate:
        mutate(files)
    blobs = {
        "stage-manifest.json": files["manifest"],
        "validation.json": files["validation"],
        "rate-extract.json": files["extract"],
        "rate-extract-report.json": files["report"],
    }
    directory = root / "staged-runs" / "staging" / snapshot_id / run_id
    directory.mkdir(parents=True, exist_ok=True)
    artifacts = {}
    for name, value in blobs.items():
        data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
        (directory / name).write_bytes(data)
        artifacts[name] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
                           "etag": _etag(data)}
    (directory / "canonical-rows.ndjson").write_bytes(files["rows"])
    artifacts["canonical-rows.ndjson"] = {
        "sha256": hashlib.sha256(files["rows"]).hexdigest(), "bytes": len(files["rows"]),
        "etag": _etag(files["rows"]),
    }
    receipt = {
        "snapshotId": snapshot_id,
        "capturedAt": manifest["capturedAt"],
        "validatedAt": files["validation"]["validatedAt"],
        "contentHash": content_hash,
        "rowCount": row_count,
        "baselineSnapshotId": None,
        "baselinePointerHash": None,
        "scope": scope,
        "extractSnapshotId": f"extract-{snapshot_id}",
        "extractDigest": digest_of(files["extract"]),
        "artifacts": artifacts,
        "status": "StagedOnly",
    }
    if "receipt" in files:
        receipt.update(files["receipt"])
    (directory / "receipt.json").write_text(json.dumps(receipt) + "\n")
    return files


def _url(suffix: str = "") -> str:
    return f"/api/price-book/staged/{SNAPSHOT}/{RUN}{suffix}"


def _decision(client: TestClient, **overrides: Any) -> dict[str, Any]:
    detail = client.get(_url()).json()
    body = {
        "stageManifestDigest": detail["stageManifestDigest"],
        "extractDigest": detail["extractDigest"],
        "evidenceDigest": detail["evidenceDigest"],
        "skuMapDigest": detail["skuMapDigest"],
        "attested": True,
    }
    body.update(overrides)
    return body


def _review(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    as_user(monkeypatch, *REVIEWER)
    return client.post(_url("/skumap-review"), json=_decision(client))


def test_off_mode_reports_not_configured_and_refuses_actions(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PRICEBOOK_APPROVAL_MODE", raising=False)
    assert client.get("/api/price-book/staged").json() == {"configured": False, "runs": []}
    assert client.get(_url()).status_code == 503
    as_user(monkeypatch, *REVIEWER)
    body = {"stageManifestDigest": "a" * 64, "extractDigest": "b" * 64,
            "evidenceDigest": "f" * 64, "skuMapDigest": "c" * 64, "attested": True}
    assert client.post(_url("/skumap-review"), json=body).status_code == 503


def test_viewing_needs_an_app_role(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    as_user(monkeypatch, "44444444-4444-4444-4444-444444444444", "No Role", "")
    assert client.get("/api/price-book/staged").status_code == 403


def test_review_then_distinct_approval_produces_a_harvester_verifiable_record(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = stage_run(local_root)
    listing = client.get("/api/price-book/staged").json()
    assert listing["configured"] is True
    assert [(run["snapshotId"], run["state"]) for run in listing["runs"]] == [
        (SNAPSHOT, "AwaitingSkuMapReview")
    ]
    detail = client.get(_url()).json()
    assert detail["skuMapDigest"] == pricing_engine.skumap_digest
    assert detail["extract"]["diff"]["rates"][0]["percentChange"] == "5.0000"
    assert {"check": "aws-ec2", "covered": True} in detail["validation"]["coverage"]
    assert detail["actions"] == {
        "canReview": False, "canApprove": False, "canPublish": False,
        "waiting": "Waiting for a SkuMapReviewer.",
    }

    as_user(monkeypatch, *APPROVER)
    early = client.post(_url("/approval"), json=_decision(client))
    assert early.status_code == 409
    assert "review" in early.json()["detail"]

    reviewed = _review(client, monkeypatch)
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["state"] == "AwaitingApproval"
    assert reviewed.json()["review"]["reviewerDisplayName"] == "Sample Reviewer"
    assert client.post(_url("/skumap-review"), json=_decision(client)).status_code == 409

    as_user(monkeypatch, *APPROVER)
    assert client.get(_url()).json()["actions"]["canApprove"] is True
    approved = client.post(_url("/approval"), json=_decision(client))
    assert approved.status_code == 200, approved.text
    assert approved.json()["state"] == "Approved"
    assert approved.json()["approval"]["nonProduction"] is True
    assert client.post(_url("/approval"), json=_decision(client)).status_code == 409

    stored = json.loads(
        (local_root / "publication-control" / "approvals" / SNAPSHOT / "approval.json").read_text()
    )
    assert stored["runId"] == RUN
    record = stored["record"]
    assert record["approverId"] == APPROVER[0]
    assert record["skuMapReviewerId"] == REVIEWER[0]
    assert record["extractDigest"] == digest_of(files["extract"])
    assert record["keyId"] == "local-hmac"
    assert record["runId"] == RUN
    assert record["evidenceDigest"] == client.get(_url()).json()["evidenceDigest"]
    _verify_approval_record(
        record,
        validation=files["validation"],
        sku_map_digest=pricing_engine.skumap_digest,
        approval_key=HMAC_KEY,
    )


def test_roles_are_enforced_per_action(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_run(local_root)
    as_user(monkeypatch, *ESTIMATOR)
    assert client.get(_url()).status_code == 200
    assert client.post(_url("/skumap-review"), json=_decision(client)).status_code == 403
    assert client.post(_url("/approval"), json=_decision(client)).status_code == 403
    as_user(monkeypatch, *APPROVER)
    assert client.post(_url("/skumap-review"), json=_decision(client)).status_code == 403


@pytest.mark.parametrize(
    "overrides, code",
    [
        ({"stageManifestDigest": "0" * 64}, 409),
        ({"extractDigest": "0" * 64}, 409),
        ({"evidenceDigest": "0" * 64}, 409),
        ({"skuMapDigest": "0" * 64}, 409),
        ({"attested": False}, 422),
        ({"stageManifestDigest": "NOT-HEX"}, 422),
        ({"approverId": "someone-else"}, 422),
    ],
)
def test_decisions_confirm_what_the_person_saw(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch,
    overrides: dict, code: int,
) -> None:
    stage_run(local_root)
    as_user(monkeypatch, *REVIEWER)
    response = client.post(_url("/skumap-review"), json=_decision(client, **overrides))
    assert response.status_code == code
    assert not (local_root / "publication-control").exists()


def _tamper_validation(root: Path) -> None:
    path = root / "staged-runs" / "staging" / SNAPSHOT / RUN / "validation.json"
    path.write_text(path.read_text().replace('"failures": []', '"failures": [ ]'))


def _tamper_rows(root: Path) -> None:
    path = root / "staged-runs" / "staging" / SNAPSHOT / RUN / "canonical-rows.ndjson"
    path.write_bytes(b'{"rowId":"z"}\n')


def _remove_rows(root: Path) -> None:
    (root / "staged-runs" / "staging" / SNAPSHOT / RUN / "canonical-rows.ndjson").unlink()


@pytest.mark.parametrize(
    "tamper, problem",
    [
        (_tamper_validation, "changed after staging"),
        (_tamper_rows, "canonical-rows.ndjson does not match"),
        (_remove_rows, "missing"),
    ],
)
def test_changed_staged_files_block_the_run(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch, tamper, problem: str
) -> None:
    stage_run(local_root)
    as_user(monkeypatch, *REVIEWER)
    body = _decision(client)
    tamper(local_root)
    detail = client.get(_url()).json()
    assert detail["state"] == "Blocked"
    assert any(problem in item for item in detail["problems"])
    assert detail["actions"]["canReview"] is False
    assert client.post(_url("/skumap-review"), json=body).status_code == 409


def _failed_validation(files: dict) -> None:
    files["validation"]["failures"] = ["Required catalog coverage is missing: azure-vm."]


def _missing_coverage(files: dict) -> None:
    files["validation"]["coverage"]["missing"] = ["azure-vm"]


def _foreign_extract(files: dict) -> None:
    files["extract"]["manifest"]["sourceSnapshotId"] = "some-other-snapshot"
    files["report"]["extractDigest"] = digest_of(files["extract"])


def _receipt_digest_mismatch(files: dict) -> None:
    files["receipt"] = {"extractDigest": "f" * 64}


def _preapproved_manifest(files: dict) -> None:
    files["manifest"]["publishingHuman"] = "Mallory"
    files["validation"]["stageManifestDigest"] = digest_of(files["manifest"])


def _row_count_mismatch(files: dict) -> None:
    files["receipt"] = {"rowCount": 3}


def _rows_not_the_content_hash(files: dict) -> None:
    other = "a" * 64
    files["manifest"]["contentHash"] = other
    files["validation"]["contentHash"] = other
    files["validation"]["stageManifestDigest"] = digest_of(files["manifest"])
    files["receipt"] = {"contentHash": other}


def _missing_coverage_digest(files: dict) -> None:
    del files["validation"]["coverageMatrixDigest"]


def _malformed_rates(files: dict) -> None:
    files["report"]["diff"]["rates"] = "not a list"


def _malformed_rate_entry(files: dict) -> None:
    files["report"]["diff"]["rates"] = [{"rateKey": {"nested": True}, "assumed": False}]


def _malformed_checks(files: dict) -> None:
    files["validation"]["coverage"]["checks"] = ["aws-ec2"]


def _malformed_comparison(files: dict) -> None:
    files["validation"]["comparison"] = {"bootstrap": "yes"}


@pytest.mark.parametrize(
    "mutate, problem",
    [
        (_failed_validation, "failures"),
        (_missing_coverage, "coverage"),
        (_foreign_extract, "not derived from this staged snapshot"),
        (_receipt_digest_mismatch, "rate extract does not match"),
        (_preapproved_manifest, "already names an approver"),
        (_row_count_mismatch, "rowCount"),
        (_rows_not_the_content_hash, "do not match the snapshot content hash"),
        (_missing_coverage_digest, "missing a required digest"),
        (_malformed_rates, "rate change report is malformed"),
        (_malformed_rate_entry, "rate change report is malformed"),
        (_malformed_checks, "coverage checks are malformed"),
        (_malformed_comparison, "validation comparison is malformed"),
    ],
)
def test_inconsistent_staged_runs_are_blocked(
    client: TestClient, local_root: Path, mutate, problem: str
) -> None:
    stage_run(local_root, mutate=mutate)
    detail = client.get(_url()).json()
    assert detail["state"] == "Blocked"
    assert any(problem in item for item in detail["problems"]), detail["problems"]


def test_a_blocked_run_refuses_decisions_without_writing(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_run(local_root, mutate=_malformed_rates)
    as_user(monkeypatch, *REVIEWER)
    response = client.post(_url("/skumap-review"), json=_decision(client))
    assert response.status_code == 409
    assert not (local_root / "publication-control").exists()


def test_changed_evidence_after_viewing_is_refused(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_run(local_root)
    as_user(monkeypatch, *REVIEWER)
    body = _decision(client)
    receipt = local_root / "staged-runs" / "staging" / SNAPSHOT / RUN / "receipt.json"
    data = json.loads(receipt.read_text())
    data["validatedAt"] = "2027-01-01T00:11:00+00:00"
    receipt.write_text(json.dumps(data) + "\n")
    assert client.get(_url()).json()["evidenceDigest"] != body["evidenceDigest"]
    assert client.post(_url("/skumap-review"), json=body).status_code == 409
    assert not (local_root / "publication-control").exists()


def test_an_approval_cannot_be_moved_to_another_run(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_run(local_root)
    assert _review(client, monkeypatch).status_code == 200
    as_user(monkeypatch, *APPROVER)
    assert client.post(_url("/approval"), json=_decision(client)).status_code == 200
    other = "f" * 32
    stage_run(local_root, run_id=other)
    path = local_root / "publication-control" / "approvals" / SNAPSHOT / "approval.json"
    wrapper = json.loads(path.read_text())
    wrapper["runId"] = other
    path.write_text(json.dumps(wrapper))
    assert client.get(f"/api/price-book/staged/{SNAPSHOT}/{other}").json()["state"] == "Blocked"
    assert client.get(_url()).json()["state"] == "Blocked"


def test_forged_review_record_blocks_the_run(
    client: TestClient, local_root: Path
) -> None:
    files = stage_run(local_root)
    forged = sign_record(
        {
            "recordType": "SkuMapReview",
            "snapshotId": SNAPSHOT,
            "runId": RUN,
            "contentHash": files["validation"]["contentHash"],
            "stageManifestDigest": files["validation"]["stageManifestDigest"],
            "extractDigest": digest_of(files["extract"]),
            "skuMapDigest": pricing_engine.skumap_digest,
            "reviewerId": "attacker",
            "reviewerDisplayName": "Attacker",
            "reviewerRole": "SkuMapReviewer",
        },
        HmacApprovalSigner("a-different-key-that-is-long-enough-000"),
    )
    path = local_root / "publication-control" / "approvals" / SNAPSHOT / "runs" / RUN
    path.mkdir(parents=True)
    (path / "skumap-review.json").write_text(json.dumps(forged))
    detail = client.get(_url()).json()
    assert detail["state"] == "Blocked"
    assert any("invalid signature" in item for item in detail["problems"])


def test_skumap_change_blocks_a_reviewed_run_until_approval_only(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_run(local_root)
    assert _review(client, monkeypatch).status_code == 200
    original = pricing_engine.skumap_digest
    monkeypatch.setattr(pricing_engine, "_skumap_digest", "f" * 64)
    detail = client.get(_url()).json()
    assert detail["state"] == "Blocked"
    assert "The SkuMap changed after it was reviewed." in detail["problems"]
    monkeypatch.setattr(pricing_engine, "_skumap_digest", original)
    as_user(monkeypatch, *APPROVER)
    assert client.post(_url("/approval"), json=_decision(client)).status_code == 200
    monkeypatch.setattr(pricing_engine, "_skumap_digest", "f" * 64)
    assert client.get(_url()).json()["state"] == "Approved"


def test_second_run_of_an_approved_snapshot_is_blocked(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_run(local_root)
    assert _review(client, monkeypatch).status_code == 200
    as_user(monkeypatch, *APPROVER)
    assert client.post(_url("/approval"), json=_decision(client)).status_code == 200
    other = "f" * 32
    stage_run(local_root, run_id=other)
    detail = client.get(f"/api/price-book/staged/{SNAPSHOT}/{other}").json()
    assert detail["state"] == "Blocked"
    assert "Another staged run of this snapshot is already approved." in detail["problems"]


def test_unknown_or_malformed_run_ids(
    client: TestClient, local_root: Path
) -> None:
    assert client.get(_url()).status_code == 404
    assert client.get(f"/api/price-book/staged/{SNAPSHOT}/not-a-run").status_code == 422
    assert client.get(f"/api/price-book/staged/..%2F..%2Fx/{RUN}").status_code in (404, 422)


def test_local_mode_is_refused_when_hosted(
    local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WEBSITE_SITE_NAME", "app-api")
    with pytest.raises(ApprovalConfigurationError, match="developer machine"):
        approval_settings()


@pytest.mark.parametrize(
    "env, message",
    [
        ({"PRICEBOOK_APPROVAL_MODE": "sometimes"}, "not supported"),
        ({"PRICEBOOK_APPROVAL_MODE": "azure", "PRICEBOOK_BLOB_ENDPOINT": "http://x.blob.core.windows.net",
          "APPROVAL_SIGNING_KEY_ID": ""}, "PRICEBOOK_BLOB_ENDPOINT"),
        ({"PRICEBOOK_APPROVAL_MODE": "azure",
          "PRICEBOOK_BLOB_ENDPOINT": "https://stpbexample.blob.core.windows.net/",
          "APPROVAL_SIGNING_KEY_ID": "https://kv-example.vault.azure.net/keys/approval-signing"},
         "versioned"),
        ({"PRICEBOOK_APPROVAL_MODE": "local", "APPROVAL_HMAC_KEY": "short"}, "32 characters"),
        ({"PRICEBOOK_STAGING_CONTAINER": "same", "PRICEBOOK_CONTROL_CONTAINER": "same"}, "differ"),
    ],
)
def test_invalid_approval_settings_fail_startup(
    local_root: Path, monkeypatch: pytest.MonkeyPatch, env: dict, message: str
) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ApprovalConfigurationError, match=message):
        create_app()


def test_azure_settings_accept_a_pinned_versioned_key(monkeypatch: pytest.MonkeyPatch) -> None:
    key_id = "https://kv-example.vault.azure.net/keys/approval-signing/" + "0" * 32
    monkeypatch.setenv("PRICEBOOK_APPROVAL_MODE", "azure")
    monkeypatch.setenv("PRICEBOOK_BLOB_ENDPOINT", "https://stpbexample.blob.core.windows.net/")
    monkeypatch.setenv("APPROVAL_SIGNING_KEY_ID", key_id)
    settings = approval_settings()
    assert settings.blob_endpoint == "https://stpbexample.blob.core.windows.net"
    assert settings.key_id == key_id


class FakeCryptographyClient:
    """Stands in for Key Vault with a real RSA key, so RS256 signing and verification are exercised."""

    def __init__(self, key_id: str, reported_key_id: str | None = None) -> None:
        from cryptography.hazmat.primitives.asymmetric import rsa

        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._key_id = key_id
        self._reported = reported_key_id or key_id

    def sign(self, algorithm, digest: bytes):
        from types import SimpleNamespace

        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding, utils

        assert str(algorithm) in ("RS256", "SignatureAlgorithm.rs256")
        signature = self._key.sign(digest, padding.PKCS1v15(), utils.Prehashed(hashes.SHA256()))
        return SimpleNamespace(key_id=self._reported, signature=signature)

    def verify(self, algorithm, digest: bytes, signature: bytes):
        from types import SimpleNamespace

        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding, utils

        try:
            self._key.public_key().verify(
                signature, digest, padding.PKCS1v15(), utils.Prehashed(hashes.SHA256())
            )
        except InvalidSignature:
            return SimpleNamespace(is_valid=False)
        return SimpleNamespace(is_valid=True)


def test_key_vault_signer_signs_and_verifies_with_the_pinned_key() -> None:
    from app.approvals import record_signature_problem

    key_id = "https://kv-example.vault.azure.net/keys/approval-signing/" + "1" * 32
    signer = KeyVaultApprovalSigner(key_id, FakeCryptographyClient(key_id))
    record = sign_record({"snapshotId": SNAPSHOT, "approverId": "a"}, signer)
    assert record["algorithm"] == "RS256" and record["keyId"] == key_id
    assert record_signature_problem(record, signer) is None
    assert "invalid signature" in record_signature_problem({**record, "approverId": "b"}, signer)
    assert "approval key" in record_signature_problem({**record, "keyId": key_id[:-1] + "2"}, signer)
    assert "invalid signature" in record_signature_problem({**record, "signature": "zz"}, signer)

    wrong = KeyVaultApprovalSigner(
        key_id, FakeCryptographyClient(key_id, reported_key_id=key_id[:-1] + "2")
    )
    with pytest.raises(StorageUnavailable, match="unexpected key version"):
        wrong.sign(b"payload")


def test_an_approval_without_an_approver_blocks_the_run(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_run(local_root)
    assert _review(client, monkeypatch).status_code == 200
    as_user(monkeypatch, *APPROVER)
    assert client.post(_url("/approval"), json=_decision(client)).status_code == 200
    path = local_root / "publication-control" / "approvals" / SNAPSHOT / "approval.json"
    wrapper = json.loads(path.read_text())
    wrapper["record"] = sign_record({**wrapper["record"], "approverId": " "}, HmacApprovalSigner(HMAC_KEY))
    path.write_text(json.dumps(wrapper))
    detail = client.get(_url()).json()
    assert detail["state"] == "Blocked"
    assert any("has no approver" in item for item in detail["problems"])


def test_azure_store_maps_a_failed_write_once_precondition_to_record_exists() -> None:
    from azure.core.exceptions import ResourceModifiedError

    from app.approvals import AzurePriceBookStore, RecordExists

    class Blob:
        def upload_blob(self, *args: Any, **kwargs: Any) -> None:
            assert kwargs["if_none_match"] == "*" and kwargs["overwrite"] is False
            raise ResourceModifiedError("ConditionNotMet")

    class Container:
        def get_blob_client(self, name: str) -> Blob:
            return Blob()

    store = AzurePriceBookStore.__new__(AzurePriceBookStore)
    store._control = Container()
    with pytest.raises(RecordExists):
        store.create_control("approvals/x/approval.json", b"{}")


def test_listing_skips_a_run_removed_after_listing(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.approvals as approvals

    stage_run(local_root)
    real = approvals.load_staged_run

    def vanished(*args: Any, **kwargs: Any):
        raise approvals.StagedRunNotFound("gone")

    monkeypatch.setattr(approvals, "load_staged_run", vanished)
    listing = client.get("/api/price-book/staged")
    assert listing.status_code == 200
    assert listing.json()["runs"] == []
    monkeypatch.setattr(approvals, "load_staged_run", real)


@pytest.mark.parametrize("same_etag", [True, False])
def test_decisions_hash_the_row_bytes_not_the_blob_metadata(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch, same_etag: bool
) -> None:
    from app.approvals import BlobProperties, LocalPriceBookStore

    stage_run(local_root)
    receipt = json.loads(
        (local_root / "staged-runs" / "staging" / SNAPSHOT / RUN / "receipt.json").read_text()
    )
    entry = receipt["artifacts"]["canonical-rows.ndjson"]
    # Metadata written by the staging identity claims the approved hash while the bytes differ.
    monkeypatch.setattr(
        LocalPriceBookStore, "staged_properties",
        lambda self, name: BlobProperties(size=entry["bytes"], etag=entry["etag"], sha256=entry["sha256"]),
    )
    (local_root / "staged-runs" / "staging" / SNAPSHOT / RUN / "canonical-rows.ndjson").write_bytes(
        b'{"rowId":"x"}\n{"rowId":"y"}\n'
    )
    if same_etag:
        real_etag = LocalPriceBookStore._etag
        monkeypatch.setattr(
            LocalPriceBookStore, "_etag",
            staticmethod(lambda data: entry["etag"] if data.startswith(b'{"rowId"') else real_etag(data)),
        )
    as_user(monkeypatch, *REVIEWER)
    assert client.get(_url()).json()["state"] == "AwaitingSkuMapReview"
    response = client.post(_url("/skumap-review"), json=_decision(client))
    assert response.status_code == 409
    assert "canonical rows" in response.json()["detail"]
    assert not (local_root / "publication-control").exists()

