from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable

from .core import (
    SCHEMA_VERSION,
    HarvestError,
    PublicationError,
    ValidationError,
    canonical_json,
    decimal_string,
    read_ndjson,
    row_identity,
    sha256_text,
    validate_snapshot_id,
)


MATERIAL_CHANGE_RATIO = Decimal("0.10")
MAX_EVIDENCE_ROWS = 100
APPROVAL_KEY_ENV = "HARVESTER_APPROVAL_HMAC_KEY"
COVERAGE_MATRIX_PATH = Path(__file__).with_name("coverage-matrix.json")
APPROVAL_MANIFEST_FIELDS = {
    "publishingHuman",
    "publishingHumanId",
    "publishingHumanRole",
    "skuMapReviewer",
    "skuMapReviewerId",
    "skuMapReviewerRole",
    "skuMapDigest",
    "approvedAt",
    "approvalAlgorithm",
    "approvalSignature",
    "stageManifestDigest",
    "evidencePolicy",
    "nonProduction",
    "approvedExtractDigest",
    "approvalKeyId",
    "approvedStagedRunId",
    "approvedEvidenceDigest",
}
# Optional signed-record fields and the published-manifest fields that carry them.
OPTIONAL_APPROVAL_BINDINGS = {
    "extractDigest": "approvedExtractDigest",
    "keyId": "approvalKeyId",
    "runId": "approvedStagedRunId",
    "evidenceDigest": "approvedEvidenceDigest",
}
# Records signed by the web app's key (they carry keyId) must bind all of these.
KEYED_APPROVAL_BINDINGS = ("extractDigest", "runId", "evidenceDigest")

# Verifies a signed approval record's signature; HMAC records use the approval key instead.
ApprovalVerifier = Callable[[dict[str, Any]], bool]


def build_staged_snapshot(
    *,
    snapshot_id: str,
    captured_at: str,
    azure_region: str,
    aws_region: str,
    source_paths: Iterable[Path],
    run_dir: Path,
    collector_version: str,
) -> dict[str, Any]:
    validate_snapshot_id(snapshot_id)
    spool_db = run_dir / "spool" / "canonical.sqlite"
    spool_db.parent.mkdir(parents=True, exist_ok=True)
    duplicate_ids: list[str] = []
    duplicate_count = 0
    with closing(sqlite3.connect(spool_db)) as connection:
        connection.execute(
            "CREATE TABLE rows (row_id TEXT PRIMARY KEY, canonical_json TEXT NOT NULL, "
            "provider TEXT NOT NULL, service_code TEXT NOT NULL)"
        )
        for source_path in source_paths:
            for row in read_ndjson(source_path):
                _validate_row(row)
                try:
                    connection.execute(
                        "INSERT INTO rows VALUES (?, ?, ?, ?)",
                        (
                            row["rowId"],
                            canonical_json(row),
                            row["provider"],
                            row["serviceCode"],
                        ),
                    )
                except sqlite3.IntegrityError:
                    duplicate_count += 1
                    if len(duplicate_ids) < MAX_EVIDENCE_ROWS:
                        duplicate_ids.append(row["rowId"])
        connection.commit()
        rows_path = run_dir / "canonical-rows.ndjson"
        digest = hashlib.sha256()
        row_count = 0
        service_counts: dict[str, int] = {}
        with rows_path.open("wb") as handle:
            cursor = connection.execute(
                "SELECT canonical_json, provider, service_code FROM rows ORDER BY row_id"
            )
            for row_json, provider, service_code in cursor:
                encoded = f"{row_json}\n".encode("utf-8")
                handle.write(encoded)
                digest.update(encoded)
                row_count += 1
                key = f"{provider}:{service_code}"
                service_counts[key] = service_counts.get(key, 0) + 1
    spool_db.unlink(missing_ok=True)
    if row_count == 0:
        raise ValidationError("Cannot stage an empty price snapshot.")
    manifest = {
        "snapshotId": snapshot_id,
        "capturedAt": captured_at,
        "pricedAsOf": captured_at[:10],
        "schemaVersion": SCHEMA_VERSION,
        "collectorVersion": collector_version,
        "validationStatus": "Staged",
        "publishingHuman": None,
        "skuMapReviewer": None,
        "skuMapDigest": None,
        "contentHash": digest.hexdigest(),
        "rowCount": row_count,
        "serviceCounts": dict(sorted(service_counts.items())),
        "scope": {"azureRegion": azure_region, "awsRegion": aws_region},
        "coverageMatrixDigest": hashlib.sha256(
            COVERAGE_MATRIX_PATH.read_bytes()
        ).hexdigest(),
        "duplicateRowCount": duplicate_count,
        "duplicateRowIds": duplicate_ids,
        "duplicateRowIdsTruncated": duplicate_count > len(duplicate_ids),
    }
    _write_json_atomic(run_dir / "stage-manifest.json", manifest)
    return manifest


