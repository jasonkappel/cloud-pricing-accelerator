from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from src.harvester import derive
from src.harvester.core import HarvestError, ValidationError, canonical_json
from src.harvester.main import _run, build_parser
from src.harvester.snapshot import validate_snapshot
from src.harvester.tests.test_harvester import _row, _stage


REPO = Path(__file__).resolve().parents[3]
FIXTURE = Path(__file__).with_name("fixtures") / "trust-20260922e-extract-rows.json"
SEED = REPO / "samples" / "pricebook_seed_v1.json"
APPROVAL = REPO / "samples" / "pricebook_extract_approval_v1.json"
ARTIFACT = REPO / "src" / "harvester" / "data" / "published" / "trust-20260922e.pricebook.ndjson"


def _fixture() -> tuple[list[dict], dict]:
    value = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return value["rows"], value["source"]


def _spec() -> dict:
    return derive.load_spec()


def _seed() -> dict:
    return json.loads(SEED.read_text(encoding="utf-8"))


def test_real_rows_reproduce_the_approved_demo_extract() -> None:
    rows, source = _fixture()
    extract, report = derive.derive_rate_extract(rows, source=source, spec=_spec())
    approval = json.loads(APPROVAL.read_text(encoding="utf-8"))
    assert extract == _seed()
    assert report["extractDigest"] == derive.extract_digest(_seed())
    assert approval["extractContentDigest"] == report["extractDigest"]
    assert report["rateCount"] == 28 and report["assumedRateCount"] == 2
    assert report["selections"]["aws.vm.rhel.4x16.hour_uplift"]["exact"] == "0.0576"
    reservation = report["selections"]["aws.vm.linux.4x16.hour.reservation"]
    assert reservation["value"] == "0.079604261796"
    assert set(reservation["rows"]) == {"upfront", "recurring"}


def test_row_order_does_not_change_the_extract() -> None:
    rows, source = _fixture()
    first, _ = derive.derive_rate_extract(rows, source=source, spec=_spec())
    second, _ = derive.derive_rate_extract(list(reversed(rows)), source=source, spec=_spec())
    assert derive.extract_digest(first) == derive.extract_digest(second)


def test_missing_row_fails_closed_and_names_every_selector() -> None:
    rows, source = _fixture()
    report = derive.derive_rate_extract(rows, source=source, spec=_spec())[1]
    missing = {
        report["selections"]["aws.block.iops_month"]["rows"]["row"]["rowId"],
        report["selections"]["azure.vm.linux.4x16.hour"]["rows"]["row"]["rowId"],
    }
    with pytest.raises(ValidationError) as error:
        derive.derive_rate_extract(
            [row for row in rows if row["rowId"] not in missing], source=source, spec=_spec()
        )
    assert "aws.block.iops_month/row matched 0 rows" in str(error.value)
    assert "azure.vm.linux.4x16.hour/row matched 0 rows" in str(error.value)


def test_duplicate_match_fails_closed_instead_of_choosing() -> None:
    rows, source = _fixture()
    report = derive.derive_rate_extract(rows, source=source, spec=_spec())[1]
    chosen = report["selections"]["aws.vm.linux.4x16.hour"]["rows"]["row"]["rowId"]
    twin = copy.deepcopy(next(row for row in rows if row["rowId"] == chosen))
    twin["rowId"] = "0" * 64
    twin["price"] = "0.0001"
    with pytest.raises(ValidationError, match="aws.vm.linux.4x16.hour/row matched 2 rows"):
        derive.derive_rate_extract([*rows, twin], source=source, spec=_spec())


def test_tier_selector_is_what_excludes_the_free_baseline_tier() -> None:
    rows, source = _fixture()
    spec = _spec()
    del spec["rates"]["azure.block.iops_month"]["selectors"]["row"]["dimensions"]["tierMinimumUnits"]
    with pytest.raises(ValidationError, match="azure.block.iops_month/row matched 2 rows"):
        derive.derive_rate_extract(rows, source=source, spec=spec)


def test_rows_outside_the_scope_region_never_match() -> None:
    rows, source = _fixture()
    moved = [
        {**row, "region": "westus2"} if row["provider"] == "azure" else row for row in rows
    ]
    with pytest.raises(ValidationError, match="azure.vm.linux.4x16.hour/row matched 0 rows"):
        derive.derive_rate_extract(moved, source=source, spec=_spec())


def test_scope_mismatch_fails_before_matching() -> None:
    rows, source = _fixture()
    with pytest.raises(ValidationError, match="scope"):
        derive.derive_rate_extract(
            rows,
            source={**source, "scope": {"azureRegion": "westus2", "awsRegion": "us-east-1"}},
            spec=_spec(),
        )


