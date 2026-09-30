from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import ijson

from .core import HarvestError, canonical_json, decimal_string, sha256_text
from .http import SafeHttpClient


AWS_ROOT = "https://pricing.us-east-1.amazonaws.com"
AWS_OFFERS = ("AmazonEC2", "AmazonRDS")
AWS_COMPUTE_SAVINGS_PLAN = "AWSComputeSavingsPlan"


def collect_aws_offer(
    client: SafeHttpClient,
    *,
    offer: str,
    region: str,
    work_dir: Path,
) -> Iterator[dict[str, Any]]:
    if offer not in AWS_OFFERS:
        raise HarvestError(f"Unsupported AWS offer: {offer}")
    index_url, publication_date = _resolve_offer_url(client, offer, region)
    raw_path = work_dir / "raw" / f"{offer}-{region}.json"
    client.stream_to_file(index_url, raw_path)
    database_path = work_dir / "spool" / f"{offer}-{region}.sqlite"
    _index_products(raw_path, database_path)
    try:
        yield from _iter_offer_terms(
            raw_path,
            database_path,
            offer=offer,
            region=region,
            source_url=index_url,
            publication_date=publication_date,
        )
    finally:
        raw_path.unlink(missing_ok=True)
        database_path.unlink(missing_ok=True)


def collect_compute_savings_plan(
    client: SafeHttpClient,
    *,
    region: str,
    work_dir: Path,
) -> Iterator[dict[str, Any]]:
    region_index_url = (
        f"{AWS_ROOT}/savingsPlan/v1.0/aws/{AWS_COMPUTE_SAVINGS_PLAN}"
        "/current/region_index.json"
    )
    region_index = client.get_json(region_index_url)
    publication_date = str(region_index.get("publicationDate") or "")
    regions = region_index.get("regions")
    if not isinstance(regions, list):
        raise HarvestError("AWS Savings Plans region index is missing regions.")
    version_url = next(
        (
            item.get("versionUrl")
            for item in regions
            if isinstance(item, dict) and item.get("regionCode") == region
        ),
        None,
    )
    if not isinstance(version_url, str):
        raise HarvestError(f"AWS Savings Plans has no catalog for {region}.")
    source_url = _absolute_aws_url(version_url)
    raw_path = work_dir / "raw" / f"{AWS_COMPUTE_SAVINGS_PLAN}-{region}.json"
    client.stream_to_file(source_url, raw_path)
    database_path = work_dir / "spool" / f"{AWS_COMPUTE_SAVINGS_PLAN}-{region}.sqlite"
    _index_savings_plan_products(raw_path, database_path)
    try:
        yield from _iter_savings_plan_rows(
            raw_path,
            database_path,
            region=region,
            source_url=source_url,
            publication_date=publication_date,
        )
    finally:
        raw_path.unlink(missing_ok=True)
        database_path.unlink(missing_ok=True)


def _resolve_offer_url(
    client: SafeHttpClient, offer: str, region: str
) -> tuple[str, str]:
    region_index_url = f"{AWS_ROOT}/offers/v1.0/aws/{offer}/current/region_index.json"
    region_index = client.get_json(region_index_url)
    regions = region_index.get("regions")
    if not isinstance(regions, dict):
        raise HarvestError(f"AWS {offer} region index is missing regions.")
    region_entry = regions.get(region)
    if not isinstance(region_entry, dict):
        raise HarvestError(f"AWS {offer} has no catalog for {region}.")
    version_url = region_entry.get("currentVersionUrl")
    if not isinstance(version_url, str):
        raise HarvestError(f"AWS {offer} region entry is missing currentVersionUrl.")
    return _absolute_aws_url(version_url), str(region_index.get("publicationDate") or "")


def _absolute_aws_url(path: str) -> str:
    if path.startswith("https://"):
        return path
    if not path.startswith("/"):
        raise HarvestError(f"AWS feed returned an invalid relative URL: {path}")
    return f"{AWS_ROOT}{path}"


