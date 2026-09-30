import hashlib
from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import load_workbook
from pydantic import ValidationError

from app.data import GapResolutionError, InMemoryApplicationRepository
from app.models import (
    ApprovalRecord,
    Application,
    Cloud,
    CommercialScenario,
    ComparisonState,
    CommitmentOffer,
    Gap,
    GapKind,
    GapResolutionRequest,
    GapStatus,
    LineStatus,
)
from app.pricing import (
    PricingConfigurationError,
    _presentation_policy,
    pricing_engine,
)


def test_commercial_scenario_availability_requires_total_or_reason() -> None:
    with pytest.raises(ValidationError, match="requires a monthly total"):
        CommercialScenario(
            id="invalid-available",
            provider=Cloud.AZURE,
            label="Invalid",
            offer=CommitmentOffer.LIST,
            monthly_total=None,
            available=True,
        )
    with pytest.raises(ValidationError, match="requires a reason"):
        CommercialScenario(
            id="invalid-unavailable",
            provider=Cloud.AZURE,
            label="Invalid",
            offer=CommitmentOffer.LIST,
            monthly_total=None,
            available=False,
        )


def test_breakeven_sensitivity_is_two_sided_and_directional() -> None:
    aws = CommercialScenario(
        id="aws-list",
        provider=Cloud.AWS,
        label="AWS",
        offer=CommitmentOffer.LIST,
        monthly_total=Decimal("120"),
        available=True,
    )
    azure = CommercialScenario(
        id="azure-list",
        provider=Cloud.AZURE,
        label="Azure",
        offer=CommitmentOffer.LIST,
        monthly_total=Decimal("100"),
        available=True,
    )

    sensitivity = pricing_engine._breakeven_sensitivity(
        CommitmentOffer.LIST,
        "List parity",
        aws,
        azure,
    )

    assert sensitivity.reference_provider == Cloud.AZURE
    assert sensitivity.target_provider == Cloud.AWS
    assert sensitivity.points[0].target_discount_to_parity_percent == Decimal(
        "16.67"
    )
    assert sensitivity.points[1].reference_discount_percent == Decimal("10")
    assert sensitivity.points[1].target_discount_to_parity_percent == Decimal(
        "25.00"
    )
    assert sensitivity.points[1].additional_discount_advantage_percent == Decimal(
        "15.00"
    )


def test_breakeven_sensitivity_fails_closed_when_basis_is_unavailable() -> None:
    aws = CommercialScenario(
        id="aws-savings-plan",
        provider=Cloud.AWS,
        label="AWS",
        offer=CommitmentOffer.SAVINGS_PLAN,
        monthly_total=None,
        available=False,
        unavailable_reason="AWS commitment coverage is incomplete.",
    )
    azure = CommercialScenario(
        id="azure-savings-plan",
        provider=Cloud.AZURE,
        label="Azure",
        offer=CommitmentOffer.SAVINGS_PLAN,
        monthly_total=Decimal("100"),
        available=True,
    )

    sensitivity = pricing_engine._breakeven_sensitivity(
        CommitmentOffer.SAVINGS_PLAN,
        "Savings-plan parity",
        aws,
        azure,
    )

    assert sensitivity.available is False
    assert sensitivity.points == []
    assert sensitivity.unavailable_reason == "AWS commitment coverage is incomplete."


def test_breakeven_sensitivity_normalizes_equal_total_percentages() -> None:
    aws = CommercialScenario(
        id="aws-list",
        provider=Cloud.AWS,
        label="AWS",
        offer=CommitmentOffer.LIST,
        monthly_total=Decimal("100"),
        available=True,
    )
    azure = CommercialScenario(
        id="azure-list",
        provider=Cloud.AZURE,
        label="Azure",
        offer=CommitmentOffer.LIST,
        monthly_total=Decimal("100"),
        available=True,
    )

    sensitivity = pricing_engine._breakeven_sensitivity(
        CommitmentOffer.LIST,
        "List parity",
        aws,
        azure,
    )

    assert sensitivity.reference_provider is None
    assert sensitivity.target_provider is None
    assert sensitivity.points[1].target_discount_to_parity_percent == Decimal(
        "10.00"
    )