def validate_snapshot(
    run_dir: Path,
    *,
    previous_artifact: Path | None,
    bootstrap: bool,
    current_pointer: Path | None = None,
    approval_key: str | None = None,
    approval_verifier: ApprovalVerifier | None = None,
) -> dict[str, Any]:
    manifest = _read_json(run_dir / "stage-manifest.json")
    rows_path = run_dir / "canonical-rows.ndjson"
    digest, row_count = _hash_file(rows_path)
    failures: list[str] = []
    if digest != manifest.get("contentHash"):
        failures.append("Canonical row content hash does not match the stage manifest.")
    if row_count != manifest.get("rowCount"):
        failures.append("Canonical row count does not match the stage manifest.")
    if manifest.get("duplicateRowCount", 0):
        failures.append(
            f"Duplicate normalized row IDs detected: {manifest['duplicateRowCount']}."
        )
    coverage_matrix = _read_json(COVERAGE_MATRIX_PATH)
    coverage_matrix_digest = hashlib.sha256(COVERAGE_MATRIX_PATH.read_bytes()).hexdigest()
    if manifest.get("coverageMatrixDigest") != coverage_matrix_digest:
        failures.append("Coverage matrix digest does not match the staged snapshot.")
    scope = manifest.get("scope") or {}
    approved_regions = coverage_matrix.get("approvedRegions") or {}
    if scope.get("azureRegion") not in approved_regions.get("azure", []):
        failures.append("Azure region is not approved by the coverage matrix.")
    if scope.get("awsRegion") not in approved_regions.get("aws", []):
        failures.append("AWS region is not approved by the coverage matrix.")
    coverage = _check_coverage(rows_path)
    if coverage["missing"]:
        failures.append(
            f"Required catalog coverage is missing: {', '.join(coverage['missing'])}."
        )

    baseline_pointer_hash: str | None = None
    baseline_snapshot_id: str | None = None
    comparison: dict[str, Any] = {
        "bootstrap": bootstrap,
        "added": 0,
        "retired": 0,
        "changed": 0,
        "materialRateChangeCount": 0,
        "materialRateChanges": [],
        "materialRateChangesTruncated": False,
    }
    if previous_artifact is None:
        if not bootstrap:
            failures.append("No previous Published snapshot was supplied; use explicit bootstrap.")
        if current_pointer is not None:
            failures.append("Bootstrap validation cannot bind an existing manifest pointer.")
    else:
        if bootstrap:
            failures.append("Bootstrap cannot be combined with a previous Published snapshot.")
        if current_pointer is None:
            failures.append("Refresh validation requires the current manifest pointer.")
        else:
            try:
                pointer = _read_json(current_pointer)
                baseline_pointer_hash = hashlib.sha256(
                    current_pointer.read_bytes()
                ).hexdigest()
                previous_manifest = verify_published_artifact(
                    previous_artifact,
                    approval_key=approval_key,
                    approval_verifier=approval_verifier,
                )
                baseline_snapshot_id = str(previous_manifest["snapshotId"])
                if pointer.get("artifact") != previous_artifact.name:
                    failures.append(
                        "Previous artifact is not the artifact named by the current pointer."
                    )
                if (
                    pointer.get("snapshotId") != previous_manifest.get("snapshotId")
                    or pointer.get("contentHash") != previous_manifest.get("contentHash")
                ):
                    failures.append(
                        "Current pointer does not bind the supplied previous artifact."
                    )
                comparison = _compare_previous(
                    rows_path,
                    previous_artifact,
                    run_dir,
                    approval_key=approval_key,
                    approval_verifier=approval_verifier,
                )
            except (OSError, PublicationError, ValidationError) as exc:
                failures.append(str(exc))

    status = "Validated" if not failures else "Failed"
    manifest["validationStatus"] = status
    stage_manifest_digest = hashlib.sha256(
        canonical_json(manifest).encode("utf-8")
    ).hexdigest()
    report = {
        "snapshotId": manifest["snapshotId"],
        "validatedAt": datetime.now(UTC).isoformat(),
        "validationStatus": status,
        "failures": failures,
        "coverage": coverage,
        "coverageMatrixDigest": coverage_matrix_digest,
        "scope": scope,
        "stageManifestDigest": stage_manifest_digest,
        "comparison": comparison,
        "baselinePointerHash": baseline_pointer_hash,
        "baselineSnapshotId": baseline_snapshot_id,
        "contentHash": digest,
        "rowCount": row_count,
    }
    _write_json_atomic(run_dir / "stage-manifest.json", manifest)
    _write_json_atomic(run_dir / "validation.json", report)
    if failures:
        raise ValidationError("; ".join(failures))
    return report


