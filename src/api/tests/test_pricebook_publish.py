import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.approvals import (
    AzurePriceBookStore,
    LocalPriceBookStore,
    PointerConflict,
    RecordExists,
    StagedArtifactInvalid,
    StorageUnavailable,
)
from tests.test_pricebook_approval import (
    APPROVER,
    ESTIMATOR,
    HMAC_KEY,
    REVIEWER,
    RUN,
    SNAPSHOT,
    _decision,
    _url,
    as_user,
    local_root,  # noqa: F401 - fixture
    reset_backend,  # noqa: F401 - autouse fixture
    stage_run,
)

from harvester.core import canonical_json, row_identity, sha256_text  # noqa: E402
from harvester.snapshot import verify_published_artifact  # noqa: E402

RUN_2 = "fedcba9876543210fedcba9876543210"
SNAPSHOT_2 = "trust-20270201a"


def _rows(price: str = "0.1008") -> bytes:
    rows = []
    for sku, meter in (("m7i.large", "BoxUsage"), ("m7i.xlarge", "BoxUsage")):
        row = {
            "provider": "aws",
            "serviceCode": "AmazonEC2",
            "region": "us-east-1",
            "sku": sku,
            "meter": meter,
            "term": "OnDemand",
            "effectiveStart": "2027-01-01T00:00:00Z",
            "unit": "Hrs",
            "currency": "USD",
            "price": price,
            "dimensions": {"instanceType": sku},
            "sourceUrl": "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/us-east-1/index.json",
            "sourcePublicationDate": "2027-01-01T00:00:00Z",
        }
        row["rowId"] = sha256_text(canonical_json(row_identity(row)))
        rows.append(row)
    rows.sort(key=lambda row: row["rowId"])
    return "".join(f"{canonical_json(row)}\n" for row in rows).encode("utf-8")


def _approve(client: TestClient, monkeypatch: pytest.MonkeyPatch, url_suffix: str = "") -> None:
    as_user(monkeypatch, *REVIEWER)
    reviewed = client.post(_url_for(url_suffix, "/skumap-review"), json=_decision_for(client, url_suffix))
    assert reviewed.status_code == 200, reviewed.text
    as_user(monkeypatch, *APPROVER)
    approved = client.post(_url_for(url_suffix, "/approval"), json=_decision_for(client, url_suffix))
    assert approved.status_code == 200, approved.text


def _url_for(run: str, suffix: str = "") -> str:
    return f"/api/price-book/staged/{SNAPSHOT_2}/{RUN_2}{suffix}" if run else _url(suffix)


def _decision_for(client: TestClient, run: str) -> dict[str, Any]:
    if not run:
        return _decision(client)
    detail = client.get(_url_for(run)).json()
    return {
        name: detail[name]
        for name in ("stageManifestDigest", "extractDigest", "evidenceDigest", "skuMapDigest")
    } | {"attested": True}


def _publish(client: TestClient, run: str = "", **overrides: Any):
    detail = client.get(_url_for(run)).json()
    body = {"evidenceDigest": detail["evidenceDigest"], "attested": True} | overrides
    return client.post(_url_for(run, "/publish"), json=body)


