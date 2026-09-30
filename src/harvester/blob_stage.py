from __future__ import annotations

import hashlib
import json
import re
from contextlib import ExitStack
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urlsplit
from uuid import uuid4

from azure.core import MatchConditions
from azure.core.exceptions import AzureError, ResourceExistsError
from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
from azure.storage.blob import BlobServiceClient

from .core import PublicationError, ValidationError, canonical_json, validate_snapshot_id
from .derive import EXTRACT_NAME, REPORT_NAME, SPEC_PATH, derive_validated_run, extract_digest
from .snapshot import _read_json


_ACCOUNT_HOST = re.compile(
    r"^[a-z0-9]{3,24}\.blob\.core\."
    r"(?:windows\.net|usgovcloudapi\.net|chinacloudapi\.cn)$"
)
_CONTAINER = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])?$")
_CHUNK_SIZE = 1024 * 1024


def _checked_account_url(value: str) -> str:
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or not _ACCOUNT_HOST.fullmatch(parts.netloc)
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
    ):
        raise PublicationError("Blob account URL must be an Azure HTTPS account endpoint.")
    return value.rstrip("/")


def _digest_stream(handle: BinaryIO) -> tuple[str, int, int, bool]:
    digest = hashlib.sha256()
    length = 0
    rows = 0
    last_byte = b""
    for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
        digest.update(chunk)
        length += len(chunk)
        rows += chunk.count(b"\n")
        last_byte = chunk[-1:]
    return digest.hexdigest(), length, rows, last_byte == b"\n"


