"""Derive the pricing engine's rate extract from a harvested snapshot.

Every SkuMap rate key is bound to rows by explicit equality selectors in rate-extract-spec.json. Each
selector must match exactly one row in the snapshot; zero or several matches fail closed. Values are
computed with decimal only, and the output carries the source snapshot identity so the approval step can
bind it. A per-rate diff against a previous extract (old, new, change, percent) is part of the report.
"""
from __future__ import annotations

import hashlib
import json
import os
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any, Callable, Iterable

from .core import ValidationError, canonical_json
from .snapshot import verify_published_artifact


SPEC_PATH = Path(__file__).with_name("rate-extract-spec.json")
EXTRACT_NAME = "rate-extract.json"
REPORT_NAME = "rate-extract-report.json"
SPEC_VERSION = "rate-extract-spec-v1"
PERCENT_PLACES = Decimal("0.0001")
_SELECTOR_FIELDS = {"provider", "serviceCode", "productFamily", "term", "unit", "dimensions"}
_RECORD_FIELDS = {"sku", "meter", "row_id", "row_ids"}
# Formula type -> (required fields, optional fields), excluding "type".
_FORMULA_FIELDS: dict[str, tuple[set[str], set[str]]] = {
    "price": (set(), {"row"}),
    "difference": ({"row", "minus"}, set()),
    "scaled": ({"row"}, {"multiply", "divide"}),
    "termNormalized": ({"upfront", "hours"}, {"recurringHourly"}),
}


def load_spec(path: Path = SPEC_PATH) -> dict[str, Any]:
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"Cannot read rate extract spec: {path}") from exc
    _check_spec(spec)
    return spec


def extract_digest(extract: dict[str, Any]) -> str:
    # Same canonical form the API uses to verify the extract approval digest.
    return hashlib.sha256(canonical_json(extract).encode("utf-8")).hexdigest()