def test_approval_record_requires_non_production_evidence() -> None:
    with pytest.raises(ValidationError, match="non_production"):
        ApprovalRecord(
            approver="reviewer@example.com",
            approved_at="2026-09-22T00:00:00Z",
            content_digest="abc123",
        )


def test_aws_presentation_is_hidden_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRESENT_AWS_PRICING", raising=False)
    assert _presentation_policy().show_aws is False


def test_aws_presentation_flag_is_strict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRESENT_AWS_PRICING", "true")
    assert _presentation_policy().show_aws is True
    monkeypatch.setenv("PRESENT_AWS_PRICING", "yes")
    with pytest.raises(PricingConfigurationError, match="true.*false"):
        _presentation_policy()


def test_run_hash_and_decimal_lines_are_deterministic(repository_root: Path) -> None:
    repository = InMemoryApplicationRepository()
    content = (repository_root / "samples" / "synthetic_intake_completed.xlsx").read_bytes()
    detail = repository.create_from_intake("fixture.xlsx", content)

    first = repository.get(detail.application.id).comparison
    second = repository.get(detail.application.id).comparison

    assert first.run_hash == second.run_hash
    assert all(
        line.aws_amount is None or line.aws_amount.as_tuple().exponent == -2
        for line in first.line_items
    )


def test_unapproved_skumap_digest_blocks_pricing(repository_root: Path) -> None:
    original = pricing_engine._approval_digest
    pricing_engine._approval_digest = "tampered"
    try:
        repository = InMemoryApplicationRepository()
        content = (repository_root / "samples" / "synthetic_intake_completed.xlsx").read_bytes()
        with pytest.raises(PricingConfigurationError, match="approval digest"):
            repository.create_from_intake("fixture.xlsx", content)
    finally:
        pricing_engine._approval_digest = original


def test_unpublished_pricebook_blocks_pricing(repository_root: Path) -> None:
    original = pricing_engine.manifest.validation_status
    pricing_engine.manifest.validation_status = "Failed"
    try:
        repository = InMemoryApplicationRepository()
        content = (repository_root / "samples" / "synthetic_intake_completed.xlsx").read_bytes()
        with pytest.raises(PricingConfigurationError, match="not Published"):
            repository.create_from_intake("fixture.xlsx", content)
    finally:
        pricing_engine.manifest.validation_status = original


@pytest.mark.parametrize("source", ["staged-runs", "typo", ""])
def test_unconfigured_pricebook_source_fails_closed(monkeypatch, source: str) -> None:
    from app.pricing import PilotPricingEngine

    monkeypatch.setenv("PRICEBOOK_SOURCE", source)
    with pytest.raises(PricingConfigurationError, match="not enabled"):
        PilotPricingEngine()


def _resolve_regions(repository: InMemoryApplicationRepository, application_id: object):
    return repository.resolve_gap(
        application_id,
        "P1.regions",
        GapResolutionRequest(
            resolved_by="Reviewer",
            azure_region="eastus2",
            aws_region="us-east-1",
        ),
    )


def _resolve_license(
    repository: InMemoryApplicationRepository,
    application_id: object,
    gap_id: str,
    *,
    active_sa: bool = True,
    azure_ahb: bool = True,
    aws_mobility: bool = False,
    pre_2019: bool = False,
    perpetual: bool = True,
    eligible_version: bool = True,
    passive_secondary: bool = False,
    passive_only: bool = False,
    sql_deployment_model: str = "SqlVm",
):
    return repository.resolve_gap(
        application_id,
        gap_id,
        GapResolutionRequest(
            resolved_by="SAM Reviewer",
            active_software_assurance=active_sa,
            azure_hybrid_benefit_eligible=azure_ahb,
            aws_license_mobility_eligible=aws_mobility,
            acquired_before_2019_10_01=pre_2019,
            perpetual_license=perpetual,
            eligible_product_version=eligible_version,
            passive_secondary=passive_secondary,
            passive_use_only=passive_only,
            azure_sql_deployment_model=sql_deployment_model,
        ),
    )