def approve_snapshot(
    run_dir: Path,
    *,
    approval_record_path: Path,
    sku_map_path: Path,
    approval_key: str | None = None,
    approval_verifier: ApprovalVerifier | None = None,
) -> dict[str, Any]:
    if not (run_dir / "validation.json").exists():
        raise PublicationError("Only a Validated snapshot can be approved.")
    validation = _read_json(run_dir / "validation.json")
    if validation.get("validationStatus") != "Validated":
        raise PublicationError("Only a Validated snapshot can be approved.")
    sku_map_digest = hashlib.sha256(sku_map_path.read_bytes()).hexdigest()
    record = unwrap_approval(_read_json(approval_record_path))
    _verify_approval_record(
        record,
        validation=validation,
        sku_map_digest=sku_map_digest,
        approval_key=approval_key,
        signature_verifier=approval_verifier,
    )
    _write_json_atomic(run_dir / "approval.json", record)
    return record


def unwrap_approval(document: Any) -> dict[str, Any]:
    """The web app stores {schemaVersion, runId, record}; older approval files are the record itself."""
    if not isinstance(document, dict):
        raise PublicationError("Approval record is not an object.")
    if "record" not in document:
        return document
    record = document.get("record")
    if (
        set(document) != {"schemaVersion", "runId", "record"}
        or not isinstance(record, dict)
        or record.get("runId") != document.get("runId")
    ):
        raise PublicationError("Approval file wrapper does not match its signed record.")
    return record


def build_published_manifest(
    manifest: dict[str, Any],
    validation: dict[str, Any],
    approval: dict[str, Any],
    *,
    approval_key: str | None = None,
    approval_verifier: ApprovalVerifier | None = None,
) -> dict[str, Any]:
    """Check the staged manifest, validation, and signed approval agree, and return the Published manifest.

    The caller still proves the row bytes hash to the validated contentHash and binds the rate extract.
    """
    if "evidencePolicy" in manifest or "nonProduction" in manifest:
        raise PublicationError("Evidence policy labels must be bound in the signed approval record.")
    snapshot_id = validate_snapshot_id(str(manifest.get("snapshotId") or ""))
    if validation.get("validationStatus") != "Validated":
        raise PublicationError("Failed or unknown validation cannot publish.")
    bindings = {
        "snapshotId": snapshot_id,
        "contentHash": manifest.get("contentHash"),
        "rowCount": manifest.get("rowCount"),
        "coverageMatrixDigest": manifest.get("coverageMatrixDigest"),
        "scope": manifest.get("scope"),
        "stageManifestDigest": hashlib.sha256(
            canonical_json(manifest).encode("utf-8")
        ).hexdigest(),
    }
    for field, value in bindings.items():
        if value != validation.get(field):
            raise PublicationError(
                f"Staged {field} does not match the validated and approved value."
            )
    _verify_approval_record(
        approval,
        validation=validation,
        sku_map_digest=str(approval.get("skuMapDigest") or ""),
        approval_key=approval_key,
        signature_verifier=approval_verifier,
    )
    published_manifest = {
        **manifest,
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
        "stageManifestDigest": approval["stageManifestDigest"],
    }
    for field in ("evidencePolicy", "nonProduction"):
        if field in approval:
            published_manifest[field] = approval[field]
    for field, manifest_field in OPTIONAL_APPROVAL_BINDINGS.items():
        if field in approval:
            published_manifest[manifest_field] = approval[field]
    return published_manifest


