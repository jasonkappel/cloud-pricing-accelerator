from __future__ import annotations

import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from src.harvester.aws import (
    _index_products,
    _index_savings_plan_products,
    _iter_offer_terms,
    _iter_savings_plan_rows,
    _resolve_offer_url,
)
from src.harvester.azure import collect_azure_rows
from src.harvester.core import (
    HarvestError,
    PublicationError,
    ValidationError,
    canonical_json,
    row_identity,
    sha256_text,
    write_ndjson,
)
from src.harvester.http import SafeHttpClient
from src.harvester.reconcile import reconcile_representative_application
from src.harvester.snapshot import (
    approve_snapshot,
    build_staged_snapshot,
    publish_snapshot,
    validate_snapshot,
    verify_published_artifact,
)


APPROVAL_KEY = "test-approval-key-with-at-least-32-characters"


def _client(handler, *, retries: int = 0) -> SafeHttpClient:
    return SafeHttpClient(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        retries=retries,
    )


def _row(
    provider: str,
    service: str,
    *,
    price: str = "1.25",
    region: str | None = None,
    term: str = "OnDemand",
    unit: str = "Hrs",
    dimensions: dict | None = None,
    product_family: str | None = None,
    suffix: str = "",
) -> dict:
    identity = {
        "provider": provider,
        "serviceCode": service,
        "region": region or ("us-east-1" if provider == "aws" else "eastus2"),
        "sku": f"sku-{service}-{suffix}",
        "meter": f"meter-{service}-{suffix}",
        "term": term,
        "effectiveStart": "2026-01-01T00:00:00Z",
        "unit": unit,
        "dimensions": dimensions or {},
    }
    if product_family is not None:
        identity["productFamily"] = product_family
    return {
        **identity,
        "rowId": sha256_text(canonical_json(identity)),
        "currency": "USD",
        "price": price,
        "sourceUrl": (
            "https://pricing.us-east-1.amazonaws.com/catalog"
            if provider == "aws"
            else "https://prices.azure.com/api/retail/prices"
        ),
        "sourcePublicationDate": "2026-01-01T00:00:00Z",
    }


def _complete_rows() -> list[dict]:
    return [
        _row("aws", "AmazonEC2", suffix="vm"),
        _row(
            "aws",
            "AmazonEC2",
            product_family="Storage",
            dimensions={"volumeApiName": "gp3"},
            unit="GB-Mo",
            suffix="gp3-capacity",
        ),
        _row(
            "aws",
            "AmazonEC2",
            product_family="System Operation",
            dimensions={"volumeApiName": "gp3"},
            unit="IOPS-Mo",
            suffix="gp3-iops",
        ),
        _row(
            "aws",
            "AmazonEC2",
            product_family="Provisioned Throughput",
            dimensions={"volumeApiName": "gp3"},
            unit="GiBps-mo",
            suffix="gp3-throughput",
        ),
        _row("aws", "AmazonRDS", suffix="rds"),
        _row(
            "aws",
            "AWSComputeSavingsPlan",
            term="SavingsPlan",
            suffix="sp",
        ),
        _row(
            "azure",
            "Virtual Machines",
            term="Consumption",
            unit="1 Hour",
            suffix="vm",
        ),
        _row(
            "azure",
            "Virtual Machines",
            term="SavingsPlan",
            unit="1 Hour",
            suffix="vm-sp",
        ),
        _row(
            "azure",
            "Storage",
            term="Consumption",
            dimensions={
                "productName": "Azure Premium SSD v2",
                "meterName": "Premium LRS Provisioned Capacity",
            },
            suffix="capacity",
        ),
        _row(
            "azure",
            "Storage",
            term="Consumption",
            dimensions={
                "productName": "Azure Premium SSD v2",
                "meterName": "Premium LRS Provisioned IOPS",
            },
            suffix="iops",
        ),
        _row(
            "azure",
            "Storage",
            term="Consumption",
            dimensions={
                "productName": "Azure Premium SSD v2",
                "meterName": "Premium LRS Provisioned Throughput",
            },
            suffix="throughput",
        ),
        _row("azure", "Azure Database for PostgreSQL", suffix="pg"),
        _row("azure", "Bandwidth", region="", suffix="bandwidth"),
        _row("azure", "Load Balancer", region="", suffix="lb"),
    ]


