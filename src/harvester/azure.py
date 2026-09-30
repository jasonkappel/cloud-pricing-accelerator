from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from .core import HarvestError, canonical_json, decimal_string, sha256_text
from .http import SafeHttpClient


AZURE_RETAIL_URL = "https://prices.azure.com/api/retail/prices"
AZURE_API_VERSION = "2023-01-01-preview"
TARGET_SERVICES = (
    "Virtual Machines",
    "Storage",
    "Azure Database for PostgreSQL",
    "Bandwidth",
    "Load Balancer",
)


def collect_azure_rows(
    client: SafeHttpClient,
    *,
    region: str,
    services: tuple[str, ...] = TARGET_SERVICES,
) -> Iterator[dict[str, Any]]:
    for service in services:
        filter_value = (
            f"serviceName eq '{service}' and "
            f"(armRegionName eq '{region}' or armRegionName eq '' "
            "or armRegionName eq 'Global')"
        )
        query = urlencode(
            {"api-version": AZURE_API_VERSION, "$filter": filter_value},
            safe="'()",
        )
        url: str | None = f"{AZURE_RETAIL_URL}?{query}"
        seen_pages: set[str] = set()
        while url:
            if url in seen_pages:
                raise HarvestError(f"Azure pagination loop detected for {service}.")
            seen_pages.add(url)
            page = client.get_json(url)
            items = page.get("Items")
            if not isinstance(items, list):
                raise HarvestError("Azure Retail Prices response is missing Items.")
            for item in items:
                if not isinstance(item, dict):
                    raise HarvestError("Azure Retail Prices item must be an object.")
                yield _normalize_item(item, region=region, service=service)
                savings_plans = item.get("savingsPlan") or []
                if not isinstance(savings_plans, list):
                    raise HarvestError("Azure savingsPlan must be an array.")
                for savings_plan in savings_plans:
                    if not isinstance(savings_plan, dict):
                        raise HarvestError("Azure savingsPlan entry must be an object.")
                    yield _normalize_item(
                        item,
                        region=region,
                        service=service,
                        savings_plan=savings_plan,
                    )
            next_page = page.get("NextPageLink")
            if next_page is not None and not isinstance(next_page, str):
                raise HarvestError("Azure NextPageLink must be a string or null.")
            url = next_page or None


def _normalize_item(
    item: dict[str, Any],
    *,
    region: str,
    service: str,
    savings_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    price_source = savings_plan or item
    price = price_source.get("retailPrice")
    if price is None:
        raise HarvestError("Azure price row is missing retailPrice.")
    term = "SavingsPlan" if savings_plan else str(item.get("type") or "Consumption")
    dimensions = {}
    for key, value in item.items():
        if key in {"retailPrice", "unitPrice", "savingsPlan"}:
            continue
        if isinstance(value, (float, Decimal)):
            dimensions[key] = decimal_string(value)
        elif isinstance(value, (str, int, bool)):
            dimensions[key] = value
    if savings_plan:
        dimensions["savingsPlanTerm"] = str(
            savings_plan.get("term") or savings_plan.get("termDuration") or ""
        )
    identity = {
        "provider": "azure",
        "serviceCode": service,
        "region": str(item.get("armRegionName") or ""),
        "sku": str(item.get("skuId") or item.get("meterId") or ""),
        "meter": str(item.get("meterId") or ""),
        "term": term,
        "effectiveStart": str(item.get("effectiveStartDate") or ""),
        "unit": str(item.get("unitOfMeasure") or ""),
        "dimensions": dimensions,
    }
    return {
        **identity,
        "rowId": sha256_text(canonical_json(identity)),
        "currency": str(item.get("currencyCode") or "USD"),
        "price": decimal_string(price),
        "sourceUrl": AZURE_RETAIL_URL,
        "sourcePublicationDate": str(item.get("effectiveStartDate") or ""),
    }