def published_header(published_manifest: dict[str, Any]) -> bytes:
    """The first line of a Published artifact; the canonical rows follow it unchanged."""
    return f"{canonical_json({'recordType': 'manifest', 'manifest': published_manifest})}\n".encode("utf-8")


def publish_snapshot(
    run_dir: Path,
    *,
    store_dir: Path,
    expected_pointer_hash: str | None,
    approval_key: str | None = None,
    approval_verifier: ApprovalVerifier | None = None,
) -> dict[str, Any]:
    manifest = _read_json(run_dir / "stage-manifest.json")
    validation = _read_json(run_dir / "validation.json")
    approval = _read_json(run_dir / "approval.json")
    published_manifest = build_published_manifest(
        manifest,
        validation,
        approval,
        approval_key=approval_key,
        approval_verifier=approval_verifier,
    )
    snapshot_id = published_manifest["snapshotId"]
    rows_digest, rows_count = _hash_file(run_dir / "canonical-rows.ndjson")
    if (
        rows_digest != validation.get("contentHash")
        or rows_count != validation.get("rowCount")
    ):
        raise PublicationError("Canonical row bytes changed after validation.")
    if "extractDigest" in approval:
        extract_path = run_dir / "rate-extract.json"
        if not extract_path.is_file() or hashlib.sha256(
            canonical_json(_read_json(extract_path)).encode("utf-8")
        ).hexdigest() != approval["extractDigest"]:
            raise PublicationError("The run's rate extract is not the approved extract.")
    if expected_pointer_hash != validation.get("baselinePointerHash"):
        raise PublicationError(
            "Expected pointer hash does not match the pointer bound during validation."
        )

    store_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = store_dir / f"{snapshot_id}.pricebook.ndjson"
    if artifact_path.parent.resolve() != store_dir.resolve():
        raise PublicationError("Snapshot artifact path escaped the publication store.")
    temporary = store_dir / f".{snapshot_id}.{uuid.uuid4().hex}.tmp"
    with temporary.open("xb") as destination:
        destination.write(published_header(published_manifest))
        with (run_dir / "canonical-rows.ndjson").open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                destination.write(chunk)

    lock_path = store_dir / ".publish.lock"
    lock_fd: int | None = None
    artifact_linked = False
    try:
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise PublicationError("Another publisher holds the manifest pointer lock.") from exc
        pointer_path = store_dir / "current.json"
        actual_pointer_hash = (
            hashlib.sha256(pointer_path.read_bytes()).hexdigest()
            if pointer_path.exists()
            else None
        )
        if actual_pointer_hash != expected_pointer_hash:
            raise PublicationError("Manifest pointer compare-and-set failed.")
        current_pointer = _read_json(pointer_path) if pointer_path.exists() else None
        baseline_snapshot_id = validation.get("baselineSnapshotId")
        if baseline_snapshot_id is None and current_pointer is not None:
            raise PublicationError("Bootstrap publish cannot replace an existing pointer.")
        if baseline_snapshot_id is not None and (
            not isinstance(current_pointer, dict)
            or current_pointer.get("snapshotId") != baseline_snapshot_id
        ):
            raise PublicationError("Validated baseline is no longer the current snapshot.")
        verify_published_artifact(
            temporary, approval_key=approval_key, approval_verifier=approval_verifier
        )
        try:
            os.link(temporary, artifact_path)
            artifact_linked = True
        except FileExistsError as exc:
            raise PublicationError("Published snapshot artifacts are append-only.") from exc
        pointer = {
            "snapshotId": snapshot_id,
            "contentHash": manifest["contentHash"],
            "artifact": artifact_path.name,
            "previousSnapshotId": baseline_snapshot_id,
        }
        _write_json_atomic(pointer_path, pointer)
        return pointer
    except Exception:
        if artifact_linked:
            artifact_path.unlink(missing_ok=True)
        raise
    finally:
        temporary.unlink(missing_ok=True)
        if lock_fd is not None:
            os.close(lock_fd)
            lock_path.unlink(missing_ok=True)


