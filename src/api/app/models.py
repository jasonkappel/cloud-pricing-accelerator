from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ComparisonState(StrEnum):
    DRAFT_BENCHMARK = "DraftBenchmark"
    REVIEW_BASELINE = "ReviewBaseline"


class GapStatus(StrEnum):
    OPEN = "Open"
    RESOLVED = "Resolved"


class GapKind(StrEnum):
    RUNTIME_HOURS = "RuntimeHours"
    APPROVED_REGIONS = "ApprovedRegions"
    STORAGE_PERFORMANCE = "StoragePerformance"
    LICENSE_ELIGIBILITY = "LicenseEligibility"


class Cloud(StrEnum):
    AWS = "AWS"
    AZURE = "Azure"


class LineStatus(StrEnum):
    PRICED = "Priced"
    UNPRICED = "Unpriced"
    EXCLUDED = "Excluded"


class MatchClass(StrEnum):
    EQUIVALENT = "equivalent"
    CLOSEST_AVAILABLE = "closest-available"
    REQUIRES_REDESIGN = "requires-redesign"
    UNSUPPORTED = "unsupported"


class CommercialView(StrEnum):
    LIST = "List"


class CommitmentOffer(StrEnum):
    LIST = "List"
    SAVINGS_PLAN = "SavingsPlan"
    RESERVATION = "Reservation"


class CommitmentPaymentOption(StrEnum):
    ALL_UPFRONT = "All Upfront"


class CommitmentTenancy(StrEnum):
    SHARED = "Shared"


class SqlDeploymentModel(StrEnum):
    SQL_VM = "SqlVm"
    SQL_DATABASE_PROVISIONED_VCORE = "SqlDatabaseProvisionedVCore"
    SQL_MANAGED_INSTANCE_PROVISIONED_VCORE = "SqlManagedInstanceProvisionedVCore"
    SQL_DATABASE_SERVERLESS = "SqlDatabaseServerless"
    SQL_DATABASE_DTU = "SqlDatabaseDtu"


class TopologyTier(BaseModel):
    mode: str
    instance_count: int = Field(ge=1)


class StoragePerformanceProfile(BaseModel):
    capacity_gb: Decimal | None = Field(default=None, ge=0)
    target_iops: int | None = Field(default=None, ge=0)
    target_mbps: Decimal | None = Field(default=None, ge=0)


class NetworkTransferProfile(BaseModel):
    egress_gb_month: Decimal | None = Field(default=None, ge=0)
    interzone_gb: Decimal | None = Field(default=None, ge=0)
    on_prem_gb: Decimal | None = Field(default=None, ge=0)
    private_endpoint_required: bool | None = None
    exclusion_policy_id: str | None = None


class BackupPolicy(BaseModel):
    description: str
    retained_gb: Decimal | None = Field(default=None, ge=0)


class SupportTier(BaseModel):
    name: str
    exclusion_policy_id: str | None = None


class ObservabilityProfile(BaseModel):
    ingest_gb_month: Decimal | None = Field(default=None, ge=0)
    retention_days: int | None = Field(default=None, ge=0)
    exclusion_policy_id: str | None = None


class SecurityProfile(BaseModel):
    required_controls: list[str]
    exclusion_policy_id: str | None = None


class LicenseEligibility(BaseModel):
    active_software_assurance: bool | None = None
    azure_hybrid_benefit_eligible: bool | None = None
    aws_license_mobility_eligible: bool | None = None
    acquired_before_2019_10_01: bool | None = None
    perpetual_license: bool | None = None
    eligible_product_version: bool | None = None
    azure_sql_deployment_model: SqlDeploymentModel | None = None
    passive_secondary: bool = False
    passive_use_only: bool | None = None


class ComputeUnit(BaseModel):
    id: str
    name: str
    environment: str
    count: int = Field(ge=1)
    operating_system: str
    license_model: str
    license_eligibility: LicenseEligibility = Field(
        default_factory=LicenseEligibility
    )
    vcpu_each: int = Field(ge=1)
    ram_gb_each: Decimal = Field(gt=0)
    runtime_hours_month: Decimal | None = Field(default=None, gt=0, le=744)
    topology_tier: TopologyTier
    storage_performance_profile: StoragePerformanceProfile
    network_transfer_profile: NetworkTransferProfile
    backup_policy: BackupPolicy
    support_tier: SupportTier
    observability_profile: ObservabilityProfile
    security_profile: SecurityProfile


