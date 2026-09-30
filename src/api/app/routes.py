from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response

from app.auth import CurrentPrincipal, EstimatorPrincipal, current_principal
from app.data import (
    ApplicationNotFoundError,
    ExportLockedError,
    GapResolutionError,
    RepositoryCapacityError,
    WORKBOOK_EXPORT_RETRY_SECONDS,
    WorkbookExportBusyError,
    application_max_age_hours,
    application_repository,
)
from app.intake import IntakeValidationError, MAX_UPLOAD_BYTES
from app.models import ApplicationDetail, ApplicationSummary, GapResolutionRequest
from app.pricing import PricingConfigurationError, pricing_engine, presentation_policy
from app.workbook_export import UnsafeSourceWorkbookError

router = APIRouter(prefix="/api", dependencies=[Depends(current_principal)])

STALE_HARVEST_DAYS = 30


def is_stale_harvest(
    priced_as_of: str,
    non_production: bool,
    today: date,
    stale_after_days: int = STALE_HARVEST_DAYS,
) -> bool:
    """A Published harvest older than ``stale_after_days`` (30 by default) is stale. Frozen demo sets never are.
    An unreadable date on a Published harvest is treated as stale so the warning shows."""
    if non_production:
        return False
    try:
        as_of = date.fromisoformat(priced_as_of[:10])
    except ValueError:
        return True
    return (today - as_of).days > stale_after_days


@router.get(
    "/applications",
    response_model=list[ApplicationSummary],
    tags=["Applications"],
)
async def list_applications(_: EstimatorPrincipal) -> list[ApplicationSummary]:
    return await run_in_threadpool(application_repository.list)


@router.get(
    "/applications/{application_id}",
    response_model=ApplicationDetail,
    tags=["Applications"],
)
async def get_application(application_id: UUID, _: EstimatorPrincipal) -> ApplicationDetail:
    try:
        return await run_in_threadpool(application_repository.get, application_id)
    except ApplicationNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from error
    except PricingConfigurationError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from error


@router.post(
    "/intakes",
    response_model=ApplicationDetail,
    status_code=status.HTTP_201_CREATED,
    tags=["Intakes"],
)
async def upload_intake(
    principal: EstimatorPrincipal,
    file: UploadFile = File(...),
) -> ApplicationDetail:
    filename = Path(file.filename or "").name
    if not filename.casefold().endswith(".xlsx"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="The Intake filename must end with .xlsx.",
        )
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    try:
        return await run_in_threadpool(
            application_repository.create_from_intake,
            filename,
            content,
            principal.record(),
        )
    except IntakeValidationError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error
    except RepositoryCapacityError as error:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(error),
            headers={"Retry-After": str(error.retry_after_seconds)},
        ) from error
    except PricingConfigurationError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from error


@router.post(
    "/applications/{application_id}/gaps/{gap_id}/resolve",
    response_model=ApplicationDetail,
    tags=["Gaps"],
)
async def resolve_gap(
    application_id: UUID,
    gap_id: str,
    request: GapResolutionRequest,
    principal: EstimatorPrincipal,
) -> ApplicationDetail:
    try:
        return await run_in_threadpool(
            application_repository.resolve_gap,
            application_id,
            gap_id,
            request,
            principal.record(),
        )
    except ApplicationNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from error
    except GapResolutionError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error
    except PricingConfigurationError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from error


@router.get(
    "/applications/{application_id}/comparison/export",
    tags=["Comparisons"],
)
async def export_comparison(application_id: UUID, _: EstimatorPrincipal) -> Response:
    try:
        content = await run_in_threadpool(application_repository.export, application_id)
    except ApplicationNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from error
    except ExportLockedError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    return Response(
        content=content,
        media_type="application/json",
        headers={
            "Content-Disposition": (
                f'attachment; filename="comparison-{application_id}.json"'
            )
        },
    )


@router.get(
    "/applications/{application_id}/comparison/workbook",
    tags=["Comparisons"],
)
async def export_priced_workbook(
    application_id: UUID,
    _: EstimatorPrincipal,
    view: Literal["azure", "aws", "both"] | None = Query(default=None),
) -> Response:
    try:
        content, source_filename = await run_in_threadpool(
            application_repository.export_workbook,
            application_id,
            view,
        )
    except ApplicationNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from error
    except ExportLockedError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    except UnsafeSourceWorkbookError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
    except WorkbookExportBusyError as error:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(error),
            headers={"Retry-After": str(WORKBOOK_EXPORT_RETRY_SECONDS)},
        ) from error
    display_filename = f"{Path(source_filename).stem}-priced.xlsx"
    fallback_filename = f"priced-{application_id}.xlsx"
    content_disposition = (
        f'attachment; filename="{fallback_filename}"; '
        f"filename*=UTF-8''{quote(display_filename, safe='')}"
    )
    return Response(
        content=content,
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={"Content-Disposition": content_disposition},
    )


@router.get("/capabilities", tags=["metadata"])
async def capabilities() -> dict[str, object]:
    try:
        show_aws = presentation_policy().show_aws
        manifest = await run_in_threadpool(pricing_engine.admissible_manifest)
    except PricingConfigurationError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from error
    # A Published harvest is flagged stale no later than the engine stops pricing it.
    stale_after_days = (
        STALE_HARVEST_DAYS
        if pricing_engine.source == "demo-extract"
        else min(STALE_HARVEST_DAYS, pricing_engine.max_age_days)
    )
    return {
        "productLabel": "public-list run-rate benchmark",
        "applicationMaxAgeHours": application_max_age_hours(),
        "defaultMode": "both" if show_aws else "azure",
        "priceBook": {
            "source": pricing_engine.source,
            "snapshotId": manifest.snapshot_id,
            "pricedAsOf": manifest.priced_as_of,
            "nonProduction": manifest.non_production,
            "stale": is_stale_harvest(
                manifest.priced_as_of,
                # Only the frozen demo extract is exempt; mutable-pilot Published snapshots age too.
                pricing_engine.source == "demo-extract" and manifest.non_production,
                datetime.now(UTC).date(),
                stale_after_days,
            ),
            "staleAfterDays": stale_after_days,
        },
        "available": [
            "Bounded OpenXML Intake validation",
            "Cloud-neutral normalization",
            "Typed Gap resolution",
            "Decimal-only pricing",
            "CompletenessGate",
            "Auditable ReviewBaseline export",
            "Original-workbook-preserving priced Excel export",
            "Microsoft Entra sign-in with app roles",
            "In-tenant PriceBook harvesting with human review, approval, and publication",
        ],
        "deferred": [
            "Azure SQL and WORM persistence",
            "Full-catalog PriceBook harvesting",
            "Foundry narration",
        ],
    }


@router.get("/me", tags=["metadata"])
async def me(principal: CurrentPrincipal) -> dict[str, object]:
    return {
        "name": principal.name,
        "objectId": principal.object_id,
        "tenantId": principal.tenant_id,
        "roles": sorted(principal.roles),
    }