def verify_published_artifact(
    path: Path,
    *,
    on_row: Callable[[dict[str, Any]], None] | None = None,
    approval_key: str | None = None,
    approval_verifier: ApprovalVerifier | None = None,
) -> dict[str, Any]:
    digest = hashlib.sha256()
    row_count = 0
    previous_row_id: str | None = None
    try:
        with path.open("rb") as handle:
            header_line = handle.readline()
            header = json.loads(header_line)
            if (
                not isinstance(header, dict)
                or header.get("recordType") != "manifest"
                or not isinstance(header.get("manifest"), dict)
            ):
                raise ValidationError("Published artifact has no manifest record.")
            manifest = header["manifest"]
            validate_snapshot_id(str(manifest.get("snapshotId") or ""))
            if manifest.get("validationStatus") != "Published":
                raise ValidationError("Published artifact status is not Published.")
            for line_number, line in enumerate(handle, start=2):
                if not line.strip():
                    continue
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValidationError(
                        f"Published artifact row {line_number} is not an object."
                    )
                _validate_row(row)
                if previous_row_id is not None and row["rowId"] <= previous_row_id:
                    raise ValidationError(
                        "Published artifact rows are duplicated or not in canonical order."
                    )
                previous_row_id = row["rowId"]
                canonical_line = f"{canonical_json(row)}\n".encode("utf-8")
                if canonical_line != line:
                    raise ValidationError(
                        f"Published artifact row {line_number} is not canonical."
                    )
                digest.update(line)
                row_count += 1
                if on_row is not None:
                    on_row(row)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"Cannot read Published artifact: {path}") from exc
    if row_count != manifest.get("rowCount"):
        raise ValidationError("Published artifact row count does not match its manifest.")
    if digest.hexdigest() != manifest.get("contentHash"):
        raise ValidationError("Published artifact hash does not match its manifest.")
    verify_published_manifest(
        manifest, approval_key=approval_key, approval_verifier=approval_verifier
    )
    return manifest


def verify_published_manifest(
    manifest: dict[str, Any],
    *,
    approval_key: str | None = None,
    approval_verifier: ApprovalVerifier | None = None,
) -> None:
    """Rebuild the staged manifest and the signed approval from a Published manifest and verify both."""
    validate_snapshot_id(str(manifest.get("snapshotId") or ""))
    if manifest.get("validationStatus") != "Published":
        raise ValidationError("Published artifact status is not Published.")
    stage_manifest = {
        key: value
        for key, value in manifest.items()
        if key not in APPROVAL_MANIFEST_FIELDS
    }
    stage_manifest["validationStatus"] = "Validated"
    stage_manifest["publishingHuman"] = None
    stage_manifest["skuMapReviewer"] = None
    stage_manifest["skuMapDigest"] = None
    stage_manifest_digest = hashlib.sha256(
        canonical_json(stage_manifest).encode("utf-8")
    ).hexdigest()
    if stage_manifest_digest != manifest.get("stageManifestDigest"):
        raise ValidationError("Published manifest does not match its approved stage digest.")
    approval_record = {
        "snapshotId": manifest.get("snapshotId"),
        "contentHash": manifest.get("contentHash"),
        "approverId": manifest.get("publishingHumanId"),
        "approverDisplayName": manifest.get("publishingHuman"),
        "approverRole": manifest.get("publishingHumanRole"),
        "approvedAt": manifest.get("approvedAt"),
        "skuMapReviewerId": manifest.get("skuMapReviewerId"),
        "skuMapReviewerDisplayName": manifest.get("skuMapReviewer"),
        "skuMapReviewerRole": manifest.get("skuMapReviewerRole"),
        "skuMapDigest": manifest.get("skuMapDigest"),
        "coverageMatrixDigest": manifest.get("coverageMatrixDigest"),
        "scope": manifest.get("scope"),
        "stageManifestDigest": manifest.get("stageManifestDigest"),
        "algorithm": manifest.get("approvalAlgorithm"),
        "signature": manifest.get("approvalSignature"),
    }
    for field in ("evidencePolicy", "nonProduction"):
        if field in manifest:
            approval_record[field] = manifest[field]
    for field, manifest_field in OPTIONAL_APPROVAL_BINDINGS.items():
        if manifest_field in manifest:
            approval_record[field] = manifest[manifest_field]
    _verify_approval_record(
        approval_record,
        validation={
            "snapshotId": manifest.get("snapshotId"),
            "contentHash": manifest.get("contentHash"),
            "coverageMatrixDigest": manifest.get("coverageMatrixDigest"),
            "scope": manifest.get("scope"),
            "stageManifestDigest": manifest.get("stageManifestDigest"),
        },
        sku_map_digest=str(manifest.get("skuMapDigest") or ""),
        approval_key=approval_key,
        signature_verifier=approval_verifier,
    )