def _modified_intake(
    repository_root: Path,
    tmp_path: Path,
    name: str,
    edits: list[tuple[str, str, object]],
) -> bytes:
    workbook = load_workbook(
        repository_root / "samples" / "synthetic_intake_completed.xlsx"
    )
    for sheet, cell, value in edits:
        workbook[sheet][cell] = value
    path = tmp_path / name
    workbook.save(path)
    return path.read_bytes()


def test_missing_approved_rate_fails_closed_without_poisoning_registry(
    repository_root: Path,
) -> None:
    repository = InMemoryApplicationRepository()
    content = (repository_root / "samples" / "synthetic_intake_completed.xlsx").read_bytes()
    detail = repository.create_from_intake("fixture.xlsx", content)
    removed = pricing_engine._pricebook["rates"].pop("aws.vm.linux.4x16.hour")
    try:
        with pytest.raises(PricingConfigurationError, match="approval digest"):
            _resolve_regions(repository, detail.application.id)
        assert repository.list()
    finally:
        pricing_engine._pricebook["rates"]["aws.vm.linux.4x16.hour"] = removed


def test_missing_committed_rate_degrades_only_that_offer() -> None:
    key = "aws.vm.linux.4x16.hour.savings_plan"
    removed = pricing_engine._pricebook["rates"].pop(key)
    try:
        rates = pricing_engine._committed_vm_rates("4x16")
        assert rates[0] is None
        assert rates[1:] == (
            Decimal("0.079604261796"),
            Decimal("0.092920"),
            Decimal("0.078614916286"),
        )
    finally:
        pricing_engine._pricebook["rates"][key] = removed


@pytest.mark.parametrize(
    ("sheet", "cell", "value", "expected_component"),
    [
        ("2 Servers", "E2", "Solaris 11", "Compute operating system"),
        ("3 Databases", "C2", "MySQL 8", "Managed database"),
        ("4 Storage", "C2", "object", "Storage service"),
        ("4 Storage", "D2", "NFS", "Storage service"),
    ],
)
def test_unsupported_workloads_never_fall_through_to_supported_pricing(
    repository_root: Path,
    tmp_path: Path,
    sheet: str,
    cell: str,
    value: str,
    expected_component: str,
) -> None:
    repository = InMemoryApplicationRepository()
    content = _modified_intake(
        repository_root,
        tmp_path,
        f"unsupported-{cell}.xlsx",
        [(sheet, cell, value)],
    )
    detail = repository.create_from_intake("unsupported.xlsx", content)

    assert any(
        line.component == expected_component
        and line.match_class.value == "unsupported"
        and line.status == LineStatus.UNPRICED
        for line in detail.comparison.line_items
    )


def test_license_models_are_visible_and_unknown_models_stay_unpriced(
    repository_root: Path,
    tmp_path: Path,
) -> None:
    byol_repository = InMemoryApplicationRepository()
    byol = _modified_intake(
        repository_root,
        tmp_path,
        "byol.xlsx",
        [("2 Servers", "F2", "BYOL")],
    )
    byol_detail = byol_repository.create_from_intake("byol.xlsx", byol)
    byol_detail = _resolve_regions(byol_repository, byol_detail.application.id)
    assert any(
        line.status == LineStatus.UNPRICED
        and line.skumap_component_id == "vm-win-oslicense"
        and "Dedicated Host" in (line.unpriced_reason or "")
        for line in byol_detail.comparison.line_items
    )

    unknown_repository = InMemoryApplicationRepository()
    unknown = _modified_intake(
        repository_root,
        tmp_path,
        "unknown-license.xlsx",
        [("2 Servers", "F2", "Mystery License")],
    )
    unknown_detail = unknown_repository.create_from_intake(
        "unknown-license.xlsx",
        unknown,
    )
    unknown_detail = _resolve_regions(
        unknown_repository,
        unknown_detail.application.id,
    )
    assert any(
        line.status == LineStatus.UNPRICED
        and line.skumap_component_id == "vm-win-oslicense"
        and "not approved" in (line.unpriced_reason or "")
        for line in unknown_detail.comparison.line_items
    )


