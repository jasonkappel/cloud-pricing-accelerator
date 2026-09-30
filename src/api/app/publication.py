"""Publish an Approved staged run: a direct, authenticated SnapshotApprover action.

Publishing assembles ``{snapshotId}.pricebook.ndjson`` in the published container: one manifest line built
by the harvester's own contract (``build_published_manifest``), then the staged canonical rows copied
server-side. The rows copy is pinned to the ETag whose bytes were hashed when the run was approved, so
nothing reaches the artifact that was not approved. The artifact is created once and never overwritten.

Before the pointer moves, a write-once intent record pins the artifact's ETag; an existing artifact is
reused only under that ETag, or after its rows are hashed in full when no intent exists yet. Then the
current pointer (``current.json`` in the control container) moves by compare-and-swap, and only from the
exact pointer the run was validated against. If the price book moved since validation, the run can't be
published and a new harvest is needed. This keeps every Published snapshot compared against its
predecessor.

The publication record is written last. If that fails, the record is completed from what only the API
wrote (the intent, the pointer, and the artifact under the intent's ETag), never by re-validating the
staged run, which the harvester can still change: publishing the snapshot again completes it, and so does
publishing its successor. No snapshot replaces a current one whose record can't be completed.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from app.approvals import (
    MAX_CONTROL_BYTES,
    ROWS_FILE,
    ApprovalBackend,
    DecisionRefused,
    PointerConflict,
    RecordExists,
    RunState,
    StagedArtifactInvalid,
    StagedRun,
    _now,
    canonical_json,
    load_staged_run,
    record_signature_problem,
)
from app.auth import Principal, Role
from app.harvester_contract import PublicationError, build_published_manifest, published_header

logger = logging.getLogger(__name__)

POINTER_NAME = "current.json"
MAX_POINTER_BYTES = 8 * 1024


def artifact_name(snapshot_id: str) -> str:
    return f"{snapshot_id}.pricebook.ndjson"


def publication_record_name(snapshot_id: str) -> str:
    return f"approvals/{snapshot_id}/publication.json"


def publication_intent_name(snapshot_id: str) -> str:
    return f"approvals/{snapshot_id}/publication-intent.json"


def _read_record(backend: ApprovalBackend, name: str) -> dict[str, Any] | None | bool:
    """The parsed record, None when absent, or False when unreadable."""
    try:
        blob = backend.store.read_control(name, max_bytes=MAX_CONTROL_BYTES)
    except StagedArtifactInvalid:
        return False
    if blob is None:
        return None
    try:
        record = json.loads(blob.data)
    except ValueError:
        return False
    return record if isinstance(record, dict) else False


def read_pointer(backend: ApprovalBackend) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Return (pointer, sha256 of its bytes, etag); all None when nothing is published yet."""
    try:
        blob = backend.store.read_control(POINTER_NAME, max_bytes=MAX_POINTER_BYTES)
    except StagedArtifactInvalid:
        # Oversized: matches no baseline and no compare-and-swap, so nothing can publish over it.
        return {}, "unreadable", None
    if blob is None:
        return None, None, None
    try:
        pointer = json.loads(blob.data)
    except ValueError:
        pointer = None
    if not isinstance(pointer, dict):
        pointer = {}
    return pointer, hashlib.sha256(blob.data).hexdigest(), blob.etag


def _expected_header(backend: ApprovalBackend, run: StagedRun, *, verify: bool) -> bytes:
    """The artifact's first line; the approval signature is verified again only when publishing."""
    approval = run.approval
    assert approval is not None

    def verifier(record: dict[str, Any]) -> bool:
        if not verify:
            return record == approval  # _load_decisions already verified this record's signature.
        return record_signature_problem(record, backend.signer) is None

    try:
        manifest = build_published_manifest(
            run.manifest, run.validation, approval, approval_verifier=verifier
        )
    except PublicationError as error:
        raise DecisionRefused(f"This run can't be published: {error}") from error
    return published_header(manifest)