def _verify_approval_record(
    record: dict[str, Any],
    *,
    validation: dict[str, Any],
    sku_map_digest: str,
    approval_key: str | None,
    signature_verifier: ApprovalVerifier | None = None,
) -> None:
    required = {
        "snapshotId",
        "contentHash",
        "approverId",
        "approverDisplayName",
        "approverRole",
        "approvedAt",
        "skuMapReviewerId",
        "skuMapReviewerDisplayName",
        "skuMapReviewerRole",
        "skuMapDigest",
        "coverageMatrixDigest",
        "scope",
        "stageManifestDigest",
        "algorithm",
        "signature",
    }
    missing = sorted(required - record.keys())
    if missing:
        raise PublicationError(
            f"Signed approval record is missing: {', '.join(missing)}."
        )
    policy = record.get("evidencePolicy")
    non_production = record.get("nonProduction")
    if not (
        (policy is None and non_production is None)
        or (policy == "MutablePilot" and non_production is True)
        or (policy == "WORM" and non_production is False)
    ):
        raise PublicationError("Approval evidence policy and non-production label are invalid.")
    if "extractDigest" in record and not (
        isinstance(record["extractDigest"], str)
        and re.fullmatch(r"[0-9a-f]{64}", record["extractDigest"])
    ):
        raise PublicationError("Approval extract digest must be a SHA-256 hex digest.")
    if "keyId" in record and not (
        isinstance(record["keyId"], str) and record["keyId"].strip()
    ):
        raise PublicationError("Approval signing key ID must be nonempty.")
    if "runId" in record and not (
        isinstance(record["runId"], str) and re.fullmatch(r"[0-9a-f]{32}", record["runId"])
    ):
        raise PublicationError("Approval staged run ID must be 32 hex characters.")
    if "evidenceDigest" in record and not (
        isinstance(record["evidenceDigest"], str)
        and re.fullmatch(r"[0-9a-f]{64}", record["evidenceDigest"])
    ):
        raise PublicationError("Approval evidence digest must be a SHA-256 hex digest.")
    if "keyId" in record and any(field not in record for field in KEYED_APPROVAL_BINDINGS):
        raise PublicationError(
            "A keyed approval record must bind the extract, staged run, and evidence digests."
        )
    if not isinstance(record["algorithm"], str) or not record["algorithm"]:
        raise PublicationError("Approval signature algorithm is missing.")
    if signature_verifier is None and record["algorithm"] != "HMAC-SHA256":
        raise PublicationError("Unsupported approval signature algorithm.")
    if any(
        not isinstance(record[field], str) or not record[field].strip()
        for field in ("approverId", "approverDisplayName", "skuMapReviewerId",
                      "skuMapReviewerDisplayName")
    ):
        raise PublicationError("Approval actor identities must be nonempty.")
    if (
        str(record["approverRole"]) != "SnapshotApprover"
        or str(record["skuMapReviewerRole"]) != "SkuMapReviewer"
    ):
        raise PublicationError("Approval record contains invalid approval roles.")
    if (
        record["snapshotId"] != validation.get("snapshotId")
        or record["contentHash"] != validation.get("contentHash")
        or record["skuMapDigest"] != sku_map_digest
        or record["coverageMatrixDigest"] != validation.get("coverageMatrixDigest")
        or record["scope"] != validation.get("scope")
        or record["stageManifestDigest"] != validation.get("stageManifestDigest")
    ):
        raise PublicationError("Approval record does not bind the validated artifacts.")
    try:
        approved_at = datetime.fromisoformat(str(record["approvedAt"]))
    except ValueError as exc:
        raise PublicationError("Approval timestamp is invalid.") from exc
    if approved_at.tzinfo is None:
        raise PublicationError("Approval timestamp must include a timezone.")
    if signature_verifier is not None:
        if signature_verifier(record) is not True:
            raise PublicationError("Published artifact approval was not verified.")
        return
    key = approval_key or os.getenv(APPROVAL_KEY_ENV)
    if key is None or len(key) < 32:
        raise PublicationError(
            f"{APPROVAL_KEY_ENV} must contain at least 32 characters."
        )
    payload = {key_name: value for key_name, value in record.items() if key_name != "signature"}
    expected = hmac.new(
        key.encode("utf-8"),
        canonical_json(payload).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, str(record["signature"])):
        raise PublicationError("Approval signature is invalid.")