def test_negative_or_invalid_prices_fail_closed() -> None:
    rows, source = _fixture()
    report = derive.derive_rate_extract(rows, source=source, spec=_spec())[1]
    linux = report["selections"]["aws.vm.rhel.4x16.hour_uplift"]["rows"]["base"]["rowId"]
    swapped = [{**row, "price": "9.0"} if row["rowId"] == linux else row for row in rows]
    with pytest.raises(ValidationError, match="derived rate is negative"):
        derive.derive_rate_extract(swapped, source=source, spec=_spec())
    for bad in ("-1", "NaN", "abc"):
        broken = [{**row, "price": bad} if row["rowId"] == linux else row for row in rows]
        with pytest.raises(ValidationError, match="price"):
            derive.derive_rate_extract(broken, source=source, spec=_spec())


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda s: s.update(specVersion="v0"), "specVersion"),
        (lambda s: s["rates"]["aws.block.iops_month"]["formula"].update(type="cheapest"), "formula"),
        (lambda s: s["rates"]["aws.block.iops_month"]["record"].update(row_id="nope"), "undefined"),
        (lambda s: s["rates"]["aws.block.iops_month"]["selectors"]["row"].update(dimensions={}), "explicit"),
        (lambda s: s["rates"]["aws.block.iops_month"]["selectors"]["row"].update(region="x"), "explicit"),
        (lambda s: s["rates"]["aws.block.iops_month"]["selectors"]["row"].update(provider="azure"), "provider"),
        (lambda s: s["rates"]["aws.block.throughput_mbps_month"]["formula"].update(divide="0"), "positive"),
        (lambda s: s["rates"]["aws.block.iops_month"].update(places=40), "places"),
        (
            lambda s: s["assumedRates"]["azure.vm.rhel.4x16.hour_uplift"]["record"].update(
                source_type="PublishedSnapshot"
            ),
            "DemoAssumption",
        ),
        (lambda s: s["assumedRates"].update({"aws.block.iops_month": {}}), "both"),
        (lambda s: s["rates"]["aws.block.throughput_mbps_month"].update(
            formula={"type": "scaled", "row": "row", "divde": "1024"}), "fields"),
        (lambda s: s["rates"]["aws.block.throughput_mbps_month"].update(
            formula={"type": "scaled", "row": "row"}), "multiply or divide"),
        (lambda s: s["rates"]["azure.block.iops_month"]["formula"].update(multiply=730), "strings"),
        (lambda s: s["rates"]["azure.block.iops_month"]["formula"].update(multiply=True), "strings"),
        (lambda s: s["rates"]["azure.block.iops_month"]["formula"].update(multiply="NaN"), "non-decimal"),
        (lambda s: s["rates"]["aws.block.iops_month"]["selectors"]["row"]["dimensions"].update(
            usagetype=None), "explicit"),
        (lambda s: s["rates"]["aws.block.iops_month"]["selectors"]["row"]["dimensions"].update(
            usagetype=1), "explicit"),
    ]
    + [
        (lambda s, bad=bad: s["assumedRates"]["azure.vm.rhel.4x16.hour_uplift"].update(rate=bad),
         "non-negative")
        for bad in ("-1", "NaN", "Infinity", "abc")
    ],
)
def test_invalid_spec_is_rejected(mutate, message: str) -> None:
    rows, source = _fixture()
    spec = _spec()
    mutate(spec)
    with pytest.raises(ValidationError, match=message):
        derive.derive_rate_extract(rows, source=source, spec=spec)


def test_diff_reports_old_new_change_and_percent_in_decimal() -> None:
    previous = _seed()
    current = copy.deepcopy(previous)
    current["rates"]["aws.vm.linux.4x16.hour"] = "0.221760"
    current["rates"]["azure.block.iops_month"] = "0.005110"
    previous["rates"]["azure.pg.new_rate"] = "0"
    current["rates"]["azure.pg.new_rate"] = "0.5"
    del current["rates"]["aws.block.iops_month"]
    current["rates"]["aws.block.added"] = "1"
    diff = derive.diff_extracts(previous, current)
    by_key = {entry["rateKey"]: entry for entry in diff["rates"]}
    assert by_key["aws.vm.linux.4x16.hour"] == {
        "rateKey": "aws.vm.linux.4x16.hour",
        "assumed": False,
        "old": "0.201600",
        "new": "0.221760",
        "change": "0.020160",
        "percentChange": "10.0000",
        "status": "changed",
    }
    assert by_key["azure.block.iops_month"]["status"] == "unchanged"
    assert by_key["azure.block.iops_month"]["percentChange"] == "0.0000"
    assert by_key["azure.pg.new_rate"]["percentChange"] is None
    assert by_key["aws.block.iops_month"]["status"] == "removed"
    assert by_key["aws.block.added"]["status"] == "added"
    assert by_key["azure.vm.rhel.4x16.hour_uplift"]["assumed"] is True
    assert (diff["changedCount"], diff["addedCount"], diff["removedCount"]) == (2, 1, 1)
    assert diff["baselineDigest"] == derive.extract_digest(previous)


