"""PRICEBOOK_SOURCE=published-blob prices only from a verified Published snapshot, and fails closed."""

import hashlib
import json
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.approvals import digest_of, reset_approval_backend
from app.pricing import PilotPricingEngine, PricingConfigurationError, pricing_engine
from tests.test_answer_first_api import _resolved_repository
from tests.test_pricebook_approval import (
    APPROVER,
    RUN,
    SNAPSHOT,
    local_root,  # noqa: F401 - fixture
    reset_backend,  # noqa: F401 - autouse fixture
    stage_run,
)
from tests.test_pricebook_publish import (
    RUN_2,
    SNAPSHOT_2,
    _approve,
    _publish,
    _rows,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def _seed_extract(priced_as_of: str, **rate_overrides: str):
    def mutate(files: dict[str, Any]) -> None:
        extract = json.loads((REPOSITORY_ROOT / "samples" / "pricebook_seed_v1.json").read_text())
        source = files["extract"]["manifest"]
        extract["manifest"].update(
            snapshotId=source["snapshotId"],
            sourceSnapshotId=source["sourceSnapshotId"],
            sourceContentHash=source["sourceContentHash"],
            pricedAsOf=priced_as_of,
            validationStatus="Validated",
            publishingHuman=None,
        )
        extract["rates"].update(rate_overrides)
        files["extract"] = extract
        files["report"]["extractDigest"] = digest_of(extract)

    return mutate


def _today(days_ago: int = 0) -> str:
    return (datetime.now(UTC).date() - timedelta(days=days_ago)).isoformat()


def _publish_seed(
    client: TestClient, root: Path, monkeypatch: pytest.MonkeyPatch, *, days_ago: int = 0
) -> dict[str, Any]:
    files = stage_run(root, rows=_rows(), mutate=_seed_extract(_today(days_ago)))
    _approve(client, monkeypatch)
    published = _publish(client)
    assert published.status_code == 200, published.text
    return files


def _engine(monkeypatch: pytest.MonkeyPatch, **env: str) -> PilotPricingEngine:
    monkeypatch.setenv("PRICEBOOK_SOURCE", "published-blob")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return PilotPricingEngine()


def _price(engine: PilotPricingEngine, record: Any):
    return engine.compare(
        record.application,
        record.normalized.compute_units,
        record.normalized.database_units,
        record.normalized.storage_units,
        record.normalized.gaps,
    )


def _refused(engine: PilotPricingEngine, match: str) -> None:
    with pytest.raises(PricingConfigurationError, match=match):
        engine.manifest


def test_published_snapshot_prices_like_the_extract_it_approved(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _publish_seed(client, local_root, monkeypatch)
    _, record = _resolved_repository(REPOSITORY_ROOT)
    demo = _price(pricing_engine, record)
    published = _price(_engine(monkeypatch), record)

    assert published.state == demo.state
    assert published.aws_monthly_total == demo.aws_monthly_total
    assert published.azure_monthly_total == demo.azure_monthly_total
    assert [line.model_dump() for line in published.line_items] == [
        line.model_dump() for line in demo.line_items
    ]
    assert published.pricebook_content_hash == digest_of(files["extract"])
    assert published.source_pricebook_snapshot_id == SNAPSHOT
    assert published.source_pricebook_content_hash == files["manifest"]["contentHash"]
    assert published.priced_as_of == _today()
    assert published.run_hash != demo.run_hash
    # The Azure RHEL uplift stays a visible, labeled placeholder under Published pricing.
    assert any(line.demo_assumption for line in published.line_items)


def test_published_manifest_names_the_publishing_human(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_seed(client, local_root, monkeypatch)
    manifest = _engine(monkeypatch).manifest
    assert manifest.validation_status == "Published"
    assert manifest.publishing_human == APPROVER[1]
    assert manifest.non_production is True


def test_nothing_published_fails_closed(local_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _refused(_engine(monkeypatch), "no PriceBook snapshot is Published")


def test_published_source_needs_approval_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRICEBOOK_APPROVAL_MODE", raising=False)
    reset_approval_backend()
    _refused(_engine(monkeypatch), "not configured")


def test_a_tampered_staged_extract_fails_closed(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_seed(client, local_root, monkeypatch)
    path = local_root / "staged-runs" / "staging" / SNAPSHOT / RUN / "rate-extract.json"
    extract = json.loads(path.read_text())
    extract["rates"]["azure.vm.linux.4x16.hour"] = "0.000100"
    path.write_text(json.dumps(extract))
    _refused(_engine(monkeypatch), "does not match its approved digest")


def test_a_missing_staged_extract_fails_closed(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_seed(client, local_root, monkeypatch)
    (local_root / "staged-runs" / "staging" / SNAPSHOT / RUN / "rate-extract.json").unlink()
    _refused(_engine(monkeypatch), "approved rate extract is missing")


def test_a_different_signing_key_fails_closed(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_seed(client, local_root, monkeypatch)
    monkeypatch.setenv("APPROVAL_HMAC_KEY", "another-deployment-key-0123456789abcdef")
    reset_approval_backend()
    _refused(_engine(monkeypatch), "signed approval does not verify")


def test_an_artifact_changed_after_publication_fails_closed(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_seed(client, local_root, monkeypatch)
    artifact = local_root / "published-pricebooks" / f"{SNAPSHOT}.pricebook.ndjson"
    artifact.write_bytes(artifact.read_bytes() + b"{}\n")
    _refused(_engine(monkeypatch), "changed after it was published")


def test_a_forged_manifest_line_fails_closed(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_seed(client, local_root, monkeypatch)
    artifact = local_root / "published-pricebooks" / f"{SNAPSHOT}.pricebook.ndjson"
    header, rows = artifact.read_bytes().split(b"\n", 1)
    document = json.loads(header)
    document["manifest"]["publishingHuman"] = "Someone Else"
    artifact.write_bytes(json.dumps(document, sort_keys=True, separators=(",", ":")).encode() + b"\n" + rows)
    record_path = local_root / "publication-control" / "approvals" / SNAPSHOT / "publication.json"
    record = json.loads(record_path.read_text())
    from app.approvals import LocalPriceBookStore

    record["artifactEtag"] = LocalPriceBookStore._etag(artifact.read_bytes())
    record_path.write_text(json.dumps(record))
    _refused(_engine(monkeypatch), "signed approval does not verify")


def test_an_unrecorded_publication_fails_closed(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_seed(client, local_root, monkeypatch)
    (local_root / "publication-control" / "approvals" / SNAPSHOT / "publication.json").unlink()
    _refused(_engine(monkeypatch), "is not recorded yet")


def test_a_skumap_changed_since_approval_fails_closed(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_seed(client, local_root, monkeypatch)
    engine = _engine(monkeypatch)
    engine._skumap_digest = "f" * 64
    _refused(engine, "approved against a different SkuMap")


def test_a_stale_published_snapshot_fails_closed(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_seed(client, local_root, monkeypatch, days_ago=10)
    _, record = _resolved_repository(REPOSITORY_ROOT)
    assert _price(_engine(monkeypatch, PRICEBOOK_MAX_AGE_DAYS="10"), record).state
    with pytest.raises(PricingConfigurationError, match="freshness limit"):
        _price(_engine(monkeypatch, PRICEBOOK_MAX_AGE_DAYS="9"), record)


@pytest.mark.parametrize(
    ("name", "value"),
    [("PRICEBOOK_MAX_AGE_DAYS", "0"), ("PRICEBOOK_REFRESH_SECONDS", "abc")],
)
def test_invalid_published_settings_fail_startup(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    with pytest.raises(PricingConfigurationError, match=name):
        _engine(monkeypatch, **{name: value})


def test_a_moved_pointer_reloads_the_new_snapshot(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_seed(client, local_root, monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    engine = _engine(monkeypatch, PRICEBOOK_REFRESH_SECONDS="60")
    assert engine.manifest.source_snapshot_id == SNAPSHOT

    pointer = local_root / "publication-control" / "current.json"
    baseline_hash = hashlib.sha256(pointer.read_bytes()).hexdigest()
    seed = _seed_extract(_today(), **{"azure.vm.linux.4x16.hour": "0.200000"})

    def successor(files: dict[str, Any]) -> None:
        files["validation"].update(baselinePointerHash=baseline_hash, baselineSnapshotId=SNAPSHOT)
        files["validation"]["comparison"]["bootstrap"] = False
        files["receipt"] = {"baselineSnapshotId": SNAPSHOT, "baselinePointerHash": baseline_hash}
        seed(files)

    stage_run(local_root, snapshot_id=SNAPSHOT_2, run_id=RUN_2, rows=_rows("0.1100"), mutate=successor)
    _approve(client, monkeypatch, "2")
    assert _publish(client, "2").status_code == 200

    assert engine.manifest.source_snapshot_id == SNAPSHOT  # still inside the refresh interval
    clock[0] += 61
    assert engine.manifest.source_snapshot_id == SNAPSHOT_2
    assert engine._pricebook["rates"]["azure.vm.linux.4x16.hour"] == "0.200000"


def test_a_pointer_moved_to_an_unverifiable_snapshot_stops_pricing(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_seed(client, local_root, monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    engine = _engine(monkeypatch, PRICEBOOK_REFRESH_SECONDS="60")
    assert engine.manifest.source_snapshot_id == SNAPSHOT
    pointer = local_root / "publication-control" / "current.json"
    pointer.write_text(json.dumps({"snapshotId": "rollback-attempt", "contentHash": "0" * 64,
                                   "artifact": "rollback-attempt.pricebook.ndjson",
                                   "previousSnapshotId": SNAPSHOT}))
    clock[0] += 61
    _refused(engine, "is not recorded yet")
    # Pricing does not fall back to the snapshot it had before.
    _refused(engine, "is not recorded yet")


def test_a_comparison_keeps_one_pricebook_while_a_new_one_loads(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_seed(client, local_root, monkeypatch)
    engine = _engine(monkeypatch)
    _, record = _resolved_repository(REPOSITORY_ROOT)
    original_rates = engine._rates
    swapped: list[bool] = []

    def swap_mid_comparison(aws_key: str, azure_key: str):
        if not swapped:
            # A concurrent reload replaces the shared state with another snapshot's book.
            state = engine._state
            engine._state = replace(
                state,
                manifest=state.manifest.model_copy(update={"source_snapshot_id": "another"}),
                pricebook={**state.pricebook, "rates": {}},
            )
            engine._checked_at = time.monotonic()
            swapped.append(True)
        return original_rates(aws_key, azure_key)

    monkeypatch.setattr(engine, "_rates", swap_mid_comparison)
    comparison = _price(engine, record)
    assert swapped
    assert comparison.source_pricebook_snapshot_id == SNAPSHOT
    assert comparison.aws_monthly_total is not None
    assert engine.manifest.source_snapshot_id == "another"


def test_capabilities_report_published_pricing_or_refuse(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.routes

    monkeypatch.setattr(app.routes, "pricing_engine", _engine(monkeypatch))
    refused = client.get("/api/capabilities")
    assert refused.status_code == 503
    assert "no PriceBook snapshot is Published" in refused.json()["detail"]

    _publish_seed(client, local_root, monkeypatch, days_ago=31)
    monkeypatch.setattr(app.routes, "pricing_engine", _engine(monkeypatch))
    book = client.get("/api/capabilities").json()["priceBook"]
    assert book["source"] == "published-blob"
    assert book["pricedAsOf"] == _today(31)
    assert book["nonProduction"] is True
    # A mutable-pilot Published harvest ages like any harvest; only the frozen demo set is exempt.
    assert book["stale"] is True


def test_an_artifact_rewritten_after_its_etag_check_fails_closed(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.approvals import LocalPriceBookStore

    _publish_seed(client, local_root, monkeypatch)
    artifact = local_root / "published-pricebooks" / f"{SNAPSHOT}.pricebook.ndjson"
    checked = LocalPriceBookStore.published_properties

    def rewrite_after_check(self: LocalPriceBookStore, name: str):
        properties = checked(self, name)
        # Keep the signed header, replace the rows between the ETag check and the header read.
        header = artifact.read_bytes().split(b"\n", 1)[0]
        artifact.write_bytes(header + b"\n{}\n")
        return properties

    monkeypatch.setattr(LocalPriceBookStore, "published_properties", rewrite_after_check)
    _refused(_engine(monkeypatch), "changed after it was published")


def test_evidence_changed_under_an_unchanged_pointer_stops_pricing(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_seed(client, local_root, monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    engine = _engine(monkeypatch, PRICEBOOK_REFRESH_SECONDS="60")
    assert engine.manifest.source_snapshot_id == SNAPSHOT
    path = local_root / "staged-runs" / "staging" / SNAPSHOT / RUN / "rate-extract.json"
    original = path.read_bytes()
    extract = json.loads(original)
    extract["rates"]["azure.vm.linux.4x16.hour"] = "0.000100"
    path.write_text(json.dumps(extract))

    clock[0] += 61
    _refused(engine, "does not match its approved digest")
    # A refusal is cached briefly, then the chain is verified again.
    path.write_bytes(original)
    clock[0] += 1
    _refused(engine, "does not match its approved digest")
    clock[0] += 5
    assert engine.manifest.source_snapshot_id == SNAPSHOT


def test_capabilities_refuse_a_snapshot_the_engine_would_refuse(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.routes

    _publish_seed(client, local_root, monkeypatch, days_ago=25)
    monkeypatch.setattr(
        app.routes, "pricing_engine", _engine(monkeypatch, PRICEBOOK_MAX_AGE_DAYS="20")
    )
    expired = client.get("/api/capabilities")
    assert expired.status_code == 503
    assert "20-day freshness limit" in expired.json()["detail"]

    monkeypatch.setattr(
        app.routes, "pricing_engine", _engine(monkeypatch, PRICEBOOK_MAX_AGE_DAYS="24")
    )
    assert client.get("/api/capabilities").status_code == 503

    monkeypatch.setattr(
        app.routes, "pricing_engine", _engine(monkeypatch, PRICEBOOK_MAX_AGE_DAYS="26")
    )
    book = client.get("/api/capabilities").json()["priceBook"]
    # Flagged stale before the engine stops pricing it.
    assert book["staleAfterDays"] == 26
    assert book["stale"] is False

    monkeypatch.setattr(
        app.routes, "pricing_engine", _engine(monkeypatch, PRICEBOOK_MAX_AGE_DAYS="25")
    )
    book = client.get("/api/capabilities").json()["priceBook"]
    assert book["staleAfterDays"] == 25
    assert book["stale"] is False


def test_a_publication_through_this_api_is_priced_at_once(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.pricebook_routes

    monkeypatch.setattr(time, "monotonic", lambda: 1000.0)
    engine = _engine(monkeypatch, PRICEBOOK_REFRESH_SECONDS="3600")
    monkeypatch.setattr(app.pricebook_routes, "pricing_engine", engine)
    _refused(engine, "no PriceBook snapshot is Published")
    _publish_seed(client, local_root, monkeypatch)
    # No clock movement: the refusal cached a moment ago does not outlive the publication.
    assert engine.manifest.source_snapshot_id == SNAPSHOT


def _publish_successor(client: TestClient, root: Path, monkeypatch: pytest.MonkeyPatch) -> bytes:
    """Publish SNAPSHOT_2 over SNAPSHOT; return SNAPSHOT's pointer bytes."""
    pointer = root / "publication-control" / "current.json"
    first = pointer.read_bytes()
    baseline_hash = hashlib.sha256(first).hexdigest()
    seed = _seed_extract(_today())

    def successor(files: dict[str, Any]) -> None:
        files["validation"].update(baselinePointerHash=baseline_hash, baselineSnapshotId=SNAPSHOT)
        files["validation"]["comparison"]["bootstrap"] = False
        files["receipt"] = {"baselineSnapshotId": SNAPSHOT, "baselinePointerHash": baseline_hash}
        seed(files)

    stage_run(root, snapshot_id=SNAPSHOT_2, run_id=RUN_2, rows=_rows("0.1100"), mutate=successor)
    _approve(client, monkeypatch, "2")
    assert _publish(client, "2").status_code == 200
    return first


def test_a_pointer_rolled_back_to_a_replaced_snapshot_fails_closed(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_seed(client, local_root, monkeypatch)
    first = _publish_successor(client, local_root, monkeypatch)
    assert _engine(monkeypatch).manifest.source_snapshot_id == SNAPSHOT_2

    # An administrator restores the older pointer: every other check on SNAPSHOT still passes.
    (local_root / "publication-control" / "current.json").write_bytes(first)
    _refused(_engine(monkeypatch), f"{SNAPSHOT} was replaced by {SNAPSHOT_2}")


@pytest.mark.parametrize(
    "tamper",
    [
        lambda record: {},
        lambda record: {k: v for k, v in record.items() if k != "previousSnapshotId"},
        lambda record: {**record, "previousSnapshotId": 7},
        lambda record: {**record, "snapshotId": "other-snapshot"},
    ],
    ids=["empty", "no-predecessor", "predecessor-not-a-string", "wrong-snapshot"],
)
def test_a_malformed_successor_record_fails_closed(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch, tamper: Any
) -> None:
    _publish_seed(client, local_root, monkeypatch)
    first = _publish_successor(client, local_root, monkeypatch)
    record_path = local_root / "publication-control" / "approvals" / SNAPSHOT_2 / "publication.json"
    record_path.write_text(json.dumps(tamper(json.loads(record_path.read_text()))))
    (local_root / "publication-control" / "current.json").write_bytes(first)
    _refused(_engine(monkeypatch), "a rollback can't be ruled out")


def test_a_successor_record_that_vanishes_after_listing_fails_closed(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.approvals import LocalPriceBookStore

    _publish_seed(client, local_root, monkeypatch)
    first = _publish_successor(client, local_root, monkeypatch)
    (local_root / "publication-control" / "current.json").write_bytes(first)
    real = LocalPriceBookStore.read_control
    successor = f"approvals/{SNAPSHOT_2}/publication.json"

    def vanishing(self: Any, name: str, *, max_bytes: int) -> Any:
        return None if name == successor else real(self, name, max_bytes=max_bytes)

    monkeypatch.setattr(LocalPriceBookStore, "read_control", vanishing)
    _refused(_engine(monkeypatch), "a rollback can't be ruled out")


@pytest.mark.parametrize(
    "history",
    # The service omits IsCurrentVersion on versions that are not current, so the SDK reports None, not False.
    [{"version_id": "2026-01-01T00:00:00.0000000Z", "is_current_version": None}, {"deleted": True}],
    ids=["overwritten-or-deleted-version", "soft-deleted"],
)
def test_azure_listing_refuses_a_changed_publication_history(history: dict[str, Any]) -> None:
    from types import SimpleNamespace

    from app.approvals import AzurePriceBookStore, StagedArtifactInvalid

    name = f"approvals/{SNAPSHOT_2}/publication.json"

    class Container:
        def list_blobs(self, *, name_starts_with: str, include: list[str]) -> list[Any]:
            assert name_starts_with == "approvals/" and {"versions", "deleted"} <= set(include)
            return [
                SimpleNamespace(name=name, deleted=False, version_id="2026-02-01T00:00:00Z", is_current_version=True),
                SimpleNamespace(
                    **{"name": name, "deleted": False, "version_id": None, "is_current_version": None, **history}
                ),
            ]

    store = AzurePriceBookStore.__new__(AzurePriceBookStore)
    store._control = Container()
    with pytest.raises(StagedArtifactInvalid, match="was deleted or changed"):
        store.list_publication_records()


def test_azure_listing_returns_each_intact_record_once() -> None:
    from types import SimpleNamespace

    from app.approvals import AzurePriceBookStore

    class Container:
        def list_blobs(self, *, name_starts_with: str, include: list[str]) -> list[Any]:
            return [
                SimpleNamespace(name="approvals/b/publication.json", deleted=False, version_id="v2", is_current_version=True),
                # Versioning off: no version ID and no current-version flag.
                SimpleNamespace(name="approvals/a/publication.json", deleted=False, version_id=None, is_current_version=None),
                SimpleNamespace(name="approvals/a/approval.json", deleted=False, version_id="v1", is_current_version=None),
            ]

    store = AzurePriceBookStore.__new__(AzurePriceBookStore)
    store._control = Container()
    assert store.list_publication_records() == ["approvals/a/publication.json", "approvals/b/publication.json"]


def test_azure_pointer_history_reads_versions_oldest_first() -> None:
    from types import SimpleNamespace

    from azure.core.exceptions import ResourceNotFoundError

    from app.approvals import AzurePriceBookStore, StagedArtifactInvalid

    versions = {"2026-03-01T00:00:00Z": b"c", "2026-01-01T00:00:00Z": b"a", "2026-02-01T00:00:00Z": b"b"}

    class Blob:
        def download_blob(self, *, version_id: str, max_concurrency: int) -> Any:
            if version_id not in versions:
                raise ResourceNotFoundError("gone")
            return SimpleNamespace(size=1, readall=lambda: versions[version_id])

    class Container:
        listed = list(versions)

        def list_blobs(self, *, name_starts_with: str, include: list[str]) -> list[Any]:
            assert {"versions", "deleted"} <= set(include)
            return [SimpleNamespace(name="current.json", version_id=v) for v in self.listed] + [
                SimpleNamespace(name="current.json.bak", version_id="2026-04-01T00:00:00Z")
            ]

        def get_blob_client(self, name: str) -> Blob:
            assert name == "current.json"
            return Blob()

    store = AzurePriceBookStore.__new__(AzurePriceBookStore)
    store._control = Container()
    assert store.control_history("current.json", max_bytes=10, max_versions=5) == [b"a", b"b", b"c"]
    with pytest.raises(StagedArtifactInvalid, match="more than 2 versions"):
        store.control_history("current.json", max_bytes=10, max_versions=2)
    Container.listed = [*versions, "2026-01-15T00:00:00Z"]
    with pytest.raises(StagedArtifactInvalid, match="was deleted"):
        store.control_history("current.json", max_bytes=10, max_versions=5)


def _with_pointer_history(monkeypatch: pytest.MonkeyPatch, versions: list[bytes]) -> None:
    from app.approvals import LocalPriceBookStore

    monkeypatch.setattr(LocalPriceBookStore, "control_history", lambda self, name, **_: list(versions))


def test_a_pointer_that_moved_forward_still_loads(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_seed(client, local_root, monkeypatch)
    first = _publish_successor(client, local_root, monkeypatch)
    second = (local_root / "publication-control" / "current.json").read_bytes()
    _with_pointer_history(monkeypatch, [first, first, second])
    assert _engine(monkeypatch).manifest.source_snapshot_id == SNAPSHOT_2


def test_pointer_history_catches_a_rollback_with_no_successor_record(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_seed(client, local_root, monkeypatch)
    first = _publish_successor(client, local_root, monkeypatch)
    second = (local_root / "publication-control" / "current.json").read_bytes()
    # The pointer moved to SNAPSHOT_2 but its record was never written (or was purged), then was put back.
    (local_root / "publication-control" / "approvals" / SNAPSHOT_2 / "publication.json").unlink()
    (local_root / "publication-control" / "current.json").write_bytes(first)
    _with_pointer_history(monkeypatch, [first, second, first])
    _refused(_engine(monkeypatch), f"moved off {SNAPSHOT} and later moved back")


def test_an_unreadable_pointer_version_fails_closed(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_seed(client, local_root, monkeypatch)
    current = (local_root / "publication-control" / "current.json").read_bytes()
    _with_pointer_history(monkeypatch, [b"not json", current])
    _refused(_engine(monkeypatch), "a version of the current pointer is unreadable")


def test_a_second_first_publication_fails_closed(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_seed(client, local_root, monkeypatch)
    record_path = local_root / "publication-control" / "approvals" / SNAPSHOT / "publication.json"
    other = local_root / "publication-control" / "approvals" / "other-snapshot" / "publication.json"
    other.parent.mkdir(parents=True)
    other.write_text(json.dumps({**json.loads(record_path.read_text()), "snapshotId": "other-snapshot"}))
    _refused(_engine(monkeypatch), "more than one publication replaced no snapshot")


def test_publish_refuses_to_rebuild_a_missing_pointer(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_seed(client, local_root, monkeypatch)
    (local_root / "publication-control" / "current.json").unlink()
    stage_run(local_root, snapshot_id=SNAPSHOT_2, run_id=RUN_2, rows=_rows("0.1100"), mutate=_seed_extract(_today()))
    _approve(client, monkeypatch, "2")
    refused = _publish(client, "2")
    assert refused.status_code == 409, refused.text
    assert "current pointer is missing" in refused.text
    assert not (local_root / "publication-control" / "current.json").exists()


def test_an_unreadable_publication_record_fails_closed(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_seed(client, local_root, monkeypatch)
    stray = local_root / "publication-control" / "approvals" / "other-snapshot" / "publication.json"
    stray.parent.mkdir(parents=True)
    stray.write_text("not json")
    _refused(_engine(monkeypatch), "a rollback can't be ruled out")