def _validate_row(row: dict[str, Any]) -> None:
    required = {
        "rowId",
        "provider",
        "serviceCode",
        "region",
        "sku",
        "meter",
        "term",
        "effectiveStart",
        "unit",
        "currency",
        "price",
        "dimensions",
        "sourceUrl",
        "sourcePublicationDate",
    }
    missing = sorted(required - row.keys())
    if missing:
        raise ValidationError(f"Normalized price row is missing: {', '.join(missing)}.")
    if not isinstance(row["price"], str) or not isinstance(row["dimensions"], dict):
        raise ValidationError("Normalized price and dimensions have invalid types.")
    try:
        normalized_price = decimal_string(row["price"])
    except HarvestError as exc:
        raise ValidationError(str(exc)) from exc
    if normalized_price != row["price"]:
        raise ValidationError("Normalized price is not in canonical decimal-string form.")
    if row["provider"] not in {"aws", "azure"}:
        raise ValidationError(f"Unknown provider: {row['provider']}")
    expected_row_id = sha256_text(canonical_json(row_identity(row)))
    if row["rowId"] != expected_row_id:
        raise ValidationError("Normalized row ID does not bind its exact identity.")


def _check_coverage(rows_path: Path) -> dict[str, Any]:
    flags = {
        "aws-ec2": False,
        "aws-rds": False,
        "aws-compute-savings-plan": False,
        "aws-gp3-capacity": False,
        "aws-gp3-iops": False,
        "aws-gp3-throughput": False,
        "azure-vm": False,
        "azure-vm-savings-plan": False,
        "azure-premium-ssd-v2-capacity": False,
        "azure-premium-ssd-v2-iops": False,
        "azure-premium-ssd-v2-throughput": False,
        "azure-postgresql": False,
        "azure-bandwidth": False,
        "azure-load-balancer": False,
    }
    for row in read_ndjson(rows_path):
        service = row["serviceCode"]
        dimensions = row["dimensions"]
        family = row.get("productFamily")
        volume = str(dimensions.get("volumeApiName") or "").casefold()
        product_name = str(dimensions.get("productName") or "").casefold()
        meter_name = str(dimensions.get("meterName") or "").casefold()
        flags["aws-ec2"] |= service == "AmazonEC2"
        flags["aws-rds"] |= service == "AmazonRDS"
        flags["aws-compute-savings-plan"] |= service == "AWSComputeSavingsPlan"
        flags["aws-gp3-capacity"] |= (
            service == "AmazonEC2" and family == "Storage" and volume == "gp3"
        )
        flags["aws-gp3-iops"] |= (
            service == "AmazonEC2"
            and family == "System Operation"
            and volume == "gp3"
        )
        flags["aws-gp3-throughput"] |= (
            service == "AmazonEC2"
            and family == "Provisioned Throughput"
            and volume == "gp3"
        )
        flags["azure-vm"] |= service == "Virtual Machines" and row["term"] == "Consumption"
        flags["azure-vm-savings-plan"] |= (
            service == "Virtual Machines" and row["term"] == "SavingsPlan"
        )
        flags["azure-premium-ssd-v2-capacity"] |= (
            service == "Storage"
            and "premium ssd v2" in product_name
            and "provisioned capacity" in meter_name
        )
        flags["azure-premium-ssd-v2-iops"] |= (
            service == "Storage"
            and "premium ssd v2" in product_name
            and "provisioned iops" in meter_name
        )
        flags["azure-premium-ssd-v2-throughput"] |= (
            service == "Storage"
            and "premium ssd v2" in product_name
            and "provisioned throughput" in meter_name
        )
        flags["azure-postgresql"] |= service == "Azure Database for PostgreSQL"
        flags["azure-bandwidth"] |= service == "Bandwidth"
        flags["azure-load-balancer"] |= service == "Load Balancer"
    return {
        "checks": flags,
        "missing": sorted(name for name, covered in flags.items() if not covered),
    }


