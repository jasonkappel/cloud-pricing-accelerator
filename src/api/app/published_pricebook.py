"""``PRICEBOOK_SOURCE=published-blob``: price from the current Published snapshot, or not at all.

Every step fails closed with ``PublishedPriceBookError``; nothing falls back to demo rates.

1. ``current.json`` in the control container names the Published snapshot.
2. Its publication record (written by the API, last) must match the pointer and pin the artifact's ETag.
3. The artifact's first line is the Published manifest. It must be canonical, and the harvester's own
   ``verify_published_manifest`` rebuilds the signed approval from it and checks the signature with this
   deployment's approval key, so a record signed by any other key is refused.
4. The approval binds the rate extract digest, the staged run, and the SkuMap digest. The staged
   ``rate-extract.json`` prices only when its canonical digest equals the signed extract digest, and the
   approved SkuMap digest must equal the API's current SkuMap.

The artifact's rows are not re-hashed here: publishing copied them pinned to the approved rows' ETag (or
hashed them in full), and the publication record pins the artifact ETag, which changes on any write.
Freshness is checked on every comparison by the engine, not only when the snapshot loads.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from app.approvals import (
    MAX_RUN_FILE_BYTES,
    ApprovalBackend,
    ApprovalConfigurationError,
    ApprovalNotConfigured,
    StagedArtifactInvalid,
    StagedRunNotFound,
    StorageUnavailable,
    _strict_json,
    digest_of,
    record_signature_problem,
)
from app.harvester_contract import (
    PublicationError,
    ValidationError,
    published_header,
    verify_published_manifest,
)
from app.publication import (
    MAX_POINTER_BYTES,
    POINTER_NAME,
    artifact_name,
    publication_record_name,
    read_pointer,
    _read_record,
)

MAX_HEADER_BYTES = 256 * 1024
MAX_POINTER_VERSIONS = 1000
EXTRACT_FILE = "rate-extract.json"
_DIGEST = re.compile(r"[0-9a-f]{64}")
_RUN_ID = re.compile(r"[0-9a-f]{32}")


class PublishedPriceBookError(RuntimeError):
    pass


@dataclass(frozen=True)
class PublishedPriceBook:
    extract: dict[str, Any]
    extract_digest: str
    snapshot_id: str
    content_hash: str
    publishing_human: str
    approved_skumap_digest: str
    non_production: bool
    pointer_hash: str


def _fail(message: str) -> PublishedPriceBookError:
    return PublishedPriceBookError(f"The Published PriceBook can't be used: {message}")


def load_published_pricebook(backend: ApprovalBackend, *, skumap_digest: str) -> PublishedPriceBook:
    try:
        return _load(backend, skumap_digest)
    except (StorageUnavailable, ApprovalNotConfigured, ApprovalConfigurationError) as error:
        raise _fail(str(error)) from error


def _load(backend: ApprovalBackend, skumap_digest: str) -> PublishedPriceBook:
    pointer, current_hash, _ = read_pointer(backend)
    if pointer is None or current_hash is None:
        raise _fail("no PriceBook snapshot is Published yet.")
    snapshot_id = pointer.get("snapshotId")
    content_hash = pointer.get("contentHash")
    if (
        not isinstance(snapshot_id, str)
        or not snapshot_id
        or pointer.get("artifact") != artifact_name(snapshot_id)
        or not isinstance(content_hash, str)
        or not _DIGEST.fullmatch(content_hash)
    ):
        raise _fail("the current pointer is unreadable.")

    record = _read_record(backend, publication_record_name(snapshot_id))
    if record is None:
        raise _fail(f"the publication of {snapshot_id} is not recorded yet.")
    if not (
        isinstance(record, dict)
        and record.get("recordType") == "Publication"
        and record.get("snapshotId") == snapshot_id
        and record.get("artifact") == pointer.get("artifact")
        and record.get("contentHash") == content_hash
        and record.get("previousSnapshotId") == pointer.get("previousSnapshotId")
        and isinstance(record.get("runId"), str)
        and _RUN_ID.fullmatch(record["runId"])
        and isinstance(record.get("artifactEtag"), str)
    ):
        raise _fail("the publication record does not match the current pointer.")
    run_id = record["runId"]
    _refuse_superseded(backend, snapshot_id)

    name = artifact_name(snapshot_id)
    properties = backend.store.published_properties(name)
    if properties is None:
        raise _fail("the Published artifact is missing.")
    if properties.etag != record["artifactEtag"]:
        raise _fail("the Published artifact changed after it was published.")
    try:
        head = backend.store.read_published_head(
            name, min(properties.size, MAX_HEADER_BYTES), etag=record["artifactEtag"]
        )
    except StagedArtifactInvalid as error:
        raise _fail("the Published artifact changed after it was published.") from error
    end = head.find(b"\n")
    if end < 0:
        raise _fail("the Published artifact has no manifest line.")
    try:
        header = _strict_json(head[:end], "The Published manifest")
    except ValueError as error:
        raise _fail(str(error)) from error
    if not (
        isinstance(header, dict)
        and set(header) == {"recordType", "manifest"}
        and header["recordType"] == "manifest"
        and isinstance(header["manifest"], dict)
    ):
        raise _fail("the Published artifact has no manifest record.")
    manifest = header["manifest"]
    if published_header(manifest) != head[: end + 1]:
        raise _fail("the Published manifest is not canonical.")
    try:
        verify_published_manifest(
            manifest,
            approval_verifier=lambda approval: record_signature_problem(approval, backend.signer)
            is None,
        )
    except (PublicationError, ValidationError) as error:
        raise _fail(f"its signed approval does not verify: {error}") from error

    extract_digest = manifest.get("approvedExtractDigest")
    if not (
        manifest.get("snapshotId") == snapshot_id
        and manifest.get("contentHash") == content_hash
        and manifest.get("approvedStagedRunId") == run_id
        and isinstance(manifest.get("approvalKeyId"), str)
        and isinstance(extract_digest, str)
        and _DIGEST.fullmatch(extract_digest)
    ):
        raise _fail("the signed approval does not bind this snapshot's run and rate extract.")
    approved_skumap = manifest.get("skuMapDigest")
    if approved_skumap != skumap_digest:
        raise _fail(
            f"{snapshot_id} was approved against a different SkuMap. Review and publish a new snapshot."
        )

    extract_name = f"staging/{snapshot_id}/{run_id}/{EXTRACT_FILE}"
    try:
        blob = backend.store.read_staged(extract_name, etag=None, max_bytes=MAX_RUN_FILE_BYTES)
    except StagedRunNotFound as error:
        raise _fail("the approved rate extract is missing.") from error
    except StagedArtifactInvalid as error:
        raise _fail(f"the approved rate extract is unreadable: {error}") from error
    try:
        extract = _strict_json(blob.data, "The rate extract")
    except ValueError as error:
        raise _fail(str(error)) from error
    if digest_of(extract) != extract_digest:
        raise _fail("the rate extract does not match its approved digest.")
    _check_extract(extract, snapshot_id, content_hash)

    publisher = manifest.get("publishingHuman")
    if not isinstance(publisher, str) or not publisher.strip():
        raise _fail("the Published manifest names no publishing human.")
    return PublishedPriceBook(
        extract=extract,
        extract_digest=extract_digest,
        snapshot_id=snapshot_id,
        content_hash=content_hash,
        publishing_human=publisher,
        approved_skumap_digest=approved_skumap,
        # Mutable-pilot evidence stays labeled non-production until WORM retention exists.
        non_production=manifest.get("nonProduction") is not False
        or extract["manifest"]["nonProduction"] is not False,
        pointer_hash=current_hash,
    )


def _refuse_superseded(backend: ApprovalBackend, snapshot_id: str) -> None:
    """Refuse a pointer moved back to an older snapshot.

    Publishing only moves the pointer forward (compare-and-swap on the snapshot a run was validated against),
    and each publication record names the snapshot it replaced. So if any record names this snapshot as its
    predecessor, the pointer was rolled back outside the API. A record that is missing, unreadable, malformed,
    deleted, or overwritten fails closed, since it might be that successor. This holds only while the history
    can't be erased: an account owner who purges record versions can still roll back until WORM retention."""
    own = publication_record_name(snapshot_id)
    try:
        names = backend.store.list_publication_records()
        history = backend.store.control_history(
            POINTER_NAME, max_bytes=MAX_POINTER_BYTES, max_versions=MAX_POINTER_VERSIONS
        )
    except StagedArtifactInvalid as error:
        raise _fail(f"{error} A rollback can't be ruled out.") from error
    _refuse_pointer_history(history, snapshot_id)
    first_publications = 0
    for name in names:
        record = _read_record(backend, name)
        if not isinstance(record, dict) or not _well_formed_record(record, name):
            raise _fail(f"the publication record {name} is missing or unreadable, so a rollback can't be ruled out.")
        if record["previousSnapshotId"] is None:
            first_publications += 1
        if name == own:
            continue
        if record["previousSnapshotId"] == snapshot_id:
            successor = record.get("snapshotId")
            raise _fail(
                f"{snapshot_id} was replaced by {successor}, and the current pointer was moved back to it "
                "outside the API. Publish a new harvest instead of rolling back."
            )
    if first_publications > 1:
        raise _fail("more than one publication replaced no snapshot, so a rollback can't be ruled out.")