def test_throughput_uses_provider_specific_units(
    repository_root: Path,
) -> None:
    repository = InMemoryApplicationRepository()
    content = (repository_root / "samples" / "synthetic_intake_completed.xlsx").read_bytes()
    detail = repository.create_from_intake("fixture.xlsx", content)
    detail = _resolve_regions(repository, detail.application.id)
    throughput = next(
        line
        for line in detail.comparison.line_items
        if line.skumap_component_id == "block-throughput"
    )

    assert throughput.aws_quantity != throughput.azure_quantity
    assert throughput.aws_quantity == pytest.approx(Decimal("46.661376953125"))
    assert throughput.azure_quantity == Decimal("55")
    assert throughput.quantity is None


def test_skumap_metadata_drives_priced_line_evidence(
    repository_root: Path,
) -> None:
    repository = InMemoryApplicationRepository()
    content = (repository_root / "samples" / "synthetic_intake_completed.xlsx").read_bytes()
    detail = repository.create_from_intake("fixture.xlsx", content)
    detail = _resolve_regions(repository, detail.application.id)
    line = next(
        item
        for item in detail.comparison.line_items
        if item.skumap_component_id == "block-throughput"
    )
    mapping = pricing_engine._mapping("SD-BLOCK", "block-throughput")

    assert line.service_definition_id == "SD-BLOCK"
    assert line.role == mapping["role"]
    assert line.formula == mapping["quantityFormula"]
    assert mapping["capabilityEvidence"] in line.evidence
    assert "converted to MiB/s" in line.evidence


def test_draft_benchmark_has_no_ranked_cost_drivers(repository_root: Path) -> None:
    repository = InMemoryApplicationRepository()
    content = (repository_root / "samples" / "synthetic_intake_completed.xlsx").read_bytes()
    detail = repository.create_from_intake("fixture.xlsx", content)
    detail = _resolve_regions(repository, detail.application.id)

    assert detail.comparison.state.value == "DraftBenchmark"
    assert detail.comparison.cost_drivers == []


def test_configuration_failure_does_not_store_partial_application(
    repository_root: Path,
) -> None:
    repository = InMemoryApplicationRepository()
    content = (repository_root / "samples" / "synthetic_intake_completed.xlsx").read_bytes()
    original = pricing_engine._approval_digest
    pricing_engine._approval_digest = "tampered"
    try:
        with pytest.raises(PricingConfigurationError):
            repository.create_from_intake("fixture.xlsx", content)
    finally:
        pricing_engine._approval_digest = original

    assert repository.list() == []


def test_azure_hybrid_benefit_only_zeroes_the_azure_uplift(
    repository_root: Path,
    tmp_path: Path,
) -> None:
    repository = InMemoryApplicationRepository()
    content = _modified_intake(
        repository_root,
        tmp_path,
        "ahb.xlsx",
        [("2 Servers", "F2", "Azure Hybrid Benefit")],
    )
    detail = repository.create_from_intake("ahb.xlsx", content)
    assert any(gap.id == "compute-1.license" for gap in detail.gaps)
    detail = _resolve_regions(repository, detail.application.id)
    detail = _resolve_license(
        repository,
        detail.application.id,
        "compute-1.license",
    )
    license_line = next(
        line
        for line in detail.comparison.line_items
        if line.skumap_component_id == "vm-win-oslicense"
    )

    assert license_line.status == LineStatus.PRICED
    assert license_line.aws_amount is not None
    assert license_line.aws_amount > 0
    assert license_line.azure_amount == Decimal("0.00")
    assessment = next(
        item
        for item in detail.comparison.license_assessments
        if item.unit_id == "compute-1"
    )
    assert assessment.blocks_pricing is False
    assert "no License Mobility" in assessment.aws_treatment