def stage_validated_run(
    run_dir: Path,
    *,
    account_url: str,
    container: str = "staged-runs",
    credential_mode: str = "managed-identity",
    managed_identity_client_id: str | None = None,
    service_client: BlobServiceClient | None = None,
    spec_path: Path = SPEC_PATH,
    previous_extract: Path | None = None,
) -> dict:
    account_url = _checked_account_url(account_url)
    if not _CONTAINER.fullmatch(container) or "--" in container:
        raise PublicationError("Invalid Azure Blob container name.")
    if container in {"publication-control", "published-pricebooks"}:
        raise PublicationError("Blob staging cannot target publication containers.")
    if credential_mode not in ("local", "managed-identity"):
        raise PublicationError("Unknown Blob credential mode.")
    if credential_mode == "local" and managed_identity_client_id:
        raise PublicationError("Managed identity client ID requires managed-identity mode.")

    manifest = _read_json(run_dir / "stage-manifest.json")
    validation = _read_json(run_dir / "validation.json")
    snapshot_id = validate_snapshot_id(str(manifest.get("snapshotId") or ""))
    if (
        manifest.get("validationStatus") != "Validated"
        or validation.get("validationStatus") != "Validated"
        or validation.get("failures") != []
    ):
        raise PublicationError("Only a successful Validated run can be staged to Blob.")
    bindings = {
        "snapshotId": snapshot_id,
        "contentHash": manifest.get("contentHash"),
        "rowCount": manifest.get("rowCount"),
        "coverageMatrixDigest": manifest.get("coverageMatrixDigest"),
        "scope": manifest.get("scope"),
        "stageManifestDigest": hashlib.sha256(
            canonical_json(manifest).encode("utf-8")
        ).hexdigest(),
    }
    if any(validation.get(field) != value for field, value in bindings.items()):
        raise PublicationError("Validation does not bind the staged manifest.")
    if not isinstance(bindings["rowCount"], int) or bindings["rowCount"] < 1:
        raise PublicationError("Validated run must contain canonical rows.")
    if not isinstance(bindings["contentHash"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", bindings["contentHash"]
    ):
        raise PublicationError("Validated run has an invalid content hash.")

    extract = _read_json(run_dir / EXTRACT_NAME)
    extract_report = _read_json(run_dir / REPORT_NAME)
    source = extract.get("manifest") if isinstance(extract.get("manifest"), dict) else {}
    extract_bindings = {
        "extractDigest": extract_digest(extract),
        "sourceSnapshotId": snapshot_id,
        "sourceContentHash": bindings["contentHash"],
        "stageManifestDigest": bindings["stageManifestDigest"],
        "extractSnapshotId": source.get("snapshotId"),
    }
    if (
        any(extract_report.get(field) != value for field, value in extract_bindings.items())
        or source.get("sourceSnapshotId") != snapshot_id
        or source.get("sourceContentHash") != bindings["contentHash"]
        or source.get("validationStatus") != "Validated"
        or source.get("publishingHuman") is not None
    ):
        raise PublicationError("Rate extract does not bind the Validated run.")
    # Recompute from the validated rows and the installed spec, so an extract and report rewritten
    # together (with matching digests) are still refused.
    try:
        expected_extract, expected_report = derive_validated_run(
            run_dir, spec_path=spec_path, previous_extract=previous_extract
        )
    except ValidationError as exc:
        raise PublicationError(f"Rate extract re-derivation failed: {exc}") from exc
    if expected_extract != extract or expected_report != extract_report:
        raise PublicationError("Rate extract does not match a fresh derivation from the run.")

    paths = {
        "canonical-rows.ndjson": run_dir / "canonical-rows.ndjson",
        "stage-manifest.json": run_dir / "stage-manifest.json",
        "validation.json": run_dir / "validation.json",
        EXTRACT_NAME: run_dir / EXTRACT_NAME,
        REPORT_NAME: run_dir / REPORT_NAME,
    }
    try:
        with ExitStack() as stack:
            streams = {
                name: stack.enter_context(path.open("rb"))
                for name, path in paths.items()
            }
            artifacts = {}
            for name, handle in streams.items():
                digest, size, rows, terminated = _digest_stream(handle)
                if name == "canonical-rows.ndjson" and (
                    digest != bindings["contentHash"]
                    or rows != bindings["rowCount"]
                    or not terminated
                ):
                    raise PublicationError("Canonical rows changed after validation.")
                artifacts[name] = {"sha256": digest, "bytes": size}
                handle.seek(0)
            for name, expected in (
                ("stage-manifest.json", manifest),
                ("validation.json", validation),
                (EXTRACT_NAME, extract),
                (REPORT_NAME, extract_report),
            ):
                if json.load(streams[name]) != expected:
                    raise PublicationError(f"Run metadata changed before Blob staging: {name}.")
                streams[name].seek(0)

            prefix = f"staging/{snapshot_id}/{uuid4().hex}/"
            if service_client is None:
                credential = (
                    DefaultAzureCredential()
                    if credential_mode == "local"
                    else ManagedIdentityCredential(client_id=managed_identity_client_id)
                )
                stack.callback(credential.close)
                service_client = stack.enter_context(
                    BlobServiceClient(account_url, credential=credential)
                )
            destination = service_client.get_container_client(container)
            if destination.get_container_properties().get("public_access"):
                raise PublicationError("Blob staging requires a private container.")

            metadata = {
                "snapshotid": snapshot_id,
                "contenthash": bindings["contentHash"],
                "rowcount": str(bindings["rowCount"]),
                "validationstatus": "validated",
            }
            for name, handle in streams.items():
                blob = destination.get_blob_client(prefix + name)
                uploaded = blob.upload_blob(
                    handle,
                    length=artifacts[name]["bytes"],
                    overwrite=False,
                    if_none_match="*",
                    metadata={**metadata, "sha256": artifacts[name]["sha256"]},
                    max_concurrency=1,
                )
                handle.seek(0)
                if _digest_stream(handle)[:2] != (
                    artifacts[name]["sha256"],
                    artifacts[name]["bytes"],
                ):
                    raise PublicationError(f"Run artifact changed during upload: {name}.")
                remote_digest = hashlib.sha256()
                remote_size = 0
                for chunk in blob.download_blob(
                    etag=uploaded["etag"],
                    match_condition=MatchConditions.IfNotModified,
                    max_concurrency=1,
                ).chunks():
                    remote_digest.update(chunk)
                    remote_size += len(chunk)
                if (
                    remote_digest.hexdigest() != artifacts[name]["sha256"]
                    or remote_size != artifacts[name]["bytes"]
                ):
                    raise PublicationError(f"Blob artifact failed remote verification: {name}.")
                artifacts[name]["etag"] = uploaded["etag"]

            receipt = {
                "snapshotId": snapshot_id,
                "capturedAt": manifest.get("capturedAt"),
                "validatedAt": validation.get("validatedAt"),
                "contentHash": bindings["contentHash"],
                "rowCount": bindings["rowCount"],
                "baselineSnapshotId": validation.get("baselineSnapshotId"),
                "baselinePointerHash": validation.get("baselinePointerHash"),
                "scope": bindings["scope"],
                "extractSnapshotId": extract_bindings["extractSnapshotId"],
                "extractDigest": extract_bindings["extractDigest"],
                "artifacts": artifacts,
                "status": "StagedOnly",
            }
            payload = f"{canonical_json(receipt)}\n".encode("utf-8")
            destination.get_blob_client(prefix + "receipt.json").upload_blob(
                payload,
                overwrite=False,
                if_none_match="*",
                metadata=metadata,
            )
            return {
                "snapshotId": snapshot_id,
                "status": "StagedOnly",
                "container": container,
                "prefix": prefix,
                "receiptDigest": hashlib.sha256(payload).hexdigest(),
            }
    except ResourceExistsError as exc:
        raise PublicationError("Blob staging artifacts already exist; no overwrite permitted.") from exc
    except AzureError as exc:
        raise PublicationError(f"Blob staging failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise PublicationError("Run metadata changed during Blob staging.") from exc
    except OSError as exc:
        raise PublicationError(f"Cannot read run artifact for Blob staging: {exc}") from exc