def _refuse_pointer_history(history: list[bytes], snapshot_id: str) -> None:
    """Refuse when the pointer's retained versions show it named this snapshot, moved on, and came back."""
    names: list[str] = []
    for version in history:
        try:
            pointer = json.loads(version)
        except ValueError:
            pointer = None
        name = pointer.get("snapshotId") if isinstance(pointer, dict) else None
        if not isinstance(name, str):
            raise _fail("a version of the current pointer is unreadable, so a rollback can't be ruled out.")
        if not names or names[-1] != name:
            names.append(name)
    if names and names[-1] == snapshot_id:
        names.pop()
    if snapshot_id in names:
        raise _fail(
            f"the current pointer was moved off {snapshot_id} and later moved back to it outside the API. "
            "Publish a new harvest instead of rolling back."
        )


def _well_formed_record(record: dict[str, Any], name: str) -> bool:
    previous = record.get("previousSnapshotId", False)
    return (
        record.get("recordType") == "Publication"
        and isinstance(record.get("snapshotId"), str)
        and publication_record_name(record["snapshotId"]) == name
        and (previous is None or isinstance(previous, str))
    )


def _check_extract(extract: Any, snapshot_id: str, content_hash: str) -> None:
    if not isinstance(extract, dict):
        raise _fail("the rate extract is not an object.")
    manifest = extract.get("manifest")
    rates = extract.get("rates")
    assumed = extract.get("assumedRates", {})
    sources = extract.get("rateSources")
    if not (
        isinstance(manifest, dict)
        and isinstance(rates, dict)
        and rates
        and isinstance(assumed, dict)
        and isinstance(sources, dict)
        and isinstance(extract.get("notice"), str)
        and extract["notice"].strip()
    ):
        raise _fail("the rate extract is incomplete.")
    if manifest.get("sourceSnapshotId") != snapshot_id or manifest.get("sourceContentHash") != content_hash:
        raise _fail("the rate extract is not derived from the Published snapshot.")
    if not (
        isinstance(manifest.get("snapshotId"), str)
        and manifest["snapshotId"]
        and isinstance(manifest.get("schemaVersion"), str)
        and isinstance(manifest.get("nonProduction"), bool)
        and isinstance(manifest.get("sourceUrls"), list)
        and all(isinstance(url, str) for url in manifest["sourceUrls"])
    ):
        raise _fail("the rate extract manifest is incomplete.")
    priced_as_of(extract)
    if set(rates) & set(assumed) or {*rates, *assumed} != set(sources):
        raise _fail("every rate must have exactly one provenance record.")
    for key, value in {**rates, **assumed}.items():
        try:
            number = Decimal(value) if isinstance(value, str) else None
        except InvalidOperation:
            number = None
        if number is None or not number.is_finite() or number < 0:
            raise _fail(f"the rate {key} is not a non-negative decimal string.")
        if not isinstance(sources[key], dict):
            raise _fail(f"the rate {key} has no provenance record.")


def priced_as_of(extract: dict[str, Any]) -> date:
    value = extract.get("manifest", {}).get("pricedAsOf")
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise _fail("the rate extract has no valid pricedAsOf date.")