def _artifact_state(
    backend: ApprovalBackend, run: StagedRun, header: bytes
) -> tuple[bool | None, str | None]:
    """(True, etag) when the artifact's size and manifest are this run's; (None, None) when absent;
    (False, etag) when it differs. The rows are checked separately, against the intent's ETag or by hash."""
    name = artifact_name(run.snapshot_id)
    properties = backend.store.published_properties(name)
    if properties is None:
        return None, None
    rows_size = run.receipt["artifacts"][ROWS_FILE]["bytes"]
    if properties.size != len(header) + rows_size:
        return False, properties.etag
    try:
        head = backend.store.read_published_head(name, len(header), etag=properties.etag)
    except StagedArtifactInvalid:
        return False, properties.etag
    return head == header, properties.etag


def _pointer_names(pointer: dict[str, Any] | None, run: StagedRun) -> bool:
    return (
        isinstance(pointer, dict)
        and pointer.get("snapshotId") == run.snapshot_id
        and pointer.get("artifact") == artifact_name(run.snapshot_id)
        and pointer.get("contentHash") == run.receipt.get("contentHash")
    )


def _write_record(backend: ApprovalBackend, principal: Principal, intent: dict[str, Any]) -> None:
    record = {
        "recordType": "Publication",
        "snapshotId": intent.get("snapshotId"),
        "runId": intent.get("runId"),
        "artifact": intent.get("artifact"),
        "artifactEtag": intent.get("artifactEtag"),
        "contentHash": intent.get("contentHash"),
        "previousSnapshotId": intent.get("previousSnapshotId"),
        "requestedBy": intent.get("requesterDisplayName"),
        "requestedAt": intent.get("requestedAt"),
        "publisherId": principal.object_id,
        "publisherDisplayName": principal.name,
        "publishedAt": _now(),
    }
    try:
        backend.store.create_control(
            publication_record_name(str(intent.get("snapshotId"))),
            f"{canonical_json(record)}\n".encode("utf-8"),
        )
    except RecordExists:
        pass


def _complete_from_intent(
    backend: ApprovalBackend, principal: Principal, snapshot_id: str, pointer: dict[str, Any] | None
) -> bool:
    """Record a publication whose pointer moved but whose record is missing.

    Only API-written evidence is used: the write-once intent (written after the manifest signature was
    verified), the pointer naming it, and the append-only artifact still under the intent's ETag."""
    intent = _read_record(backend, publication_intent_name(snapshot_id))
    name = artifact_name(snapshot_id)
    if not (
        isinstance(intent, dict)
        and isinstance(pointer, dict)
        and intent.get("recordType") == "PublicationIntent"
        and intent.get("snapshotId") == snapshot_id == pointer.get("snapshotId")
        and intent.get("artifact") == name == pointer.get("artifact")
        and intent.get("contentHash") == pointer.get("contentHash")
        and intent.get("previousSnapshotId") == pointer.get("previousSnapshotId")
        and isinstance(intent.get("runId"), str)
    ):
        return False
    properties = backend.store.published_properties(name)
    if properties is None or properties.etag != intent.get("artifactEtag"):
        return False
    _write_record(backend, principal, intent)
    logger.info("Snapshot publication completed", extra={"snapshotId": snapshot_id})
    return True


