from __future__ import annotations

import hashlib
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from .core import ValidationError, canonical_json
from .snapshot import verify_published_artifact


HOURS_PER_MONTH = Decimal("730")


def reconcile_representative_application(
    artifact_path: Path,
    *,
    enterprise_discount_percent: Decimal = Decimal("10"),
    approval_key: str | None = None,
    approval_verifier: Callable[[dict[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    if not enterprise_discount_percent.is_finite() or not (
        Decimal("0") <= enterprise_discount_percent <= Decimal("100")
    ):
        raise ValidationError("Enterprise discount percent must be between 0 and 100.")
    selectors = _selectors()
    candidate_counts: dict[str, int] = {
        name: 0 for name, _, _ in selectors
    }
    candidates: dict[str, dict[str, Any] | None] = {
        name: None for name, _, _ in selectors
    }

    def collect_candidate(row: dict[str, Any]) -> None:
        for name, provider, predicate in selectors:
            if row["provider"] == provider and predicate(row):
                candidate_counts[name] += 1
                if candidates[name] is None:
                    candidates[name] = row

    manifest = verify_published_artifact(
        artifact_path,
        on_row=collect_candidate,
        approval_key=approval_key,
        approval_verifier=approval_verifier,
    )
    selected: dict[str, dict[str, Any]] = {}
    for name, row in candidates.items():
        if candidate_counts[name] != 1 or row is None:
            raise ValidationError(
                f"Reconciliation selector {name} matched {candidate_counts[name]} rows; "
                "expected exactly one."
            )
        selected[name] = row

    quantities = {
        "vmHours": HOURS_PER_MONTH,
        "blockStorageGb": Decimal("200"),
        "storageHours": HOURS_PER_MONTH,
    }
    aws_storage = (
        Decimal(selected["aws-gp3-capacity"]["price"]) * quantities["blockStorageGb"]
    )
    azure_storage = (
        Decimal(selected["azure-premium-ssd-v2-capacity"]["price"])
        * quantities["blockStorageGb"]
        * quantities["storageHours"]
    )
    list_aws = (
        Decimal(selected["aws-vm-linux"]["price"]) * quantities["vmHours"]
        + aws_storage
    )
    list_azure = (
        Decimal(selected["azure-vm-linux"]["price"]) * quantities["vmHours"]
        + azure_storage
    )
    committed_aws = (
        Decimal(selected["aws-vm-linux-committed"]["price"])
        * quantities["vmHours"]
        + aws_storage
    )
    committed_azure = (
        Decimal(selected["azure-vm-linux-committed"]["price"])
        * quantities["vmHours"]
        + azure_storage
    )
    common_discount = enterprise_discount_percent / Decimal("100")
    enterprise_aws = list_aws * (Decimal("1") - common_discount)
    enterprise_azure = list_azure * (Decimal("1") - common_discount)
    sensitivity = _two_sided_sensitivity(
        aws=list_aws,
        azure=list_azure,
        common_discount=common_discount,
    )
    report = {
        "snapshotId": manifest["snapshotId"],
        "contentHash": manifest["contentHash"],
        "pricedAsOf": manifest["pricedAsOf"],
        "application": {
            "compute": "4 vCPU / 16 GiB Linux, 730 hours/month",
            "blockStorage": "200 GB",
        },
        "selectedRows": {
            key: {
                "rowId": row["rowId"],
                "sku": row["sku"],
                "meter": row["meter"],
                "unit": row["unit"],
                "price": row["price"],
                "candidateCount": candidate_counts[key],
            }
            for key, row in selected.items()
        },
        "commercialViews": {
            "List": _view(list_aws, list_azure),
            "Committed": _view(committed_aws, committed_azure),
            "Enterprise": {
                **_view(enterprise_aws, enterprise_azure),
                "commonDiscountPercent": str(enterprise_discount_percent),
                "twoSidedSensitivity": sensitivity,
                "basis": "List",
            },
        },
        "azureHigherLinePresent": (
            azure_storage > aws_storage
            or Decimal(selected["azure-vm-linux"]["price"])
            > Decimal(selected["aws-vm-linux"]["price"])
        ),
        "arithmetic": {
            "awsList": (
                f"{selected['aws-vm-linux']['price']}*730 + "
                f"{selected['aws-gp3-capacity']['price']}*200"
            ),
            "azureList": (
                f"{selected['azure-vm-linux']['price']}*730 + "
                f"{selected['azure-premium-ssd-v2-capacity']['price']}*200*730"
            ),
            "awsCommitted": (
                f"{selected['aws-vm-linux-committed']['price']}*730 + "
                f"{selected['aws-gp3-capacity']['price']}*200"
            ),
            "azureCommitted": (
                f"{selected['azure-vm-linux-committed']['price']}*730 + "
                f"{selected['azure-premium-ssd-v2-capacity']['price']}*200*730"
            ),
            "awsEnterprise": (
                f"({selected['aws-vm-linux']['price']}*730 + "
                f"{selected['aws-gp3-capacity']['price']}*200)"
                f"*(1-{enterprise_discount_percent}/100)"
            ),
            "azureEnterprise": (
                f"({selected['azure-vm-linux']['price']}*730 + "
                f"{selected['azure-premium-ssd-v2-capacity']['price']}*200*730)"
                f"*(1-{enterprise_discount_percent}/100)"
            ),
        },
        "approvedCommittedDefault": {
            "horizon": "3 years",
            "payment": "All Upfront",
            "utilization": "100%",
            "tenancy": "Shared",
            "awsPlan": "Compute Savings Plan",
            "azurePlan": "Azure savings plan for compute",
            "rateTreatment": (
                "Feed-provided discounted hourly rates; no workload-specific commitment "
                "amount is invented or amortized."
            ),
        },
    }
    if not report["azureHigherLinePresent"]:
        raise ValidationError("Reconciliation lacks the required line where the Azure price is higher.")
    report["reportDigest"] = hashlib.sha256(
        canonical_json(report).encode("utf-8")
    ).hexdigest()
    return report


def _selectors() -> list[tuple[str, str, Callable[[dict[str, Any]], bool]]]:
    return [
        (
            "aws-vm-linux",
            "aws",
            lambda row: row["region"] == "us-east-1"
            and _dimension(row, "instanceType") == "m6i.xlarge"
            and _dimension(row, "operatingSystem") == "Linux"
            and _dimension(row, "operation") == "RunInstances"
            and _dimension(row, "tenancy") == "Shared"
            and _dimension(row, "preInstalledSw") == "NA"
            and _dimension(row, "capacitystatus") == "Used"
            and _dimension(row, "licenseModel") == "No License required"
            and row["term"] == "OnDemand"
            and row["unit"] == "Hrs",
        ),
        (
            "azure-vm-linux",
            "azure",
            lambda row: row["region"] == "eastus2"
            and row["serviceCode"] == "Virtual Machines"
            and _dimension(row, "armSkuName") == "Standard_D4s_v5"
            and _dimension(row, "meterName") == "D4s v5"
            and _dimension(row, "skuName") == "Standard_D4s_v5"
            and "Windows" not in _dimension(row, "productName")
            and row["term"] == "Consumption"
            and row["unit"] == "1 Hour",
        ),
        (
            "aws-vm-linux-committed",
            "aws",
            lambda row: row["region"] == "us-east-1"
            and row["serviceCode"] == "AWSComputeSavingsPlan"
            and row.get("productFamily") == "ComputeSavingsPlans"
            and _dimension(row, "purchaseOption") == "All Upfront"
            and _dimension(row, "purchaseTerm") == "3yr"
            and _dimension(row, "discountedInstanceType") == "m6i.xlarge"
            and _dimension(row, "discountedUsageType") == "BoxUsage:m6i.xlarge"
            and _dimension(row, "discountedOperation") == "RunInstances"
            and row["term"] == "SavingsPlan"
            and row["unit"] == "Hrs",
        ),
        (
            "azure-vm-linux-committed",
            "azure",
            lambda row: row["region"] == "eastus2"
            and row["serviceCode"] == "Virtual Machines"
            and _dimension(row, "armSkuName") == "Standard_D4s_v5"
            and _dimension(row, "meterName") == "D4s v5"
            and _dimension(row, "skuName") == "Standard_D4s_v5"
            and _dimension(row, "savingsPlanTerm") == "3 Years"
            and "Windows" not in _dimension(row, "productName")
            and row["term"] == "SavingsPlan"
            and row["unit"] == "1 Hour",
        ),
        (
            "aws-gp3-capacity",
            "aws",
            lambda row: row["region"] == "us-east-1"
            and row["serviceCode"] == "AmazonEC2"
            and row.get("productFamily") == "Storage"
            and _dimension(row, "volumeApiName") == "gp3"
            and row["term"] == "OnDemand"
            and row["unit"] == "GB-Mo",
        ),
        (
            "azure-premium-ssd-v2-capacity",
            "azure",
            lambda row: row["region"] == "eastus2"
            and row["serviceCode"] == "Storage"
            and _dimension(row, "productName") == "Azure Premium SSD v2"
            and _dimension(row, "meterName") == "Premium LRS Provisioned Capacity"
            and _dimension(row, "skuName") == "Premium LRS"
            and row["term"] == "Consumption"
            and row["unit"] == "1 GiB/Hour",
        ),
    ]


def _view(aws: Decimal, azure: Decimal) -> dict[str, str]:
    delta = azure - aws
    return {
        "awsMonthly": format(aws, ".6f"),
        "azureMonthly": format(azure, ".6f"),
        "azureMinusAws": format(delta, ".6f"),
        "lowerCostProvider": "aws" if delta > 0 else "azure" if delta < 0 else "tie",
    }


def _two_sided_sensitivity(
    *, aws: Decimal, azure: Decimal, common_discount: Decimal
) -> dict[str, str]:
    if aws == azure:
        return {
            "providerNeedingAdditionalDiscount": "none",
            "additionalDiscountPercentagePointsToFlip": "0",
        }
    if azure > aws:
        additional = (Decimal("1") - common_discount) * (
            Decimal("1") - (aws / azure)
        )
        provider = "azure"
    else:
        additional = (Decimal("1") - common_discount) * (
            Decimal("1") - (azure / aws)
        )
        provider = "aws"
    return {
        "providerNeedingAdditionalDiscount": provider,
        "additionalDiscountPercentagePointsToFlip": format(
            additional * Decimal("100"), ".6f"
        ),
    }


def _dimension(row: dict[str, Any], name: str) -> str:
    return str(row["dimensions"].get(name) or "")