def _stage(run_dir: Path, snapshot_id: str, rows: list[dict] | None = None) -> None:
    source = run_dir / "sources" / "all.ndjson"
    write_ndjson(source, rows or _complete_rows())
    build_staged_snapshot(
        snapshot_id=snapshot_id,
        captured_at="2026-01-01T00:00:00+00:00",
        azure_region="eastus2",
        aws_region="us-east-1",
        source_paths=[source],
        run_dir=run_dir,
        collector_version="test",
    )


def _approval_record(
    validation: dict,
    sku_map: Path,
    *,
    approver_id: str = "sample-approver-id",
    reviewer_id: str = "sample-reviewer-id",
    extra: dict | None = None,
) -> dict:
    payload = {
        "snapshotId": validation["snapshotId"],
        "contentHash": validation["contentHash"],
        "approverId": approver_id,
        "approverDisplayName": "Sample Approver",
        "approverRole": "SnapshotApprover",
        "approvedAt": "2026-01-01T01:00:00+00:00",
        "skuMapReviewerId": reviewer_id,
        "skuMapReviewerDisplayName": "Sample Reviewer",
        "skuMapReviewerRole": "SkuMapReviewer",
        "skuMapDigest": hashlib.sha256(sku_map.read_bytes()).hexdigest(),
        "coverageMatrixDigest": validation["coverageMatrixDigest"],
        "scope": validation["scope"],
        "stageManifestDigest": validation["stageManifestDigest"],
        "algorithm": "HMAC-SHA256",
        **(extra or {}),
    }
    payload["signature"] = hmac.new(
        APPROVAL_KEY.encode(),
        canonical_json(payload).encode(),
        hashlib.sha256,
    ).hexdigest()
    return payload


def _approve(run_dir: Path, tmp_path: Path, validation: dict) -> None:
    sku_map = tmp_path / f"{run_dir.name}-skumap.json"
    sku_map.write_text('{"version":1}\n', encoding="utf-8")
    approval_path = tmp_path / f"{run_dir.name}-approval.json"
    approval_path.write_text(
        canonical_json(_approval_record(validation, sku_map)),
        encoding="utf-8",
    )
    approve_snapshot(
        run_dir,
        approval_record_path=approval_path,
        sku_map_path=sku_map,
        approval_key=APPROVAL_KEY,
    )


def _write_published(path: Path, rows: list[dict]) -> None:
    sorted_rows = sorted(rows, key=lambda row: row["rowId"])
    digest = hashlib.sha256()
    lines = []
    for row in sorted_rows:
        line = f"{canonical_json(row)}\n".encode()
        digest.update(line)
        lines.append(line)
    stage_manifest = {
        "snapshotId": "snapshot-1",
        "contentHash": digest.hexdigest(),
        "pricedAsOf": "2026-01-01",
        "schemaVersion": "public-pricebook-v1",
        "validationStatus": "Validated",
        "publishingHuman": None,
        "skuMapReviewer": None,
        "skuMapDigest": None,
        "rowCount": len(rows),
        "coverageMatrixDigest": "coverage-digest",
        "scope": {"azureRegion": "eastus2", "awsRegion": "us-east-1"},
    }
    stage_manifest_digest = hashlib.sha256(
        canonical_json(stage_manifest).encode()
    ).hexdigest()
    approval = {
        "snapshotId": stage_manifest["snapshotId"],
        "contentHash": stage_manifest["contentHash"],
        "approverId": "sample-approver-id",
        "approverDisplayName": "Sample Approver",
        "approverRole": "SnapshotApprover",
        "approvedAt": "2026-01-01T01:00:00+00:00",
        "skuMapReviewerId": "sample-reviewer-id",
        "skuMapReviewerDisplayName": "Sample Reviewer",
        "skuMapReviewerRole": "SkuMapReviewer",
        "skuMapDigest": "skumap-digest",
        "coverageMatrixDigest": stage_manifest["coverageMatrixDigest"],
        "scope": stage_manifest["scope"],
        "stageManifestDigest": stage_manifest_digest,
        "algorithm": "HMAC-SHA256",
    }
    approval["signature"] = hmac.new(
        APPROVAL_KEY.encode(),
        canonical_json(approval).encode(),
        hashlib.sha256,
    ).hexdigest()
    manifest = {
        **stage_manifest,
        "validationStatus": "Published",
        "publishingHuman": approval["approverDisplayName"],
        "publishingHumanId": approval["approverId"],
        "publishingHumanRole": approval["approverRole"],
        "skuMapReviewer": approval["skuMapReviewerDisplayName"],
        "skuMapReviewerId": approval["skuMapReviewerId"],
        "skuMapReviewerRole": approval["skuMapReviewerRole"],
        "skuMapDigest": approval["skuMapDigest"],
        "approvedAt": approval["approvedAt"],
        "approvalAlgorithm": approval["algorithm"],
        "approvalSignature": approval["signature"],
        "stageManifestDigest": stage_manifest_digest,
    }
    with path.open("wb") as handle:
        handle.write(
            f"{canonical_json({'recordType': 'manifest', 'manifest': manifest})}\n".encode()
        )
        for line in lines:
            handle.write(line)