def test_unconfirmed_ahb_and_windows_byol_fail_closed(
    repository_root: Path,
    tmp_path: Path,
) -> None:
    ahb_repository = InMemoryApplicationRepository()
    ahb = _modified_intake(
        repository_root,
        tmp_path,
        "ahb-ineligible.xlsx",
        [("2 Servers", "F2", "Azure Hybrid Benefit")],
    )
    detail = ahb_repository.create_from_intake("ahb-ineligible.xlsx", ahb)
    detail = _resolve_regions(ahb_repository, detail.application.id)
    detail = _resolve_license(
        ahb_repository,
        detail.application.id,
        "compute-1.license",
        active_sa=False,
        azure_ahb=False,
    )
    assert any(
        line.skumap_component_id == "vm-win-oslicense"
        and line.status == LineStatus.UNPRICED
        for line in detail.comparison.line_items
    )

    byol_repository = InMemoryApplicationRepository()
    byol = _modified_intake(
        repository_root,
        tmp_path,
        "windows-byol.xlsx",
        [("2 Servers", "F2", "BYOL")],
    )
    byol_detail = byol_repository.create_from_intake("windows-byol.xlsx", byol)
    byol_detail = _resolve_regions(byol_repository, byol_detail.application.id)
    byol_detail = _resolve_license(
        byol_repository,
        byol_detail.application.id,
        "compute-1.license",
        pre_2019=True,
    )
    assert byol_detail.comparison.state == ComparisonState.DRAFT_BENCHMARK
    assert any(
        item.unit_id == "compute-1" and item.blocks_pricing
        for item in byol_detail.comparison.license_assessments
    )
    assert any(
        item.unit_id == "compute-1"
        and "ineligible" in item.aws_treatment
        for item in byol_detail.comparison.license_assessments
    )


def test_sql_server_license_rules_create_material_gate(
    repository_root: Path,
    tmp_path: Path,
) -> None:
    repository = InMemoryApplicationRepository()
    content = _modified_intake(
        repository_root,
        tmp_path,
        "sql-server.xlsx",
        [
            ("3 Databases", "C2", "SQL Server 2022 Enterprise"),
            ("3 Databases", "D2", "BYOL"),
        ],
    )
    detail = repository.create_from_intake("sql-server.xlsx", content)
    assert any(
        gap.id == "database-1.license" and gap.material
        for gap in detail.gaps
    )
    detail = _resolve_regions(repository, detail.application.id)
    detail = _resolve_license(
        repository,
        detail.application.id,
        "database-1.license",
        aws_mobility=True,
    )
    assessment = next(
        item
        for item in detail.comparison.license_assessments
        if item.unit_id == "database-1"
    )
    assert assessment.product == "SQL Server"
    assert "License Mobility eligibility is confirmed" in assessment.aws_treatment
    assert assessment.blocks_pricing is True
    assert detail.comparison.can_export is False


def test_postgresql_server_text_does_not_trigger_sql_server_license_gate(
    repository_root: Path,
    tmp_path: Path,
) -> None:
    repository = InMemoryApplicationRepository()
    content = _modified_intake(
        repository_root,
        tmp_path,
        "postgresql-server.xlsx",
        [("3 Databases", "C2", "PostgreSQL Server 15")],
    )
    detail = repository.create_from_intake("postgresql-server.xlsx", content)
    assert not any(gap.license_product == "SQL Server" for gap in detail.gaps)
    assert not any(
        item.product == "SQL Server"
        for item in detail.comparison.license_assessments
    )


