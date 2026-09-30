import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from math import ceil
from threading import BoundedSemaphore, Lock
from uuid import UUID

from app.intake import parse_intake, sanitize_untrusted_text
from app.models import (
    Application,
    ApplicationDetail,
    ApplicationSummary,
    Comparison,
    ComparisonState,
    GapKind,
    GapResolutionRequest,
    GapStatus,
    IntakeSummary,
    NormalizedIntake,
    PresentationPolicy,
    PrincipalRecord,
)
from app.pricing import RULE_VERSION, pricing_engine
from app.workbook_export import build_priced_workbook

MAX_APPLICATIONS = 1000
DEFAULT_APPLICATION_MAX_AGE_HOURS = Decimal("1")
MAX_APPLICATION_MAX_AGE_HOURS = Decimal("8760")
MIN_APPLICATION_MAX_AGE_SECONDS = Decimal("60")
SECONDS_PER_HOUR = Decimal("3600")
MAX_STORED_WORKBOOK_BYTES = 100 * 1024 * 1024
WORKBOOK_EXPORT_RETRY_SECONDS = 5
workbook_export_slots = BoundedSemaphore(value=1)


class RepositoryCapacityError(RuntimeError):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("The application registry has reached its capacity.")
        self.retry_after_seconds = retry_after_seconds


class ApplicationConfigurationError(ValueError):
    pass


def application_max_age_hours() -> Decimal:
    raw = os.getenv("APPLICATION_MAX_AGE_HOURS")
    if raw is None or raw == "":
        return DEFAULT_APPLICATION_MAX_AGE_HOURS
    try:
        hours = Decimal(raw.strip())
    except InvalidOperation as error:
        raise ApplicationConfigurationError(
            "APPLICATION_MAX_AGE_HOURS must be a positive number of hours."
        ) from error
    if (
        not hours.is_finite()
        or hours * SECONDS_PER_HOUR < MIN_APPLICATION_MAX_AGE_SECONDS
        or hours > MAX_APPLICATION_MAX_AGE_HOURS
    ):
        raise ApplicationConfigurationError(
            "APPLICATION_MAX_AGE_HOURS must be a number of hours of at least one minute and "
            f"at most {MAX_APPLICATION_MAX_AGE_HOURS}."
        )
    return hours


def application_max_age() -> timedelta:
    microseconds = application_max_age_hours() * SECONDS_PER_HOUR * Decimal("1000000")
    return timedelta(microseconds=int(microseconds))


class ApplicationNotFoundError(KeyError):
    pass


def _sanitized_principal(principal: PrincipalRecord | None) -> PrincipalRecord | None:
    if principal is None:
        return None
    return PrincipalRecord(
        tenant_id=sanitize_untrusted_text(principal.tenant_id),
        object_id=sanitize_untrusted_text(principal.object_id),
        name=sanitize_untrusted_text(principal.name),
    )


class GapResolutionError(ValueError):
    pass


class ExportLockedError(RuntimeError):
    pass


class WorkbookExportBusyError(RuntimeError):
    pass


@dataclass
class ApplicationRecord:
    application: Application
    normalized: NormalizedIntake
    comparison: Comparison
    source_workbook: bytes