def test_http_allowlist_rejects_arbitrary_hosts() -> None:
    with pytest.raises(HarvestError, match="allowlist"):
        SafeHttpClient.validate_url("https://example.com/prices.json")
    with pytest.raises(HarvestError, match="allowlist"):
        SafeHttpClient.validate_url("http://prices.azure.com/api/retail/prices")


def test_http_retries_503_and_azure_preserves_exact_decimal(monkeypatch) -> None:
    attempts = 0
    monkeypatch.setattr("src.harvester.http.time.sleep", lambda _: None)
    body = (
        '{"Items":[{"currencyCode":"USD","retailPrice":'
        "0.1234567890123456789012345678,"
        '"armRegionName":"eastus2","meterId":"m1","skuId":"s1",'
        '"serviceName":"Virtual Machines","unitOfMeasure":"1 Hour",'
        '"type":"Consumption","effectiveStartDate":"2026-01-01"}],'
        '"NextPageLink":null}'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(503)
        return httpx.Response(200, content=body.encode())

    rows = list(
        collect_azure_rows(
            _client(handler, retries=3),
            region="eastus2",
            services=("Virtual Machines",),
        )
    )
    assert attempts == 3
    assert rows[0]["price"] == "0.1234567890123456789012345678"


def test_aws_region_index_resolution() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "publicationDate": "2026-01-01",
                "regions": {
                    "us-east-1": {
                        "currentVersionUrl": "/offers/v1.0/aws/AmazonEC2/v/us-east-1/index.json"
                    }
                },
            },
        )

    url, publication = _resolve_offer_url(_client(handler), "AmazonEC2", "us-east-1")
    assert url.startswith("https://pricing.us-east-1.amazonaws.com/")
    assert publication == "2026-01-01"


def test_aws_terms_retain_all_gp3_product_families(tmp_path: Path) -> None:
    catalog = {
        "products": {
            sku: {
                "productFamily": family,
                "attributes": {"volumeApiName": "gp3"},
            }
            for sku, family in {
                "capacity": "Storage",
                "iops": "System Operation",
                "throughput": "Provisioned Throughput",
            }.items()
        },
        "terms": {
            "OnDemand": {
                sku: {
                    f"{sku}.term": {
                        "effectiveDate": "2026-01-01",
                        "priceDimensions": {
                            f"{sku}.dimension": {
                                "unit": "GB-Mo",
                                "pricePerUnit": {"USD": "0.08"},
                            }
                        },
                        "termAttributes": {},
                    }
                }
                for sku in ("capacity", "iops", "throughput")
            },
            "Reserved": {},
        },
    }
    raw = tmp_path / "catalog.json"
    raw.write_text(json.dumps(catalog), encoding="utf-8")
    database = tmp_path / "products.sqlite"
    _index_products(raw, database)
    rows = list(
        _iter_offer_terms(
            raw,
            database,
            offer="AmazonEC2",
            region="us-east-1",
            source_url="https://pricing.us-east-1.amazonaws.com/catalog",
            publication_date="2026-01-01",
        )
    )
    assert {row["productFamily"] for row in rows} == {
        "Storage",
        "System Operation",
        "Provisioned Throughput",
    }