def test_sql_passive_secondary_answers_are_consistent(
    repository_root: Path,
    tmp_path: Path,
) -> None:
    repository = InMemoryApplicationRepository()
    content = _modified_intake(
        repository_root,
        tmp_path,
        "sql-passive.xlsx",
        [
            ("3 Databases", "C2", "SQL Server 2022 Enterprise"),
            ("3 Databases", "D2", "BYOL"),
        ],
    )
    detail = repository.create_from_intake("sql-passive.xlsx", content)
    with pytest.raises(GapResolutionError, match="cannot be true"):
        repository.resolve_gap(
            detail.application.id,
            "database-1.license",
            GapResolutionRequest(
                resolved_by="SAM Reviewer",
                active_software_assurance=True,
                azure_hybrid_benefit_eligible=True,
                aws_license_mobility_eligible=True,
                acquired_before_2019_10_01=False,
                perpetual_license=True,
                eligible_product_version=True,
                passive_secondary=False,
                passive_use_only=True,
                azure_sql_deployment_model="SqlVm",
            ),
        )


@pytest.mark.parametrize("license_model", ["Not included", "No subscription"])
def test_negated_license_phrases_do_not_match_approved_models(
    repository_root: Path,
    tmp_path: Path,
    license_model: str,
) -> None:
    repository = InMemoryApplicationRepository()
    content = _modified_intake(
        repository_root,
        tmp_path,
        f"negated-{license_model.replace(' ', '-')}.xlsx",
        [("2 Servers", "F2", license_model)],
    )
    detail = repository.create_from_intake("negated.xlsx", content)
    detail = _resolve_regions(repository, detail.application.id)

    assert any(
        line.skumap_component_id == "vm-win-oslicense"
        and line.status == LineStatus.UNPRICED
        for line in detail.comparison.line_items
    )


def test_suse_subscription_requires_an_approved_uplift(
    repository_root: Path,
    tmp_path: Path,
) -> None:
    repository = InMemoryApplicationRepository()
    content = _modified_intake(
        repository_root,
        tmp_path,
        "suse.xlsx",
        [
            ("2 Servers", "E3", "SUSE Linux Enterprise"),
            ("2 Servers", "F3", "Subscription"),
        ],
    )
    detail = repository.create_from_intake("suse.xlsx", content)
    detail = _resolve_regions(repository, detail.application.id)

    assert any(
        line.unit_id == "compute-2"
        and line.skumap_component_id == "vm-lnx-oslicense"
        and line.status == LineStatus.UNPRICED
        and "SUSE" in (line.unpriced_reason or "")
        for line in detail.comparison.line_items
    )


def test_ha_standby_storage_is_explicitly_excluded(
    repository_root: Path,
) -> None:
    repository = InMemoryApplicationRepository()
    content = (repository_root / "samples" / "synthetic_intake_completed.xlsx").read_bytes()
    detail = repository.create_from_intake("fixture.xlsx", content)
    detail = _resolve_regions(repository, detail.application.id)

    exclusion = next(
        line
        for line in detail.comparison.line_items
        if line.component == "PostgreSQL HA standby storage"
    )
    assert exclusion.status == LineStatus.EXCLUDED
    assert exclusion.exclusion_policy_id == "PILOT-PG-STANDBY-STORAGE-EXCLUSION-V1"
    assert any(
        entry.id == exclusion.id
        and entry.policy_id == exclusion.exclusion_policy_id
        for entry in detail.comparison.excluded_cost_ledger
    )


def test_skumap_approval_binds_exact_artifact_bytes(repository_root: Path) -> None:
    expected = hashlib.sha256(
        (repository_root / "samples" / "skumap_seed_v1.json").read_bytes()
    ).hexdigest()

    assert pricing_engine.skumap_approval.content_digest == expected
    assert pricing_engine._skumap_digest == expected


def test_empty_comparison_cannot_pass_completeness_gate() -> None:
    comparison = pricing_engine.compare(
        Application(name="empty", intake_file_name="empty.xlsx"),
        [],
        [],
        [],
        [
            Gap(
                id="P1.regions",
                kind=GapKind.APPROVED_REGIONS,
                prompt="regions",
                reason="test",
                status=GapStatus.RESOLVED,
                canonical_value={
                    "azure_region": "eastus2",
                    "aws_region": "us-east-1",
                },
            )
        ],
    )

    assert comparison.state == ComparisonState.DRAFT_BENCHMARK
    assert comparison.can_export is False
    assert comparison.aws_monthly_total is None
    assert comparison.azure_monthly_total is None