def test_diff_rejects_non_decimal_rates() -> None:
    for bad in (1.5, "NaN", "-1"):
        with pytest.raises(ValidationError, match="decimal"):
            derive.diff_extracts({"rates": {"a": "1.0"}}, {"rates": {"a": bad}})


def test_non_usd_rows_are_never_candidates() -> None:
    rows, source = _fixture()
    report = derive.derive_rate_extract(rows, source=source, spec=_spec())[1]
    chosen = report["selections"]["azure.vm.linux.4x16.hour"]["rows"]["row"]["rowId"]
    eur = [{**row, "currency": "EUR"} if row["rowId"] == chosen else row for row in rows]
    with pytest.raises(ValidationError, match="azure.vm.linux.4x16.hour/row matched 0 rows"):
        derive.derive_rate_extract(eur, source=source, spec=_spec())


def test_boolean_row_dimension_never_matches_a_string_selector() -> None:
    rows, source = _fixture()
    report = derive.derive_rate_extract(rows, source=source, spec=_spec())[1]
    chosen = report["selections"]["aws.block.iops_month"]["rows"]["row"]["rowId"]
    typed = [
        {**row, "dimensions": {**row["dimensions"], "volumeApiName": True}}
        if row["rowId"] == chosen else row
        for row in rows
    ]
    with pytest.raises(ValidationError, match="aws.block.iops_month/row matched 0 rows"):
        derive.derive_rate_extract(typed, source=source, spec=_spec())


# A tiny spec over the synthetic staged rows from test_harvester, used for run-mode and Blob staging.
def tiny_spec(path: Path) -> Path:
    spec = {
        "specVersion": derive.SPEC_VERSION,
        "scope": {"azureRegion": "eastus2", "awsRegion": "us-east-1"},
        "extract": {
            "snapshotIdTemplate": "{sourceSnapshotId}-extract",
            "schemaVersion": "pilot-pricebook-v1",
            "nonProduction": True,
            "commercialView": "List",
            "sourceUrls": ["https://example.test/prices"],
            "noticeTemplate": "Derived from {sourceSnapshotId}.",
        },
        "rates": {
            "aws.block.capacity.gb_month": {
                "sourceType": "PublishedSnapshot",
                "selectors": {
                    "row": {
                        "provider": "aws",
                        "serviceCode": "AmazonEC2",
                        "productFamily": "Storage",
                        "term": "OnDemand",
                        "unit": "GB-Mo",
                        "dimensions": {"volumeApiName": "gp3"},
                    }
                },
                "formula": {"type": "price"},
                "places": 6,
                "record": {"sku": "row", "row_id": "row"},
                "note": "test",
            },
            "azure.block.iops_month": {
                "sourceType": "PublishedSnapshot",
                "selectors": {
                    "row": {
                        "provider": "azure",
                        "serviceCode": "Storage",
                        "term": "Consumption",
                        "unit": "Hrs",
                        "dimensions": {"meterName": "Premium LRS Provisioned IOPS"},
                    }
                },
                "formula": {"type": "scaled", "row": "row", "multiply": "730"},
                "places": 6,
                "record": {"row_id": "row"},
                "note": "test",
            },
        },
        "assumedRates": {},
    }
    path.write_text(json.dumps(spec), encoding="utf-8")
    return path


def derived_run(tmp_path: Path, rows: list[dict] | None = None) -> Path:
    run = tmp_path / "run"
    _stage(run, "test-snapshot", rows)
    validate_snapshot(run, previous_artifact=None, bootstrap=True)
    derive.derive_run(run, spec_path=tiny_spec(tmp_path / "spec.json"))
    return run


def test_run_mode_writes_a_validated_unpublished_extract(tmp_path: Path) -> None:
    run = derived_run(tmp_path)
    extract = json.loads((run / derive.EXTRACT_NAME).read_text(encoding="utf-8"))
    report = json.loads((run / derive.REPORT_NAME).read_text(encoding="utf-8"))
    manifest = json.loads((run / "stage-manifest.json").read_text(encoding="utf-8"))
    assert extract["rates"] == {
        "aws.block.capacity.gb_month": "1.250000",
        "azure.block.iops_month": "912.500000",
    }
    assert extract["manifest"]["validationStatus"] == "Validated"
    assert extract["manifest"]["publishingHuman"] is None
    assert extract["manifest"]["sourceContentHash"] == manifest["contentHash"]
    assert extract["manifest"]["pricedAsOf"] == "2026-01-01"
    assert report["extractDigest"] == derive.extract_digest(extract)
    assert report["diff"] is None