class InMemoryApplicationRepository:
    """Process-local store. Replace with Azure SQL before production use."""

    def __init__(self) -> None:
        self._records: dict[UUID, ApplicationRecord] = {}
        self._lock = Lock()

    def is_ready(self) -> bool:
        return True

    def list(self) -> list[ApplicationSummary]:
        with self._lock:
            self._remove_expired(datetime.now(UTC))
            max_age = application_max_age()
            return sorted(
                (self._summary(record, max_age) for record in self._records.values()),
                key=lambda application: application.created_at,
                reverse=True,
            )

    def create_from_intake(
        self,
        filename: str,
        content: bytes,
        principal: PrincipalRecord | None = None,
    ) -> ApplicationDetail:
        normalized = parse_intake(content)
        application = Application(
            name=normalized.application_name,
            intake_file_name=sanitize_untrusted_text(filename),
            created_by=_sanitized_principal(principal),
        )
        comparison = pricing_engine.compare(
            application,
            normalized.compute_units,
            normalized.database_units,
            normalized.storage_units,
            normalized.gaps,
        )
        record = ApplicationRecord(
            application=application,
            normalized=normalized,
            comparison=comparison,
            source_workbook=content,
        )
        with self._lock:
            now = datetime.now(UTC)
            self._remove_expired(now)
            stored_workbook_bytes = sum(
                len(record.source_workbook) for record in self._records.values()
            )
            if (
                len(self._records) >= MAX_APPLICATIONS
                or stored_workbook_bytes + len(content) > MAX_STORED_WORKBOOK_BYTES
            ):
                oldest = min(
                    item.application.created_at for item in self._records.values()
                )
                retry_after_seconds = max(
                    1,
                    ceil((oldest + application_max_age() - now).total_seconds()),
                )
                raise RepositoryCapacityError(retry_after_seconds)
            self._records[application.id] = record
            return self._detail(record)

    def get(self, application_id: UUID) -> ApplicationDetail:
        with self._lock:
            self._remove_expired(datetime.now(UTC))
            record = self._records.get(application_id)
            if record is None:
                raise ApplicationNotFoundError(application_id)
            return self._detail(record)

    def resolve_gap(
        self,
        application_id: UUID,
        gap_id: str,
        request: GapResolutionRequest,
        principal: PrincipalRecord | None = None,
    ) -> ApplicationDetail:
        with self._lock:
            self._remove_expired(datetime.now(UTC))
            current = self._records.get(application_id)
            if current is None:
                raise ApplicationNotFoundError(application_id)

            normalized = current.normalized.model_copy(deep=True)
            gap = next((item for item in normalized.gaps if item.id == gap_id), None)
            if gap is None:
                raise GapResolutionError("The requested Gap does not exist.")
            if gap.status == GapStatus.RESOLVED:
                raise GapResolutionError("The Gap is already resolved.")

            if gap.kind == GapKind.RUNTIME_HOURS:
                if request.runtime_hours_month is None:
                    raise GapResolutionError("runtime_hours_month is required for this Gap.")
                unit = next(
                    (item for item in normalized.compute_units if item.id == gap.unit_id),
                    None,
                )
                if unit is None:
                    raise GapResolutionError("The Gap does not reference a ComputeUnit.")
                unit.runtime_hours_month = request.runtime_hours_month
                canonical_value = {
                    "runtime_hours_month": str(request.runtime_hours_month),
                    "unit": "hours/month",
                }
            elif gap.kind == GapKind.APPROVED_REGIONS:
                if request.azure_region != "eastus2" or request.aws_region != "us-east-1":
                    raise GapResolutionError(
                        "The PriceBook supports only Azure eastus2 and AWS us-east-1."
                    )
                canonical_value = {
                    "azure_region": request.azure_region,
                    "aws_region": request.aws_region,
                }
            elif gap.kind == GapKind.STORAGE_PERFORMANCE:
                if request.target_iops is None or request.target_mbps is None:
                    raise GapResolutionError(
                        "target_iops and target_mbps are required for this Gap."
                    )
                unit = next(
                    (item for item in normalized.storage_units if item.id == gap.unit_id),
                    None,
                )
                if unit is None:
                    raise GapResolutionError("The Gap does not reference a StorageUnit.")
                unit.storage_performance_profile.target_iops = request.target_iops
                unit.storage_performance_profile.target_mbps = request.target_mbps
                canonical_value = {
                    "target_iops": request.target_iops,
                    "target_mbps": str(request.target_mbps),
                    "units": {"target_iops": "IOPS", "target_mbps": "MB/s"},
                }
            elif gap.kind == GapKind.LICENSE_ELIGIBILITY:
                if gap.license_product == "Windows Server":
                    required = (
                        request.active_software_assurance,
                        request.azure_hybrid_benefit_eligible,
                        request.acquired_before_2019_10_01,
                        request.perpetual_license,
                        request.eligible_product_version,
                    )
                    if any(value is None for value in required):
                        raise GapResolutionError(
                            "Windows licensing requires SA, AHB, license vintage, "
                            "perpetual-license, and product-version answers."
                        )
                    request.aws_license_mobility_eligible = False
                    request.passive_secondary = False
                    request.passive_use_only = False
                    request.azure_sql_deployment_model = None
                elif gap.license_product == "SQL Server":
                    required = (
                        request.active_software_assurance,
                        request.azure_hybrid_benefit_eligible,
                        request.aws_license_mobility_eligible,
                        request.acquired_before_2019_10_01,
                        request.perpetual_license,
                        request.eligible_product_version,
                        request.passive_secondary,
                        request.azure_sql_deployment_model,
                    )
                    if any(value is None for value in required):
                        raise GapResolutionError(
                            "SQL licensing requires SA, AHB, License Mobility, license "
                            "vintage, license type/version, passive-secondary, and "
                            "deployment-model answers."
                        )
                    if request.passive_secondary and request.passive_use_only is None:
                        raise GapResolutionError(
                            "passive_use_only is required for a passive secondary."
                        )
                    if not request.passive_secondary and request.passive_use_only:
                        raise GapResolutionError(
                            "passive_use_only cannot be true without a passive secondary."
                        )
                else:
                    raise GapResolutionError(
                        "The license Gap does not identify a supported product."
                    )
                unit = next(
                    (
                        item
                        for item in [
                            *normalized.compute_units,
                            *normalized.database_units,
                        ]
                        if item.id == gap.unit_id
                    ),
                    None,
                )
                if unit is None:
                    raise GapResolutionError(
                        "The Gap does not reference a licensable unit."
                    )
                eligibility = unit.license_eligibility
                eligibility.active_software_assurance = (
                    request.active_software_assurance
                )
                eligibility.azure_hybrid_benefit_eligible = (
                    request.azure_hybrid_benefit_eligible
                )
                eligibility.aws_license_mobility_eligible = (
                    request.aws_license_mobility_eligible
                )
                eligibility.acquired_before_2019_10_01 = (
                    request.acquired_before_2019_10_01
                )
                eligibility.perpetual_license = request.perpetual_license
                eligibility.eligible_product_version = (
                    request.eligible_product_version
                )
                eligibility.azure_sql_deployment_model = (
                    request.azure_sql_deployment_model
                )
                eligibility.passive_secondary = request.passive_secondary is True
                eligibility.passive_use_only = request.passive_use_only
                canonical_value = eligibility.model_dump(mode="json")
            else:
                raise GapResolutionError("The Gap kind is not supported.")

            gap.status = GapStatus.RESOLVED
            gap.canonical_value = canonical_value
            gap.resolved_by = sanitize_untrusted_text(request.resolved_by)
            gap.resolved_by_principal = _sanitized_principal(principal)
            gap.resolved_at = datetime.now(UTC)

        # Priced outside the repository lock: loading a Published PriceBook can wait on storage.
        comparison = pricing_engine.compare(
            current.application,
            normalized.compute_units,
            normalized.database_units,
            normalized.storage_units,
            normalized.gaps,
        )
        updated = ApplicationRecord(
            application=current.application,
            normalized=normalized,
            comparison=comparison,
            source_workbook=current.source_workbook,
        )
        with self._lock:
            if self._records.get(application_id) is not current:
                raise GapResolutionError(
                    "The Application changed while this Gap was being resolved. Reload it and try again."
                )
            self._records[application_id] = updated
            return self._detail(updated)

    def export(self, application_id: UUID) -> bytes:
        detail = self.get(application_id)
        if detail.comparison.state != ComparisonState.REVIEW_BASELINE:
            raise ExportLockedError("Export remains locked until the CompletenessGate passes.")
        bundle = {
            "schema_version": "pilot-evidence-bundle-v1",
            "calculator_rule_version": RULE_VERSION,
            "application": detail.application.model_dump(mode="json"),
            "normalized_facts": {
                "compute_units": [
                    unit.model_dump(mode="json") for unit in detail.compute_units
                ],
                "database_units": [
                    unit.model_dump(mode="json") for unit in detail.database_units
                ],
                "storage_units": [
                    unit.model_dump(mode="json") for unit in detail.storage_units
                ],
                "gaps": [gap.model_dump(mode="json") for gap in detail.gaps],
            },
            "comparison": detail.comparison.model_dump(mode="json"),
        }
        return json.dumps(
            bundle,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")

    def export_workbook(
        self,
        application_id: UUID,
        view: str | None = None,
    ) -> tuple[bytes, str]:
        if view not in {None, "azure", "aws", "both"}:
            raise ValueError("view must be 'azure', 'aws', or 'both'.")
        if not workbook_export_slots.acquire(blocking=False):
            raise WorkbookExportBusyError(
                "Another priced workbook is being generated. Retry shortly."
            )
        try:
            with self._lock:
                self._remove_expired(datetime.now(UTC))
                record = self._records.get(application_id)
                if record is None:
                    raise ApplicationNotFoundError(application_id)
                if record.comparison.state != ComparisonState.REVIEW_BASELINE:
                    raise ExportLockedError(
                        "Workbook export remains locked until the CompletenessGate passes."
                    )
                detail = self._detail(record)
                if view in {"azure", "both"}:
                    detail.comparison = detail.comparison.model_copy(
                        update={"presentation": PresentationPolicy(show_aws=view == "both")}
                    )
                source_workbook = record.source_workbook
                source_filename = record.application.intake_file_name
            return build_priced_workbook(source_workbook, detail, view), source_filename
        finally:
            workbook_export_slots.release()

    def clear(self) -> None:
        with self._lock:
            self._records.clear()

    def _detail(self, record: ApplicationRecord) -> ApplicationDetail:
        application = record.application.model_copy(
            update={"comparison_state": record.comparison.state}
        )
        return ApplicationDetail(
            application=application,
            compute_units=[unit.model_copy(deep=True) for unit in record.normalized.compute_units],
            database_units=[
                unit.model_copy(deep=True) for unit in record.normalized.database_units
            ],
            storage_units=[unit.model_copy(deep=True) for unit in record.normalized.storage_units],
            gaps=[gap.model_copy(deep=True) for gap in record.normalized.gaps],
            comparison=record.comparison.model_copy(deep=True),
            expires_at=record.application.created_at + application_max_age(),
            intake_summary=IntakeSummary(
                server_count=sum(unit.count for unit in record.normalized.compute_units),
                server_vcpu=sum(
                    unit.count * unit.vcpu_each for unit in record.normalized.compute_units
                ),
                database_count=len(record.normalized.database_units),
                storage_count=len(record.normalized.storage_units),
                storage_allocated_gb=sum(
                    (unit.allocated_gb for unit in record.normalized.storage_units),
                    Decimal("0"),
                ),
                open_question_count=sum(
                    1 for gap in record.normalized.gaps if gap.status == GapStatus.OPEN
                ),
            ),
        )

    def _summary(self, record: ApplicationRecord, max_age: timedelta) -> ApplicationSummary:
        comparison = record.comparison
        open_gaps = [gap for gap in record.normalized.gaps if gap.status == GapStatus.OPEN]
        return ApplicationSummary(
            **record.application.model_dump(exclude={"comparison_state"}),
            comparison_state=comparison.state,
            expires_at=record.application.created_at + max_age,
            open_question_count=len(open_gaps),
            first_open_prompt=open_gaps[0].prompt if open_gaps else None,
            verdict_confidence=comparison.verdict_confidence,
            cheaper_cloud=comparison.cheaper_cloud,
            headline_delta=comparison.headline_delta,
            headline_delta_percent=comparison.headline_delta_percent,
            aws_monthly_total=comparison.aws_monthly_total,
            azure_monthly_total=comparison.azure_monthly_total,
            priced_as_of=comparison.priced_as_of,
            excluded_cost_categories=[
                item.category for item in comparison.excluded_cost_ledger
            ],
            placeholder_providers=sorted(
                {item.provider for item in comparison.assumption_sensitivities},
                key=lambda provider: provider.value,
            ),
        )

    def _remove_expired(self, now: datetime) -> None:
        cutoff = now - application_max_age()
        expired_ids = [
            application_id
            for application_id, record in self._records.items()
            if record.application.created_at <= cutoff
        ]
        for application_id in expired_ids:
            del self._records[application_id]


application_repository = InMemoryApplicationRepository()