def derive_rate_extract(
    rows: Iterable[dict[str, Any]],
    *,
    source: dict[str, Any],
    spec: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Select, compute, and assemble the extract. ``source`` is the snapshot manifest identity."""
    _check_spec(spec)
    scope = source.get("scope") or {}
    if scope != spec["scope"]:
        raise ValidationError(
            f"Snapshot scope {scope} does not match the rate extract spec scope {spec['scope']}."
        )
    regions = {"azure": scope["azureRegion"], "aws": scope["awsRegion"]}
    index: dict[tuple[str, str, str, str], list[tuple[str, str, dict[str, Any]]]] = {}
    for rate_key, entry in spec["rates"].items():
        for name, selector in entry["selectors"].items():
            bucket = (
                selector["provider"],
                selector["serviceCode"],
                selector["term"],
                selector["unit"],
            )
            index.setdefault(bucket, []).append((rate_key, name, selector))
    counts: dict[tuple[str, str], int] = {
        (rate_key, name): 0
        for rate_key, entry in spec["rates"].items()
        for name in entry["selectors"]
    }
    selected: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        candidates = index.get(
            (row.get("provider"), row.get("serviceCode"), row.get("term"), row.get("unit"))
        )
        if not candidates or row.get("region") != regions.get(row.get("provider")):
            continue
        # Rates are published in USD only; a same-key row in another currency is never a candidate.
        if row.get("currency") != "USD":
            continue
        for rate_key, name, selector in candidates:
            if _matches(row, selector):
                counts[(rate_key, name)] += 1
                selected.setdefault((rate_key, name), row)

    problems = [
        f"{rate_key}/{name} matched {count} rows"
        for (rate_key, name), count in sorted(counts.items())
        if count != 1
    ]
    if problems:
        raise ValidationError(
            "Rate extract selectors must match exactly one row: " + "; ".join(problems) + "."
        )

    rates: dict[str, str] = {}
    sources: dict[str, dict[str, Any]] = {}
    selections: dict[str, dict[str, Any]] = {}
    for rate_key, entry in spec["rates"].items():
        rows_by_name = {name: selected[(rate_key, name)] for name in entry["selectors"]}
        exact = _evaluate(rate_key, entry["formula"], rows_by_name)
        quantum = Decimal(1).scaleb(-int(entry["places"]))
        rates[rate_key] = format(exact.quantize(quantum, rounding=ROUND_HALF_UP), "f")
        record: dict[str, Any] = {
            "source_type": entry["sourceType"],
            "provider": next(iter(entry["selectors"].values()))["provider"],
        }
        for field, reference in entry["record"].items():
            if field == "row_ids":
                record[field] = [rows_by_name[name]["rowId"] for name in reference]
            elif field == "row_id":
                record[field] = rows_by_name[reference]["rowId"]
            else:
                record[field] = rows_by_name[reference][field]
        record["note"] = entry["note"]
        sources[rate_key] = record
        selections[rate_key] = {
            "value": rates[rate_key],
            "exact": format(exact, "f"),
            "rows": {
                name: {"rowId": row["rowId"], "price": row["price"], "candidateCount": 1}
                for name, row in rows_by_name.items()
            },
        }
    assumed = {key: value["rate"] for key, value in spec["assumedRates"].items()}
    sources.update({key: dict(value["record"]) for key, value in spec["assumedRates"].items()})

    template = spec["extract"]
    source_id = str(source["snapshotId"])
    extract = {
        "manifest": {
            "snapshotId": template["snapshotIdTemplate"].replace("{sourceSnapshotId}", source_id),
            "sourceSnapshotId": source_id,
            "sourceContentHash": source["contentHash"],
            "pricedAsOf": source["pricedAsOf"],
            "schemaVersion": template["schemaVersion"],
            "validationStatus": source["validationStatus"],
            "publishingHuman": source.get("publishingHuman"),
            "nonProduction": template["nonProduction"],
            "sourceUrls": list(template["sourceUrls"]),
        },
        "scope": {**spec["scope"], "commercialView": template["commercialView"]},
        "rates": rates,
        "assumedRates": assumed,
        "rateSources": dict(sorted(sources.items())),
        "notice": template["noticeTemplate"].replace("{sourceSnapshotId}", source_id),
    }
    report = {
        "specVersion": spec["specVersion"],
        "specDigest": hashlib.sha256(canonical_json(spec).encode("utf-8")).hexdigest(),
        "sourceSnapshotId": source_id,
        "sourceContentHash": source["contentHash"],
        "extractSnapshotId": extract["manifest"]["snapshotId"],
        "extractDigest": extract_digest(extract),
        "rateCount": len(rates),
        "assumedRateCount": len(assumed),
        "selections": selections,
    }
    return extract, report


def diff_extracts(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Per-rate old/new/change/percent in decimal. Percent is null when the old rate is zero."""
    old_rates = _all_rates(previous)
    new_rates = _all_rates(current)
    assumed = set(previous.get("assumedRates", {})) | set(current.get("assumedRates", {}))
    changes = []
    counts = {"added": 0, "removed": 0, "changed": 0, "unchanged": 0}
    for key in sorted(set(old_rates) | set(new_rates)):
        old = old_rates.get(key)
        new = new_rates.get(key)
        entry: dict[str, Any] = {
            "rateKey": key,
            "assumed": key in assumed,
            "old": None if old is None else format(old, "f"),
            "new": None if new is None else format(new, "f"),
            "change": None,
            "percentChange": None,
        }
        if old is None:
            entry["status"] = "added"
        elif new is None:
            entry["status"] = "removed"
        else:
            change = new - old
            entry["change"] = format(change, "f")
            if old != 0:
                entry["percentChange"] = format(
                    (change / old * 100).quantize(PERCENT_PLACES, rounding=ROUND_HALF_UP), "f"
                )
            entry["status"] = "unchanged" if change == 0 else "changed"
        counts[entry["status"]] += 1
        changes.append(entry)
    return {
        "baselineSnapshotId": (previous.get("manifest") or {}).get("snapshotId"),
        "baselineDigest": extract_digest(previous),
        **{f"{status}Count": count for status, count in counts.items()},
        "rates": changes,
    }


def derive_from_artifact(
    artifact_path: Path,
    *,
    spec_path: Path = SPEC_PATH,
    previous_extract: Path | None = None,
    approval_key: str | None = None,
    approval_verifier: Callable[[dict[str, Any]], bool] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Derive from a Published artifact; the artifact's hash and approval are verified first."""
    spec = load_spec(spec_path)
    rows: list[dict[str, Any]] = []
    matcher = _prefilter(spec)
    manifest = verify_published_artifact(
        artifact_path,
        on_row=lambda row: rows.append(row) if matcher(row) else None,
        approval_key=approval_key,
        approval_verifier=approval_verifier,
    )
    extract, report = derive_rate_extract(rows, source=manifest, spec=spec)
    return extract, _with_diff(report, extract, previous_extract)


def derive_run(
    run_dir: Path,
    *,
    spec_path: Path = SPEC_PATH,
    previous_extract: Path | None = None,
) -> dict[str, Any]:
    """Derive from a Validated staged run and write the extract and report into the run."""
    extract, report = derive_validated_run(
        run_dir, spec_path=spec_path, previous_extract=previous_extract
    )
    write_outputs(run_dir, extract, report)
    return {
        "extractSnapshotId": report["extractSnapshotId"],
        "extractDigest": report["extractDigest"],
        "rateCount": report["rateCount"],
        "changedCount": (report["diff"] or {}).get("changedCount"),
    }


def derive_validated_run(
    run_dir: Path,
    *,
    spec_path: Path = SPEC_PATH,
    previous_extract: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Derive without writing; Blob staging calls this to re-verify the extract it uploads."""
    spec = load_spec(spec_path)
    manifest = _read_json(run_dir / "stage-manifest.json")
    validation = _read_json(run_dir / "validation.json")
    stage_manifest_digest = hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest()
    if (
        manifest.get("validationStatus") != "Validated"
        or validation.get("validationStatus") != "Validated"
        or validation.get("failures") != []
        or any(
            validation.get(field) != manifest.get(field)
            for field in ("snapshotId", "contentHash", "rowCount", "scope")
        )
        or validation.get("stageManifestDigest") != stage_manifest_digest
    ):
        raise ValidationError("A rate extract can only be derived from a Validated run.")
    matcher = _prefilter(spec)
    digest = hashlib.sha256()
    row_count = 0
    rows: list[dict[str, Any]] = []
    try:
        with (run_dir / "canonical-rows.ndjson").open("rb") as handle:
            for line in handle:
                digest.update(line)
                row_count += 1
                row = json.loads(line)
                if matcher(row):
                    rows.append(row)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError("Cannot read the staged canonical rows.") from exc
    if digest.hexdigest() != manifest.get("contentHash") or row_count != manifest.get("rowCount"):
        raise ValidationError("Canonical rows changed after validation.")
    source = {
        "snapshotId": manifest["snapshotId"],
        "contentHash": manifest["contentHash"],
        "pricedAsOf": manifest["pricedAsOf"],
        "validationStatus": "Validated",
        "publishingHuman": None,
        "scope": manifest.get("scope"),
    }
    extract, report = derive_rate_extract(rows, source=source, spec=spec)
    report["stageManifestDigest"] = stage_manifest_digest
    return extract, _with_diff(report, extract, previous_extract)


def write_outputs(directory: Path, extract: dict[str, Any], report: dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, value in ((EXTRACT_NAME, extract), (REPORT_NAME, report)):
        temporary = directory / f".{name}.tmp"
        temporary.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
        )
        os.replace(temporary, directory / name)


def _with_diff(
    report: dict[str, Any], extract: dict[str, Any], previous_extract: Path | None
) -> dict[str, Any]:
    report["diff"] = (
        None if previous_extract is None
        else diff_extracts(_read_json(previous_extract), extract)
    )
    return report


def _prefilter(spec: dict[str, Any]) -> Callable[[dict[str, Any]], bool]:
    buckets = {
        (s["provider"], s["serviceCode"], s["term"], s["unit"])
        for entry in spec["rates"].values()
        for s in entry["selectors"].values()
    }
    return lambda row: (
        row.get("provider"), row.get("serviceCode"), row.get("term"), row.get("unit")
    ) in buckets


def _matches(row: dict[str, Any], selector: dict[str, Any]) -> bool:
    if "productFamily" in selector and row.get("productFamily") != selector["productFamily"]:
        return False
    dimensions = row.get("dimensions") or {}
    # Selector values are strings; exact type match stops 1 == True style coincidences.
    return all(
        isinstance(dimensions.get(name), str) and dimensions[name] == value
        for name, value in selector["dimensions"].items()
    )


def _evaluate(
    rate_key: str, formula: dict[str, Any], rows: dict[str, dict[str, Any]]
) -> Decimal:
    def price(name: str) -> Decimal:
        try:
            value = Decimal(str(rows[name]["price"]))
        except (InvalidOperation, KeyError) as exc:
            raise ValidationError(f"{rate_key}: selected row {name} has no decimal price.") from exc
        if not value.is_finite() or value < 0:
            raise ValidationError(f"{rate_key}: selected row {name} has an invalid price.")
        return value

    with localcontext() as context:
        context.prec = 50
        kind = formula["type"]
        if kind == "price":
            value = price(formula.get("row", "row"))
        elif kind == "difference":
            value = price(formula["row"]) - price(formula["minus"])
        elif kind == "scaled":
            value = price(formula["row"])
            if "multiply" in formula:
                value *= Decimal(formula["multiply"])
            if "divide" in formula:
                value /= Decimal(formula["divide"])
        elif kind == "termNormalized":
            value = price(formula["upfront"]) / Decimal(formula["hours"])
            if "recurringHourly" in formula:
                value += price(formula["recurringHourly"])
        else:
            raise ValidationError(f"{rate_key}: unknown formula {kind!r}.")
    if value < 0:
        raise ValidationError(f"{rate_key}: derived rate is negative ({value}).")
    return value


def _check_spec(spec: Any) -> None:
    def fail(message: str) -> None:
        raise ValidationError(f"Invalid rate extract spec: {message}")

    if not isinstance(spec, dict) or spec.get("specVersion") != SPEC_VERSION:
        fail("unsupported specVersion.")
    scope = spec.get("scope")
    if not isinstance(scope, dict) or set(scope) != {"azureRegion", "awsRegion"}:
        fail("scope must name azureRegion and awsRegion.")
    template = spec.get("extract")
    required_template = {
        "snapshotIdTemplate", "schemaVersion", "nonProduction", "commercialView",
        "sourceUrls", "noticeTemplate",
    }
    if not isinstance(template, dict) or set(template) != required_template:
        fail("extract template fields are incomplete.")
    if not isinstance(template["nonProduction"], bool):
        fail("nonProduction must be a boolean.")
    rates = spec.get("rates")
    assumed = spec.get("assumedRates")
    if not isinstance(rates, dict) or not rates or not isinstance(assumed, dict):
        fail("rates and assumedRates are required.")
    if set(rates) & set(assumed):
        fail("a rate key cannot be both harvested and assumed.")
    for key, entry in rates.items():
        selectors = entry.get("selectors") if isinstance(entry, dict) else None
        if not isinstance(selectors, dict) or not selectors:
            fail(f"{key} has no selectors.")
        for name, selector in selectors.items():
            if (
                not isinstance(selector, dict)
                or not set(selector) <= _SELECTOR_FIELDS
                or not {"provider", "serviceCode", "term", "unit", "dimensions"} <= set(selector)
                or selector["provider"] not in ("aws", "azure")
                or not isinstance(selector["dimensions"], dict)
                or not selector["dimensions"]
                or not all(
                    isinstance(value, str) and value
                    for value in (
                        *selector["dimensions"].values(),
                        *(selector[field] for field in selector if field != "dimensions"),
                    )
                )
            ):
                fail(f"{key}/{name} is not an explicit selector.")
        providers = {selector["provider"] for selector in selectors.values()}
        if len(providers) != 1 or not key.startswith(f"{providers.pop()}."):
            fail(f"{key} selectors must all use the provider named by the key.")
        formula = entry.get("formula")
        if not isinstance(formula, dict) or formula.get("type") not in _FORMULA_FIELDS:
            fail(f"{key} has an unknown formula.")
        required, optional = _FORMULA_FIELDS[formula["type"]]
        if not required <= set(formula) or not set(formula) <= required | optional | {"type"}:
            fail(f"{key} formula fields do not match its type {formula['type']!r}.")
        if formula["type"] == "scaled" and not {"multiply", "divide"} & set(formula):
            fail(f"{key} scaled formula needs multiply or divide.")
        if any(not isinstance(value, str) for value in formula.values()):
            fail(f"{key} formula values must be strings (decimal constants as strings).")
        references = [
            value for field, value in formula.items()
            if field in ("row", "minus", "upfront", "recurringHourly")
        ]
        if formula["type"] == "price" and "row" not in formula:
            references.append("row")
        record = entry.get("record")
        if not isinstance(record, dict) or not set(record) <= _RECORD_FIELDS:
            fail(f"{key} has an invalid provenance record.")
        for value in record.values():
            references.extend(value if isinstance(value, list) else [value])
        if any(reference not in selectors for reference in references):
            fail(f"{key} references an undefined selector.")
        for number in (formula.get("multiply"), formula.get("divide"), formula.get("hours")):
            if number is not None:
                parsed = _decimal_or_none(number)
                if parsed is None:
                    fail(f"{key} has a non-decimal formula constant.")
                if parsed <= 0:
                    fail(f"{key} formula constants must be positive.")
        places = entry.get("places")
        if not isinstance(places, int) or not 0 <= places <= 18:
            fail(f"{key} places must be an integer from 0 to 18.")
        if not isinstance(entry.get("note"), str) or not isinstance(entry.get("sourceType"), str):
            fail(f"{key} needs a note and sourceType.")
    for key, value in assumed.items():
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("rate"), str)
            or not isinstance(value.get("record"), dict)
            or value["record"].get("source_type") != "DemoAssumption"
        ):
            fail(f"assumed rate {key} must be a visible DemoAssumption.")
        parsed = _decimal_or_none(value["rate"])
        if parsed is None or parsed < 0:
            fail(f"assumed rate {key} must be a finite, non-negative decimal string.")


def _decimal_or_none(value: Any) -> Decimal | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def _all_rates(extract: dict[str, Any]) -> dict[str, Decimal]:
    rates: dict[str, Decimal] = {}
    try:
        for section in ("rates", "assumedRates"):
            for key, value in (extract.get(section) or {}).items():
                if not isinstance(value, str):
                    raise TypeError(key)
                rates[key] = Decimal(value)
                if not rates[key].is_finite() or rates[key] < 0:
                    raise InvalidOperation(key)
    except (InvalidOperation, TypeError, AttributeError) as exc:
        raise ValidationError("Extract rates must be finite, non-negative decimal strings.") from exc
    return rates


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"Cannot read JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"Expected a JSON object: {path}")
    return value