def attach_publication(
    backend: ApprovalBackend,
    runs: list[StagedRun],
    pointer_state: tuple[dict[str, Any] | None, str | None, str | None] | None = None,
) -> None:
    """Load each run's publication status; call once per request with all the runs it returns."""
    pointer, pointer_hash, _ = pointer_state or read_pointer(backend)
    current_snapshot = pointer.get("snapshotId") if isinstance(pointer, dict) else None
    for run in runs:
        if run.problems or run.approval is None:
            continue
        record = _read_record(backend, publication_record_name(run.snapshot_id))
        intent = _read_record(backend, publication_intent_name(run.snapshot_id))
        if record is False or (record is not None and record.get("runId") != run.run_id):
            run.problems.append("The publication record does not describe this run.")
            continue
        if intent is False or (intent is not None and intent.get("runId") != run.run_id):
            run.problems.append("The publication intent does not describe this run.")
            continue
        try:
            header = _expected_header(backend, run, verify=False)
        except DecisionRefused as error:
            run.problems.append(str(error))
            continue
        matches, etag = _artifact_state(backend, run, header)
        if matches is False:
            run.problems.append("A different artifact is already published under this snapshot ID.")
            continue
        if matches and intent is not None and intent.get("artifactEtag") != etag:
            run.problems.append("The published artifact changed after it was assembled.")
            continue
        is_current = matches is True and _pointer_names(pointer, run)
        run.publication = {
            "published": matches is True and record is not None,
            "completionPending": is_current and record is None and intent is not None,
            "artifact": artifact_name(run.snapshot_id) if matches else None,
            "current": is_current,
            "currentSnapshotId": current_snapshot if isinstance(current_snapshot, str) else None,
            "pointerMatchesBaseline": pointer_hash == run.validation.get("baselinePointerHash")
            and (pointer or {}).get("snapshotId") == run.validation.get("baselineSnapshotId"),
            "publishedAt": record.get("publishedAt") if record else None,
            "publishedBy": record.get("publisherDisplayName") if record else None,
        }


