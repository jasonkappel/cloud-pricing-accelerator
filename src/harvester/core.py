from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Iterator


SCHEMA_VERSION = "public-pricebook-v1"
SNAPSHOT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class HarvestError(RuntimeError):
    pass


class ValidationError(HarvestError):
    pass


class PublicationError(HarvestError):
    pass


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def decimal_string(value: Any) -> str:
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise HarvestError(f"Invalid decimal price value: {value!r}") from exc
    if not decimal_value.is_finite() or decimal_value < 0:
        raise HarvestError(f"Price must be a finite non-negative decimal: {value!r}")
    text = format(decimal_value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def validate_snapshot_id(value: str) -> str:
    if not SNAPSHOT_ID_PATTERN.fullmatch(value):
        raise ValidationError(
            "Snapshot ID must be 1-64 characters using letters, numbers, dot, underscore, or hyphen."
        )
    return value


def row_identity(row: dict[str, Any]) -> dict[str, Any]:
    identity = {
        key: row[key]
        for key in (
            "provider",
            "serviceCode",
            "region",
            "sku",
            "meter",
            "term",
            "effectiveStart",
            "unit",
            "dimensions",
        )
    }
    if "productFamily" in row:
        identity["productFamily"] = row["productFamily"]
    return identity


def write_ndjson(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(canonical_json(row))
            handle.write("\n")
            count += 1
    return count


def read_ndjson(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValidationError(
                    f"Invalid NDJSON in {path} at line {line_number}."
                ) from exc