def test_savings_plan_stream_parser(tmp_path: Path) -> None:
    raw = tmp_path / "savings.json"
    raw.write_text(
        json.dumps(
            {
                "products": [
                    {
                        "sku": "sp1",
                        "productFamily": "ComputeSavingsPlans",
                        "attributes": {"purchaseOption": "All Upfront"},
                    }
                ],
                "terms": {
                    "savingsPlan": [
                        {
                            "sku": "sp1",
                            "description": "3 year All Upfront Compute Savings Plan",
                            "effectiveDate": "2026-01-01",
                            "leaseContractLength": {"duration": 3, "unit": "year"},
                            "rates": [
                                {
                                    "discountedSku": "ec2sku",
                                    "discountedRate": {
                                        "price": "0.088360",
                                        "currency": "USD",
                                    },
                                    "rateCode": "sp1.ec2sku",
                                    "unit": "Hrs",
                                }
                            ],
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    database = tmp_path / "savings.sqlite"
    _index_savings_plan_products(raw, database)
    rows = list(
        _iter_savings_plan_rows(
            raw,
            database,
            region="us-east-1",
            source_url="https://pricing.us-east-1.amazonaws.com/savings.json",
            publication_date="2026-01-01",
        )
    )
    assert rows[0]["price"] == "0.08836"
    assert rows[0]["term"] == "SavingsPlan"


@pytest.mark.parametrize("price", ["NaN", "Infinity", "-5", "not-a-price"])
def test_invalid_prices_fail_at_staging(tmp_path: Path, price: str) -> None:
    rows = _complete_rows()
    rows[0] = {**rows[0], "price": price}
    run_dir = tmp_path / "run"
    source = run_dir / "sources" / "bad.ndjson"
    write_ndjson(source, rows)
    with pytest.raises(ValidationError, match="Price|decimal"):
        build_staged_snapshot(
            snapshot_id="snapshot-1",
            captured_at="2026-01-01T00:00:00+00:00",
            azure_region="eastus2",
            aws_region="us-east-1",
            source_paths=[source],
            run_dir=run_dir,
            collector_version="test",
        )


def test_signed_approval_publication_and_artifact_verification(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _stage(run_dir, "snapshot-1")
    validation = validate_snapshot(
        run_dir, previous_artifact=None, bootstrap=True
    )
    _approve(run_dir, tmp_path, validation)
    store = tmp_path / "published"
    pointer = publish_snapshot(
        run_dir,
        store_dir=store,
        expected_pointer_hash=None,
        approval_key=APPROVAL_KEY,
    )
    artifact = store / pointer["artifact"]
    manifest = verify_published_artifact(artifact, approval_key=APPROVAL_KEY)
    assert manifest["publishingHuman"] == "Sample Approver"
    data = artifact.read_bytes()
    artifact.write_bytes(data[:-2] + b"9\n")
    with pytest.raises(ValidationError, match="Published artifact|hash|row"):
        verify_published_artifact(artifact, approval_key=APPROVAL_KEY)


def test_manifest_approval_metadata_tampering_is_rejected(tmp_path: Path) -> None:
    artifact = tmp_path / "snapshot.pricebook.ndjson"
    _write_published(artifact, [_row("aws", "AmazonEC2")])
    with artifact.open("rb") as handle:
        header = json.loads(handle.readline())
        rows = handle.read()
    header["manifest"]["publishingHuman"] = "Mallory"
    with artifact.open("wb") as handle:
        handle.write(f"{canonical_json(header)}\n".encode())
        handle.write(rows)
    with pytest.raises((PublicationError, ValidationError), match="signature|stage digest"):
        verify_published_artifact(artifact, approval_key=APPROVAL_KEY)


def test_post_approval_content_swap_is_rejected(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _stage(run_dir, "snapshot-1")
    validation = validate_snapshot(run_dir, previous_artifact=None, bootstrap=True)
    _approve(run_dir, tmp_path, validation)
    rows_path = run_dir / "canonical-rows.ndjson"
    rows = [json.loads(line) for line in rows_path.read_text(encoding="utf-8").splitlines()]
    rows[0]["price"] = "999"
    write_ndjson(rows_path, rows)
    stage_manifest = json.loads(
        (run_dir / "stage-manifest.json").read_text(encoding="utf-8")
    )
    digest = hashlib.sha256(rows_path.read_bytes()).hexdigest()
    stage_manifest["contentHash"] = digest
    (run_dir / "stage-manifest.json").write_text(
        canonical_json(stage_manifest) + "\n", encoding="utf-8"
    )
    with pytest.raises(PublicationError, match="does not match|changed"):
        publish_snapshot(
            run_dir,
            store_dir=tmp_path / "published",
            expected_pointer_hash=None,
            approval_key=APPROVAL_KEY,
        )


def test_pointer_lock_failure_does_not_leave_orphan_artifact(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _stage(run_dir, "snapshot-1")
    validation = validate_snapshot(run_dir, previous_artifact=None, bootstrap=True)
    _approve(run_dir, tmp_path, validation)
    store = tmp_path / "published"
    store.mkdir()
    (store / ".publish.lock").write_text("held", encoding="utf-8")
    with pytest.raises(PublicationError, match="lock"):
        publish_snapshot(
            run_dir,
            store_dir=store,
            expected_pointer_hash=None,
            approval_key=APPROVAL_KEY,
        )
    assert not (store / "snapshot-1.pricebook.ndjson").exists()


def test_missing_commitment_coverage_blocks_validation(tmp_path: Path) -> None:
    rows = [
        row
        for row in _complete_rows()
        if row["serviceCode"] != "AWSComputeSavingsPlan"
    ]
    run_dir = tmp_path / "run"
    _stage(run_dir, "snapshot-1", rows)
    with pytest.raises(ValidationError, match="aws-compute-savings-plan"):
        validate_snapshot(run_dir, previous_artifact=None, bootstrap=True)


def test_unapproved_regions_block_validation(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    source = run_dir / "sources" / "all.ndjson"
    write_ndjson(source, _complete_rows())
    build_staged_snapshot(
        snapshot_id="snapshot-1",
        captured_at="2026-01-01T00:00:00+00:00",
        azure_region="westus2",
        aws_region="eu-west-1",
        source_paths=[source],
        run_dir=run_dir,
        collector_version="test",
    )
    with pytest.raises(ValidationError, match="region"):
        validate_snapshot(run_dir, previous_artifact=None, bootstrap=True)


def test_approval_allows_one_signed_principal_in_both_roles(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _stage(run_dir, "snapshot-1")
    validation = validate_snapshot(run_dir, previous_artifact=None, bootstrap=True)
    sku_map = tmp_path / "skumap.json"
    sku_map.write_text("{}\n", encoding="utf-8")
    record = _approval_record(
        validation,
        sku_map,
        approver_id="same-id",
        reviewer_id="same-id",
    )
    approval_path = tmp_path / "approval.json"
    approval_path.write_text(canonical_json(record), encoding="utf-8")
    # Two-person maker-checker is deferred to v2; both roles are still named in the signed record.
    approve_snapshot(
        run_dir,
        approval_record_path=approval_path,
        sku_map_path=sku_map,
        approval_key=APPROVAL_KEY,
    )


def test_refresh_validation_binds_current_pointer(tmp_path: Path) -> None:
    first = tmp_path / "first"
    _stage(first, "snapshot-1")
    first_validation = validate_snapshot(
        first, previous_artifact=None, bootstrap=True
    )
    _approve(first, tmp_path, first_validation)
    store = tmp_path / "published"
    first_pointer = publish_snapshot(
        first,
        store_dir=store,
        expected_pointer_hash=None,
        approval_key=APPROVAL_KEY,
    )
    second = tmp_path / "second"
    _stage(second, "snapshot-2")
    previous = store / first_pointer["artifact"]
    validation = validate_snapshot(
        second,
        previous_artifact=previous,
        current_pointer=store / "current.json",
        bootstrap=False,
        approval_key=APPROVAL_KEY,
    )
    assert validation["comparison"]["changed"] == 0
    assert validation["baselineSnapshotId"] == "snapshot-1"


def test_concurrent_bootstrap_publishers_cannot_both_succeed(tmp_path: Path) -> None:
    store = tmp_path / "published"
    runs = []
    for snapshot_id in ("snapshot-a", "snapshot-b"):
        run_dir = tmp_path / snapshot_id
        _stage(run_dir, snapshot_id)
        validation = validate_snapshot(
            run_dir, previous_artifact=None, bootstrap=True
        )
        _approve(run_dir, tmp_path, validation)
        runs.append(run_dir)

    def publish(run_dir: Path) -> str:
        try:
            publish_snapshot(
                run_dir,
                store_dir=store,
                expected_pointer_hash=None,
                approval_key=APPROVAL_KEY,
            )
            return "success"
        except PublicationError:
            return "failed"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(publish, runs))
    assert sorted(results) == ["failed", "success"]


def test_snapshot_id_rejects_path_traversal(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="Snapshot ID"):
        _stage(tmp_path / "run", "..\\escape")


def test_reconciliation_exact_terms_units_and_two_sided_sensitivity(
    tmp_path: Path,
) -> None:
    rows = [
        _row(
            "aws",
            "AmazonEC2",
            price="0.192",
            dimensions={
                "instanceType": "m6i.xlarge",
                "operatingSystem": "Linux",
                "operation": "RunInstances",
                "tenancy": "Shared",
                "preInstalledSw": "NA",
                "capacitystatus": "Used",
                "licenseModel": "No License required",
            },
            suffix="vm",
        ),
        _row(
            "azure",
            "Virtual Machines",
            price="0.192",
            term="Consumption",
            unit="1 Hour",
            dimensions={
                "armSkuName": "Standard_D4s_v5",
                "meterName": "D4s v5",
                "skuName": "Standard_D4s_v5",
                "productName": "Virtual Machines Dsv5 Series",
            },
            suffix="vm",
        ),
        _row(
            "aws",
            "AWSComputeSavingsPlan",
            price="0.08836",
            term="SavingsPlan",
            product_family="ComputeSavingsPlans",
            dimensions={
                "purchaseOption": "All Upfront",
                "purchaseTerm": "3yr",
                "discountedInstanceType": "m6i.xlarge",
                "discountedUsageType": "BoxUsage:m6i.xlarge",
                "discountedOperation": "RunInstances",
            },
            suffix="commit",
        ),
        _row(
            "azure",
            "Virtual Machines",
            price="0.09024",
            term="SavingsPlan",
            unit="1 Hour",
            dimensions={
                "armSkuName": "Standard_D4s_v5",
                "meterName": "D4s v5",
                "skuName": "Standard_D4s_v5",
                "savingsPlanTerm": "3 Years",
                "productName": "Virtual Machines Dsv5 Series",
            },
            suffix="commit",
        ),
        _row(
            "aws",
            "AmazonEC2",
            price="0.08",
            product_family="Storage",
            unit="GB-Mo",
            dimensions={"volumeApiName": "gp3"},
            suffix="storage",
        ),
        _row(
            "azure",
            "Storage",
            price="0.00011",
            term="Consumption",
            unit="1 GiB/Hour",
            dimensions={
                "productName": "Azure Premium SSD v2",
                "meterName": "Premium LRS Provisioned Capacity",
                "skuName": "Premium LRS",
            },
            suffix="storage",
        ),
    ]
    artifact = tmp_path / "snapshot.pricebook.ndjson"
    _write_published(artifact, rows)
    report = reconcile_representative_application(
        artifact, approval_key=APPROVAL_KEY
    )
    assert report["commercialViews"]["List"]["awsMonthly"] == "156.160000"
    assert report["commercialViews"]["List"]["azureMonthly"] == "156.220000"
    assert report["commercialViews"]["Committed"]["awsMonthly"] == "80.502800"
    assert report["approvedCommittedDefault"]["payment"] == "All Upfront"
    assert (
        report["commercialViews"]["Enterprise"]["twoSidedSensitivity"][
            "providerNeedingAdditionalDiscount"
        ]
        == "azure"
    )
    duplicate_identity = {
        **row_identity(rows[0]),
        "sku": "duplicate-aws-vm",
        "meter": "duplicate-aws-vm",
    }
    duplicate = {
        **rows[0],
        **duplicate_identity,
        "rowId": sha256_text(canonical_json(duplicate_identity)),
    }
    artifact = tmp_path / "ambiguous.pricebook.ndjson"
    _write_published(artifact, [*rows, duplicate])
    with pytest.raises(ValidationError, match="matched 2"):
        reconcile_representative_application(
            artifact, approval_key=APPROVAL_KEY
        )


def test_optional_extract_and_key_bindings_survive_publication(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _stage(run_dir, "snapshot-1")
    validation = validate_snapshot(run_dir, previous_artifact=None, bootstrap=True)
    sku_map = tmp_path / "skumap.json"
    sku_map.write_text('{"version":1}\n', encoding="utf-8")
    extract = {"manifest": {"snapshotId": "extract-snapshot-1"}, "rates": {"x": "1.0"}}
    (run_dir / "rate-extract.json").write_text(json.dumps(extract), encoding="utf-8")
    extract_digest = hashlib.sha256(canonical_json(extract).encode("utf-8")).hexdigest()
    extra = {
        "extractDigest": extract_digest,
        "keyId": "https://kv.example/keys/approval/1",
        "runId": "c" * 32,
        "evidenceDigest": "e" * 64,
    }
    approval_path = tmp_path / "approval.json"
    approval_path.write_text(
        canonical_json(_approval_record(validation, sku_map, extra=extra)), encoding="utf-8"
    )
    approve_snapshot(
        run_dir, approval_record_path=approval_path, sku_map_path=sku_map,
        approval_key=APPROVAL_KEY,
    )
    store = tmp_path / "published"
    pointer = publish_snapshot(
        run_dir, store_dir=store, expected_pointer_hash=None, approval_key=APPROVAL_KEY
    )
    artifact = store / pointer["artifact"]
    manifest = verify_published_artifact(artifact, approval_key=APPROVAL_KEY)
    assert manifest["approvedExtractDigest"] == extract_digest
    assert manifest["approvalKeyId"] == extra["keyId"]
    assert manifest["approvedStagedRunId"] == "c" * 32
    assert manifest["approvedEvidenceDigest"] == "e" * 64
    with artifact.open("rb") as handle:
        header = json.loads(handle.readline())
        rows = handle.read()
    header["manifest"]["approvedExtractDigest"] = "b" * 64
    with artifact.open("wb") as handle:
        handle.write(f"{canonical_json(header)}\n".encode())
        handle.write(rows)
    with pytest.raises(PublicationError, match="signature"):
        verify_published_artifact(artifact, approval_key=APPROVAL_KEY)


@pytest.mark.parametrize(
    "extra, message",
    [
        ({"extractDigest": "not-a-digest"}, "extract digest"),
        ({"keyId": " "}, "key ID"),
        ({"runId": "../x"}, "staged run ID"),
        ({"evidenceDigest": "E" * 64}, "evidence digest"),
        ({"keyId": "https://kv.example/keys/approval/1", "extractDigest": "a" * 64}, "keyed approval"),
    ],
)
def test_optional_approval_bindings_are_validated(
    tmp_path: Path, extra: dict, message: str
) -> None:
    run_dir = tmp_path / "run"
    _stage(run_dir, "snapshot-1")
    validation = validate_snapshot(run_dir, previous_artifact=None, bootstrap=True)
    sku_map = tmp_path / "skumap.json"
    sku_map.write_text('{"version":1}\n', encoding="utf-8")
    approval_path = tmp_path / "approval.json"
    approval_path.write_text(
        canonical_json(_approval_record(validation, sku_map, extra=extra)), encoding="utf-8"
    )
    with pytest.raises(PublicationError, match=message):
        approve_snapshot(
            run_dir, approval_record_path=approval_path, sku_map_path=sku_map,
            approval_key=APPROVAL_KEY,
        )


def test_publish_refuses_an_extract_swapped_after_approval(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _stage(run_dir, "snapshot-1")
    validation = validate_snapshot(run_dir, previous_artifact=None, bootstrap=True)
    sku_map = tmp_path / "skumap.json"
    sku_map.write_text('{"version":1}\n', encoding="utf-8")
    extract = {"manifest": {"snapshotId": "extract-snapshot-1"}, "rates": {"x": "1.0"}}
    (run_dir / "rate-extract.json").write_text(json.dumps(extract), encoding="utf-8")
    extra = {
        "extractDigest": hashlib.sha256(canonical_json(extract).encode("utf-8")).hexdigest(),
        "keyId": "https://kv.example/keys/approval/1",
        "runId": "c" * 32,
        "evidenceDigest": "e" * 64,
    }
    approval_path = tmp_path / "approval.json"
    approval_path.write_text(
        canonical_json(_approval_record(validation, sku_map, extra=extra)), encoding="utf-8"
    )
    approve_snapshot(
        run_dir, approval_record_path=approval_path, sku_map_path=sku_map,
        approval_key=APPROVAL_KEY,
    )
    extract["rates"]["x"] = "0.5"
    (run_dir / "rate-extract.json").write_text(json.dumps(extract), encoding="utf-8")
    with pytest.raises(PublicationError, match="approved extract"):
        publish_snapshot(
            run_dir, store_dir=tmp_path / "published", expected_pointer_hash=None,
            approval_key=APPROVAL_KEY,
        )
    (run_dir / "rate-extract.json").unlink()
    with pytest.raises(PublicationError, match="approved extract"):
        publish_snapshot(
            run_dir, store_dir=tmp_path / "published", expected_pointer_hash=None,
            approval_key=APPROVAL_KEY,
        )