def publish_run(
    backend: ApprovalBackend,
    principal: Principal,
    *,
    snapshot_id: str,
    run_id: str,
    evidence_digest: str,
    current_skumap_digest: str,
) -> StagedRun:
    if Role.SNAPSHOT_APPROVER.value not in principal.roles:
        raise DecisionRefused("Only a SnapshotApprover can publish.")
    pointer_state = read_pointer(backend)
    pointer = pointer_state[0]
    if (
        isinstance(pointer, dict)
        and pointer.get("snapshotId") == snapshot_id
        and _read_record(backend, publication_record_name(snapshot_id)) is None
    ):
        intent = _read_record(backend, publication_intent_name(snapshot_id))
        if isinstance(intent, dict) and intent.get("runId") == run_id:
            # The approver attested when the intent was written; the staged run is not re-validated.
            if not _complete_from_intent(backend, principal, snapshot_id, pointer):
                raise DecisionRefused(
                    "This publication can't be completed: its intent, pointer, and artifact don't agree."
                )
            completed = load_staged_run(backend, current_skumap_digest, snapshot_id, run_id)
            attach_publication(backend, [completed])
            return completed
    run = load_staged_run(backend, current_skumap_digest, snapshot_id, run_id)
    attach_publication(backend, [run], pointer_state)
    if run.problems:
        raise DecisionRefused("This staged run is blocked: " + " ".join(run.problems))
    if evidence_digest != run.evidence_digest:
        raise DecisionRefused("The staged run changed since you loaded it. Reload and check again.")
    if run.state == RunState.PUBLISHED:
        raise DecisionRefused("This snapshot is already published.")
    if run.state != RunState.APPROVED or run.approval is None:
        raise DecisionRefused("Only an Approved run can be published.")
    pointer, pointer_hash, pointer_etag = pointer_state
    if pointer_hash != run.validation.get("baselinePointerHash") or (
        (pointer or {}).get("snapshotId") != run.validation.get("baselineSnapshotId")
    ):
        raise DecisionRefused(
            "The published price book changed after this run was validated. Start a new harvest."
        )
    # Never move the pointer off a snapshot whose publication is not recorded.
    previous = (pointer or {}).get("snapshotId")
    if not isinstance(previous, str):
        # Only the first publication may replace nothing; a missing pointer after that is restored, not rebuilt.
        try:
            published_before = backend.store.list_publication_records()
        except StagedArtifactInvalid as error:
            raise DecisionRefused(f"The publication history can't be checked: {error}") from error
        if published_before:
            raise DecisionRefused(
                "Snapshots were published before but the current pointer is missing. Restore it; don't publish over it."
            )
    else:
        previous_record = _read_record(backend, publication_record_name(previous))
        if previous_record is False or (
            previous_record is None and not _complete_from_intent(backend, principal, previous, pointer)
        ):
            raise DecisionRefused(
                f"Publishing {previous} did not finish and can't be completed from its evidence."
            )

    header = _expected_header(backend, run, verify=True)
    name = artifact_name(run.snapshot_id)
    rows = run.receipt["artifacts"][ROWS_FILE]
    content_hash = run.receipt["contentHash"]
    intent = _read_record(backend, publication_intent_name(run.snapshot_id))
    matches, etag = _artifact_state(backend, run, header)
    fresh = False
    if matches is None:
        try:
            etag = backend.store.assemble_published(
                name,
                header,
                rows_name=f"staging/{run.snapshot_id}/{run.run_id}/{ROWS_FILE}",
                rows_etag=rows["etag"],
                rows_size=rows["bytes"],
            )
            matches, fresh = True, True
        except StagedArtifactInvalid as error:
            raise DecisionRefused(f"The canonical rows could not be copied: {error}") from error
        except RecordExists:
            # A concurrent publish of this same run; check what it wrote like any existing artifact.
            matches, etag = _artifact_state(backend, run, header)
            intent = _read_record(backend, publication_intent_name(run.snapshot_id))
    if matches is not True or etag is None:
        raise DecisionRefused("A different artifact is already published under this snapshot ID.")
    if isinstance(intent, dict):
        if intent.get("artifactEtag") != etag:
            raise DecisionRefused("The published artifact changed after it was assembled.")
    elif intent is None:
        # Just assembled from the approved rows ETag, or left by an interrupted publish: reuse the latter
        # only if its rows hash to the approved content hash under the ETag the intent will pin.
        if not fresh:
            try:
                rows_hash = backend.store.hash_published(name, offset=len(header), etag=etag)
            except StagedArtifactInvalid as error:
                raise DecisionRefused(f"The published artifact could not be checked: {error}") from error
            if rows_hash != content_hash:
                raise DecisionRefused(
                    "A different artifact is already published under this snapshot ID."
                )
        intent = {
            "recordType": "PublicationIntent",
            "snapshotId": run.snapshot_id,
            "runId": run.run_id,
            "artifact": name,
            "artifactEtag": etag,
            "contentHash": content_hash,
            "previousSnapshotId": run.validation.get("baselineSnapshotId"),
            "requesterId": principal.object_id,
            "requesterDisplayName": principal.name,
            "requestedAt": _now(),
        }
        try:
            backend.store.create_control(
                publication_intent_name(run.snapshot_id),
                f"{canonical_json(intent)}\n".encode("utf-8"),
            )
        except RecordExists:
            intent = _read_record(backend, publication_intent_name(run.snapshot_id))
            if not isinstance(intent, dict) or intent.get("artifactEtag") != etag:
                raise DecisionRefused("The published artifact changed after it was assembled.")
    else:
        raise DecisionRefused("The publication intent does not describe this run.")
    if intent.get("runId") != run.run_id:
        raise DecisionRefused("The publication intent does not describe this run.")

    if intent.get("previousSnapshotId") != run.validation.get("baselineSnapshotId"):
        raise DecisionRefused("The publication intent does not describe this run.")

    new_pointer = {
        "snapshotId": run.snapshot_id,
        "contentHash": content_hash,
        "artifact": name,
        "previousSnapshotId": run.validation.get("baselineSnapshotId"),
    }
    try:
        backend.store.replace_control(
            POINTER_NAME, f"{canonical_json(new_pointer)}\n".encode("utf-8"), etag=pointer_etag
        )
    except PointerConflict as error:
        raise DecisionRefused(
            "The published price book changed while publishing. Reload and check again."
        ) from error
    _write_record(backend, principal, intent)
    logger.info("Snapshot published", extra={"snapshotId": run.snapshot_id, "runId": run.run_id})
    published = load_staged_run(backend, current_skumap_digest, snapshot_id, run_id)
    attach_publication(backend, [published])
    return published
