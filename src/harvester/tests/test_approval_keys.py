from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa, utils

from src.harvester.approval_keys import (
    RS256ApprovalVerifier,
    load_key_vault_verifier,
    parse_key_id,
)
from src.harvester.core import HarvestError, PublicationError, ValidationError, canonical_json
from src.harvester.snapshot import (
    approve_snapshot,
    publish_snapshot,
    unwrap_approval,
    validate_snapshot,
    verify_published_artifact,
)
from src.harvester.tests.test_harvester import _approval_record, _stage


KEY_ID = "https://vault.example.net/keys/approval/0123456789abcdef"
RUN_ID = "c" * 32


@pytest.fixture(scope="module")
def private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _verifier(private_key: rsa.RSAPrivateKey, key_id: str = KEY_ID) -> RS256ApprovalVerifier:
    numbers = private_key.public_key().public_numbers()
    return RS256ApprovalVerifier.from_numbers(
        key_id,
        n=numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big"),
        e=numbers.e.to_bytes(3, "big"),
    )


def _sign_like_key_vault(private_key: rsa.RSAPrivateKey, record: dict) -> dict:
    """The web app signs SHA-256 of the canonical record through Key Vault; Prehashed matches that."""
    unsigned = {**record, "keyId": KEY_ID, "algorithm": "RS256"}
    unsigned.pop("signature", None)
    digest = hashlib.sha256(canonical_json(unsigned).encode("utf-8")).digest()
    signature = private_key.sign(
        digest, padding.PKCS1v15(), utils.Prehashed(hashes.SHA256())
    )
    return {**unsigned, "signature": signature.hex()}


def _web_app_approval(tmp_path: Path, private_key: rsa.RSAPrivateKey, run_dir: Path):
    validation = validate_snapshot(run_dir, previous_artifact=None, bootstrap=True)
    sku_map = tmp_path / "skumap.json"
    sku_map.write_text('{"version":1}\n', encoding="utf-8")
    record = _approval_record(
        validation,
        sku_map,
        extra={
            "runId": RUN_ID,
            "evidenceDigest": "e" * 64,
            "extractDigest": "a" * 64,
            "evidencePolicy": "MutablePilot",
            "nonProduction": True,
        },
    )
    record = _sign_like_key_vault(private_key, record)
    return validation, sku_map, record


def test_web_app_rs256_approval_publishes_and_verifies(
    tmp_path: Path, private_key: rsa.RSAPrivateKey
) -> None:
    run_dir = tmp_path / "run"
    _stage(run_dir, "snapshot-1")
    _, sku_map, record = _web_app_approval(tmp_path, private_key, run_dir)
    extract = {"rates": {}}
    (run_dir / "rate-extract.json").write_text(canonical_json(extract), encoding="utf-8")
    record = _sign_like_key_vault(
        private_key,
        {**record, "extractDigest": hashlib.sha256(canonical_json(extract).encode()).hexdigest()},
    )
    wrapper = tmp_path / "approval.json"
    wrapper.write_text(
        canonical_json({"schemaVersion": 1, "runId": RUN_ID, "record": record}), encoding="utf-8"
    )
    verifier = _verifier(private_key)
    approve_snapshot(
        run_dir, approval_record_path=wrapper, sku_map_path=sku_map, approval_verifier=verifier
    )
    pointer = publish_snapshot(
        run_dir,
        store_dir=tmp_path / "published",
        expected_pointer_hash=None,
        approval_verifier=verifier,
    )
    artifact = tmp_path / "published" / pointer["artifact"]
    manifest = verify_published_artifact(artifact, approval_verifier=verifier)
    assert manifest["approvalAlgorithm"] == "RS256"
    assert manifest["approvalKeyId"] == KEY_ID
    assert manifest["approvedStagedRunId"] == RUN_ID
    assert manifest["nonProduction"] is True

    with pytest.raises(PublicationError, match="algorithm"):
        verify_published_artifact(artifact)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(PublicationError, match="not verified"):
        verify_published_artifact(artifact, approval_verifier=_verifier(other))
    with pytest.raises(PublicationError, match="not verified"):
        verify_published_artifact(
            artifact, approval_verifier=_verifier(private_key, KEY_ID.replace("0123", "9999"))
        )


