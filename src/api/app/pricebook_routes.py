"""PriceBook review and approval routes. Each decision is a direct, authenticated user-to-API action."""

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, ConfigDict, Field

from app.approvals import (
    ApprovalBackend,
    ApprovalConfigurationError,
    ApprovalNotConfigured,
    DecisionRefused,
    StagedArtifactInvalid,
    StagedRunNotFound,
    StorageUnavailable,
    approval_backend,
    approval_settings,
    ApprovalMode,
    list_staged_runs,
    load_staged_run,
    record_skumap_review,
    record_snapshot_approval,
    run_detail,
    run_summary,
)
from app.auth import (
    PriceBookViewerPrincipal,
    SkuMapReviewerPrincipal,
    SnapshotApproverPrincipal,
    current_principal,
)
from app.pricing import pricing_engine
from app.publication import attach_publication, publish_run

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/price-book", tags=["price-book"], dependencies=[Depends(current_principal)]
)

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
SnapshotId = Annotated[str, Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]
RunId = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]


class PublishDecision(BaseModel):
    """The evidence the approver saw; a changed run is refused rather than published."""

    model_config = ConfigDict(extra="forbid")

    evidenceDigest: Digest
    attested: Literal[True]


class SnapshotDecision(BaseModel):
    """The digests the person saw, so a changed run or SkuMap is refused rather than signed."""

    model_config = ConfigDict(extra="forbid")

    stageManifestDigest: Digest
    extractDigest: Digest
    evidenceDigest: Digest
    skuMapDigest: Digest
    attested: Literal[True]


def _backend() -> ApprovalBackend:
    try:
        return approval_backend()
    except ApprovalNotConfigured as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(error)) from error
    except ApprovalConfigurationError as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(error)) from error


def _unavailable(error: StorageUnavailable) -> HTTPException:
    logger.warning("Price book approval storage failed: %s", error)
    return HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "Price book storage or the approval signing key is unavailable. Try again later.",
    )


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "Staged run not found.")


@router.get("/staged")
def staged_runs(_: PriceBookViewerPrincipal) -> dict[str, object]:
    try:
        configured = approval_settings().mode != ApprovalMode.OFF
    except ApprovalConfigurationError as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(error)) from error
    if not configured:
        return {"configured": False, "runs": []}
    backend = _backend()
    try:
        runs = list_staged_runs(backend, pricing_engine.skumap_digest)
        attach_publication(backend, runs)
    except StorageUnavailable as error:
        raise _unavailable(error) from error
    return {"configured": True, "runs": [run_summary(run) for run in runs]}


@router.get("/staged/{snapshot_id}/{run_id}")
def staged_run(
    snapshot_id: SnapshotId, run_id: RunId, principal: PriceBookViewerPrincipal
) -> dict[str, object]:
    backend = _backend()
    try:
        run = load_staged_run(backend, pricing_engine.skumap_digest, snapshot_id, run_id)
        attach_publication(backend, [run])
    except StagedRunNotFound as error:
        raise _not_found() from error
    except StorageUnavailable as error:
        raise _unavailable(error) from error
    return run_detail(run, principal, pricing_engine.skumap_digest)


@router.post("/staged/{snapshot_id}/{run_id}/skumap-review")
def review_skumap(
    snapshot_id: SnapshotId,
    run_id: RunId,
    decision: SnapshotDecision,
    principal: SkuMapReviewerPrincipal,
) -> dict[str, object]:
    backend = _backend()
    try:
        run = record_skumap_review(
            backend,
            principal,
            snapshot_id=snapshot_id,
            run_id=run_id,
            stage_manifest_digest=decision.stageManifestDigest,
            extract_digest=decision.extractDigest,
            evidence_digest=decision.evidenceDigest,
            skumap_digest=decision.skuMapDigest,
            current_skumap_digest=pricing_engine.skumap_digest,
        )
        attach_publication(backend, [run])
    except StagedRunNotFound as error:
        raise _not_found() from error
    except DecisionRefused as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    except StorageUnavailable as error:
        raise _unavailable(error) from error
    return run_detail(run, principal, pricing_engine.skumap_digest)


@router.post("/staged/{snapshot_id}/{run_id}/approval")
def approve_snapshot(
    snapshot_id: SnapshotId,
    run_id: RunId,
    decision: SnapshotDecision,
    principal: SnapshotApproverPrincipal,
) -> dict[str, object]:
    backend = _backend()
    try:
        run = record_snapshot_approval(
            backend,
            principal,
            snapshot_id=snapshot_id,
            run_id=run_id,
            stage_manifest_digest=decision.stageManifestDigest,
            extract_digest=decision.extractDigest,
            evidence_digest=decision.evidenceDigest,
            skumap_digest=decision.skuMapDigest,
            current_skumap_digest=pricing_engine.skumap_digest,
        )
        attach_publication(backend, [run])
    except StagedRunNotFound as error:
        raise _not_found() from error
    except DecisionRefused as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    except StorageUnavailable as error:
        raise _unavailable(error) from error
    return run_detail(run, principal, pricing_engine.skumap_digest)


@router.post("/staged/{snapshot_id}/{run_id}/publish")
def publish_snapshot(
    snapshot_id: SnapshotId,
    run_id: RunId,
    decision: PublishDecision,
    principal: SnapshotApproverPrincipal,
) -> dict[str, object]:
    backend = _backend()
    try:
        run = publish_run(
            backend,
            principal,
            snapshot_id=snapshot_id,
            run_id=run_id,
            evidence_digest=decision.evidenceDigest,
            current_skumap_digest=pricing_engine.skumap_digest,
        )
    except StagedRunNotFound as error:
        raise _not_found() from error
    except (DecisionRefused, StagedArtifactInvalid) as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    except StorageUnavailable as error:
        raise _unavailable(error) from error
    # This instance verifies the new snapshot on its next use; other instances within the refresh interval.
    pricing_engine.invalidate()
    return run_detail(run, principal, pricing_engine.skumap_digest)