class DatabaseUnit(BaseModel):
    id: str
    name: str
    environment: str
    engine: str
    license_model: str
    license_eligibility: LicenseEligibility = Field(
        default_factory=LicenseEligibility
    )
    nodes: int = Field(ge=1)
    vcpu_each: int = Field(ge=1)
    ram_gb_each: Decimal = Field(gt=0)
    size_gb: Decimal = Field(gt=0)
    topology_tier: TopologyTier
    storage_performance_profile: StoragePerformanceProfile
    network_transfer_profile: NetworkTransferProfile
    backup_policy: BackupPolicy
    support_tier: SupportTier
    observability_profile: ObservabilityProfile
    security_profile: SecurityProfile


class StorageUnit(BaseModel):
    id: str
    name: str
    environment: str
    storage_type: str
    protocol: str
    allocated_gb: Decimal = Field(gt=0)
    used_gb: Decimal = Field(ge=0)
    storage_performance_profile: StoragePerformanceProfile
    backup_policy: BackupPolicy


class PrincipalRecord(BaseModel):
    """The verified signed-in identity recorded with an action, separate from typed attestations."""

    model_config = ConfigDict(frozen=True)

    tenant_id: str
    object_id: str
    name: str


class Gap(BaseModel):
    id: str
    unit_id: str | None = None
    kind: GapKind
    license_product: str | None = None
    prompt: str
    reason: str
    material: bool = True
    status: GapStatus = GapStatus.OPEN
    raw_value: Any = None
    canonical_value: dict[str, Any] | None = None
    resolved_by: str | None = None
    resolved_by_principal: PrincipalRecord | None = None
    resolved_at: datetime | None = None


class AssumptionSet(BaseModel):
    currency: str = "USD"
    horizon_months: int = 36
    commercial_view: CommercialView = CommercialView.LIST
    commitment_term_years: Literal[3] = 3
    commitment_payment_option: CommitmentPaymentOption = (
        CommitmentPaymentOption.ALL_UPFRONT
    )
    commitment_utilization_percent: Decimal = Field(
        default=Decimal("100"),
        ge=Decimal("100"),
        le=Decimal("100"),
    )
    commitment_tenancy: CommitmentTenancy = CommitmentTenancy.SHARED
    azure_region: str | None = None
    aws_region: str | None = None
    pricebook_snapshot_id: str


class ApprovalRecord(BaseModel):
    approver: str
    approved_at: datetime
    content_digest: str
    non_production: bool


class PresentationPolicy(BaseModel):
    show_aws: bool = False


class PriceBookManifest(BaseModel):
    snapshot_id: str
    content_hash: str
    source_snapshot_id: str | None = None
    source_content_hash: str | None = None
    priced_as_of: str
    schema_version: str
    validation_status: str
    publishing_human: str
    source_urls: list[str]
    non_production: bool


class RateSource(BaseModel):
    rate_key: str
    source_type: str
    provider: str
    sku: str | None = None
    meter: str | None = None
    row_id: str | None = None
    row_ids: list[str] = Field(default_factory=list)
    note: str | None = None


class ExcludedCost(BaseModel):
    id: str
    category: str
    materiality: str
    rationale: str
    policy_id: str


class ProviderCommercialAmounts(BaseModel):
    list_rate: Decimal | None = None
    list_amount: Decimal | None = None
    savings_plan_rate: Decimal | None = None
    savings_plan_quantity: Decimal | None = None
    savings_plan_covered_amount: Decimal | None = None
    savings_plan_uncovered_amount: Decimal | None = None
    savings_plan_amount: Decimal | None = None
    reservation_rate: Decimal | None = None
    reservation_quantity: Decimal | None = None
    reservation_covered_amount: Decimal | None = None
    reservation_uncovered_amount: Decimal | None = None
    reservation_amount: Decimal | None = None