def test_refresh_validates_against_an_rs256_published_baseline(
    tmp_path: Path, private_key: rsa.RSAPrivateKey
) -> None:
    run_dir = tmp_path / "run"
    _stage(run_dir, "snapshot-1")
    _, sku_map, record = _web_app_approval(tmp_path, private_key, run_dir)
    (run_dir / "rate-extract.json").write_text("{}", encoding="utf-8")
    record = _sign_like_key_vault(
        private_key, {**record, "extractDigest": hashlib.sha256(b"{}").hexdigest()}
    )
    wrapper = tmp_path / "approval.json"
    wrapper.write_text(
        canonical_json({"schemaVersion": 1, "runId": RUN_ID, "record": record}), encoding="utf-8"
    )
    verifier = _verifier(private_key)
    approve_snapshot(
        run_dir, approval_record_path=wrapper, sku_map_path=sku_map, approval_verifier=verifier
    )
    store = tmp_path / "published"
    pointer = publish_snapshot(
        run_dir, store_dir=store, expected_pointer_hash=None, approval_verifier=verifier
    )
    refresh = tmp_path / "refresh"
    _stage(refresh, "snapshot-2")
    report = validate_snapshot(
        refresh,
        previous_artifact=store / pointer["artifact"],
        bootstrap=False,
        current_pointer=store / "current.json",
        approval_verifier=verifier,
    )
    assert report["baselineSnapshotId"] == "snapshot-1"
    assert report["baselinePointerHash"] == hashlib.sha256(
        (store / "current.json").read_bytes()
    ).hexdigest()
    other = _verifier(rsa.generate_private_key(public_exponent=65537, key_size=2048))
    with pytest.raises(ValidationError, match="not verified"):
        validate_snapshot(
            refresh,
            previous_artifact=store / pointer["artifact"],
            bootstrap=False,
            current_pointer=store / "current.json",
            approval_verifier=other,
        )


def test_rs256_verifier_rejects_tampering(private_key: rsa.RSAPrivateKey) -> None:
    record = _sign_like_key_vault(private_key, {"snapshotId": "s", "approverId": "a"})
    verifier = _verifier(private_key)
    assert verifier(record) is True
    assert verifier({**record, "approverId": "mallory"}) is False
    assert verifier({**record, "signature": "zz"}) is False
    assert verifier({**record, "algorithm": "HMAC-SHA256"}) is False
    # The same signature verifies as ordinary RS256 over the canonical bytes.
    payload = canonical_json({k: v for k, v in record.items() if k != "signature"}).encode()
    private_key.public_key().verify(
        bytes.fromhex(record["signature"]), payload, padding.PKCS1v15(), hashes.SHA256()
    )


def test_unwrap_approval_requires_a_matching_wrapper() -> None:
    record = {"runId": RUN_ID, "snapshotId": "s"}
    assert unwrap_approval(record) is record
    assert unwrap_approval({"schemaVersion": 1, "runId": RUN_ID, "record": record}) is record
    for bad in (
        {"schemaVersion": 1, "runId": "d" * 32, "record": record},
        {"schemaVersion": 1, "runId": RUN_ID, "record": record, "extra": 1},
        {"schemaVersion": 1, "runId": RUN_ID, "record": "x"},
        [record],
    ):
        with pytest.raises(PublicationError):
            unwrap_approval(bad)


def test_parse_key_id_requires_a_versioned_key_url() -> None:
    assert parse_key_id(KEY_ID) == ("https://vault.example.net", "approval", "0123456789abcdef")
    for bad in (
        "https://vault.example.net/keys/approval",
        "http://vault.example.net/keys/approval/1",
        "https://vault.example.net/secrets/approval/1",
        "https://vault.example.net/keys/approval/1?x=1",
    ):
        with pytest.raises(HarvestError):
            parse_key_id(bad)


def test_key_vault_loader_pins_the_key_version(private_key: rsa.RSAPrivateKey) -> None:
    numbers = private_key.public_key().public_numbers()

    class FakeKeyClient:
        def __init__(self, key_id: str, kty: str = "RSA") -> None:
            self.key_id = key_id
            self.kty = kty
            self.calls: list[tuple[str, str]] = []

        def get_key(self, name: str, version: str):
            self.calls.append((name, version))
            return SimpleNamespace(
                id=self.key_id,
                key=SimpleNamespace(
                    kty=self.kty,
                    n=numbers.n.to_bytes(256, "big"),
                    e=numbers.e.to_bytes(3, "big"),
                ),
            )

    client = FakeKeyClient(KEY_ID)
    verifier = load_key_vault_verifier(KEY_ID, key_client=client)
    assert client.calls == [("approval", "0123456789abcdef")]
    assert verifier(_sign_like_key_vault(private_key, {"snapshotId": "s"})) is True
    with pytest.raises(HarvestError, match="different approval key version"):
        load_key_vault_verifier(KEY_ID, key_client=FakeKeyClient(KEY_ID + "0"))
    with pytest.raises(HarvestError, match="not an RSA key"):
        load_key_vault_verifier(KEY_ID, key_client=FakeKeyClient(KEY_ID, kty="EC"))

    class Unavailable:
        def get_key(self, *_):
            raise RuntimeError("private endpoint unreachable")

    with pytest.raises(HarvestError, match="unavailable"):
        load_key_vault_verifier(KEY_ID, key_client=Unavailable())