def _index_products(raw_path: Path, database_path: Path) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute(
            "CREATE TABLE products (sku TEXT PRIMARY KEY, product_family TEXT NOT NULL, "
            "attributes_json TEXT NOT NULL)"
        )
        with raw_path.open("rb") as handle:
            for sku, product in ijson.kvitems(handle, "products"):
                if not isinstance(product, dict):
                    raise HarvestError("AWS product entry must be an object.")
                attributes = product.get("attributes") or {}
                if not isinstance(attributes, dict):
                    raise HarvestError("AWS product attributes must be an object.")
                connection.execute(
                    "INSERT INTO products VALUES (?, ?, ?)",
                    (
                        str(sku),
                        str(product.get("productFamily") or ""),
                        canonical_json(attributes),
                    ),
                )
        connection.commit()


def _iter_offer_terms(
    raw_path: Path,
    database_path: Path,
    *,
    offer: str,
    region: str,
    source_url: str,
    publication_date: str,
) -> Iterator[dict[str, Any]]:
    with closing(sqlite3.connect(database_path)) as connection:
        for term_type in ("OnDemand", "Reserved"):
            with raw_path.open("rb") as handle:
                for sku, term_entries in ijson.kvitems(handle, f"terms.{term_type}"):
                    product = connection.execute(
                        "SELECT product_family, attributes_json FROM products WHERE sku = ?",
                        (str(sku),),
                    ).fetchone()
                    if product is None:
                        raise HarvestError(f"AWS term references unknown SKU {sku}.")
                    product_family, attributes_json = product
                    attributes = json.loads(attributes_json)
                    if not isinstance(term_entries, dict):
                        raise HarvestError("AWS term entry must be an object.")
                    for offer_term_code, term in term_entries.items():
                        if not isinstance(term, dict):
                            raise HarvestError("AWS offer term must be an object.")
                        term_attributes = term.get("termAttributes") or {}
                        price_dimensions = term.get("priceDimensions") or {}
                        if not isinstance(price_dimensions, dict):
                            raise HarvestError("AWS priceDimensions must be an object.")
                        for dimension_code, dimension in price_dimensions.items():
                            if not isinstance(dimension, dict):
                                raise HarvestError("AWS price dimension must be an object.")
                            usd = (dimension.get("pricePerUnit") or {}).get("USD")
                            if usd is None:
                                continue
                            dimensions = {
                                **attributes,
                                "offerTermCode": str(offer_term_code),
                                "dimensionCode": str(dimension_code),
                                "description": str(dimension.get("description") or ""),
                                "beginRange": str(dimension.get("beginRange") or ""),
                                "endRange": str(dimension.get("endRange") or ""),
                                **{
                                    str(key): str(value)
                                    for key, value in term_attributes.items()
                                },
                            }
                            identity = {
                                "provider": "aws",
                                "serviceCode": offer,
                                "region": region,
                                "sku": str(sku),
                                "meter": str(dimension_code),
                                "productFamily": str(product_family),
                                "term": term_type,
                                "effectiveStart": str(term.get("effectiveDate") or ""),
                                "unit": str(dimension.get("unit") or ""),
                                "dimensions": dimensions,
                            }
                            yield {
                                **identity,
                                "rowId": sha256_text(canonical_json(identity)),
                                "currency": "USD",
                                "price": decimal_string(usd),
                                "sourceUrl": source_url,
                                "sourcePublicationDate": publication_date,
                            }


