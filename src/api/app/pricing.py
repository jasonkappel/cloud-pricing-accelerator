import hashlib
import json
import os
import re
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from app.models import (
    ApprovalRecord,
    Application,
    AssumptionSensitivity,
    AssumptionSet,
    AssumptionStressScenario,
    BreakevenPoint,
    BreakevenSensitivity,
    Cloud,
    CommercialScenario,
    CommercialView,
    CommitmentOffer,
    Comparison,
    ComparisonState,
    ComputeUnit,
    CostDriver,
    DatabaseUnit,
    ExcludedCost,
    Gap,
    GapStatus,
    LineItem,
    LineStatus,
    LicenseAssessment,
    MatchClass,
    PriceBookManifest,
    PresentationPolicy,
    ProviderCommercialAmounts,
    RateSource,
    SqlDeploymentModel,
    StorageUnit,
    VerdictConfidence,
)

RULE_VERSION = "pilot-calculator-v2"
MONEY = Decimal("0.01")
PERCENT = Decimal("0.01")
BREAKEVEN_REFERENCE_DISCOUNTS = (
    Decimal("0"),
    Decimal("10"),
    Decimal("20"),
)
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
LOCAL_DATA_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = (
    REPOSITORY_ROOT
    if (REPOSITORY_ROOT / "samples" / "pricebook_seed_v1.json").is_file()
    else LOCAL_DATA_ROOT
)
PRICEBOOK_PATH = DATA_ROOT / "samples" / "pricebook_seed_v1.json"
PRICEBOOK_APPROVAL_PATH = DATA_ROOT / "samples" / "pricebook_extract_approval_v1.json"
SKUMAP_PATH = DATA_ROOT / "samples" / "skumap_seed_v1.json"
SKUMAP_APPROVAL_PATH = DATA_ROOT / "samples" / "skumap_approval_v1.json"
SHARED_EXCLUSIONS = [
    ExcludedCost(
        id="PILOT-SHARED-PLATFORM-EXCLUSION-V1",
        category="Network transfer and shared network services",
        materiality="Material",
        rationale="No workload-specific traffic quantities are approved in the Intake.",
        policy_id="PILOT-SHARED-PLATFORM-EXCLUSION-V1",
    ),
    ExcludedCost(
        id="PILOT-SUPPORT-EXCLUSION-V1",
        category="Support",
        materiality="Material",
        rationale="Support is treated as a portfolio-level cost in the approved policy.",
        policy_id="PILOT-SUPPORT-EXCLUSION-V1",
    ),
    ExcludedCost(
        id="PILOT-OBSERVABILITY-EXCLUSION-V1",
        category="Observability",
        materiality="Material",
        rationale="Logging volume and retention are owned by the shared platform policy.",
        policy_id="PILOT-OBSERVABILITY-EXCLUSION-V1",
    ),
    ExcludedCost(
        id="PILOT-SECURITY-EXCLUSION-V1",
        category="Mandatory security services",
        materiality="Material",
        rationale="Security controls are allocated by the shared platform and are not silently zero.",
        policy_id="PILOT-SECURITY-EXCLUSION-V1",
    ),
    ExcludedCost(
        id="PILOT-BOOT-BACKUP-EXCLUSION-V1",
        category="VM boot disks and workload backup",
        materiality="Material",
        rationale="The supplied fixture has no per-server boot capacity or retained backup quantity.",
        policy_id="PILOT-BOOT-BACKUP-EXCLUSION-V1",
    ),
    ExcludedCost(
        id="PILOT-PG-PERFORMANCE-EXCLUSION-V1",
        category="PostgreSQL provisioned IOPS and backup overage",
        materiality="Material",
        rationale="The fixture provides no approved database IOPS target or retained backup GB.",
        policy_id="PILOT-PG-PERFORMANCE-EXCLUSION-V1",
    ),
]


class PricingConfigurationError(RuntimeError):
    pass


PRICEBOOK_SOURCES = ("demo-extract", "published-blob")
# A refused Published PriceBook is retried after this long, so a failure is not re-verified on every request
# and a new publication is picked up quickly.
FAILURE_RETRY_SECONDS = 5


@dataclass
class _PriceBookState:
    """One loaded PriceBook; replaced whole, never edited, so a comparison never mixes two."""

    pricebook: dict[str, Any]
    digest: str
    manifest: PriceBookManifest
    notice: str
    rate_sources: list[RateSource]
    demo_approval: dict[str, Any] | None = None
    approved_skumap_digest: str | None = None
    pointer_hash: str | None = None


# The PriceBook one compare() call prices with, even if a new snapshot loads while it runs.
_PINNED_STATE: ContextVar[tuple[object, _PriceBookState] | None] = ContextVar(
    "pinned_pricebook_state", default=None
)