def _compare_previous(
    rows_path: Path,
    previous_artifact: Path,
    run_dir: Path,
    *,
    approval_key: str | None,
    approval_verifier: ApprovalVerifier | None = None,
) -> dict[str, Any]:
    comparison_db = run_dir / "spool" / "comparison.sqlite"
    comparison_db.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(comparison_db)) as connection:
        connection.execute(
            "CREATE TABLE rates (version TEXT NOT NULL, row_id TEXT NOT NULL, "
            "price TEXT NOT NULL, PRIMARY KEY (version, row_id))"
        )
        connection.executemany(
            "INSERT INTO rates VALUES ('current', ?, ?)",
            ((row["rowId"], row["price"]) for row in read_ndjson(rows_path)),
        )
        verify_published_artifact(
            previous_artifact,
            approval_key=approval_key,
            approval_verifier=approval_verifier,
            on_row=lambda row: connection.execute(
                "INSERT INTO rates VALUES ('previous', ?, ?)",
                (row["rowId"], row["price"]),
            ),
        )
        connection.commit()
        added = connection.execute(
            "SELECT COUNT(*) FROM rates c WHERE version = 'current' AND NOT EXISTS "
            "(SELECT 1 FROM rates p WHERE p.version = 'previous' AND p.row_id = c.row_id)"
        ).fetchone()[0]
        retired = connection.execute(
            "SELECT COUNT(*) FROM rates p WHERE version = 'previous' AND NOT EXISTS "
            "(SELECT 1 FROM rates c WHERE c.version = 'current' AND c.row_id = p.row_id)"
        ).fetchone()[0]
        changed = connection.execute(
            "SELECT COUNT(*) FROM rates c JOIN rates p ON p.row_id = c.row_id "
            "WHERE c.version = 'current' AND p.version = 'previous' AND c.price != p.price"
        ).fetchone()[0]
        changed_cursor = connection.execute(
            "SELECT c.row_id, p.price, c.price FROM rates c JOIN rates p ON p.row_id = c.row_id "
            "WHERE c.version = 'current' AND p.version = 'previous' AND c.price != p.price"
        )
        material_changes: list[dict[str, Any]] = []
        material_change_count = 0
        for row_id, old_price_text, new_price_text in changed_cursor:
            old_price = Decimal(old_price_text)
            new_price = Decimal(new_price_text)
            ratio = abs(new_price - old_price) / old_price if old_price else Decimal("1")
            if ratio >= MATERIAL_CHANGE_RATIO:
                material_change_count += 1
                if len(material_changes) < MAX_EVIDENCE_ROWS:
                    material_changes.append(
                        {
                            "rowId": row_id,
                            "oldPrice": str(old_price),
                            "newPrice": str(new_price),
                            "ratio": str(ratio),
                        }
                    )
    comparison_db.unlink(missing_ok=True)
    return {
        "bootstrap": False,
        "added": added,
        "retired": retired,
        "changed": changed,
        "materialRateChangeCount": material_change_count,
        "materialRateChanges": material_changes,
        "materialRateChangesTruncated": material_change_count > len(material_changes),
    }


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    count = 0
    with path.open("rb") as handle:
        for line in handle:
            digest.update(line)
            if line.strip():
                count += 1
    return digest.hexdigest(), count


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublicationError(f"Cannot read required artifact: {path}") from exc
    if not isinstance(value, dict):
        raise PublicationError(f"Artifact root must be an object: {path}")
    return value


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(f"{canonical_json(value)}\n", encoding="utf-8", newline="\n")
    os.replace(temporary, path)