def _iter_savings_plan_rows(
    raw_path: Path,
    database_path: Path,
    *,
    region: str,
    source_url: str,
    publication_date: str,
) -> Iterator[dict[str, Any]]:
    with closing(sqlite3.connect(database_path)) as connection:
        term: dict[str, Any] = {}
        rate: dict[str, Any] | None = None
        with raw_path.open("rb") as handle:
            for prefix, event, value in ijson.parse(handle):
                if prefix == "terms.savingsPlan.item" and event == "start_map":
                    term = {}
                elif prefix == "terms.savingsPlan.item.sku" and event == "string":
                    term["sku"] = value
                elif prefix == "terms.savingsPlan.item.description" and event == "string":
                    term["description"] = value
                elif prefix == "terms.savingsPlan.item.effectiveDate" and event == "string":
                    term["effectiveDate"] = value
                elif (
                    prefix == "terms.savingsPlan.item.leaseContractLength.duration"
                    and event == "number"
                ):
                    term["duration"] = str(value)
                elif (
                    prefix == "terms.savingsPlan.item.leaseContractLength.unit"
                    and event == "string"
                ):
                    term["durationUnit"] = value
                elif prefix == "terms.savingsPlan.item.rates.item" and event == "start_map":
                    rate = {}
                elif rate is not None and prefix.startswith(
                    "terms.savingsPlan.item.rates.item."
                ) and event in {"string", "number"}:
                    rate[prefix.rsplit(".", 1)[-1]] = value
                elif prefix == "terms.savingsPlan.item.rates.item" and event == "end_map":
                    if rate is None:
                        raise HarvestError("AWS Savings Plan rate state is invalid.")
                    price = rate.get("price")
                    if price is not None:
                        product = connection.execute(
                            "SELECT product_family, attributes_json FROM products WHERE sku = ?",
                            (str(term.get("sku") or ""),),
                        ).fetchone()
                        if product is None:
                            raise HarvestError(
                                f"AWS Savings Plan term references unknown SKU {term.get('sku')}."
                            )
                        product_family, attributes_json = product
                        product_attributes = json.loads(attributes_json)
                        dimensions = {
                            **product_attributes,
                            "description": str(term.get("description") or ""),
                            "leaseDuration": str(term.get("duration") or ""),
                            "leaseDurationUnit": str(term.get("durationUnit") or ""),
                            "discountedUsageType": str(
                                rate.get("discountedUsageType") or ""
                            ),
                            "discountedOperation": str(
                                rate.get("discountedOperation") or ""
                            ),
                            "discountedInstanceType": str(
                                rate.get("discountedInstanceType") or ""
                            ),
                            "discountedServiceCode": str(
                                rate.get("discountedServiceCode") or ""
                            ),
                        }
                        identity = {
                            "provider": "aws",
                            "serviceCode": AWS_COMPUTE_SAVINGS_PLAN,
                            "region": region,
                            "sku": str(term.get("sku") or ""),
                            "meter": str(rate.get("rateCode") or ""),
                            "productFamily": str(product_family),
                            "term": "SavingsPlan",
                            "effectiveStart": str(
                                term.get("effectiveDate") or publication_date
                            ),
                            "unit": str(rate.get("unit") or "Hrs"),
                            "dimensions": dimensions,
                        }
                        yield {
                            **identity,
                            "rowId": sha256_text(canonical_json(identity)),
                            "currency": str(rate.get("currency") or "USD"),
                            "price": decimal_string(price),
                            "sourceUrl": source_url,
                            "sourcePublicationDate": publication_date,
                        }
                    rate = None


def _index_savings_plan_products(raw_path: Path, database_path: Path) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute(
            "CREATE TABLE products (sku TEXT PRIMARY KEY, product_family TEXT NOT NULL, "
            "attributes_json TEXT NOT NULL)"
        )
        with raw_path.open("rb") as handle:
            for product in ijson.items(handle, "products.item"):
                if not isinstance(product, dict):
                    raise HarvestError("AWS Savings Plan product must be an object.")
                attributes = product.get("attributes") or {}
                if not isinstance(attributes, dict):
                    raise HarvestError("AWS Savings Plan attributes must be an object.")
                connection.execute(
                    "INSERT INTO products VALUES (?, ?, ?)",
                    (
                        str(product.get("sku") or ""),
                        str(product.get("productFamily") or ""),
                        canonical_json(attributes),
                    ),
                )
        connection.commit()