class CommercialScenario(BaseModel):
    id: str
    provider: Cloud
    label: str
    offer: CommitmentOffer
    monthly_total: Decimal | None
    covered_monthly_total: Decimal | None = None
    uncovered_monthly_total: Decimal | None = None
    term_years: int | None = None
    payment_option: str | None = None
    utilization_percent: Decimal | None = None
    tenancy: str | None = None
    public_price_only: bool = True
    available: bool
    unavailable_reason: str | None = None

    @model_validator(mode="after")
    def validate_availability(self) -> "CommercialScenario":
        if self.available and self.monthly_total is None:
            raise ValueError("An available commercial scenario requires a monthly total.")
        if not self.available and not self.unavailable_reason:
            raise ValueError("An unavailable commercial scenario requires a reason.")
        return self


class BreakevenPoint(BaseModel):
    reference_discount_percent: Decimal = Field(ge=0, le=100)
    target_discount_to_parity_percent: Decimal = Field(ge=0, le=100)
    additional_discount_advantage_percent: Decimal = Field(ge=0, le=100)


class BreakevenSensitivity(BaseModel):
    id: str
    label: str
    offer: CommitmentOffer
    aws_public_monthly_total: Decimal | None
    azure_public_monthly_total: Decimal | None
    reference_provider: Cloud | None = None
    target_provider: Cloud | None = None
    points: list[BreakevenPoint] = Field(default_factory=list)
    public_price_only: bool = True
    actual_contracted_price: bool = False
    workload_isolated: bool = True
    available: bool
    unavailable_reason: str | None = None
    disclosure: str

    @model_validator(mode="after")
    def validate_availability(self) -> "BreakevenSensitivity":
        if self.available and (
            self.aws_public_monthly_total is None
            or self.azure_public_monthly_total is None
            or not self.points
        ):
            raise ValueError(
                "An available breakeven sensitivity requires both totals and points."
            )
        if not self.available and not self.unavailable_reason:
            raise ValueError(
                "An unavailable breakeven sensitivity requires a reason."
            )
        return self


class LicenseAssessment(BaseModel):
    unit_id: str
    product: str
    azure_treatment: str
    aws_treatment: str
    blocks_pricing: bool
    evidence_urls: list[str]


class LineItem(BaseModel):
    id: str
    service_definition_id: str
    skumap_component_id: str
    unit_id: str
    unit_name: str
    component: str
    role: str
    match_class: MatchClass
    status: LineStatus
    quantity: Decimal | None = None
    aws_quantity: Decimal | None = None
    azure_quantity: Decimal | None = None
    quantity_unit: str | None = None
    aws_rate: Decimal | None = None
    azure_rate: Decimal | None = None
    aws_amount: Decimal | None = None
    azure_amount: Decimal | None = None
    aws_commercial: ProviderCommercialAmounts = Field(
        default_factory=ProviderCommercialAmounts
    )
    azure_commercial: ProviderCommercialAmounts = Field(
        default_factory=ProviderCommercialAmounts
    )
    higher_cloud: Cloud | None = None
    demo_assumption: bool = False
    exclusion_policy_id: str | None = None
    formula: str
    evidence: str
    unpriced_reason: str | None = None


class CostDriver(BaseModel):
    line_id: str
    component: str
    delta: Decimal
    higher_cloud: Cloud
    share_of_absolute_delta_percent: Decimal


class VerdictConfidence(StrEnum):
    FINAL = "Final"
    PLACEHOLDER = "Placeholder"
    DRAFT = "Draft"


class AssumptionStressScenario(BaseModel):
    """List-basis totals with the placeholder amount set to a stress multiple."""

    multiplier: Decimal
    assumed_monthly_amount: Decimal
    aws_monthly_total: Decimal
    azure_monthly_total: Decimal
    cheaper_cloud: Cloud | None
    headline_delta: Decimal
    changes_cheaper_cloud: bool