def _staged_and_approved(
    client: TestClient, root: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    files = stage_run(root, rows=_rows())
    _approve(client, monkeypatch)
    return files


def test_bootstrap_publish_writes_a_harvester_verifiable_artifact_and_pointer(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _staged_and_approved(client, local_root, monkeypatch)
    as_user(monkeypatch, *REVIEWER)
    assert client.get(_url()).json()["actions"]["canPublish"] is False
    as_user(monkeypatch, *APPROVER)
    detail = client.get(_url()).json()
    assert detail["state"] == "Approved"
    assert detail["actions"]["canPublish"] is True
    assert detail["publication"]["artifact"] is None

    published = _publish(client)
    assert published.status_code == 200, published.text
    body = published.json()
    assert body["state"] == "Published"
    assert body["publication"]["current"] is True
    assert body["publication"]["publishedBy"] == APPROVER[1]
    assert body["actions"]["canPublish"] is False

    artifact = local_root / "published-pricebooks" / f"{SNAPSHOT}.pricebook.ndjson"
    manifest = verify_published_artifact(artifact, approval_key=HMAC_KEY)
    assert manifest["snapshotId"] == SNAPSHOT
    assert manifest["validationStatus"] == "Published"
    assert manifest["publishingHumanId"] == APPROVER[0]
    assert manifest["skuMapReviewerId"] == REVIEWER[0]
    assert manifest["approvedStagedRunId"] == RUN
    assert manifest["nonProduction"] is True
    assert artifact.read_bytes().endswith(files["rows"])

    pointer_bytes = (local_root / "publication-control" / "current.json").read_bytes()
    assert pointer_bytes == (
        canonical_json({
            "snapshotId": SNAPSHOT,
            "contentHash": hashlib.sha256(files["rows"]).hexdigest(),
            "artifact": f"{SNAPSHOT}.pricebook.ndjson",
            "previousSnapshotId": None,
        }) + "\n"
    ).encode()
    again = _publish(client)
    assert again.status_code == 409
    assert "already published" in again.json()["detail"]
    listing = client.get("/api/price-book/staged").json()["runs"]
    assert [(run["snapshotId"], run["state"]) for run in listing] == [(SNAPSHOT, "Published")]


def test_one_person_holding_both_roles_can_review_approve_and_publish(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = stage_run(local_root, rows=_rows())
    as_user(monkeypatch, REVIEWER[0], REVIEWER[1], "SkuMapReviewer,SnapshotApprover")
    assert client.post(_url("/skumap-review"), json=_decision(client)).status_code == 200
    assert client.get(_url()).json()["actions"]["canApprove"] is True
    approved = client.post(_url("/approval"), json=_decision(client))
    assert approved.status_code == 200, approved.text
    published = _publish(client)
    assert published.status_code == 200, published.text
    manifest = verify_published_artifact(
        local_root / "published-pricebooks" / f"{SNAPSHOT}.pricebook.ndjson", approval_key=HMAC_KEY
    )
    assert manifest["skuMapReviewerId"] == manifest["publishingHumanId"] == REVIEWER[0]
    assert manifest["contentHash"] == hashlib.sha256(files["rows"]).hexdigest()


def test_publish_needs_a_snapshot_approver_and_an_approved_run(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_run(local_root, rows=_rows())
    as_user(monkeypatch, *APPROVER)
    early = _publish(client)
    assert early.status_code == 409
    assert "Approved" in early.json()["detail"]
    _approve(client, monkeypatch)
    for user in (ESTIMATOR, REVIEWER):
        as_user(monkeypatch, *user)
        assert _publish(client).status_code == 403
    as_user(monkeypatch, *APPROVER)
    assert _publish(client, evidenceDigest="0" * 64).status_code == 409
    assert _publish(client, attested=False).status_code == 422
    assert _publish(client, extra="x").status_code == 422
    assert not (local_root / "publication-control" / "current.json").exists()
    assert not (local_root / "published-pricebooks").exists()


def test_a_moved_pointer_refuses_publish_and_needs_a_new_harvest(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _staged_and_approved(client, local_root, monkeypatch)
    pointer = local_root / "publication-control" / "current.json"
    pointer.write_text('{"snapshotId":"someone-else"}\n')
    detail = client.get(_url()).json()
    assert detail["actions"]["canPublish"] is False
    assert "Start a new harvest" in detail["actions"]["waiting"]
    refused = _publish(client)
    assert refused.status_code == 409
    assert "changed after this run was validated" in refused.json()["detail"]
    assert not (local_root / "published-pricebooks").exists()
    assert pointer.read_text() == '{"snapshotId":"someone-else"}\n'


def test_a_lost_pointer_race_refuses_publish(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _staged_and_approved(client, local_root, monkeypatch)

    original = LocalPriceBookStore.replace_control
    lost = []

    def lose_once(self: LocalPriceBookStore, *args: Any, **kwargs: Any) -> None:
        if not lost:
            lost.append(True)
            raise PointerConflict("current.json")
        original(self, *args, **kwargs)

    monkeypatch.setattr(LocalPriceBookStore, "replace_control", lose_once)
    refused = _publish(client)
    assert refused.status_code == 409
    assert "changed while publishing" in refused.json()["detail"]
    assert not (local_root / "publication-control" / "current.json").exists()
    # The artifact exists but was never made current, so the run can still be published.
    detail = client.get(_url()).json()
    assert detail["state"] == "Approved"
    assert detail["publication"]["artifact"] == f"{SNAPSHOT}.pricebook.ndjson"
    assert detail["actions"]["canPublish"] is True
    retried = _publish(client)
    assert retried.status_code == 200, retried.text
    assert retried.json()["state"] == "Published"


def _lose_first_pointer_race(monkeypatch: pytest.MonkeyPatch) -> None:
    original = LocalPriceBookStore.replace_control
    lost: list[bool] = []

    def lose_once(self: LocalPriceBookStore, *args: Any, **kwargs: Any) -> None:
        if not lost:
            lost.append(True)
            raise PointerConflict("current.json")
        original(self, *args, **kwargs)

    monkeypatch.setattr(LocalPriceBookStore, "replace_control", lose_once)


def _tamper_same_length(artifact: Path) -> None:
    data = artifact.read_bytes()
    tampered = data.replace(b'"price":"0.1008"', b'"price":"9.1008"')
    assert tampered != data and len(tampered) == len(data)
    artifact.write_bytes(tampered)


def test_a_retry_refuses_an_orphaned_artifact_changed_after_assembly(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _staged_and_approved(client, local_root, monkeypatch)
    _lose_first_pointer_race(monkeypatch)
    assert _publish(client).status_code == 409
    intent = local_root / "publication-control" / "approvals" / SNAPSHOT / "publication-intent.json"
    assert intent.is_file()
    _tamper_same_length(local_root / "published-pricebooks" / f"{SNAPSHOT}.pricebook.ndjson")
    detail = client.get(_url()).json()
    assert detail["state"] == "Blocked"
    assert any("changed after it was assembled" in problem for problem in detail["problems"])
    assert _publish(client).status_code == 409
    assert not (local_root / "publication-control" / "current.json").exists()


def test_an_artifact_without_an_intent_is_reused_only_when_its_rows_hash_to_the_approval(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _staged_and_approved(client, local_root, monkeypatch)
    _lose_first_pointer_race(monkeypatch)
    assert _publish(client).status_code == 409
    intent = local_root / "publication-control" / "approvals" / SNAPSHOT / "publication-intent.json"
    artifact = local_root / "published-pricebooks" / f"{SNAPSHOT}.pricebook.ndjson"
    good = artifact.read_bytes()
    # As if the process stopped between assembling the artifact and writing the intent.
    intent.unlink()
    _tamper_same_length(artifact)
    refused = _publish(client)
    assert refused.status_code == 409
    assert "different artifact" in refused.json()["detail"]
    assert not intent.exists()
    assert not (local_root / "publication-control" / "current.json").exists()

    artifact.write_bytes(good)
    published = _publish(client)
    assert published.status_code == 200, published.text
    assert json.loads(intent.read_text())["artifactEtag"] == LocalPriceBookStore._etag(good)
    verify_published_artifact(artifact, approval_key=HMAC_KEY)


def test_a_failed_publication_record_is_completed_by_publishing_again(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _staged_and_approved(client, local_root, monkeypatch)
    _fail_record_once(monkeypatch)
    assert _publish(client).status_code == 503
    pointer = local_root / "publication-control" / "current.json"
    pointer_bytes = pointer.read_bytes()
    detail = client.get(_url()).json()
    # Current, but not Published until the record exists; publishing again completes it.
    assert detail["state"] == "Approved"
    assert detail["publication"]["current"] is True
    assert detail["actions"]["canPublish"] is True

    completed = _publish(client)
    assert completed.status_code == 200, completed.text
    assert completed.json()["state"] == "Published"
    assert pointer.read_bytes() == pointer_bytes
    record = json.loads(_record(local_root, SNAPSHOT).read_text())
    assert record["publisherId"] == APPROVER[0]
    assert record["requestedBy"] == APPROVER[1]
    assert _publish(client).status_code == 409


def _fail_record_once(monkeypatch: pytest.MonkeyPatch) -> None:
    original = LocalPriceBookStore.create_control
    failed: list[bool] = []

    def fail_record_once(self: LocalPriceBookStore, name: str, data: bytes) -> None:
        if name.endswith("/publication.json") and not failed:
            failed.append(True)
            raise StorageUnavailable("injected")
        original(self, name, data)

    monkeypatch.setattr(LocalPriceBookStore, "create_control", fail_record_once)


def _stage_successor(client: TestClient, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    baseline_hash = hashlib.sha256((root / "publication-control" / "current.json").read_bytes()).hexdigest()

    def refresh(files: dict[str, Any]) -> None:
        files["validation"].update(baselinePointerHash=baseline_hash, baselineSnapshotId=SNAPSHOT)
        files["validation"]["comparison"]["bootstrap"] = False
        files["receipt"] = {"baselineSnapshotId": SNAPSHOT, "baselinePointerHash": baseline_hash}

    stage_run(root, snapshot_id=SNAPSHOT_2, run_id=RUN_2, rows=_rows("0.1100"), mutate=refresh)
    _approve(client, monkeypatch, "2")


def _record(root: Path, snapshot: str) -> Path:
    return root / "publication-control" / "approvals" / snapshot / "publication.json"


def test_a_publication_is_completed_even_after_the_staged_run_is_changed(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _staged_and_approved(client, local_root, monkeypatch)
    _fail_record_once(monkeypatch)
    assert _publish(client).status_code == 503
    evidence = client.get(_url()).json()["evidenceDigest"]
    # The harvester identity can still write the staging container.
    (local_root / "staged-runs" / "staging" / SNAPSHOT / RUN / "canonical-rows.ndjson").write_bytes(
        b"changed\n"
    )
    assert client.get(_url()).json()["state"] == "Blocked"
    completed = client.post(_url("/publish"), json={"evidenceDigest": evidence, "attested": True})
    assert completed.status_code == 200, completed.text
    record = json.loads(_record(local_root, SNAPSHOT).read_text())
    assert record["runId"] == RUN
    assert record["artifactEtag"] == json.loads(
        (local_root / "publication-control" / "approvals" / SNAPSHOT / "publication-intent.json").read_text()
    )["artifactEtag"]


def test_publishing_a_successor_completes_its_predecessors_record(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _staged_and_approved(client, local_root, monkeypatch)
    _fail_record_once(monkeypatch)
    assert _publish(client).status_code == 503
    _stage_successor(client, local_root, monkeypatch)
    (local_root / "staged-runs" / "staging" / SNAPSHOT / RUN / "canonical-rows.ndjson").unlink()
    published = _publish(client, "2")
    assert published.status_code == 200, published.text
    assert json.loads(_record(local_root, SNAPSHOT).read_text())["requestedBy"] == APPROVER[1]
    assert _record(local_root, SNAPSHOT_2).is_file()


def test_a_successor_is_refused_when_its_predecessor_cannot_be_completed(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _staged_and_approved(client, local_root, monkeypatch)
    _fail_record_once(monkeypatch)
    assert _publish(client).status_code == 503
    _stage_successor(client, local_root, monkeypatch)
    (local_root / "publication-control" / "approvals" / SNAPSHOT / "publication-intent.json").unlink()
    pointer = local_root / "publication-control" / "current.json"
    before = pointer.read_bytes()
    refused = _publish(client, "2")
    assert refused.status_code == 409
    assert "did not finish" in refused.json()["detail"]
    assert pointer.read_bytes() == before
    assert not _record(local_root, SNAPSHOT_2).exists()


def test_an_oversized_pointer_is_a_refusal_not_a_server_error(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _staged_and_approved(client, local_root, monkeypatch)
    control = local_root / "publication-control"
    control.mkdir(exist_ok=True)
    (control / "current.json").write_bytes(b" " * (9 * 1024))
    assert client.get("/api/price-book/staged").status_code == 200
    assert client.get(_url()).json()["actions"]["canPublish"] is False
    assert _publish(client).status_code == 409
    assert not (local_root / "published-pricebooks").exists()


def test_an_unpublished_approved_run_reports_no_publication_record(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _staged_and_approved(client, local_root, monkeypatch)
    publication = client.get(_url()).json()["publication"]
    assert publication["publishedBy"] is None and publication["artifact"] is None


def test_a_different_artifact_under_the_snapshot_id_blocks_the_run(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _staged_and_approved(client, local_root, monkeypatch)
    published = local_root / "published-pricebooks"
    published.mkdir()
    (published / f"{SNAPSHOT}.pricebook.ndjson").write_bytes(b"not this run\n")
    detail = client.get(_url()).json()
    assert detail["state"] == "Blocked"
    assert any("different artifact" in problem for problem in detail["problems"])
    assert _publish(client).status_code == 409
    assert not (local_root / "publication-control" / "current.json").exists()


def test_refresh_publish_moves_the_pointer_from_the_validated_baseline(
    client: TestClient, local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _staged_and_approved(client, local_root, monkeypatch)
    assert _publish(client).status_code == 200
    pointer = local_root / "publication-control" / "current.json"
    baseline_hash = hashlib.sha256(pointer.read_bytes()).hexdigest()

    def refresh(files: dict[str, Any]) -> None:
        files["validation"].update(baselinePointerHash=baseline_hash, baselineSnapshotId=SNAPSHOT)
        files["validation"]["comparison"]["bootstrap"] = False
        files["receipt"] = {"baselineSnapshotId": SNAPSHOT, "baselinePointerHash": baseline_hash}

    stage_run(local_root, snapshot_id=SNAPSHOT_2, run_id=RUN_2, rows=_rows("0.1100"), mutate=refresh)
    _approve(client, monkeypatch, "2")
    published = _publish(client, "2")
    assert published.status_code == 200, published.text
    assert json.loads(pointer.read_text()) == {
        "snapshotId": SNAPSHOT_2,
        "contentHash": hashlib.sha256(_rows("0.1100")).hexdigest(),
        "artifact": f"{SNAPSHOT_2}.pricebook.ndjson",
        "previousSnapshotId": SNAPSHOT,
    }
    first = client.get(_url()).json()
    assert first["state"] == "Published"
    assert first["publication"]["current"] is False
    assert first["publication"]["currentSnapshotId"] == SNAPSHOT_2


def test_local_store_pointer_cas_and_append_only_artifacts(tmp_path: Path) -> None:
    store = LocalPriceBookStore(tmp_path, "staged-runs", "publication-control")
    store.replace_control("current.json", b"one\n", etag=None)
    with pytest.raises(PointerConflict):
        store.replace_control("current.json", b"two\n", etag=None)
    etag = store.read_control("current.json", max_bytes=100).etag
    with pytest.raises(PointerConflict):
        store.replace_control("current.json", b"two\n", etag='"stale"')
    store.replace_control("current.json", b"two\n", etag=etag)
    assert (tmp_path / "publication-control" / "current.json").read_bytes() == b"two\n"

    rows = tmp_path / "staged-runs" / "staging" / "s" / "r"
    rows.mkdir(parents=True)
    (rows / "rows.ndjson").write_bytes(b"row\n")
    rows_etag = store.staged_properties("staging/s/r/rows.ndjson").etag
    with pytest.raises(StagedArtifactInvalid):
        store.assemble_published(
            "s.pricebook.ndjson", b"head\n", rows_name="staging/s/r/rows.ndjson",
            rows_etag='"other"', rows_size=4,
        )
    store.assemble_published(
        "s.pricebook.ndjson", b"head\n", rows_name="staging/s/r/rows.ndjson",
        rows_etag=rows_etag, rows_size=4,
    )
    with pytest.raises(RecordExists):
        store.assemble_published(
            "s.pricebook.ndjson", b"head\n", rows_name="staging/s/r/rows.ndjson",
            rows_etag=rows_etag, rows_size=4,
        )
    etag = store.published_properties("s.pricebook.ndjson").etag
    assert store.read_published_head("s.pricebook.ndjson", 5, etag=etag) == b"head\n"
    assert store.published_properties("s.pricebook.ndjson").size == 9
    assert list((tmp_path / "published-pricebooks").iterdir()) == [
        tmp_path / "published-pricebooks" / "s.pricebook.ndjson"
    ]


def test_azure_store_copies_rows_server_side_pinned_to_the_approved_etag() -> None:
    from azure.core.exceptions import ResourceModifiedError

    calls: list[tuple[str, Any]] = []

    class Destination:
        def stage_block(self, block_id: str, data: bytes, length: int) -> None:
            calls.append(("stage", (block_id, data, length)))

        def stage_block_from_url(self, block_id: str, url: str, **kwargs: Any) -> None:
            calls.append(("copy", (block_id, url, kwargs)))

        def commit_block_list(self, blocks: list, **kwargs: Any) -> dict[str, Any]:
            calls.append(("commit", ([block.id for block in blocks], kwargs)))
            return {"etag": '"committed"'}

    class Container:
        def __init__(self, client: Any) -> None:
            self.client = client

        def get_blob_client(self, name: str) -> Any:
            return self.client if self.client else SimpleNamespace(url=f"https://acct/staged/{name}")

    store = AzurePriceBookStore.__new__(AzurePriceBookStore)
    store._credential = SimpleNamespace(get_token=lambda scope: SimpleNamespace(token=f"tok:{scope}"))
    store._staging = Container(None)
    destination = Destination()
    store._published = Container(destination)
    size = 250 * 1024 * 1024
    assert store.assemble_published(
        "s.pricebook.ndjson", b"head\n", rows_name="staging/s/r/rows", rows_etag='"e1"',
        rows_size=size,
    ) == '"committed"'
    assert calls[0] == ("stage", ("000000", b"head\n", 5))
    copies = sorted(call[1] for call in calls if call[0] == "copy")
    assert [(block_id, kwargs["source_offset"], kwargs["source_length"]) for block_id, _, kwargs in copies] == [
        ("000001", 0, 100 * 1024 * 1024),
        ("000002", 100 * 1024 * 1024, 100 * 1024 * 1024),
        ("000003", 200 * 1024 * 1024, 50 * 1024 * 1024),
    ]
    for _, url, kwargs in copies:
        assert url == "https://acct/staged/staging/s/r/rows"
        assert kwargs["source_authorization"] == "Bearer tok:https://storage.azure.com/.default"
        assert kwargs["source_modified_access_conditions"].source_if_match == '"e1"'
    assert calls[-1] == ("commit", (["000000", "000001", "000002", "000003"], {"if_none_match": "*"}))

    class Changed(Destination):
        def stage_block_from_url(self, *_: Any, **__: Any) -> None:
            raise ResourceModifiedError("source changed")

    store._published = Container(Changed())
    with pytest.raises(StagedArtifactInvalid):
        store.assemble_published(
            "s.pricebook.ndjson", b"head\n", rows_name="staging/s/r/rows", rows_etag='"e1"',
            rows_size=10,
        )