def _bounded_int_env(name: str, default: int, low: int, high: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    if not re.fullmatch(r"[0-9]+", raw) or not low <= int(raw) <= high:
        raise PricingConfigurationError(f"{name} must be a whole number from {low} to {high}.")
    return int(raw)


def _rate_sources(pricebook: dict[str, Any]) -> list[RateSource]:
    return [
        RateSource(rate_key=key, **value)
        for key, value in sorted(pricebook["rateSources"].items())
    ]


def _demo_state() -> _PriceBookState:
    pricebook = _load_json(PRICEBOOK_PATH)
    manifest = pricebook["manifest"]
    digest = _digest(pricebook)
    return _PriceBookState(
        pricebook=pricebook,
        digest=digest,
        manifest=PriceBookManifest(
            snapshot_id=manifest["snapshotId"],
            content_hash=digest,
            source_snapshot_id=manifest.get("sourceSnapshotId"),
            source_content_hash=manifest.get("sourceContentHash"),
            priced_as_of=manifest["pricedAsOf"],
            schema_version=manifest["schemaVersion"],
            validation_status=manifest["validationStatus"],
            publishing_human=manifest["publishingHuman"],
            source_urls=manifest["sourceUrls"],
            non_production=manifest["nonProduction"],
        ),
        notice=pricebook["notice"],
        rate_sources=_rate_sources(pricebook),
        demo_approval=_load_json(PRICEBOOK_APPROVAL_PATH),
    )


def _published_state(book: Any) -> _PriceBookState:
    extract = book.extract
    manifest = extract["manifest"]
    try:
        return _PriceBookState(
            pricebook=extract,
            digest=book.extract_digest,
            manifest=PriceBookManifest(
                snapshot_id=manifest["snapshotId"],
                content_hash=book.extract_digest,
                source_snapshot_id=book.snapshot_id,
                source_content_hash=book.content_hash,
                priced_as_of=manifest["pricedAsOf"],
                schema_version=manifest["schemaVersion"],
                validation_status="Published",
                publishing_human=book.publishing_human,
                source_urls=list(manifest["sourceUrls"]),
                non_production=book.non_production,
            ),
            notice=extract["notice"],
            rate_sources=_rate_sources(extract),
            approved_skumap_digest=book.approved_skumap_digest,
            pointer_hash=book.pointer_hash,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise PricingConfigurationError(
            "The Published PriceBook can't be used: its rate provenance is malformed."
        ) from error


class PilotPricingEngine:
    def __init__(self) -> None:
        source = os.getenv("PRICEBOOK_SOURCE", "demo-extract")
        if source not in PRICEBOOK_SOURCES:
            raise PricingConfigurationError(
                f"PriceBook source '{source}' is not enabled. "
                "Use 'demo-extract' or 'published-blob'."
            )
        self.source = source
        self._max_age_days = _bounded_int_env("PRICEBOOK_MAX_AGE_DAYS", 45, 1, 366)
        self._refresh_seconds = _bounded_int_env("PRICEBOOK_REFRESH_SECONDS", 60, 5, 3600)
        self._state_lock = threading.Lock()
        self._load_lock = threading.Lock()
        self._checked_at = 0.0
        self._failure: str | None = None
        # Published pricing loads on first use, so importing the API never touches storage.
        self._state: _PriceBookState | None = _demo_state() if source == "demo-extract" else None
        self._skumap = _load_json(SKUMAP_PATH)
        self._mapping_index = self._build_mapping_index()
        approval = _load_json(SKUMAP_APPROVAL_PATH)
        self.skumap_approval = ApprovalRecord(
            approver=approval["approver"],
            approved_at=approval["approvedAt"],
            content_digest=approval["contentDigest"],
            non_production=approval["nonProduction"],
        )
        self._skumap_digest = hashlib.sha256(SKUMAP_PATH.read_bytes()).hexdigest()
        self._approval_digest = self.skumap_approval.content_digest

    @property
    def skumap_digest(self) -> str:
        return self._skumap_digest

    @property
    def max_age_days(self) -> int:
        return self._max_age_days

    def admissible_manifest(self) -> PriceBookManifest:
        """The manifest new comparisons would price with; refused exactly when compare() would refuse it."""
        state = self._pricebook_state()
        if state.demo_approval is None:
            self._check_fresh(state.manifest)
        return state.manifest

    @property
    def manifest(self) -> PriceBookManifest:
        return self._pricebook_state().manifest

    @property
    def notice(self) -> str:
        return self._pricebook_state().notice

    @property
    def rate_sources(self) -> list[RateSource]:
        return self._pricebook_state().rate_sources

    @property
    def _pricebook(self) -> dict[str, Any]:
        return self._pricebook_state().pricebook

    def _pricebook_state(self) -> _PriceBookState:
        pinned = _PINNED_STATE.get()
        if pinned is not None and pinned[0] is self:
            return pinned[1]
        if self.source == "demo-extract":
            assert self._state is not None
            return self._state
        return self._current_published_state()

    def _current_published_state(self) -> _PriceBookState:
        """The verified Published PriceBook. The whole chain is re-verified every refresh interval, so a
        changed record, artifact, or extract stops pricing even when current.json is unchanged."""
        cached = self._cached_published()
        if cached is not None:
            return cached
        # One load at a time, done outside the state lock so readers never wait on storage while holding it.
        with self._load_lock:
            cached = self._cached_published()
            if cached is not None:
                return cached
            started = time.monotonic()
            try:
                state = self._load_published()
            except PricingConfigurationError as error:
                with self._state_lock:
                    self._state, self._failure, self._checked_at = None, str(error), started
                raise
            with self._state_lock:
                self._state, self._failure, self._checked_at = state, None, started
            return state

    def invalidate(self) -> None:
        """Verify the Published PriceBook again on next use (after a publication through this API)."""
        with self._state_lock:
            self._checked_at = float("-inf")

    def _cached_published(self) -> _PriceBookState | None:
        with self._state_lock:
            age = time.monotonic() - self._checked_at
            if self._state is not None and age < self._refresh_seconds:
                return self._state
            if self._failure is not None and age < min(self._refresh_seconds, FAILURE_RETRY_SECONDS):
                raise PricingConfigurationError(self._failure)
            return None

    def _load_published(self) -> _PriceBookState:
        from app.approvals import (
            ApprovalConfigurationError,
            ApprovalNotConfigured,
            approval_backend,
        )
        from app.published_pricebook import PublishedPriceBookError, load_published_pricebook

        try:
            return _published_state(
                load_published_pricebook(approval_backend(), skumap_digest=self._skumap_digest)
            )
        except (
            PublishedPriceBookError,
            ApprovalNotConfigured,
            ApprovalConfigurationError,
        ) as error:
            raise PricingConfigurationError(str(error)) from error

    def compare(
        self,
        application: Application,
        compute_units: list[ComputeUnit],
        database_units: list[DatabaseUnit],
        storage_units: list[StorageUnit],
        gaps: list[Gap],
    ) -> Comparison:
        token = _PINNED_STATE.set((self, self._pricebook_state()))
        try:
            return self._compare(
                application, compute_units, database_units, storage_units, gaps
            )
        finally:
            _PINNED_STATE.reset(token)

    def _compare(
        self,
        application: Application,
        compute_units: list[ComputeUnit],
        database_units: list[DatabaseUnit],
        storage_units: list[StorageUnit],
        gaps: list[Gap],
    ) -> Comparison:
        self._verify_approved_inputs()
        assumptions = self._assumptions(gaps)
        license_assessments = self._license_assessments(
            compute_units,
            database_units,
        )
        regions_ready = bool(assumptions.azure_region and assumptions.aws_region)
        lines = [
            line
            for unit in compute_units
            for line in self._compute_lines(unit, regions_ready)
        ]
        lines.extend(
            line
            for unit in storage_units
            for line in self._storage_lines(unit, regions_ready)
        )
        lines.extend(
            line
            for unit in database_units
            for line in self._database_lines(unit, regions_ready)
        )

        open_material_gaps = [
            gap for gap in gaps if gap.material and gap.status == GapStatus.OPEN
        ]
        priced_lines = [line for line in lines if line.status == LineStatus.PRICED]
        aws_total = (
            _money(sum((line.aws_amount or Decimal("0")) for line in priced_lines))
            if priced_lines
            else None
        )
        azure_total = (
            _money(sum((line.azure_amount or Decimal("0")) for line in priced_lines))
            if priced_lines
            else None
        )
        state = (
            ComparisonState.REVIEW_BASELINE
            if (
                not open_material_gaps
                and bool(priced_lines)
                and aws_total is not None
                and azure_total is not None
                and all(line.status != LineStatus.UNPRICED for line in lines)
                and not any(
                    assessment.blocks_pricing
                    for assessment in license_assessments
                )
            )
            else ComparisonState.DRAFT_BENCHMARK
        )
        headline_delta = None
        headline_delta_percent = None
        cheaper_cloud = None
        if state == ComparisonState.REVIEW_BASELINE and aws_total is not None and azure_total is not None:
            headline_delta = _money(abs(aws_total - azure_total))
            higher_total = max(aws_total, azure_total)
            if higher_total > 0:
                headline_delta_percent = _percent(
                    headline_delta / higher_total * Decimal("100")
                )
            if aws_total != azure_total:
                cheaper_cloud = Cloud.AWS if aws_total < azure_total else Cloud.AZURE

        placeholder_lines = [line for line in priced_lines if line.demo_assumption]
        if state != ComparisonState.REVIEW_BASELINE:
            verdict_confidence = VerdictConfidence.DRAFT
        elif placeholder_lines:
            verdict_confidence = VerdictConfidence.PLACEHOLDER
        else:
            verdict_confidence = VerdictConfidence.FINAL
        assumption_sensitivities = (
            _assumption_sensitivities(
                placeholder_lines,
                aws_total,
                azure_total,
                cheaper_cloud,
            )
            if state == ComparisonState.REVIEW_BASELINE
            and aws_total is not None
            and azure_total is not None
            else []
        )

        dynamic_exclusions = [
            ExcludedCost(
                id=line.id,
                category=f"{line.unit_name}: {line.component}",
                materiality="Material",
                rationale=line.unpriced_reason or "Approved workload-specific exclusion.",
                policy_id=line.exclusion_policy_id or "PILOT-WORKLOAD-EXCLUSION-V1",
            )
            for line in lines
            if line.status == LineStatus.EXCLUDED
        ]
        commercial_scenarios = self._commercial_scenarios(lines, assumptions)
        breakeven_sensitivities = self._breakeven_sensitivities(
            commercial_scenarios,
            state,
        )
        cost_drivers = (
            self._drivers(priced_lines) if state == ComparisonState.REVIEW_BASELINE else []
        )
        excluded_cost_ledger = [*SHARED_EXCLUSIONS, *dynamic_exclusions]
        can_export = state == ComparisonState.REVIEW_BASELINE
        payload = {
            # comparison_state is this engine's prior output, so it stays out of the hash input.
            "application": application.model_dump(mode="json", exclude={"comparison_state"}),
            "compute_units": [unit.model_dump(mode="json") for unit in compute_units],
            "database_units": [unit.model_dump(mode="json") for unit in database_units],
            "storage_units": [unit.model_dump(mode="json") for unit in storage_units],
            "gaps": [gap.model_dump(mode="json") for gap in gaps],
            "assumptions": assumptions.model_dump(mode="json"),
            "skumap_digest": self._skumap_digest,
            "skumap_approval": self.skumap_approval.model_dump(mode="json"),
            "pricebook_hash": self.manifest.content_hash,
            "source_pricebook_hash": self.manifest.source_content_hash,
            "rule_version": RULE_VERSION,
            "lines": [line.model_dump(mode="json") for line in lines],
            "commercial_scenarios": [
                scenario.model_dump(mode="json")
                for scenario in commercial_scenarios
            ],
            "breakeven_sensitivities": [
                sensitivity.model_dump(mode="json")
                for sensitivity in breakeven_sensitivities
            ],
            "headline_delta_percent": (
                None if headline_delta_percent is None else str(headline_delta_percent)
            ),
            "verdict_confidence": verdict_confidence.value,
            "assumption_sensitivities": [
                sensitivity.model_dump(mode="json")
                for sensitivity in assumption_sensitivities
            ],
            # Outputs are bound too, so an edited verdict, total, or gate in evidence breaks the hash.
            "outputs": {
                "state": state.value,
                "can_export": can_export,
                "aws_monthly_total": _json_decimal(aws_total),
                "azure_monthly_total": _json_decimal(azure_total),
                "headline_delta": _json_decimal(headline_delta),
                "cheaper_cloud": None if cheaper_cloud is None else cheaper_cloud.value,
                "cost_drivers": [driver.model_dump(mode="json") for driver in cost_drivers],
                "excluded_cost_ledger": [
                    item.model_dump(mode="json") for item in excluded_cost_ledger
                ],
            },
        }
        return Comparison(
            state=state,
            commercial_view=CommercialView.LIST,
            priced_as_of=self.manifest.priced_as_of,
            pricebook_snapshot_id=self.manifest.snapshot_id,
            pricebook_content_hash=self.manifest.content_hash,
            source_pricebook_snapshot_id=self.manifest.source_snapshot_id,
            source_pricebook_content_hash=self.manifest.source_content_hash,
            source_urls=self.manifest.source_urls,
            rate_sources=self.rate_sources,
            skumap_content_digest=self._skumap_digest,
            skumap_approval=self.skumap_approval,
            calculator_rule_version=RULE_VERSION,
            presentation=_presentation_policy(),
            provisional=state == ComparisonState.DRAFT_BENCHMARK,
            aws_monthly_total=aws_total,
            azure_monthly_total=azure_total,
            commercial_scenarios=commercial_scenarios,
            breakeven_sensitivities=breakeven_sensitivities,
            license_assessments=license_assessments,
            headline_delta=headline_delta,
            headline_delta_percent=headline_delta_percent,
            cheaper_cloud=cheaper_cloud,
            verdict_confidence=verdict_confidence,
            assumption_sensitivities=assumption_sensitivities,
            line_items=lines,
            cost_drivers=cost_drivers,
            excluded_cost_ledger=excluded_cost_ledger,
            assumptions=assumptions,
            run_hash=_digest(payload),
            can_export=can_export,
            warnings=[
                self.notice,
                "The benchmark is workload-isolated and excludes portfolio commitments.",
            ],
        )

    def _commercial_scenarios(
        self,
        lines: list[LineItem],
        assumptions: AssumptionSet,
    ) -> list[CommercialScenario]:
        values = {
            (provider, offer): self._scenario_values(lines, provider, offer)
            for provider in ("aws", "azure")
            for offer in ("list", "savings_plan", "reservation")
        }
        common = {
            "term_years": assumptions.commitment_term_years,
            "payment_option": assumptions.commitment_payment_option,
            "utilization_percent": assumptions.commitment_utilization_percent,
            "tenancy": assumptions.commitment_tenancy,
        }
        return [
            CommercialScenario(
                id="aws-list",
                provider=Cloud.AWS,
                label="AWS public On-Demand",
                offer=CommitmentOffer.LIST,
                **values[("aws", "list")],
            ),
            CommercialScenario(
                id="aws-savings-plan",
                provider=Cloud.AWS,
                label="AWS Compute Savings Plan",
                offer=CommitmentOffer.SAVINGS_PLAN,
                **values[("aws", "savings_plan")],
                **common,
            ),
            CommercialScenario(
                id="aws-reservation",
                provider=Cloud.AWS,
                label="AWS Standard Reserved Instance",
                offer=CommitmentOffer.RESERVATION,
                **values[("aws", "reservation")],
                **common,
            ),
            CommercialScenario(
                id="azure-list",
                provider=Cloud.AZURE,
                label="Azure public consumption",
                offer=CommitmentOffer.LIST,
                **values[("azure", "list")],
            ),
            CommercialScenario(
                id="azure-savings-plan",
                provider=Cloud.AZURE,
                label="Azure savings plan for compute",
                offer=CommitmentOffer.SAVINGS_PLAN,
                **values[("azure", "savings_plan")],
                **common,
            ),
            CommercialScenario(
                id="azure-reservation",
                provider=Cloud.AZURE,
                label="Azure Reservation",
                offer=CommitmentOffer.RESERVATION,
                **values[("azure", "reservation")],
                **common,
            ),
        ]

    def _breakeven_sensitivities(
        self,
        scenarios: list[CommercialScenario],
        state: ComparisonState,
    ) -> list[BreakevenSensitivity]:
        scenario_index = {scenario.id: scenario for scenario in scenarios}
        pairs = (
            (
                CommitmentOffer.LIST,
                "Public List parity",
                scenario_index["aws-list"],
                scenario_index["azure-list"],
            ),
            (
                CommitmentOffer.SAVINGS_PLAN,
                "Public savings-plan parity",
                scenario_index["aws-savings-plan"],
                scenario_index["azure-savings-plan"],
            ),
            (
                CommitmentOffer.RESERVATION,
                "Public reservation parity",
                scenario_index["aws-reservation"],
                scenario_index["azure-reservation"],
            ),
        )
        return [
            self._breakeven_sensitivity(
                offer,
                label,
                aws,
                azure,
                basis_complete=state == ComparisonState.REVIEW_BASELINE,
            )
            for offer, label, aws, azure in pairs
        ]

    def _breakeven_sensitivity(
        self,
        offer: CommitmentOffer,
        label: str,
        aws: CommercialScenario,
        azure: CommercialScenario,
        *,
        basis_complete: bool = True,
    ) -> BreakevenSensitivity:
        disclosure = (
            "Hypothetical public-price sensitivity only; not an EA, EDP, private "
            "offer, or actual contracted price. The calculation is workload-isolated "
            "and does not model portfolio commitment drawdown."
        )
        if not basis_complete:
            return BreakevenSensitivity(
                id=f"{offer.value.casefold()}-parity",
                label=label,
                offer=offer,
                aws_public_monthly_total=None,
                azure_public_monthly_total=None,
                available=False,
                unavailable_reason=(
                    "Breakeven remains unavailable until the comparison reaches "
                    "ReviewBaseline with every material pricing gap resolved."
                ),
                disclosure=disclosure,
            )
        if (
            not aws.available
            or not azure.available
            or aws.monthly_total is None
            or azure.monthly_total is None
        ):
            reasons = list(
                dict.fromkeys(
                    scenario.unavailable_reason
                    for scenario in (aws, azure)
                    if not scenario.available and scenario.unavailable_reason
                )
            )
            return BreakevenSensitivity(
                id=f"{offer.value.casefold()}-parity",
                label=label,
                offer=offer,
                aws_public_monthly_total=aws.monthly_total,
                azure_public_monthly_total=azure.monthly_total,
                available=False,
                unavailable_reason=" ".join(reasons)
                or "Both public-price bases must be available.",
                disclosure=disclosure,
            )

        if aws.monthly_total == azure.monthly_total:
            reference_provider = None
            target_provider = None
            points = [
                BreakevenPoint(
                    reference_discount_percent=discount,
                    target_discount_to_parity_percent=_percent(discount),
                    additional_discount_advantage_percent=Decimal("0.00"),
                )
                for discount in BREAKEVEN_REFERENCE_DISCOUNTS
            ]
        else:
            reference_provider = (
                Cloud.AWS
                if aws.monthly_total < azure.monthly_total
                else Cloud.AZURE
            )
            target_provider = (
                Cloud.AZURE
                if reference_provider == Cloud.AWS
                else Cloud.AWS
            )
            lower_total = min(aws.monthly_total, azure.monthly_total)
            higher_total = max(aws.monthly_total, azure.monthly_total)
            points = []
            for reference_discount in BREAKEVEN_REFERENCE_DISCOUNTS:
                reference_multiplier = (
                    Decimal("1") - reference_discount / Decimal("100")
                )
                target_discount = _percent(
                    (
                        Decimal("1")
                        - lower_total * reference_multiplier / higher_total
                    )
                    * Decimal("100")
                )
                points.append(
                    BreakevenPoint(
                        reference_discount_percent=reference_discount,
                        target_discount_to_parity_percent=target_discount,
                        additional_discount_advantage_percent=_percent(
                            target_discount - reference_discount
                        ),
                    )
                )

        return BreakevenSensitivity(
            id=f"{offer.value.casefold()}-parity",
            label=label,
            offer=offer,
            aws_public_monthly_total=aws.monthly_total,
            azure_public_monthly_total=azure.monthly_total,
            reference_provider=reference_provider,
            target_provider=target_provider,
            points=points,
            available=True,
            disclosure=disclosure,
        )

    def _scenario_values(
        self,
        lines: list[LineItem],
        provider: str,
        offer: str,
    ) -> dict[str, Any]:
        priced_lines = [line for line in lines if line.status == LineStatus.PRICED]
        if not priced_lines:
            return {
                "monthly_total": None,
                "available": False,
                "unavailable_reason": f"No {provider.upper()} lines are currently priceable.",
            }
        if offer != "list" and any(
            line.status == LineStatus.UNPRICED for line in lines
        ):
            return {
                "monthly_total": None,
                "available": False,
                "unavailable_reason": (
                    "Committed totals remain unavailable until all material pricing gaps are resolved."
                ),
            }
        provider_field = f"{provider}_commercial"
        amount_field = f"{offer}_amount"
        amounts = [
            getattr(getattr(line, provider_field), amount_field)
            for line in priced_lines
        ]
        if any(amount is None for amount in amounts):
            return {
                "monthly_total": None,
                "available": False,
                "unavailable_reason": (
                    f"The approved PriceBook does not cover every {provider.upper()} "
                    f"{offer.replace('_', ' ')} candidate."
                ),
            }
        monthly_total = _money(
            sum((amount for amount in amounts if amount is not None), Decimal("0"))
        )
        if offer == "list":
            return {
                "monthly_total": monthly_total,
                "covered_monthly_total": Decimal("0.00"),
                "uncovered_monthly_total": monthly_total,
                "available": True,
            }
        covered_field = f"{offer}_covered_amount"
        uncovered_field = f"{offer}_uncovered_amount"
        covered = _money(
            sum(
                (
                    getattr(getattr(line, provider_field), covered_field)
                    or Decimal("0")
                )
                for line in priced_lines
            )
        )
        uncovered = _money(
            sum(
                (
                    getattr(getattr(line, provider_field), uncovered_field)
                    or Decimal("0")
                )
                for line in priced_lines
            )
        )
        return {
            "monthly_total": monthly_total,
            "covered_monthly_total": covered,
            "uncovered_monthly_total": uncovered,
            "available": True,
        }

    def _check_fresh(self, manifest: PriceBookManifest) -> None:
        age = (datetime.now(UTC).date() - date.fromisoformat(manifest.priced_as_of)).days
        if age < 0 or age > self._max_age_days:
            raise PricingConfigurationError(
                f"The Published PriceBook priced as of {manifest.priced_as_of} is outside "
                f"the {self._max_age_days}-day freshness limit. Publish a new harvest."
            )

    def _verify_approved_inputs(self) -> None:
        state = self._pricebook_state()
        pricebook, manifest = state.pricebook, state.manifest
        if manifest.validation_status != "Published":
            raise PricingConfigurationError("The PriceBook snapshot is not Published.")
        current_pricebook_digest = _digest(pricebook)
        approval = state.demo_approval
        if approval is not None:
            if (
                current_pricebook_digest != state.digest
                or approval["extractContentDigest"] != current_pricebook_digest
            ):
                raise PricingConfigurationError(
                    "The demo PriceBook approval digest does not match its content."
                )
            if (
                approval["sourceSnapshotId"] != manifest.source_snapshot_id
                or approval["sourceContentHash"] != manifest.source_content_hash
            ):
                raise PricingConfigurationError(
                    "The demo PriceBook approval does not match its source snapshot."
                )
        else:
            if current_pricebook_digest != state.digest:
                raise PricingConfigurationError(
                    "The Published rate extract does not match its approved digest."
                )
            if state.approved_skumap_digest != self._skumap_digest:
                raise PricingConfigurationError(
                    "The Published PriceBook was approved against a different SkuMap."
                )
            self._check_fresh(manifest)
        expected_rate_keys = {
            *pricebook["rates"],
            *pricebook.get("assumedRates", {}),
        }
        if expected_rate_keys != set(pricebook["rateSources"]):
            raise PricingConfigurationError(
                "Every rate must have exactly one provenance record."
            )
        if self._approval_digest != self._skumap_digest:
            raise PricingConfigurationError("The SkuMap approval digest does not match its content.")

    def _assumptions(self, gaps: list[Gap]) -> AssumptionSet:
        region_gap = next((gap for gap in gaps if gap.id == "P1.regions"), None)
        canonical = region_gap.canonical_value if region_gap else None
        return AssumptionSet(
            azure_region=canonical.get("azure_region") if canonical else None,
            aws_region=canonical.get("aws_region") if canonical else None,
            pricebook_snapshot_id=self.manifest.snapshot_id,
        )

    def _compute_lines(self, unit: ComputeUnit, regions_ready: bool) -> list[LineItem]:
        service_id = self._compute_service_id(unit.operating_system)
        if service_id is None:
            return [
                self._unsupported(
                    unit,
                    "Compute operating system",
                    f"Operating system '{unit.operating_system}' is outside the approved SkuMap.",
                )
            ]
        compute_component = (
            "vm-win-compute" if service_id == "SD-VM-WIN" else "vm-lnx-compute"
        )
        mapping = self._mapping(service_id, compute_component)
        if unit.runtime_hours_month is None:
            return [self._unpriced(unit, service_id, mapping, "Runtime hours are unresolved.")]
        if not regions_ready:
            return [self._unpriced(unit, service_id, mapping, "Approved regions are unresolved.")]

        shape = f"{unit.vcpu_each}x{_format_decimal(unit.ram_gb_each)}"
        base_rates = self._rates(
            f"aws.vm.linux.{shape}.hour",
            f"azure.vm.linux.{shape}.hour",
        )
        if base_rates is None:
            return [
                self._unpriced(
                    unit,
                    service_id,
                    mapping,
                    f"No approved PriceBook meter exists for VM shape {shape}.",
                )
            ]
        quantity = unit.runtime_hours_month * Decimal(unit.count)
        commitment_quantity = Decimal("730") * Decimal(unit.count)
        linux_aws, linux_azure = base_rates
        committed_rates = self._committed_vm_rates(shape)
        lines = [
            self._priced(
                f"{unit.id}.compute",
                unit,
                service_id,
                mapping,
                "VM compute",
                quantity,
                quantity,
                "instance-hours",
                linux_aws,
                linux_azure,
                "Same-shape shared-tenancy public List rate.",
                aws_savings_plan_rate=committed_rates[0],
                aws_reservation_rate=committed_rates[1],
                azure_savings_plan_rate=committed_rates[2],
                azure_reservation_rate=committed_rates[3],
                commitment_eligible=True,
                commitment_quantity=commitment_quantity,
            )
        ]

        license_mapping = self._mapping(
            service_id,
            "vm-win-oslicense" if service_id == "SD-VM-WIN" else "vm-lnx-oslicense",
        )
        license_model = _canonical_license_model(unit.license_model)
        if service_id == "SD-VM-WIN":
            if license_model == "byol":
                lines.append(
                    self._unpriced(
                        unit,
                        service_id,
                        license_mapping,
                        (
                            "Windows Server BYOL requires an approved Dedicated Host mapping "
                            "and eligible pre-October-2019 licenses; shared-tenancy pricing "
                            "cannot represent it."
                        ),
                    )
                )
            elif license_model in {"included", "ahb"}:
                if license_model == "ahb" and not (
                    unit.license_eligibility.active_software_assurance is True
                    and unit.license_eligibility.azure_hybrid_benefit_eligible is True
                ):
                    lines.append(
                        self._unpriced(
                            unit,
                            service_id,
                            license_mapping,
                            (
                                "Azure Hybrid Benefit requires confirmed active Software "
                                "Assurance or a qualifying subscription."
                            ),
                        )
                    )
                    return lines
                windows_rates = self._rates(
                    f"aws.vm.windows.{shape}.hour",
                    f"azure.vm.windows.{shape}.hour",
                )
                if windows_rates is None:
                    lines.append(
                        self._unpriced(
                            unit,
                            service_id,
                            license_mapping,
                            f"No approved Windows meter exists for VM shape {shape}.",
                        )
                    )
                else:
                    azure_uplift = (
                        Decimal("0")
                        if license_model == "ahb"
                        else windows_rates[1] - linux_azure
                    )
                    lines.append(
                        self._priced(
                            f"{unit.id}.windows",
                            unit,
                            service_id,
                            license_mapping,
                            "Windows license uplift",
                            quantity,
                            quantity,
                            "instance-hours",
                            windows_rates[0] - linux_aws,
                            azure_uplift,
                            (
                                "Azure Hybrid Benefit sets only the Azure uplift to zero."
                                if license_model == "ahb"
                                else "Derived separately to prevent Windows double counting."
                            ),
                        )
                    )
            else:
                lines.append(
                    self._unpriced(
                        unit,
                        service_id,
                        license_mapping,
                        f"Windows license model '{unit.license_model}' is not approved.",
                    )
                )
        elif "rhel" in unit.operating_system.casefold():
            if license_model == "byol":
                lines.append(
                    self._excluded(
                        unit,
                        service_id,
                        license_mapping,
                        "RHEL subscription uplift",
                        "PILOT-RHEL-BYOS-EXCLUSION-V1",
                        "The Intake declares a bring-your-own subscription treatment.",
                    )
                )
            elif license_model in {"subscription", "included"}:
                aws_uplift = self._published_rate(
                    f"aws.vm.rhel.{shape}.hour_uplift"
                )
                azure_uplift = self._assumed_rate(
                    f"azure.vm.rhel.{shape}.hour_uplift"
                )
                if aws_uplift is None or azure_uplift is None:
                    lines.append(
                        self._unpriced(
                            unit,
                            service_id,
                            license_mapping,
                            f"No approved RHEL uplift exists for VM shape {shape}.",
                        )
                    )
                else:
                    lines.append(
                        self._priced(
                            f"{unit.id}.rhel",
                            unit,
                            service_id,
                            license_mapping,
                            "RHEL subscription uplift",
                            quantity,
                            quantity,
                            "instance-hours",
                            aws_uplift,
                            azure_uplift,
                            (
                                "AWS RHEL uplift is derived from authenticated public rates. "
                                "Azure RHEL uplift is a visible demo assumption because the "
                                "software charge is outside the harvested Azure scope."
                            ),
                            demo_assumption=True,
                        )
                    )
            else:
                lines.append(
                    self._unpriced(
                        unit,
                        service_id,
                        license_mapping,
                        f"RHEL license model '{unit.license_model}' is not approved.",
                    )
                )
        elif "suse" in unit.operating_system.casefold():
            if license_model == "byol":
                lines.append(
                    self._excluded(
                        unit,
                        service_id,
                        license_mapping,
                        "SUSE subscription uplift",
                        "PILOT-SUSE-BYOS-EXCLUSION-V1",
                        "The Intake declares a bring-your-own SUSE subscription treatment.",
                    )
                )
            else:
                lines.append(
                    self._unpriced(
                        unit,
                        service_id,
                        license_mapping,
                        "The PriceBook has no approved SUSE subscription uplift.",
                    )
                )
        elif license_model not in {"open-source", "included"}:
            lines.append(
                self._unpriced(
                    unit,
                    service_id,
                    license_mapping,
                    f"Linux license model '{unit.license_model}' is not approved.",
                )
            )
        return lines

    def _license_assessments(
        self,
        compute_units: list[ComputeUnit],
        database_units: list[DatabaseUnit],
    ) -> list[LicenseAssessment]:
        windows_url = (
            "https://learn.microsoft.com/azure/virtual-machines/windows/"
            "hybrid-use-benefit-licensing"
        )
        aws_windows_url = "https://aws.amazon.com/windows/resources/licensing/"
        sql_ahb_url = "https://learn.microsoft.com/azure/azure-sql/azure-hybrid-benefit"
        mobility_url = (
            "https://www.microsoft.com/licensing/licensing-programs/"
            "software-assurance-license-mobility"
        )
        assessments: list[LicenseAssessment] = []
        for unit in compute_units:
            if "windows" not in unit.operating_system.casefold():
                continue
            model = _canonical_license_model(unit.license_model)
            eligibility = unit.license_eligibility
            if model == "included":
                azure = "Public Windows license uplift included."
                aws = "Public Windows license uplift included."
                blocks = False
            elif model == "ahb":
                eligible = (
                    eligibility.active_software_assurance is True
                    and eligibility.azure_hybrid_benefit_eligible is True
                )
                azure = (
                    "AHB eligible; Azure Windows uplift is removed."
                    if eligible
                    else "AHB eligibility is not confirmed."
                )
                aws = (
                    "AWS shared tenancy remains license-included; Windows Server "
                    "has no License Mobility equivalent."
                )
                blocks = not eligible
            elif model == "byol":
                azure = (
                    "Azure AHB may apply only with confirmed SA eligibility."
                    if eligibility.azure_hybrid_benefit_eligible
                    else "Azure AHB eligibility is not confirmed."
                )
                aws_eligible = (
                    eligibility.acquired_before_2019_10_01 is True
                    and eligibility.perpetual_license is True
                    and eligibility.eligible_product_version is True
                    and _windows_version_eligible_for_legacy_byol(
                        unit.operating_system
                    )
                )
                aws = (
                    "Windows Server license predicates are eligible for the legacy "
                    "Dedicated Host path, but no approved host mapping exists."
                    if aws_eligible
                    else "Windows Server BYOL is ineligible because the required "
                    "pre-October-2019 perpetual-license and product-version predicates "
                    "are not all confirmed."
                )
                blocks = True
            else:
                azure = "License treatment is not approved."
                aws = "License treatment is not approved."
                blocks = True
            assessments.append(
                LicenseAssessment(
                    unit_id=unit.id,
                    product="Windows Server",
                    azure_treatment=azure,
                    aws_treatment=aws,
                    blocks_pricing=blocks,
                    evidence_urls=[windows_url, aws_windows_url],
                )
            )
        for unit in database_units:
            if not _is_sql_server(unit.engine):
                continue
            eligibility = unit.license_eligibility
            ahb_models = {
                SqlDeploymentModel.SQL_VM,
                SqlDeploymentModel.SQL_DATABASE_PROVISIONED_VCORE,
                SqlDeploymentModel.SQL_MANAGED_INSTANCE_PROVISIONED_VCORE,
            }
            azure_eligible = (
                eligibility.active_software_assurance is True
                and eligibility.azure_hybrid_benefit_eligible is True
                and eligibility.azure_sql_deployment_model in ahb_models
            )
            azure = (
                "SQL AHB eligibility and deployment-model compatibility are confirmed."
                if azure_eligible
                else "SQL AHB is unavailable or unconfirmed for the selected deployment model."
            )
            aws = (
                "SQL Server License Mobility eligibility is confirmed for shared tenancy."
                if (
                    eligibility.active_software_assurance is True
                    and eligibility.aws_license_mobility_eligible is True
                )
                else "AWS SQL BYOL requires License Mobility or an approved Dedicated Host mapping."
            )
            if eligibility.passive_secondary:
                passive = (
                    " Passive-secondary treatment is recorded as passive-only."
                    if eligibility.passive_use_only is True
                    else " Passive-secondary rights are not confirmed."
                )
                azure += passive
                aws += passive
            assessments.append(
                LicenseAssessment(
                    unit_id=unit.id,
                    product="SQL Server",
                    azure_treatment=azure,
                    aws_treatment=aws,
                    blocks_pricing=True,
                    evidence_urls=[sql_ahb_url, mobility_url],
                )
            )
        return assessments

    def _storage_lines(self, unit: StorageUnit, regions_ready: bool) -> list[LineItem]:
        if (
            "block" not in unit.storage_type.casefold()
            or unit.protocol.casefold() not in {"iscsi", "block"}
        ):
            return [
                self._unsupported(
                    unit,
                    "Storage service",
                    (
                        f"Storage type '{unit.storage_type}' / protocol '{unit.protocol}' "
                        "is outside the approved block-storage SkuMap."
                    ),
                )
            ]
        capacity_mapping = self._mapping("SD-BLOCK", "block-capacity")
        if not regions_ready:
            return [
                self._unpriced(
                    unit,
                    "SD-BLOCK",
                    capacity_mapping,
                    "Approved regions are unresolved.",
                )
            ]
        capacity_rates = self._rates(
            "aws.block.capacity.gb_month",
            "azure.block.capacity.gb_month",
        )
        if capacity_rates is None:
            return [
                self._unpriced(
                    unit,
                    "SD-BLOCK",
                    capacity_mapping,
                    "The PriceBook is missing block-storage capacity meters.",
                )
            ]
        profile = unit.storage_performance_profile
        capacity = profile.capacity_gb or unit.allocated_gb
        lines = [
            self._priced(
                f"{unit.id}.capacity",
                unit,
                "SD-BLOCK",
                capacity_mapping,
                "Block storage capacity",
                capacity,
                capacity,
                "GiB-month",
                capacity_rates[0],
                capacity_rates[1],
                "gp3 and Premium SSD v2 capacity meters.",
            )
        ]
        if profile.target_iops is None or profile.target_mbps is None:
            lines.append(
                self._unpriced(
                    unit,
                    "SD-BLOCK",
                    self._mapping("SD-BLOCK", "block-iops"),
                    "Target IOPS and MB/s are unresolved.",
                )
            )
            return lines

        iops_mapping = self._mapping("SD-BLOCK", "block-iops")
        iops_rates = self._rates("aws.block.iops_month", "azure.block.iops_month")
        if iops_rates is None:
            lines.append(
                self._unpriced(
                    unit,
                    "SD-BLOCK",
                    iops_mapping,
                    "The PriceBook is missing block-storage IOPS meters.",
                )
            )
        else:
            extra_iops = max(Decimal("0"), Decimal(profile.target_iops) - Decimal("3000"))
            lines.append(
                self._priced(
                    f"{unit.id}.iops",
                    unit,
                    "SD-BLOCK",
                    iops_mapping,
                    "Block storage IOPS",
                    extra_iops,
                    extra_iops,
                    "IOPS-month",
                    iops_rates[0],
                    iops_rates[1],
                    "Both sides preserve the 3,000-IOPS included baseline.",
                )
            )

        throughput_mapping = self._mapping("SD-BLOCK", "block-throughput")
        throughput_rates = self._rates(
            "aws.block.throughput_mbps_month",
            "azure.block.throughput_mbps_month",
        )
        if throughput_rates is None:
            lines.append(
                self._unpriced(
                    unit,
                    "SD-BLOCK",
                    throughput_mapping,
                    "The PriceBook is missing block-storage throughput meters.",
                )
            )
        else:
            target_mb_s = profile.target_mbps
            target_mib_s = target_mb_s * Decimal("1000000") / Decimal("1048576")
            aws_extra = max(Decimal("0"), target_mib_s - Decimal("125"))
            azure_extra = max(Decimal("0"), target_mb_s - Decimal("125"))
            lines.append(
                self._priced(
                    f"{unit.id}.throughput",
                    unit,
                    "SD-BLOCK",
                    throughput_mapping,
                    "Block storage throughput",
                    aws_extra,
                    azure_extra,
                    "AWS MiB/s-month; Azure MB/s-month",
                    throughput_rates[0],
                    throughput_rates[1],
                    "Requested MB/s is converted to MiB/s before applying the AWS meter.",
                )
            )
        return lines

    def _database_lines(self, unit: DatabaseUnit, regions_ready: bool) -> list[LineItem]:
        if not unit.engine.casefold().startswith("postgresql"):
            return [
                self._unsupported(
                    unit,
                    "Managed database",
                    f"Database engine '{unit.engine}' is outside the approved PostgreSQL SkuMap.",
                )
            ]
        if _canonical_license_model(unit.license_model) not in {"open-source", "included"}:
            return [
                self._unsupported(
                    unit,
                    "Database license model",
                    f"Database license model '{unit.license_model}' is not approved.",
                )
            ]
        compute_mapping = self._mapping("SD-PG", "pg-compute")
        if not regions_ready:
            return [
                self._unpriced(
                    unit,
                    "SD-PG",
                    compute_mapping,
                    "Approved regions are unresolved.",
                )
            ]
        shape = f"{unit.vcpu_each}x{_format_decimal(unit.ram_gb_each)}"
        compute_rates = self._rates(
            f"aws.postgresql.{shape}.hour",
            f"azure.postgresql.{shape}.hour",
        )
        if compute_rates is None:
            return [
                self._unpriced(
                    unit,
                    "SD-PG",
                    compute_mapping,
                    f"No approved PostgreSQL meter exists for shape {shape}.",
                )
            ]
        lines = [
            self._priced(
                f"{unit.id}.compute",
                unit,
                "SD-PG",
                compute_mapping,
                "PostgreSQL compute",
                Decimal("730"),
                Decimal("730"),
                "instance-hours",
                compute_rates[0],
                compute_rates[1],
                "Primary managed PostgreSQL instance.",
            )
        ]
        if unit.topology_tier.mode == "zone-redundant":
            standby_count = unit.nodes - 1
            if standby_count < 1:
                return [
                    self._unpriced(
                        unit,
                        "SD-PG",
                        self._mapping("SD-PG", "pg-ha"),
                        "Zone-redundant PostgreSQL requires at least one standby node.",
                    )
                ]
            lines.append(
                self._priced(
                    f"{unit.id}.ha",
                    unit,
                    "SD-PG",
                    self._mapping("SD-PG", "pg-ha"),
                    "PostgreSQL HA standby",
                    Decimal("730") * Decimal(standby_count),
                    Decimal("730") * Decimal(standby_count),
                    "standby-hours",
                    compute_rates[0],
                    compute_rates[1],
                    "Synchronous standby is a separate visible line.",
                )
            )
            lines.append(
                self._excluded(
                    unit,
                    "SD-PG",
                    self._mapping("SD-PG", "pg-ha"),
                    "PostgreSQL HA standby storage",
                    "PILOT-PG-STANDBY-STORAGE-EXCLUSION-V1",
                    (
                        "Standby storage billing differs by provider and is excluded until "
                        "provider-specific retained capacity is approved."
                    ),
                )
            )
        storage_rates = self._rates(
            "aws.postgresql.storage.gb_month",
            "azure.postgresql.storage.gb_month",
        )
        storage_mapping = self._mapping("SD-PG", "pg-storage")
        if storage_rates is None:
            lines.append(
                self._unpriced(
                    unit,
                    "SD-PG",
                    storage_mapping,
                    "The PriceBook is missing PostgreSQL storage meters.",
                )
            )
        else:
            lines.append(
                self._priced(
                    f"{unit.id}.storage",
                    unit,
                    "SD-PG",
                    storage_mapping,
                    "PostgreSQL storage",
                    unit.size_gb,
                    unit.size_gb,
                    "GB-month",
                    storage_rates[0],
                    storage_rates[1],
                    "Managed PostgreSQL storage granularity differs and is disclosed.",
                )
            )
        return lines

    def _priced(
        self,
        line_id: str,
        unit: Any,
        service_definition_id: str,
        mapping: dict[str, Any],
        component: str,
        aws_quantity: Decimal,
        azure_quantity: Decimal,
        quantity_unit: str,
        aws_rate: Decimal,
        azure_rate: Decimal,
        evidence: str,
        demo_assumption: bool = False,
        aws_savings_plan_rate: Decimal | None = None,
        aws_reservation_rate: Decimal | None = None,
        azure_savings_plan_rate: Decimal | None = None,
        azure_reservation_rate: Decimal | None = None,
        commitment_eligible: bool = False,
        commitment_quantity: Decimal | None = None,
    ) -> LineItem:
        aws_amount = _money(aws_quantity * aws_rate)
        azure_amount = _money(azure_quantity * azure_rate)
        aws_commercial = self._provider_commercial_amounts(
            list_quantity=aws_quantity,
            list_rate=aws_rate,
            savings_plan_rate=aws_savings_plan_rate,
            reservation_rate=aws_reservation_rate,
            commitment_eligible=commitment_eligible,
            commitment_quantity=commitment_quantity,
        )
        azure_commercial = self._provider_commercial_amounts(
            list_quantity=azure_quantity,
            list_rate=azure_rate,
            savings_plan_rate=azure_savings_plan_rate,
            reservation_rate=azure_reservation_rate,
            commitment_eligible=commitment_eligible,
            commitment_quantity=commitment_quantity,
        )
        return LineItem(
            id=line_id,
            service_definition_id=service_definition_id,
            skumap_component_id=mapping["componentId"],
            unit_id=unit.id,
            unit_name=unit.name,
            component=component,
            role=mapping["role"],
            match_class=MatchClass(mapping["matchClass"]),
            status=LineStatus.PRICED,
            quantity=aws_quantity if aws_quantity == azure_quantity else None,
            aws_quantity=aws_quantity,
            azure_quantity=azure_quantity,
            quantity_unit=quantity_unit,
            aws_rate=aws_rate,
            azure_rate=azure_rate,
            aws_amount=aws_amount,
            azure_amount=azure_amount,
            aws_commercial=aws_commercial,
            azure_commercial=azure_commercial,
            higher_cloud=(
                Cloud.AZURE
                if azure_amount > aws_amount
                else Cloud.AWS if aws_amount > azure_amount else None
            ),
            demo_assumption=demo_assumption,
            formula=mapping["quantityFormula"],
            evidence=(
                f"{evidence} {mapping['capabilityEvidence']}"
                if mapping.get("capabilityEvidence")
                else evidence
            ),
        )

    def _provider_commercial_amounts(
        self,
        *,
        list_quantity: Decimal,
        list_rate: Decimal,
        savings_plan_rate: Decimal | None,
        reservation_rate: Decimal | None,
        commitment_eligible: bool,
        commitment_quantity: Decimal | None,
    ) -> ProviderCommercialAmounts:
        list_amount = _money(list_quantity * list_rate)
        if not commitment_eligible:
            return ProviderCommercialAmounts(
                list_rate=list_rate,
                list_amount=list_amount,
                savings_plan_rate=list_rate,
                savings_plan_quantity=list_quantity,
                savings_plan_covered_amount=Decimal("0.00"),
                savings_plan_uncovered_amount=list_amount,
                savings_plan_amount=list_amount,
                reservation_rate=list_rate,
                reservation_quantity=list_quantity,
                reservation_covered_amount=Decimal("0.00"),
                reservation_uncovered_amount=list_amount,
                reservation_amount=list_amount,
            )
        if commitment_quantity is None:
            raise PricingConfigurationError(
                "Commitment-eligible pricing requires a billing quantity."
            )

        def committed(
            rate: Decimal | None,
        ) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
            if rate is None:
                return None, None, None
            covered = _money(commitment_quantity * rate)
            excess_quantity = max(list_quantity - commitment_quantity, Decimal("0"))
            uncovered = _money(excess_quantity * list_rate)
            return covered, uncovered, _money(covered + uncovered)

        savings_covered, savings_uncovered, savings_total = committed(
            savings_plan_rate
        )
        reservation_covered, reservation_uncovered, reservation_total = committed(
            reservation_rate
        )
        return ProviderCommercialAmounts(
            list_rate=list_rate,
            list_amount=list_amount,
            savings_plan_rate=savings_plan_rate,
            savings_plan_quantity=(
                commitment_quantity if savings_plan_rate is not None else None
            ),
            savings_plan_covered_amount=savings_covered,
            savings_plan_uncovered_amount=savings_uncovered,
            savings_plan_amount=savings_total,
            reservation_rate=reservation_rate,
            reservation_quantity=(
                commitment_quantity if reservation_rate is not None else None
            ),
            reservation_covered_amount=reservation_covered,
            reservation_uncovered_amount=reservation_uncovered,
            reservation_amount=reservation_total,
        )

    def _unpriced(
        self,
        unit: Any,
        service_definition_id: str,
        mapping: dict[str, Any],
        reason: str,
    ) -> LineItem:
        return LineItem(
            id=f"{unit.id}.{mapping['componentId']}.unpriced",
            service_definition_id=service_definition_id,
            skumap_component_id=mapping["componentId"],
            unit_id=unit.id,
            unit_name=unit.name,
            component=mapping["role"],
            role=mapping["role"],
            match_class=MatchClass(mapping["matchClass"]),
            status=LineStatus.UNPRICED,
            formula=mapping["quantityFormula"],
            evidence=mapping.get("capabilityEvidence", "Approved SkuMap"),
            unpriced_reason=reason,
        )

    def _unsupported(self, unit: Any, component: str, reason: str) -> LineItem:
        return LineItem(
            id=f"{unit.id}.unsupported",
            service_definition_id="unsupported",
            skumap_component_id="unsupported",
            unit_id=unit.id,
            unit_name=unit.name,
            component=component,
            role="Unsupported workload",
            match_class=MatchClass.UNSUPPORTED,
            status=LineStatus.UNPRICED,
            formula="No approved SkuMap component exists.",
            evidence="v1 scope boundary",
            unpriced_reason=reason,
        )

    def _excluded(
        self,
        unit: Any,
        service_definition_id: str,
        mapping: dict[str, Any],
        component: str,
        policy_id: str,
        reason: str,
    ) -> LineItem:
        return LineItem(
            id=f"{unit.id}.{mapping['componentId']}.excluded",
            service_definition_id=service_definition_id,
            skumap_component_id=mapping["componentId"],
            unit_id=unit.id,
            unit_name=unit.name,
            component=component,
            role=mapping["role"],
            match_class=MatchClass(mapping["matchClass"]),
            status=LineStatus.EXCLUDED,
            formula=mapping["quantityFormula"],
            evidence=mapping.get("capabilityEvidence", "Approved exclusion"),
            unpriced_reason=reason,
            exclusion_policy_id=policy_id,
        )

    def _mapping(self, service_id: str, component_id: str) -> dict[str, Any]:
        mapping = self._mapping_index.get((service_id, component_id))
        if mapping is None:
            raise PricingConfigurationError(
                f"The approved SkuMap is missing {service_id}/{component_id}."
            )
        return mapping

    def _rates(self, aws_key: str, azure_key: str) -> tuple[Decimal, Decimal] | None:
        aws_value = self._pricebook["rates"].get(aws_key)
        azure_value = self._pricebook["rates"].get(azure_key)
        if aws_value is None or azure_value is None:
            return None
        return Decimal(aws_value), Decimal(azure_value)

    def _committed_vm_rates(
        self, shape: str
    ) -> tuple[Decimal | None, Decimal | None, Decimal | None, Decimal | None]:
        keys = (
            f"aws.vm.linux.{shape}.hour.savings_plan",
            f"aws.vm.linux.{shape}.hour.reservation",
            f"azure.vm.linux.{shape}.hour.savings_plan",
            f"azure.vm.linux.{shape}.hour.reservation",
        )
        values = [self._pricebook["rates"].get(key) for key in keys]
        return tuple(
            Decimal(value) if value is not None else None for value in values
        )  # type: ignore[return-value]

    def _published_rate(self, key: str) -> Decimal | None:
        value = self._pricebook["rates"].get(key)
        return Decimal(value) if value is not None else None

    def _assumed_rate(self, key: str) -> Decimal | None:
        value = self._pricebook.get("assumedRates", {}).get(key)
        return Decimal(value) if value is not None else None

    def _compute_service_id(self, operating_system: str) -> str | None:
        folded = operating_system.casefold()
        if "windows" in folded:
            return "SD-VM-WIN"
        if any(term in folded for term in ("linux", "rhel", "ubuntu", "debian", "suse")):
            return "SD-VM-LNX"
        return None

    def _build_mapping_index(self) -> dict[tuple[str, str], dict[str, Any]]:
        return {
            (service["id"], component["componentId"]): component
            for service in self._skumap["serviceDefinitions"]
            for component in service["components"]
            if "componentId" in component
        }

    def _drivers(self, lines: list[LineItem]) -> list[CostDriver]:
        deltas = [
            (
                line,
                _money(
                    abs(
                        (line.azure_amount or Decimal("0"))
                        - (line.aws_amount or Decimal("0"))
                    )
                ),
            )
            for line in lines
            if line.aws_amount != line.azure_amount
        ]
        total_absolute_delta = sum((delta for _, delta in deltas), Decimal("0"))
        drivers = [
            CostDriver(
                line_id=line.id,
                component=f"{line.unit_name}: {line.component}",
                delta=delta,
                higher_cloud=(
                    Cloud.AZURE
                    if (line.azure_amount or Decimal("0"))
                    >= (line.aws_amount or Decimal("0"))
                    else Cloud.AWS
                ),
                share_of_absolute_delta_percent=(
                    (delta / total_absolute_delta * Decimal("100")).quantize(
                        Decimal("0.1"),
                        rounding=ROUND_HALF_UP,
                    )
                    if total_absolute_delta
                    else Decimal("0.0")
                ),
            )
            for line, delta in deltas
        ]
        return sorted(drivers, key=lambda driver: driver.delta, reverse=True)[:5]


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _digest(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _json_decimal(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)


# Every demo_assumption component must be listed here with the provider whose rate
# is assumed and its plain display label. An unlisted component fails closed.
PLACEHOLDER_COMPONENTS: dict[str, tuple[Cloud, str]] = {
    "RHEL subscription uplift": (Cloud.AZURE, "Red Hat Enterprise Linux licensing"),
}
ASSUMPTION_STRESS_MULTIPLIERS = (Decimal("0"), Decimal("2"))


def _assumption_sensitivities(
    placeholder_lines: list[LineItem],
    aws_total: Decimal,
    azure_total: Decimal,
    cheaper_cloud: Cloud | None,
) -> list[AssumptionSensitivity]:
    groups: dict[str, list[LineItem]] = {}
    for line in placeholder_lines:
        if line.component not in PLACEHOLDER_COMPONENTS:
            raise PricingConfigurationError(
                f"Placeholder component '{line.component}' has no stress-test mapping."
            )
        groups.setdefault(line.component, []).append(line)

    sensitivities = []
    for component, lines in groups.items():
        provider, label = PLACEHOLDER_COMPONENTS[component]
        assumed = _money(
            sum(
                (
                    (line.azure_amount if provider == Cloud.AZURE else line.aws_amount)
                    or Decimal("0")
                    for line in lines
                ),
                Decimal("0"),
            )
        )
        at_zero, at_double = (
            _assumption_stress(
                multiplier,
                assumed,
                provider,
                aws_total,
                azure_total,
                cheaper_cloud,
            )
            for multiplier in ASSUMPTION_STRESS_MULTIPLIERS
        )
        sensitivities.append(
            AssumptionSensitivity(
                id="placeholder-" + re.sub(r"[^a-z0-9]+", "-", component.casefold()).strip("-"),
                label=label,
                provider=provider,
                line_ids=[line.id for line in lines],
                assumed_monthly_amount=assumed,
                at_zero=at_zero,
                at_double=at_double,
                changes_cheaper_cloud=(
                    at_zero.changes_cheaper_cloud or at_double.changes_cheaper_cloud
                ),
            )
        )
    return sensitivities


def _assumption_stress(
    multiplier: Decimal,
    assumed: Decimal,
    provider: Cloud,
    aws_total: Decimal,
    azure_total: Decimal,
    cheaper_cloud: Cloud | None,
) -> AssumptionStressScenario:
    stressed = _money(assumed * multiplier)
    aws = _money(aws_total - assumed + stressed) if provider == Cloud.AWS else aws_total
    azure = (
        _money(azure_total - assumed + stressed) if provider == Cloud.AZURE else azure_total
    )
    stressed_cheaper = None if aws == azure else (Cloud.AWS if aws < azure else Cloud.AZURE)
    return AssumptionStressScenario(
        multiplier=multiplier,
        assumed_monthly_amount=stressed,
        aws_monthly_total=aws,
        azure_monthly_total=azure,
        cheaper_cloud=stressed_cheaper,
        headline_delta=_money(abs(aws - azure)),
        changes_cheaper_cloud=stressed_cheaper != cheaper_cloud,
    )


def _percent(value: Decimal) -> Decimal:
    return value.quantize(PERCENT, rounding=ROUND_HALF_UP)


def _presentation_policy() -> PresentationPolicy:
    value = os.getenv("PRESENT_AWS_PRICING", "false").strip().casefold()
    if value not in {"true", "false"}:
        raise PricingConfigurationError(
            "PRESENT_AWS_PRICING must be either 'true' or 'false'."
        )
    return PresentationPolicy(show_aws=value == "true")


def presentation_policy() -> PresentationPolicy:
    return _presentation_policy()


def _format_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _canonical_license_model(value: str) -> str:
    folded = re.sub(r"\s+", " ", value.strip().casefold())
    aliases = {
        "license included": "included",
        "included": "included",
        "subscription": "subscription",
        "open source": "open-source",
        "open-source": "open-source",
        "open source / no fee": "open-source",
        "none": "open-source",
        "byol": "byol",
        "bring your own license": "byol",
        "bring your own subscription": "byol",
        "byos": "byol",
        "azure hybrid benefit": "ahb",
        "ahb": "ahb",
        "software assurance with azure hybrid benefit": "ahb",
        "postgresql open source": "open-source",
    }
    return aliases.get(folded, "unknown")


def _is_sql_server(value: str) -> bool:
    return re.search(r"(?<![a-z])sql\s+server(?![a-z])", value.casefold()) is not None


def _windows_version_eligible_for_legacy_byol(value: str) -> bool:
    years = [int(year) for year in re.findall(r"\b(20\d{2})\b", value)]
    return not years or max(years) <= 2019


pricing_engine = PilotPricingEngine()