def test_run_mode_with_previous_extract_includes_diff(tmp_path: Path) -> None:
    run = derived_run(tmp_path)
    previous = tmp_path / "previous.json"
    previous.write_text(json.dumps({"rates": {"aws.block.capacity.gb_month": "1.000000"}}))
    derive.derive_run(
        run, spec_path=tiny_spec(tmp_path / "spec.json"), previous_extract=previous
    )
    diff = json.loads((run / derive.REPORT_NAME).read_text(encoding="utf-8"))["diff"]
    assert diff["changedCount"] == 1 and diff["addedCount"] == 1
    assert diff["rates"][0]["percentChange"] == "25.0000"


def test_run_mode_refuses_tampered_or_unvalidated_runs(tmp_path: Path) -> None:
    spec = tiny_spec(tmp_path / "spec.json")
    run = tmp_path / "run"
    _stage(run, "test-snapshot")
    with pytest.raises(ValidationError, match="Cannot read JSON"):
        derive.derive_run(run, spec_path=spec)
    validate_snapshot(run, previous_artifact=None, bootstrap=True)

    rows_path = run / "canonical-rows.ndjson"
    original = rows_path.read_bytes()
    rows_path.write_bytes(original.replace(b'"1.25"', b'"0.01"', 1))
    with pytest.raises(ValidationError, match="changed after validation"):
        derive.derive_run(run, spec_path=spec)
    rows_path.write_bytes(original)

    manifest_path = run / "stage-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_path.write_text(
        canonical_json({**manifest, "pricedAsOf": "2020-01-01"}), encoding="utf-8"
    )
    with pytest.raises(ValidationError, match="Validated run"):
        derive.derive_run(run, spec_path=spec)
    manifest_path.write_text(canonical_json(manifest), encoding="utf-8")

    validation_path = run / "validation.json"
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    validation_path.write_text(
        canonical_json({**validation, "validationStatus": "Failed"}), encoding="utf-8"
    )
    with pytest.raises(ValidationError, match="Validated run"):
        derive.derive_run(run, spec_path=spec)
    assert not (run / derive.EXTRACT_NAME).exists()


def test_run_mode_fails_closed_when_a_selector_is_ambiguous(tmp_path: Path) -> None:
    from src.harvester.tests.test_harvester import _complete_rows

    rows = _complete_rows()
    rows.append(
        _row(
            "aws", "AmazonEC2", product_family="Storage", dimensions={"volumeApiName": "gp3"},
            unit="GB-Mo", suffix="gp3-capacity-twin",
        )
    )
    with pytest.raises(ValidationError, match="matched 2 rows"):
        derived_run(tmp_path, rows)
    assert not (tmp_path / "run" / derive.EXTRACT_NAME).exists()


def test_cli_requires_exactly_one_source_and_matching_output(tmp_path: Path) -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["derive"])
    with pytest.raises(SystemExit):
        parser.parse_args(["derive", "--run-dir", "a", "--artifact", "b"])
    args = parser.parse_args(["derive", "--run-dir", "a", "--output-dir", "b"])
    with pytest.raises(HarvestError, match="--output-dir"):
        _run(args)
    args = parser.parse_args(["derive", "--artifact", "a"])
    with pytest.raises(HarvestError, match="--output-dir"):
        _run(args)
    assert parser.parse_args(["derive", "--run-dir", "a"]).spec == derive.SPEC_PATH


@pytest.mark.skipif(
    os.environ.get("HARVESTER_FULL_ARTIFACT_TEST") != "1"
    or not ARTIFACT.exists()
    or not os.environ.get("HARVESTER_APPROVAL_HMAC_KEY"),
    reason="Opt-in: set HARVESTER_FULL_ARTIFACT_TEST=1 with the local Published artifact and key.",
)
def test_full_published_artifact_reproduces_the_approved_extract(tmp_path: Path) -> None:
    extract, report = derive.derive_from_artifact(ARTIFACT, previous_extract=SEED)
    # The bundled seed renames the artifact's approver to "Sample Approver"; the rates must still match.
    seed = _seed()
    assert extract["rates"] == seed["rates"]
    assert {**extract["manifest"], "publishingHuman": None} == {**seed["manifest"], "publishingHuman": None}
    assert report["diff"]["unchangedCount"] == 30
    assert report["diff"]["changedCount"] == 0