class AssumptionSensitivity(BaseModel):
    id: str
    label: str
    provider: Cloud
    line_ids: list[str]
    commercial_view: CommercialView = CommercialView.LIST
    assumed_monthly_amount: Decimal
    at_zero: AssumptionStressScenario
    at_double: AssumptionStressScenario
    changes_cheaper_cloud: bool


class Comparison(BaseModel):
    state: ComparisonState
    product_label: str = "public-list run-rate benchmark"
    commercial_view: CommercialView
    priced_as_of: str
    pricebook_snapshot_id: str
    pricebook_content_hash: str
    source_pricebook_snapshot_id: str | None = None
    source_pricebook_content_hash: str | None = None
    source_urls: list[str]
    rate_sources: list[RateSource]
    skumap_content_digest: str
    skumap_approval: ApprovalRecord
    calculator_rule_version: str
    presentation: PresentationPolicy
    provisional: bool
    aws_monthly_total: Decimal | None
    azure_monthly_total: Decimal | None
    commercial_scenarios: list[CommercialScenario]
    breakeven_sensitivities: list[BreakevenSensitivity]
    license_assessments: list[LicenseAssessment]
    headline_delta: Decimal | None
    headline_delta_percent: Decimal | None = None
    cheaper_cloud: Cloud | None
    verdict_confidence: VerdictConfidence = VerdictConfidence.DRAFT
    assumption_sensitivities: list[AssumptionSensitivity] = Field(default_factory=list)
    line_items: list[LineItem]
    cost_drivers: list[CostDriver]
    excluded_cost_ledger: list[ExcludedCost]
    assumptions: AssumptionSet
    run_hash: str
    can_export: bool
    warnings: list[str]


class Application(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    name: str
    intake_file_name: str
    comparison_state: ComparisonState = ComparisonState.DRAFT_BENCHMARK
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    created_by: PrincipalRecord | None = None


class ApplicationSummary(Application):
    """Additive list view: enough for Home without per-estimate detail calls."""

    expires_at: datetime
    open_question_count: int
    first_open_prompt: str | None = None
    verdict_confidence: VerdictConfidence
    cheaper_cloud: Cloud | None = None
    headline_delta: Decimal | None = None
    headline_delta_percent: Decimal | None = None
    aws_monthly_total: Decimal | None = None
    azure_monthly_total: Decimal | None = None
    priced_as_of: str
    excluded_cost_categories: list[str] = Field(default_factory=list)
    placeholder_providers: list[Cloud] = Field(default_factory=list)


class IntakeSummary(BaseModel):
    """Counts of parsed units, computed server-side so the browser never sums."""

    server_count: int
    server_vcpu: int
    database_count: int
    storage_count: int
    storage_allocated_gb: Decimal
    open_question_count: int


class ApplicationDetail(BaseModel):
    application: Application
    compute_units: list[ComputeUnit]
    database_units: list[DatabaseUnit]
    storage_units: list[StorageUnit]
    gaps: list[Gap]
    comparison: Comparison
    expires_at: datetime | None = None
    intake_summary: IntakeSummary | None = None


class NormalizedIntake(BaseModel):
    application_name: str
    environments: list[str]
    compute_units: list[ComputeUnit]
    database_units: list[DatabaseUnit]
    storage_units: list[StorageUnit]
    gaps: list[Gap]


class GapResolutionRequest(BaseModel):
    resolved_by: str = Field(min_length=1, max_length=200)
    runtime_hours_month: Decimal | None = Field(default=None, gt=0, le=744)
    azure_region: str | None = Field(default=None, min_length=1, max_length=64)
    aws_region: str | None = Field(default=None, min_length=1, max_length=64)
    target_iops: int | None = Field(default=None, ge=0, le=80000)
    target_mbps: Decimal | None = Field(default=None, ge=0, le=2000)
    active_software_assurance: bool | None = None
    azure_hybrid_benefit_eligible: bool | None = None
    aws_license_mobility_eligible: bool | None = None
    acquired_before_2019_10_01: bool | None = None
    passive_secondary: bool | None = None
    passive_use_only: bool | None = None
    perpetual_license: bool | None = None
    eligible_product_version: bool | None = None
    azure_sql_deployment_model: SqlDeploymentModel | None = None
