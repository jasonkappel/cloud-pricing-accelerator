"""Download the current Published price book so a new harvest validates against it.

The pointer and artifact are only fetched here. Trust comes later: ``validate`` hashes the pointer bytes,
verifies the artifact's signed approval, and checks that the pointer binds that artifact.
"""
from __future__ import annotations

import argparse
import json
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from azure.core.exceptions import ResourceNotFoundError
from azure.identity import ManagedIdentityCredential
from azure.storage.blob import BlobServiceClient

from .blob_stage import _checked_account_url
from .core import HarvestError, canonical_json, validate_snapshot_id

CONTROL_CONTAINER = "publication-control"
PUBLISHED_CONTAINER = "published-pricebooks"
POINTER_NAME = "current.json"
MAX_POINTER_BYTES = 8 * 1024
ARTIFACT_SUFFIX = ".pricebook.ndjson"


def artifact_from_pointer(data: bytes) -> str:
    try:
        pointer = json.loads(data)
    except ValueError as exc:
        raise HarvestError("The current price book pointer is not JSON.") from exc
    if not isinstance(pointer, dict):
        raise HarvestError("The current price book pointer is not an object.")
    artifact = pointer.get("artifact")
    snapshot_id = pointer.get("snapshotId")
    if not isinstance(artifact, str) or not isinstance(snapshot_id, str):
        raise HarvestError("The current price book pointer has no artifact.")
    validate_snapshot_id(snapshot_id)
    if artifact != f"{snapshot_id}{ARTIFACT_SUFFIX}":
        raise HarvestError("The current price book pointer names an unexpected artifact.")
    return artifact


def fetch_current(service: BlobServiceClient, output_dir: Path) -> dict[str, Any]:
    """Write current.json and its artifact into output_dir; report bootstrap when nothing is published."""
    output_dir.mkdir(parents=True, exist_ok=False)
    control = service.get_container_client(CONTROL_CONTAINER)
    try:
        downloader = control.download_blob(POINTER_NAME)
    except ResourceNotFoundError:
        return {"published": False}
    if downloader.size > MAX_POINTER_BYTES:
        raise HarvestError("The current price book pointer is too large.")
    data = downloader.readall()
    artifact = artifact_from_pointer(data)
    pointer_path = output_dir / POINTER_NAME
    pointer_path.write_bytes(data)
    artifact_path = output_dir / artifact
    try:
        published = service.get_container_client(PUBLISHED_CONTAINER).download_blob(
            artifact, max_concurrency=4
        )
        with artifact_path.open("xb") as handle:
            published.readinto(handle)
    except ResourceNotFoundError as exc:
        raise HarvestError("The current price book pointer names a missing artifact.") from exc
    return {
        "published": True,
        "pointer": str(pointer_path),
        "artifact": str(artifact_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m harvester.published_fetch")
    parser.add_argument("--account-url", required=True)
    parser.add_argument("--managed-identity-client-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        account_url = _checked_account_url(args.account_url)
        with ExitStack() as stack:
            credential = ManagedIdentityCredential(client_id=args.managed_identity_client_id)
            stack.callback(credential.close)
            service = stack.enter_context(BlobServiceClient(account_url, credential=credential))
            result = fetch_current(service, args.output_dir)
    except HarvestError as exc:
        raise SystemExit(f"harvester failed: {exc}") from exc
    print(canonical_json(result))


if __name__ == "__main__":
    main()
