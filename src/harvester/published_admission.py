from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Any, Callable, Literal

from azure.core import MatchConditions
from azure.core.exceptions import AzureError
from azure.storage.blob import BlobServiceClient

from .core import PublicationError, validate_snapshot_id
from .snapshot import verify_published_artifact


MAX_POINTER_BYTES = 8192
MAX_ARTIFACT_BYTES = 4 * 1024 * 1024 * 1024
DOWNLOAD_RANGE_BYTES = 4 * 1024 * 1024


def admit_published_blob(
    service_client: BlobServiceClient,
    *,
    scratch_dir: Path,
    verify_approval: Callable[[dict[str, Any]], bool] | None,
    verify_immutability: Callable[[str], bool] | None,
    evidence_policy: Literal["worm", "mutable-pilot"] = "worm",
    verify_pilot_storage: Callable[[str], bool] | None = None,
    verify_freshness: Callable[[dict[str, Any]], bool] | None = None,
    max_artifact_bytes: int = MAX_ARTIFACT_BYTES,
) -> dict[str, Any]:
    """Verify a Published artifact without granting this path any write capability."""
    if verify_approval is None:
        raise PublicationError("Published Blob admission requires a trusted approval verifier.")
    if evidence_policy == "worm":
        if verify_immutability is None or verify_immutability("published-pricebooks") is not True:
            raise PublicationError("Published container has no verified WORM policy.")
    elif evidence_policy == "mutable-pilot":
        if verify_pilot_storage is None or verify_pilot_storage("published-pricebooks") is not True:
            raise PublicationError("Mutable pilot storage must have verified versioning and soft delete.")
        if verify_freshness is None:
            raise PublicationError("Mutable pilot admission requires an independent freshness verifier.")
    else:
        raise PublicationError("Unknown Published evidence policy.")
    if max_artifact_bytes < 1 or max_artifact_bytes > MAX_ARTIFACT_BYTES:
        raise PublicationError("Invalid Published artifact size limit.")
    if not scratch_dir.is_dir():
        raise PublicationError("Published Blob admission needs an existing scratch directory.")

    control = service_client.get_container_client("publication-control")
    published = service_client.get_container_client("published-pricebooks")
    pointer_blob = control.get_blob_client("current.json")
    try:
        pointer_properties = pointer_blob.get_blob_properties()
        if not 0 < pointer_properties.size <= MAX_POINTER_BYTES:
            raise PublicationError("Published pointer exceeds the size limit.")
        pointer_etag = pointer_properties.etag
        pointer_bytes = pointer_blob.download_blob(
            offset=0,
            length=pointer_properties.size,
            etag=pointer_etag,
            match_condition=MatchConditions.IfNotModified,
            max_concurrency=1,
            decompress=False,
        ).readall()
        if len(pointer_bytes) != pointer_properties.size:
            raise PublicationError("Published pointer was truncated.")
        pointer = json.loads(pointer_bytes)
        if not isinstance(pointer, dict):
            raise PublicationError("Published pointer is not an object.")
        snapshot_id = validate_snapshot_id(str(pointer.get("snapshotId") or ""))
        artifact_name = f"{snapshot_id}.pricebook.ndjson"
        if pointer.get("artifact") != artifact_name:
            raise PublicationError("Published pointer artifact name is invalid.")
        content_hash = pointer.get("contentHash")
        if not isinstance(content_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", content_hash):
            raise PublicationError("Published pointer content hash is invalid.")
        blob = published.get_blob_client(artifact_name)
        artifact_properties = blob.get_blob_properties()
        if not 0 < artifact_properties.size <= max_artifact_bytes:
            raise PublicationError("Published artifact exceeds the size limit.")
        artifact_etag = artifact_properties.etag
        with tempfile.TemporaryDirectory(dir=scratch_dir) as temp_dir:
            artifact_path = Path(temp_dir) / artifact_name
            with artifact_path.open("xb") as staged:
                for offset in range(0, artifact_properties.size, DOWNLOAD_RANGE_BYTES):
                    length = min(DOWNLOAD_RANGE_BYTES, artifact_properties.size - offset)
                    chunk = blob.download_blob(
                        offset=offset,
                        length=length,
                        etag=artifact_etag,
                        match_condition=MatchConditions.IfNotModified,
                        max_concurrency=1,
                        decompress=False,
                    ).readall()
                    if len(chunk) != length:
                        raise PublicationError("Published artifact download was truncated.")
                    staged.write(chunk)
            manifest = verify_published_artifact(
                artifact_path,
                approval_verifier=verify_approval,
            )
        if evidence_policy == "mutable-pilot" and (
            manifest.get("evidencePolicy") != "MutablePilot"
            or manifest.get("nonProduction") is not True
        ):
            raise PublicationError("Mutable pilot Published artifact lacks its non-production label.")
        if evidence_policy == "worm" and (
            manifest.get("evidencePolicy") not in (None, "WORM")
            or manifest.get("nonProduction", False) is not False
        ):
            raise PublicationError("Non-production artifact cannot be admitted as WORM evidence.")
        if (
            manifest.get("snapshotId") != snapshot_id
            or manifest.get("contentHash") != content_hash
        ):
            raise PublicationError("Published pointer does not bind the verified artifact.")
        if evidence_policy == "mutable-pilot" and verify_freshness({
            "snapshotId": snapshot_id,
            "contentHash": content_hash,
            "capturedAt": manifest.get("capturedAt"),
            "pointerEtag": pointer_etag,
        }) is not True:
            raise PublicationError("Mutable pilot snapshot failed the independent freshness check.")
        if pointer_blob.get_blob_properties().etag != pointer_etag:
            raise PublicationError("Published pointer changed during admission.")
        return {
            "snapshotId": snapshot_id,
            "contentHash": content_hash,
            "rowCount": manifest["rowCount"],
            "artifactEtag": artifact_etag,
            "pointerEtag": pointer_etag,
            "evidencePolicy": "WORM" if evidence_policy == "worm" else "MutablePilot",
        }
    except (ValueError, UnicodeDecodeError) as exc:
        raise PublicationError("Published pointer is malformed.") from exc
    except AzureError as exc:
        raise PublicationError(f"Published Blob admission failed: {exc}") from exc
