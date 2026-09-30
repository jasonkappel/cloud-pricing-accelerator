"""PriceBook snapshot review and approval (direct, authenticated user-to-API actions).

The in-tenant harvester stages a Validated run under ``staging/{snapshotId}/{runId}/`` in the private
staging container, finishing with ``receipt.json``. This module never trusts the receipt alone: it reads
the small run files by their receipt ETags, checks their SHA-256 digests and cross-bindings, and shows
the result. The full canonical rows are re-hashed at publication, not here.

Two sign-offs, in order (one person may hold both roles; a two-person maker-checker is deferred to v2):

1. A SkuMapReviewer attests the SkuMap digest for one staged run.
2. A SnapshotApprover approves it.

The API builds each record from the signed-in principal's verified claims, never from the request body,
and signs it. In Azure the signature comes from a pinned, versioned Key Vault key that the API can use but
never export. The HMAC signer exists for local development and tests only. Records are written once with
exclusive create into the publication-control container. Publishing an Approved run and moving the current pointer is
in ``app.publication``.

PRICEBOOK_APPROVAL_MODE:

- ``off`` (default): approval is not configured; the Price book page says so.
- ``azure``: managed identity for Blob and Key Vault. Needs PRICEBOOK_BLOB_ENDPOINT and
  APPROVAL_SIGNING_KEY_ID (a versioned Key Vault key URL).
- ``local``: developer machines only (AUTH_MODE=local, no Azure hosting indicators). Needs
  PRICEBOOK_LOCAL_ROOT and APPROVAL_HMAC_KEY (at least 32 characters).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from app.auth import AZURE_HOSTING_INDICATORS, Principal, Role

logger = logging.getLogger(__name__)

APPROVAL_MODE_ENV = "PRICEBOOK_APPROVAL_MODE"
EVIDENCE_POLICY = "MutablePilot"
RECORD_SCHEMA_VERSION = 1
MAX_LISTED_RUNS = 20
MAX_RECEIPT_BYTES = 64 * 1024
MAX_RUN_FILE_BYTES = 8 * 1024 * 1024
MAX_CONTROL_BYTES = 64 * 1024
# Put Block From URL copies at most this many bytes per block.
COPY_BLOCK_BYTES = 100 * 1024 * 1024
COPY_CONCURRENCY = 4

SNAPSHOT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
RUN_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
RECEIPT_NAME_PATTERN = re.compile(
    r"^staging/(?P<snapshot>[A-Za-z0-9][A-Za-z0-9._-]{0,63})/(?P<run>[0-9a-f]{32})/receipt\.json$"
)
PUBLICATION_RECORD_PATTERN = re.compile(r"^approvals/[A-Za-z0-9][A-Za-z0-9._-]{0,63}/publication\.json$")
CONTAINER_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])?$")
BLOB_HOST_PATTERN = re.compile(
    r"^[a-z0-9]{3,24}\.blob\.core\.(?:windows\.net|usgovcloudapi\.net|chinacloudapi\.cn)$"
)
KEY_ID_PATTERN = re.compile(
    r"^https://[a-z0-9-]{3,24}\.vault\.(?:azure\.net|usgovcloudapi\.net|azure\.cn)"
    r"/keys/[A-Za-z0-9-]{1,127}/[0-9a-f]{32}$"
)

SMALL_RUN_FILES = (
    "stage-manifest.json",
    "validation.json",
    "rate-extract.json",
    "rate-extract-report.json",
)
ROWS_FILE = "canonical-rows.ndjson"
RUN_FILES = frozenset((*SMALL_RUN_FILES, ROWS_FILE))


class ApprovalConfigurationError(RuntimeError):
    pass


class ApprovalNotConfigured(RuntimeError):
    pass


class StorageUnavailable(RuntimeError):
    pass


class StagedArtifactInvalid(ValueError):
    """A staged file is oversized or changed after staging; the run is Blocked, not an outage."""


class RecordExists(RuntimeError):
    pass


class DecisionRefused(RuntimeError):
    """A review or approval that the current state does not allow (HTTP 409)."""


class StagedRunNotFound(RuntimeError):
    pass


class PointerConflict(RuntimeError):
    """The current-pointer compare-and-swap lost: someone else moved it."""


class ApprovalMode(StrEnum):
    OFF = "off"
    AZURE = "azure"
    LOCAL = "local"


class RunState(StrEnum):
    AWAITING_REVIEW = "AwaitingSkuMapReview"
    AWAITING_APPROVAL = "AwaitingApproval"
    APPROVED = "Approved"
    PUBLISHED = "Published"
    BLOCKED = "Blocked"


@dataclass(frozen=True)
class ApprovalSettings:
    mode: ApprovalMode
    blob_endpoint: str = ""
    staging_container: str = "staged-runs"
    control_container: str = "publication-control"
    published_container: str = "published-pricebooks"
    key_id: str = ""
    local_root: Path | None = None
    hmac_key: str = field(default="", repr=False)


def canonical_json(value: Any) -> str:
    """Byte-identical to the harvester's canonical JSON, so both sides hash and sign the same text."""
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def digest_of(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def approval_settings() -> ApprovalSettings:
    raw = (os.getenv(APPROVAL_MODE_ENV) or "off").strip()
    try:
        mode = ApprovalMode(raw)
    except ValueError as error:
        raise ApprovalConfigurationError(
            f"{APPROVAL_MODE_ENV} '{raw}' is not supported; use 'off', 'azure', or 'local'."
        ) from error
    staging = (os.getenv("PRICEBOOK_STAGING_CONTAINER") or "staged-runs").strip()
    control = (os.getenv("PRICEBOOK_CONTROL_CONTAINER") or "publication-control").strip()
    published = (os.getenv("PRICEBOOK_PUBLISHED_CONTAINER") or "published-pricebooks").strip()
    for name, value in (("staging", staging), ("control", control), ("published", published)):
        if not CONTAINER_PATTERN.fullmatch(value):
            raise ApprovalConfigurationError(f"The price book {name} container name is invalid.")
    if len({staging, control, published}) != 3:
        raise ApprovalConfigurationError(
            "The staging, control, and published containers must differ."
        )
    if mode == ApprovalMode.OFF:
        return ApprovalSettings(mode=mode)
    if mode == ApprovalMode.AZURE:
        endpoint = (os.getenv("PRICEBOOK_BLOB_ENDPOINT") or "").strip()
        parts = urlsplit(endpoint)
        if (
            parts.scheme != "https"
            or not BLOB_HOST_PATTERN.fullmatch(parts.hostname or "")
            or parts.port is not None
            or parts.path not in ("", "/")
            or parts.query
            or parts.fragment
            or parts.username
        ):
            raise ApprovalConfigurationError(
                "PRICEBOOK_BLOB_ENDPOINT must be an https Azure Blob account endpoint."
            )
        key_id = (os.getenv("APPROVAL_SIGNING_KEY_ID") or "").strip()
        if not KEY_ID_PATTERN.fullmatch(key_id):
            raise ApprovalConfigurationError(
                "APPROVAL_SIGNING_KEY_ID must be a versioned Key Vault key URL."
            )
        return ApprovalSettings(
            mode=mode,
            blob_endpoint=f"https://{parts.hostname}",
            staging_container=staging,
            control_container=control,
            published_container=published,
            key_id=key_id,
        )
    present = [name for name in AZURE_HOSTING_INDICATORS if os.getenv(name)]
    if (os.getenv("AUTH_MODE") or "").strip() != "local" or present:
        raise ApprovalConfigurationError(
            f"{APPROVAL_MODE_ENV}=local is only for a developer machine with AUTH_MODE=local."
        )
    root = (os.getenv("PRICEBOOK_LOCAL_ROOT") or "").strip()
    if not root or not Path(root).is_dir():
        raise ApprovalConfigurationError("PRICEBOOK_LOCAL_ROOT must name an existing directory.")
    key = os.getenv("APPROVAL_HMAC_KEY") or ""
    if len(key) < 32:
        raise ApprovalConfigurationError("APPROVAL_HMAC_KEY must contain at least 32 characters.")
    return ApprovalSettings(
        mode=mode,
        staging_container=staging,
        control_container=control,
        published_container=published,
        local_root=Path(root).resolve(),
        hmac_key=key,
    )


# --- Storage -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class StoredBlob:
    data: bytes
    etag: str


@dataclass(frozen=True)
class BlobProperties:
    size: int
    etag: str
    sha256: str | None


class PriceBookStore(Protocol):
    def list_receipts(self) -> list[tuple[str, datetime]]: ...

    def read_staged(self, name: str, *, etag: str | None, max_bytes: int) -> StoredBlob: ...

    def staged_properties(self, name: str) -> BlobProperties: ...

    def hash_staged(self, name: str, *, etag: str) -> str: ...

    def read_control(self, name: str, *, max_bytes: int) -> StoredBlob | None: ...

    def list_publication_records(self) -> list[str]:
        """Names of every ``approvals/<snapshotId>/publication.json`` in the control container.

        Publication records are write-once, so raises ``StagedArtifactInvalid`` when the store can show that one
        was deleted or overwritten."""
        ...

    def control_history(self, name: str, *, max_bytes: int, max_versions: int) -> list[bytes]:
        """Every retained version of a control blob, oldest first; empty where the store keeps no versions."""
        ...

    def create_control(self, name: str, data: bytes) -> None: ...

    def replace_control(self, name: str, data: bytes, *, etag: str | None) -> None:
        """Write only if the blob still has ``etag``, or does not exist when ``etag`` is None."""
        ...

    def published_properties(self, name: str) -> BlobProperties | None: ...

    def read_published_head(self, name: str, length: int, *, etag: str) -> bytes:
        """The artifact's first ``length`` bytes, read only while it still has ``etag``."""
        ...

    def hash_published(self, name: str, *, offset: int, etag: str) -> str:
        """SHA-256 of the artifact's bytes from ``offset``, read only while it still has ``etag``."""
        ...

    def assemble_published(
        self, name: str, header: bytes, *, rows_name: str, rows_etag: str, rows_size: int
    ) -> str:
        """Create ``name`` as header + the staged rows pinned to ``rows_etag``; never overwrites.

        Returns the new artifact's ETag."""
        ...


class LocalPriceBookStore:
    """Two directories standing in for the staging and control containers."""

    def __init__(
        self, root: Path, staging: str, control: str, published: str = "published-pricebooks"
    ) -> None:
        self._staging = (root / staging).resolve()
        self._control = (root / control).resolve()
        self._published = (root / published).resolve()
        self._lock = threading.Lock()

    @staticmethod
    def _etag(data: bytes) -> str:
        return f'"{hashlib.sha256(data).hexdigest()[:32]}"'

    @staticmethod
    def _inside(base: Path, name: str) -> Path:
        path = (base / name).resolve()
        if base not in path.parents:
            raise StorageUnavailable("Blob name escaped its container.")
        return path

    def list_receipts(self) -> list[tuple[str, datetime]]:
        if not self._staging.is_dir():
            return []
        found = []
        for path in self._staging.glob("staging/*/*/receipt.json"):
            name = path.relative_to(self._staging).as_posix()
            found.append((name, datetime.fromtimestamp(path.stat().st_mtime, UTC)))
        return found

    def read_staged(self, name: str, *, etag: str | None, max_bytes: int) -> StoredBlob:
        path = self._inside(self._staging, name)
        if not path.is_file():
            raise StagedRunNotFound(name)
        if path.stat().st_size > max_bytes:
            raise StagedArtifactInvalid(f"{name} exceeds the size bound.")
        data = path.read_bytes()
        current = self._etag(data)
        if etag is not None and etag != current:
            raise StagedArtifactInvalid(f"{name} changed after staging.")
        return StoredBlob(data=data, etag=current)

    def staged_properties(self, name: str) -> BlobProperties:
        path = self._inside(self._staging, name)
        if not path.is_file():
            raise StagedRunNotFound(name)
        data = path.read_bytes()
        return BlobProperties(
            size=len(data), etag=self._etag(data), sha256=hashlib.sha256(data).hexdigest()
        )

    def hash_staged(self, name: str, *, etag: str) -> str:
        path = self._inside(self._staging, name)
        if not path.is_file():
            raise StagedRunNotFound(name)
        data = path.read_bytes()
        if self._etag(data) != etag:
            raise StagedArtifactInvalid(f"{name} changed after staging.")
        return hashlib.sha256(data).hexdigest()

    def list_publication_records(self) -> list[str]:
        if not self._control.is_dir():
            return []
        return sorted(
            path.relative_to(self._control).as_posix()
            for path in self._control.glob("approvals/*/publication.json")
        )

    def control_history(self, name: str, *, max_bytes: int, max_versions: int) -> list[bytes]:
        return []  # A local folder keeps no versions.

    def read_control(self, name: str, *, max_bytes: int) -> StoredBlob | None:
        path = self._inside(self._control, name)
        if not path.is_file():
            return None
        if path.stat().st_size > max_bytes:
            raise StagedArtifactInvalid(f"{name} exceeds the size bound.")
        data = path.read_bytes()
        return StoredBlob(data=data, etag=self._etag(data))

    def create_control(self, name: str, data: bytes) -> None:
        path = self._inside(self._control, name)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with path.open("xb") as handle:
                    handle.write(data)
            except FileExistsError as error:
                raise RecordExists(name) from error

    def replace_control(self, name: str, data: bytes, *, etag: str | None) -> None:
        path = self._inside(self._control, name)
        with self._lock:
            current = path.read_bytes() if path.is_file() else None
            if (None if current is None else self._etag(current)) != etag:
                raise PointerConflict(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            temporary.write_bytes(data)
            os.replace(temporary, path)

    def published_properties(self, name: str) -> BlobProperties | None:
        path = self._inside(self._published, name)
        if not path.is_file():
            return None
        data = path.read_bytes()
        return BlobProperties(size=len(data), etag=self._etag(data), sha256=None)

    def read_published_head(self, name: str, length: int, *, etag: str) -> bytes:
        path = self._inside(self._published, name)
        data = path.read_bytes() if path.is_file() else b""
        if self._etag(data) != etag:
            raise StagedArtifactInvalid(f"{name} changed while it was checked.")
        return data[:length]

    def hash_published(self, name: str, *, offset: int, etag: str) -> str:
        path = self._inside(self._published, name)
        data = path.read_bytes() if path.is_file() else b""
        if self._etag(data) != etag:
            raise StagedArtifactInvalid(f"{name} changed while it was checked.")
        return hashlib.sha256(data[offset:]).hexdigest()

    def assemble_published(
        self, name: str, header: bytes, *, rows_name: str, rows_etag: str, rows_size: int
    ) -> str:
        target = self._inside(self._published, name)
        source = self._inside(self._staging, rows_name)
        with self._lock:
            self._published.mkdir(parents=True, exist_ok=True)
            temporary = self._published / f".{name}.{uuid.uuid4().hex}.tmp"
            try:
                rows = source.read_bytes() if source.is_file() else b""
                if self._etag(rows) != rows_etag or len(rows) != rows_size:
                    raise StagedArtifactInvalid(f"{rows_name} changed after approval.")
                with temporary.open("xb") as handle:
                    handle.write(header)
                    handle.write(rows)
                try:
                    os.link(temporary, target)
                except FileExistsError as error:
                    raise RecordExists(name) from error
                return self._etag(header + rows)
            finally:
                temporary.unlink(missing_ok=True)


class AzurePriceBookStore:
    """Blob access with the API's system-assigned managed identity. Never uses account keys."""

    def __init__(
        self, endpoint: str, staging: str, control: str, published: str, credential: Any
    ) -> None:
        from azure.storage.blob import BlobServiceClient

        self._credential = credential
        self._service = BlobServiceClient(endpoint, credential=credential)
        self._staging = self._service.get_container_client(staging)
        self._control = self._service.get_container_client(control)
        self._published = self._service.get_container_client(published)

    def list_receipts(self) -> list[tuple[str, datetime]]:
        from azure.core.exceptions import AzureError

        try:
            return [
                (blob.name, blob.last_modified)
                for blob in self._staging.list_blobs(name_starts_with="staging/")
                if RECEIPT_NAME_PATTERN.fullmatch(blob.name)
            ]
        except AzureError as error:
            raise StorageUnavailable("Price book staging storage is unavailable.") from error

    def read_staged(self, name: str, *, etag: str | None, max_bytes: int) -> StoredBlob:
        from azure.core import MatchConditions
        from azure.core.exceptions import AzureError, ResourceModifiedError, ResourceNotFoundError

        kwargs: dict[str, Any] = {"max_concurrency": 1}
        if etag is not None:
            kwargs.update(etag=etag, match_condition=MatchConditions.IfNotModified)
        try:
            downloader = self._staging.get_blob_client(name).download_blob(**kwargs)
            if downloader.size > max_bytes:
                raise StagedArtifactInvalid(f"{name} exceeds the size bound.")
            data = downloader.readall()
            return StoredBlob(data=data, etag=str(downloader.properties.etag))
        except ResourceNotFoundError as error:
            raise StagedRunNotFound(name) from error
        except ResourceModifiedError as error:
            raise StagedArtifactInvalid(f"{name} changed after staging.") from error
        except AzureError as error:
            raise StorageUnavailable("Price book staging storage is unavailable.") from error

    def staged_properties(self, name: str) -> BlobProperties:
        from azure.core.exceptions import AzureError, ResourceNotFoundError

        try:
            properties = self._staging.get_blob_client(name).get_blob_properties()
        except ResourceNotFoundError as error:
            raise StagedRunNotFound(name) from error
        except AzureError as error:
            raise StorageUnavailable("Price book staging storage is unavailable.") from error
        return BlobProperties(
            size=int(properties.size),
            etag=str(properties.etag),
            sha256=(properties.metadata or {}).get("sha256"),
        )

    def hash_staged(self, name: str, *, etag: str) -> str:
        from azure.core import MatchConditions
        from azure.core.exceptions import AzureError, ResourceModifiedError, ResourceNotFoundError

        digest = hashlib.sha256()
        try:
            downloader = self._staging.get_blob_client(name).download_blob(
                etag=etag, match_condition=MatchConditions.IfNotModified, max_concurrency=1
            )
            for chunk in downloader.chunks():
                digest.update(chunk)
        except ResourceNotFoundError as error:
            raise StagedRunNotFound(name) from error
        except ResourceModifiedError as error:
            raise StagedArtifactInvalid(f"{name} changed after staging.") from error
        except AzureError as error:
            raise StorageUnavailable("Price book staging storage is unavailable.") from error
        return digest.hexdigest()

    def list_publication_records(self) -> list[str]:
        from azure.core.exceptions import AzureError

        try:
            blobs = [
                blob
                for blob in self._control.list_blobs(name_starts_with="approvals/", include=["versions", "deleted"])
                if PUBLICATION_RECORD_PATTERN.fullmatch(blob.name)
            ]
        except AzureError as error:
            raise StorageUnavailable("Publication control storage is unavailable.") from error
        # Records are write-once and versioning is on, so a deleted or overwritten record leaves a version that
        # is not current behind. The service omits IsCurrentVersion on those, so anything but True counts.
        for blob in blobs:
            if getattr(blob, "deleted", False) or (
                getattr(blob, "version_id", None) and getattr(blob, "is_current_version", None) is not True
            ):
                raise StagedArtifactInvalid(f"the publication record {blob.name} was deleted or changed.")
        return sorted({blob.name for blob in blobs})

    def control_history(self, name: str, *, max_bytes: int, max_versions: int) -> list[bytes]:
        from azure.core.exceptions import AzureError, ResourceNotFoundError

        try:
            versions = sorted(
                str(blob.version_id)
                for blob in self._control.list_blobs(name_starts_with=name, include=["versions", "deleted"])
                if blob.name == name and getattr(blob, "version_id", None)
            )
            if len(versions) > max_versions:
                raise StagedArtifactInvalid(f"{name} has more than {max_versions} versions.")
            history = []
            for version in versions:  # Version IDs are UTC timestamps, so they sort oldest first.
                downloader = self._control.get_blob_client(name).download_blob(
                    version_id=version, max_concurrency=1
                )
                if downloader.size > max_bytes:
                    raise StagedArtifactInvalid(f"a version of {name} exceeds the size bound.")
                history.append(downloader.readall())
            return history
        except ResourceNotFoundError as error:
            raise StagedArtifactInvalid(f"a version of {name} was deleted.") from error
        except AzureError as error:
            raise StorageUnavailable("Publication control storage is unavailable.") from error

    def read_control(self, name: str, *, max_bytes: int) -> StoredBlob | None:
        from azure.core.exceptions import AzureError, ResourceNotFoundError

        try:
            downloader = self._control.get_blob_client(name).download_blob(max_concurrency=1)
            if downloader.size > max_bytes:
                raise StagedArtifactInvalid(f"{name} exceeds the size bound.")
            return StoredBlob(data=downloader.readall(), etag=str(downloader.properties.etag))
        except ResourceNotFoundError:
            return None
        except AzureError as error:
            raise StorageUnavailable("Publication control storage is unavailable.") from error

    def create_control(self, name: str, data: bytes) -> None:
        from azure.core.exceptions import AzureError, ResourceExistsError, ResourceModifiedError

        try:
            self._control.get_blob_client(name).upload_blob(
                data, overwrite=False, if_none_match="*", max_concurrency=1
            )
        # A failed If-None-Match precondition (412) surfaces as ResourceModifiedError.
        except (ResourceExistsError, ResourceModifiedError) as error:
            raise RecordExists(name) from error
        except AzureError as error:
            raise StorageUnavailable("Publication control storage is unavailable.") from error

    def replace_control(self, name: str, data: bytes, *, etag: str | None) -> None:
        from azure.core import MatchConditions
        from azure.core.exceptions import AzureError, ResourceExistsError, ResourceModifiedError

        condition: dict[str, Any] = (
            {"overwrite": False, "if_none_match": "*"}
            if etag is None
            else {"overwrite": True, "etag": etag, "match_condition": MatchConditions.IfNotModified}
        )
        try:
            self._control.get_blob_client(name).upload_blob(data, max_concurrency=1, **condition)
        except (ResourceExistsError, ResourceModifiedError) as error:
            raise PointerConflict(name) from error
        except AzureError as error:
            raise StorageUnavailable("Publication control storage is unavailable.") from error

    def published_properties(self, name: str) -> BlobProperties | None:
        from azure.core.exceptions import AzureError, ResourceNotFoundError

        try:
            properties = self._published.get_blob_client(name).get_blob_properties()
        except ResourceNotFoundError:
            return None
        except AzureError as error:
            raise StorageUnavailable("Published price book storage is unavailable.") from error
        return BlobProperties(size=int(properties.size), etag=str(properties.etag), sha256=None)

    def read_published_head(self, name: str, length: int, *, etag: str) -> bytes:
        from azure.core import MatchConditions
        from azure.core.exceptions import AzureError, ResourceModifiedError, ResourceNotFoundError

        try:
            return self._published.get_blob_client(name).download_blob(
                offset=0,
                length=length,
                etag=etag,
                match_condition=MatchConditions.IfNotModified,
                max_concurrency=1,
            ).readall()
        except (ResourceModifiedError, ResourceNotFoundError) as error:
            raise StagedArtifactInvalid(f"{name} changed while it was checked.") from error
        except AzureError as error:
            raise StorageUnavailable("Published price book storage is unavailable.") from error

    def hash_published(self, name: str, *, offset: int, etag: str) -> str:
        from azure.core import MatchConditions
        from azure.core.exceptions import AzureError, ResourceModifiedError, ResourceNotFoundError

        digest = hashlib.sha256()
        try:
            downloader = self._published.get_blob_client(name).download_blob(
                offset=offset,
                etag=etag,
                match_condition=MatchConditions.IfNotModified,
                max_concurrency=1,
            )
            for chunk in downloader.chunks():
                digest.update(chunk)
        except (ResourceModifiedError, ResourceNotFoundError) as error:
            raise StagedArtifactInvalid(f"{name} changed while it was checked.") from error
        except AzureError as error:
            raise StorageUnavailable("Published price book storage is unavailable.") from error
        return digest.hexdigest()

    def assemble_published(
        self, name: str, header: bytes, *, rows_name: str, rows_etag: str, rows_size: int
    ) -> str:
        # Server-side copy: the rows never pass through the API. Every block is read from the
        # staged rows only while they still carry the ETag whose bytes were hashed at approval.
        from azure.core.exceptions import (
            AzureError,
            ResourceExistsError,
            ResourceModifiedError,
            ResourceNotFoundError,
        )
        from azure.storage.blob import BlobBlock
        from azure.storage.blob._generated.models import SourceModifiedAccessConditions

        destination = self._published.get_blob_client(name)
        source_url = self._staging.get_blob_client(rows_name).url
        ranges = [
            (offset, min(COPY_BLOCK_BYTES, rows_size - offset))
            for offset in range(0, rows_size, COPY_BLOCK_BYTES)
        ]
        block_ids = [f"{index:06d}" for index in range(len(ranges) + 1)]
        try:
            token = self._credential.get_token("https://storage.azure.com/.default").token
            destination.stage_block(block_ids[0], header, length=len(header))

            def copy(index: int) -> None:
                offset, length = ranges[index]
                destination.stage_block_from_url(
                    block_ids[index + 1],
                    source_url,
                    source_offset=offset,
                    source_length=length,
                    source_authorization=f"Bearer {token}",
                    source_modified_access_conditions=SourceModifiedAccessConditions(
                        source_if_match=rows_etag
                    ),
                )

            with ThreadPoolExecutor(max_workers=COPY_CONCURRENCY) as pool:
                list(pool.map(copy, range(len(ranges))))
        except (ResourceModifiedError, ResourceNotFoundError) as error:
            raise StagedArtifactInvalid(f"{rows_name} changed after approval.") from error
        except AzureError as error:
            raise StorageUnavailable("Published price book storage is unavailable.") from error
        try:
            committed = destination.commit_block_list(
                [BlobBlock(block_id=block_id) for block_id in block_ids], if_none_match="*"
            )
            return str(committed["etag"])
        except (ResourceExistsError, ResourceModifiedError) as error:
            raise RecordExists(name) from error
        except AzureError as error:
            raise StorageUnavailable("Published price book storage is unavailable.") from error


# --- Signing -------------------------------------------------------------------------------------


class ApprovalSigner(Protocol):
    algorithm: str
    key_id: str

    def sign(self, payload: bytes) -> str: ...

    def verify(self, payload: bytes, signature: str) -> bool: ...


class HmacApprovalSigner:
    """Local development and tests only; the harvester verifies the same HMAC-SHA256 records."""

    algorithm = "HMAC-SHA256"
    key_id = "local-hmac"

    def __init__(self, key: str) -> None:
        if len(key) < 32:
            raise ApprovalConfigurationError("The approval HMAC key is too short.")
        self._key = key.encode("utf-8")

    def sign(self, payload: bytes) -> str:
        return hmac.new(self._key, payload, hashlib.sha256).hexdigest()

    def verify(self, payload: bytes, signature: str) -> bool:
        return hmac.compare_digest(self.sign(payload), signature)


class KeyVaultApprovalSigner:
    """RS256 over SHA-256 of the canonical record, with one pinned, versioned Key Vault key."""

    algorithm = "RS256"

    def __init__(self, key_id: str, crypto_client: Any) -> None:
        self.key_id = key_id
        self._client = crypto_client

    def sign(self, payload: bytes) -> str:
        from azure.core.exceptions import AzureError
        from azure.keyvault.keys.crypto import SignatureAlgorithm

        try:
            result = self._client.sign(SignatureAlgorithm.rs256, hashlib.sha256(payload).digest())
        except AzureError as error:
            raise StorageUnavailable("The approval signing key is unavailable.") from error
        if result.key_id != self.key_id:
            raise StorageUnavailable("Key Vault signed with an unexpected key version.")
        return bytes(result.signature).hex()

    def verify(self, payload: bytes, signature: str) -> bool:
        from azure.core.exceptions import AzureError
        from azure.keyvault.keys.crypto import SignatureAlgorithm

        try:
            raw = bytes.fromhex(signature)
        except ValueError:
            return False
        try:
            result = self._client.verify(
                SignatureAlgorithm.rs256, hashlib.sha256(payload).digest(), raw
            )
        except AzureError as error:
            raise StorageUnavailable("The approval signing key is unavailable.") from error
        return result.is_valid is True


def sign_record(record: dict[str, Any], signer: ApprovalSigner) -> dict[str, Any]:
    unsigned = {**record, "keyId": signer.key_id, "algorithm": signer.algorithm}
    unsigned.pop("signature", None)
    return {**unsigned, "signature": signer.sign(canonical_json(unsigned).encode("utf-8"))}


def record_signature_problem(record: dict[str, Any], signer: ApprovalSigner) -> str | None:
    signature = record.get("signature")
    if record.get("algorithm") != signer.algorithm or record.get("keyId") != signer.key_id:
        return "was not signed by this deployment's approval key"
    if not isinstance(signature, str) or not signature:
        return "has no signature"
    payload = {key: value for key, value in record.items() if key != "signature"}
    if not signer.verify(canonical_json(payload).encode("utf-8"), signature):
        return "has an invalid signature"
    return None


# --- Backend -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ApprovalBackend:
    store: PriceBookStore
    signer: ApprovalSigner


_backend: ApprovalBackend | None = None
_backend_lock = threading.Lock()


def approval_backend() -> ApprovalBackend:
    global _backend
    with _backend_lock:
        if _backend is not None:
            return _backend
        settings = approval_settings()
        if settings.mode == ApprovalMode.OFF:
            raise ApprovalNotConfigured("Snapshot approval is not configured on this deployment.")
        if settings.mode == ApprovalMode.LOCAL:
            assert settings.local_root is not None
            _backend = ApprovalBackend(
                store=LocalPriceBookStore(
                    settings.local_root,
                    settings.staging_container,
                    settings.control_container,
                    settings.published_container,
                ),
                signer=HmacApprovalSigner(settings.hmac_key),
            )
            return _backend
        from azure.identity import ManagedIdentityCredential
        from azure.keyvault.keys.crypto import CryptographyClient

        credential = ManagedIdentityCredential()
        _backend = ApprovalBackend(
            store=AzurePriceBookStore(
                settings.blob_endpoint,
                settings.staging_container,
                settings.control_container,
                settings.published_container,
                credential,
            ),
            signer=KeyVaultApprovalSigner(
                settings.key_id, CryptographyClient(settings.key_id, credential)
            ),
        )
        return _backend


def reset_approval_backend() -> None:
    global _backend
    with _backend_lock:
        _backend = None


# --- Staged runs ---------------------------------------------------------------------------------


def _strict_json(data: bytes, name: str) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"{name} contains the non-JSON constant {value}.")

    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{name} repeats the key {key!r}.")
            result[key] = value
        return result

    try:
        return json.loads(
            data.decode("utf-8"),
            parse_constant=reject_constant,
            object_pairs_hook=unique_pairs,
        )
    except (UnicodeDecodeError, ValueError) as error:
        raise ValueError(f"{name} is not valid JSON: {error}") from error


def review_blob_name(snapshot_id: str, run_id: str) -> str:
    return f"approvals/{snapshot_id}/runs/{run_id}/skumap-review.json"


def approval_blob_name(snapshot_id: str) -> str:
    # One approval per snapshot ID, whichever staged run it names.
    return f"approvals/{snapshot_id}/approval.json"


@dataclass
class StagedRun:
    snapshot_id: str
    run_id: str
    staged_at: datetime | None = None
    receipt: dict[str, Any] = field(default_factory=dict)
    manifest: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)
    extract: dict[str, Any] = field(default_factory=dict)
    report: dict[str, Any] = field(default_factory=dict)
    stage_manifest_digest: str | None = None
    extract_digest: str | None = None
    evidence_digest: str | None = None
    review: dict[str, Any] | None = None
    approval: dict[str, Any] | None = None
    problems: list[str] = field(default_factory=list)
    # Set by app.publication; None means publication status was not loaded.
    publication: dict[str, Any] | None = None

    @property
    def state(self) -> RunState:
        if self.problems:
            return RunState.BLOCKED
        if self.publication is not None and self.publication.get("published") is True:
            return RunState.PUBLISHED
        if self.approval is not None:
            return RunState.APPROVED
        if self.review is not None:
            return RunState.AWAITING_APPROVAL
        return RunState.AWAITING_REVIEW


def check_ids(snapshot_id: str, run_id: str) -> None:
    if not SNAPSHOT_ID_PATTERN.fullmatch(snapshot_id) or not RUN_ID_PATTERN.fullmatch(run_id):
        raise StagedRunNotFound(f"{snapshot_id}/{run_id}")


def list_staged_runs(backend: ApprovalBackend, skumap_digest: str) -> list[StagedRun]:
    receipts = []
    for name, modified in backend.store.list_receipts():
        match = RECEIPT_NAME_PATTERN.fullmatch(name)
        if match:
            receipts.append((modified, match["snapshot"], match["run"]))
    receipts.sort(reverse=True)
    runs = []
    for modified, snapshot_id, run_id in receipts[:MAX_LISTED_RUNS]:
        try:
            run = load_staged_run(backend, skumap_digest, snapshot_id, run_id)
        except StagedRunNotFound:
            continue  # Removed between listing and reading.
        run.staged_at = modified
        runs.append(run)
    return runs


def load_staged_run(
    backend: ApprovalBackend, skumap_digest: str, snapshot_id: str, run_id: str
) -> StagedRun:
    check_ids(snapshot_id, run_id)
    run = StagedRun(snapshot_id=snapshot_id, run_id=run_id)
    prefix = f"staging/{snapshot_id}/{run_id}/"
    try:
        receipt_blob = backend.store.read_staged(
            prefix + "receipt.json", etag=None, max_bytes=MAX_RECEIPT_BYTES
        )
    except StagedArtifactInvalid as error:
        run.problems.append(str(error))
        return run
    try:
        run.receipt = _strict_json(receipt_blob.data, "receipt.json")
        _load_run_files(backend.store, run, prefix)
    except ValueError as error:
        run.problems.append(str(error))
        if not isinstance(run.receipt, dict):
            run.receipt = {}
        return run
    except StagedRunNotFound:
        run.problems.append("A staged run file is missing.")
        return run
    _check_bindings(run)
    try:
        _load_decisions(backend, run, skumap_digest)
    except ValueError as error:
        run.problems.append(str(error))
    return run


def _load_run_files(store: PriceBookStore, run: StagedRun, prefix: str) -> None:
    receipt = run.receipt
    if not isinstance(receipt, dict):
        raise ValueError("receipt.json is not an object.")
    if receipt.get("snapshotId") != run.snapshot_id or receipt.get("status") != "StagedOnly":
        raise ValueError("The receipt does not describe this staged run.")
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != RUN_FILES:
        raise ValueError("The receipt does not list exactly the staged run files.")
    for name, entry in artifacts.items():
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("sha256"), str)
            or not DIGEST_PATTERN.fullmatch(entry["sha256"])
            or type(entry.get("bytes")) is not int
            or entry["bytes"] < 0
            or not isinstance(entry.get("etag"), str)
            or not entry["etag"]
        ):
            raise ValueError(f"The receipt entry for {name} is malformed.")
    loaded: dict[str, Any] = {}
    for name in SMALL_RUN_FILES:
        entry = artifacts[name]
        if entry["bytes"] > MAX_RUN_FILE_BYTES:
            raise ValueError(f"{name} exceeds the size bound.")
        blob = store.read_staged(prefix + name, etag=entry["etag"], max_bytes=MAX_RUN_FILE_BYTES)
        if len(blob.data) != entry["bytes"] or hashlib.sha256(blob.data).hexdigest() != entry["sha256"]:
            raise ValueError(f"{name} does not match its staging receipt.")
        loaded[name] = _strict_json(blob.data, name)
        if not isinstance(loaded[name], dict):
            raise ValueError(f"{name} is not an object.")
    rows = store.staged_properties(prefix + ROWS_FILE)
    rows_entry = artifacts[ROWS_FILE]
    if (
        rows.size != rows_entry["bytes"]
        or rows.etag != rows_entry["etag"]
        or rows.sha256 != rows_entry["sha256"]
    ):
        raise ValueError(f"{ROWS_FILE} does not match its staging receipt.")
    run.manifest = loaded["stage-manifest.json"]
    run.validation = loaded["validation.json"]
    run.extract = loaded["rate-extract.json"]
    run.report = loaded["rate-extract-report.json"]


def _is_digest(value: Any) -> bool:
    return isinstance(value, str) and DIGEST_PATTERN.fullmatch(value) is not None


RATE_TEXT_FIELDS = ("rateKey", "status", "old", "new", "change", "percentChange")
DIFF_COUNT_FIELDS = ("addedCount", "removedCount", "changedCount", "unchangedCount")
ROW_CHANGE_FIELDS = ("added", "retired", "changed", "materialRateChangeCount")


def _presentation_problems(validation: dict[str, Any], report: dict[str, Any]) -> list[str]:
    # Everything the page shows must have the expected shape, or the run is blocked.
    problems = []
    coverage = validation.get("coverage")
    checks = coverage.get("checks") if isinstance(coverage, dict) else None
    if not isinstance(checks, dict) or not all(type(value) is bool for value in checks.values()):
        problems.append("The coverage checks are malformed.")
    comparison = validation.get("comparison")
    if not isinstance(comparison, dict) or type(comparison.get("bootstrap")) is not bool or not all(
        comparison.get(name) is None or type(comparison.get(name)) is int for name in ROW_CHANGE_FIELDS
    ):
        problems.append("The validation comparison is malformed.")
    diff = report.get("diff")
    if diff is None:
        return problems
    rates = diff.get("rates") if isinstance(diff, dict) else None
    if (
        not isinstance(rates, list)
        or not all(type(diff.get(name)) is int for name in DIFF_COUNT_FIELDS)
        or not (diff.get("baselineSnapshotId") is None or isinstance(diff.get("baselineSnapshotId"), str))
        or not all(
            isinstance(rate, dict)
            and type(rate.get("assumed")) is bool
            and all(rate.get(name) is None or isinstance(rate.get(name), str) for name in RATE_TEXT_FIELDS)
            for rate in rates
        )
    ):
        problems.append("The rate change report is malformed.")
    return problems


def _check_bindings(run: StagedRun) -> None:
    receipt, manifest, validation = run.receipt, run.manifest, run.validation
    extract, report = run.extract, run.report
    problems = run.problems
    run.stage_manifest_digest = digest_of(manifest)
    run.extract_digest = digest_of(extract)
    # The receipt lists every staged file's SHA-256, so its digest covers all the evidence shown.
    run.evidence_digest = digest_of(receipt)
    if not all(
        _is_digest(value)
        for value in (
            receipt.get("contentHash"),
            validation.get("coverageMatrixDigest"),
            validation.get("stageManifestDigest"),
        )
    ) or type(receipt.get("rowCount")) is not int or receipt["rowCount"] <= 0:
        problems.append("The staged run is missing a required digest or row count.")
    if receipt["artifacts"][ROWS_FILE]["sha256"] != receipt.get("contentHash"):
        problems.append("The canonical rows do not match the snapshot content hash.")
    problems.extend(_presentation_problems(validation, report))
    if validation.get("validationStatus") != "Validated" or manifest.get("validationStatus") != "Validated":
        problems.append("The staged run is not Validated.")
    if validation.get("failures") != []:
        problems.append("The validation report lists failures.")
    coverage = validation.get("coverage")
    if not isinstance(coverage, dict) or coverage.get("missing") != []:
        problems.append("Required catalog coverage is missing.")
    if run.stage_manifest_digest != validation.get("stageManifestDigest"):
        problems.append("The stage manifest does not match its validation report.")
    for name in ("snapshotId", "contentHash", "rowCount"):
        values = {canonical_json(source.get(name)) for source in (receipt, manifest, validation)}
        if len(values) != 1:
            problems.append(f"The staged {name} values disagree.")
    if manifest.get("snapshotId") != run.snapshot_id:
        problems.append("The stage manifest names a different snapshot.")
    if not (receipt.get("scope") == manifest.get("scope") == validation.get("scope")) or not isinstance(
        manifest.get("scope"), dict
    ):
        problems.append("The staged scope values disagree.")
    if manifest.get("coverageMatrixDigest") != validation.get("coverageMatrixDigest"):
        problems.append("The coverage matrix digest does not match its validation report.")
    for name in ("publishingHuman", "skuMapReviewer", "skuMapDigest"):
        if manifest.get(name) is not None:
            problems.append("The stage manifest already names an approver.")
            break
    extract_manifest = extract.get("manifest") if isinstance(extract.get("manifest"), dict) else {}
    if not (run.extract_digest == receipt.get("extractDigest") == report.get("extractDigest")):
        problems.append("The rate extract does not match its receipt and report.")
    if not (
        extract_manifest.get("snapshotId")
        == receipt.get("extractSnapshotId")
        == report.get("extractSnapshotId")
    ):
        problems.append("The rate extract snapshot IDs disagree.")
    if not (
        extract_manifest.get("sourceSnapshotId") == report.get("sourceSnapshotId") == run.snapshot_id
        and extract_manifest.get("sourceContentHash")
        == report.get("sourceContentHash")
        == receipt.get("contentHash")
    ):
        problems.append("The rate extract is not derived from this staged snapshot.")
    if extract_manifest.get("publishingHuman") is not None:
        problems.append("The rate extract already names an approver.")


def _load_decisions(backend: ApprovalBackend, run: StagedRun, skumap_digest: str) -> None:
    store, signer = backend.store, backend.signer
    review_blob = store.read_control(
        review_blob_name(run.snapshot_id, run.run_id), max_bytes=MAX_CONTROL_BYTES
    )
    if review_blob is not None:
        try:
            review = _strict_json(review_blob.data, "The SkuMap review")
        except ValueError as error:
            run.problems.append(str(error))
            return
        problem = _review_problem(review, run, signer)
        if problem:
            run.problems.append(f"The SkuMap review {problem}.")
            return
        run.review = review
    approval_blob = store.read_control(
        approval_blob_name(run.snapshot_id), max_bytes=MAX_CONTROL_BYTES
    )
    if approval_blob is None:
        # A later SkuMap change blocks only a run that is not yet approved.
        if run.review is not None and run.review["skuMapDigest"] != skumap_digest:
            run.problems.append("The SkuMap changed after it was reviewed.")
        return
    try:
        wrapper = _strict_json(approval_blob.data, "The approval")
    except ValueError as error:
        run.problems.append(str(error))
        return
    if not isinstance(wrapper, dict) or wrapper.get("runId") != run.run_id or (
        isinstance(wrapper.get("record"), dict) and wrapper["record"].get("runId") != run.run_id
    ):
        run.problems.append("Another staged run of this snapshot is already approved.")
        return
    record = wrapper.get("record")
    problem = _approval_problem(record, run, signer)
    if problem:
        run.problems.append(f"The approval {problem}.")
        return
    run.approval = record


def _review_problem(review: Any, run: StagedRun, signer: ApprovalSigner) -> str | None:
    if not isinstance(review, dict) or review.get("recordType") != "SkuMapReview":
        return "is not a SkuMap review record"
    problem = record_signature_problem(review, signer)
    if problem:
        return problem
    expected = {
        "snapshotId": run.snapshot_id,
        "runId": run.run_id,
        "contentHash": run.receipt.get("contentHash"),
        "stageManifestDigest": run.stage_manifest_digest,
        "extractDigest": run.extract_digest,
        "evidenceDigest": run.evidence_digest,
        "reviewerRole": Role.SKUMAP_REVIEWER.value,
    }
    if any(review.get(name) != value for name, value in expected.items()):
        return "does not bind this staged run"
    if not isinstance(review.get("reviewerId"), str) or not review["reviewerId"].strip():
        return "has no reviewer"
    if not isinstance(review.get("skuMapDigest"), str):
        return "has no SkuMap digest"
    return None


def _approval_problem(record: Any, run: StagedRun, signer: ApprovalSigner) -> str | None:
    if not isinstance(record, dict):
        return "is not an approval record"
    problem = record_signature_problem(record, signer)
    if problem:
        return problem
    review = run.review
    if review is None:
        return "has no valid SkuMap review"
    expected = {
        "snapshotId": run.snapshot_id,
        "runId": run.run_id,
        "contentHash": run.receipt.get("contentHash"),
        "stageManifestDigest": run.stage_manifest_digest,
        "extractDigest": run.extract_digest,
        "evidenceDigest": run.evidence_digest,
        "coverageMatrixDigest": run.validation.get("coverageMatrixDigest"),
        "scope": run.validation.get("scope"),
        "skuMapDigest": review["skuMapDigest"],
        "skuMapReviewerId": review["reviewerId"],
        "skuMapReviewerRole": Role.SKUMAP_REVIEWER.value,
        "approverRole": Role.SNAPSHOT_APPROVER.value,
        "evidencePolicy": EVIDENCE_POLICY,
        "nonProduction": True,
    }
    if any(record.get(name) != value for name, value in expected.items()):
        return "does not bind this staged run"
    if not isinstance(record.get("approverId"), str) or not record["approverId"].strip():
        return "has no approver"
    return None


# --- Decisions -----------------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _confirm_rows(backend: ApprovalBackend, run: StagedRun) -> None:
    # Blob metadata is written by the staging identity, so hash the actual bytes before signing.
    prefix = f"staging/{run.snapshot_id}/{run.run_id}/"
    started = time.perf_counter()
    try:
        actual = backend.store.hash_staged(
            prefix + ROWS_FILE, etag=run.receipt["artifacts"][ROWS_FILE]["etag"]
        )
    except (StagedRunNotFound, StagedArtifactInvalid) as error:
        raise DecisionRefused(f"The canonical rows could not be verified: {error}") from error
    logging.getLogger("app.requests").info(
        "hashed staged rows: %d bytes in %.0fms",
        run.receipt["artifacts"][ROWS_FILE]["bytes"],
        (time.perf_counter() - started) * 1000,
    )
    if actual != run.receipt["contentHash"]:
        raise DecisionRefused("The canonical rows do not match the snapshot content hash.")


def _confirm(run: StagedRun, *, stage_manifest_digest: str, extract_digest: str, evidence_digest: str,
             skumap_digest: str, current_skumap_digest: str) -> None:
    if run.problems:
        raise DecisionRefused("This staged run is blocked: " + " ".join(run.problems))
    if (
        stage_manifest_digest != run.stage_manifest_digest
        or extract_digest != run.extract_digest
        or evidence_digest != run.evidence_digest
        or skumap_digest != current_skumap_digest
    ):
        raise DecisionRefused(
            "The staged run or SkuMap changed since you loaded it. Reload and check again."
        )


def record_skumap_review(
    backend: ApprovalBackend,
    principal: Principal,
    *,
    snapshot_id: str,
    run_id: str,
    stage_manifest_digest: str,
    extract_digest: str,
    evidence_digest: str,
    skumap_digest: str,
    current_skumap_digest: str,
) -> StagedRun:
    run = load_staged_run(backend, current_skumap_digest, snapshot_id, run_id)
    _confirm(
        run,
        stage_manifest_digest=stage_manifest_digest,
        extract_digest=extract_digest,
        evidence_digest=evidence_digest,
        skumap_digest=skumap_digest,
        current_skumap_digest=current_skumap_digest,
    )
    if run.state != RunState.AWAITING_REVIEW:
        raise DecisionRefused("This staged run already has a SkuMap review.")
    _confirm_rows(backend, run)
    review = sign_record(
        {
            "recordType": "SkuMapReview",
            "schemaVersion": RECORD_SCHEMA_VERSION,
            "snapshotId": run.snapshot_id,
            "runId": run.run_id,
            "contentHash": run.receipt["contentHash"],
            "stageManifestDigest": run.stage_manifest_digest,
            "extractDigest": run.extract_digest,
            "evidenceDigest": run.evidence_digest,
            "skuMapDigest": current_skumap_digest,
            "reviewerId": principal.object_id,
            "reviewerTenantId": principal.tenant_id,
            "reviewerDisplayName": principal.name,
            "reviewerRole": Role.SKUMAP_REVIEWER.value,
            "reviewedAt": _now(),
        },
        backend.signer,
    )
    try:
        backend.store.create_control(
            review_blob_name(run.snapshot_id, run.run_id),
            f"{canonical_json(review)}\n".encode("utf-8"),
        )
    except RecordExists as error:
        raise DecisionRefused("This staged run already has a SkuMap review.") from error
    logger.info(
        "SkuMap review recorded", extra={"snapshotId": run.snapshot_id, "runId": run.run_id}
    )
    return load_staged_run(backend, current_skumap_digest, snapshot_id, run_id)


def record_snapshot_approval(
    backend: ApprovalBackend,
    principal: Principal,
    *,
    snapshot_id: str,
    run_id: str,
    stage_manifest_digest: str,
    extract_digest: str,
    evidence_digest: str,
    skumap_digest: str,
    current_skumap_digest: str,
) -> StagedRun:
    run = load_staged_run(backend, current_skumap_digest, snapshot_id, run_id)
    _confirm(
        run,
        stage_manifest_digest=stage_manifest_digest,
        extract_digest=extract_digest,
        evidence_digest=evidence_digest,
        skumap_digest=skumap_digest,
        current_skumap_digest=current_skumap_digest,
    )
    if run.state == RunState.AWAITING_REVIEW:
        raise DecisionRefused("A SkuMapReviewer must review this staged run first.")
    if run.state != RunState.AWAITING_APPROVAL or run.review is None:
        raise DecisionRefused("This snapshot is already approved.")
    review = run.review
    _confirm_rows(backend, run)
    record = sign_record(
        {
            "snapshotId": run.snapshot_id,
            "runId": run.run_id,
            "contentHash": run.receipt["contentHash"],
            "approverId": principal.object_id,
            "approverDisplayName": principal.name,
            "approverRole": Role.SNAPSHOT_APPROVER.value,
            "approvedAt": _now(),
            "skuMapReviewerId": review["reviewerId"],
            "skuMapReviewerDisplayName": review["reviewerDisplayName"],
            "skuMapReviewerRole": Role.SKUMAP_REVIEWER.value,
            "skuMapDigest": review["skuMapDigest"],
            "coverageMatrixDigest": run.validation["coverageMatrixDigest"],
            "scope": run.validation["scope"],
            "stageManifestDigest": run.stage_manifest_digest,
            "extractDigest": run.extract_digest,
            "evidenceDigest": run.evidence_digest,
            "evidencePolicy": EVIDENCE_POLICY,
            "nonProduction": True,
        },
        backend.signer,
    )
    wrapper = {"schemaVersion": RECORD_SCHEMA_VERSION, "runId": run.run_id, "record": record}
    try:
        backend.store.create_control(
            approval_blob_name(run.snapshot_id), f"{canonical_json(wrapper)}\n".encode("utf-8")
        )
    except RecordExists as error:
        raise DecisionRefused("This snapshot is already approved.") from error
    logger.info(
        "Snapshot approval recorded", extra={"snapshotId": run.snapshot_id, "runId": run.run_id}
    )
    return load_staged_run(backend, current_skumap_digest, snapshot_id, run_id)


# --- Presentation --------------------------------------------------------------------------------


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def run_summary(run: StagedRun) -> dict[str, Any]:
    return {
        "snapshotId": run.snapshot_id,
        "runId": run.run_id,
        "state": run.state.value,
        "stagedAt": run.staged_at.isoformat() if run.staged_at else None,
        "capturedAt": _text(run.receipt.get("capturedAt")),
        "problems": list(run.problems),
    }


def run_detail(run: StagedRun, principal: Principal, skumap_digest: str) -> dict[str, Any]:
    validation = run.validation
    comparison = validation.get("comparison") if isinstance(validation.get("comparison"), dict) else {}
    coverage = validation.get("coverage") if isinstance(validation.get("coverage"), dict) else {}
    checks = coverage.get("checks") if isinstance(coverage.get("checks"), dict) else {}
    diff = run.report.get("diff") if isinstance(run.report.get("diff"), dict) else None
    state = run.state
    is_reviewer = Role.SKUMAP_REVIEWER.value in principal.roles
    is_approver = Role.SNAPSHOT_APPROVER.value in principal.roles
    if state == RunState.AWAITING_REVIEW:
        can_review, can_approve = is_reviewer, False
        waiting = None if is_reviewer else "Waiting for a SkuMapReviewer."
    elif state == RunState.AWAITING_APPROVAL:
        can_review, can_approve = False, is_approver
        waiting = None if can_approve else "Waiting for a SnapshotApprover."
    elif state == RunState.APPROVED and run.publication is not None:
        can_review = can_approve = False
        completing = run.publication.get("completionPending") is True
        baseline_ok = completing or run.publication.get("pointerMatchesBaseline") is True
        can_publish = is_approver and baseline_ok
        waiting = (
            None if can_publish
            else "The published price book changed after this run was validated, so it can't be "
            "published. Start a new harvest."
            if not baseline_ok
            else "This snapshot is current, but publishing did not finish. Waiting for a "
            "SnapshotApprover to publish again to record it."
            if completing
            else "Waiting for a SnapshotApprover to publish."
        )
    else:
        can_review = can_approve = False
        waiting = None
    if state != RunState.APPROVED or run.publication is None:
        can_publish = False
    return {
        **run_summary(run),
        "contentHash": _text(run.receipt.get("contentHash")),
        "rowCount": run.receipt.get("rowCount") if type(run.receipt.get("rowCount")) is int else None,
        "validatedAt": _text(run.receipt.get("validatedAt")),
        "scope": run.manifest.get("scope") if isinstance(run.manifest.get("scope"), dict) else None,
        "baselineSnapshotId": _text(run.receipt.get("baselineSnapshotId")),
        "stageManifestDigest": run.stage_manifest_digest,
        "extractDigest": run.extract_digest,
        "evidenceDigest": run.evidence_digest,
        "extractSnapshotId": _text(run.receipt.get("extractSnapshotId")),
        "skuMapDigest": skumap_digest,
        "validation": {
            "status": _text(validation.get("validationStatus")),
            "failures": [str(item) for item in validation.get("failures") or []]
            if isinstance(validation.get("failures"), list) else [],
            "coverage": [
                {"check": str(name), "covered": covered is True}
                for name, covered in sorted(checks.items())
            ],
            "bootstrap": comparison.get("bootstrap") is True,
            "rowChanges": {
                name: comparison.get(name) if type(comparison.get(name)) is int else None
                for name in ("added", "retired", "changed", "materialRateChangeCount")
            },
        },
        "extract": {
            "specVersion": _text(run.report.get("specVersion")),
            "rateCount": run.report.get("rateCount") if type(run.report.get("rateCount")) is int else None,
            "assumedRateCount": run.report.get("assumedRateCount")
            if type(run.report.get("assumedRateCount")) is int else None,
            "diff": _diff_view(diff),
        },
        "review": None if run.review is None else {
            "reviewerDisplayName": run.review.get("reviewerDisplayName"),
            "reviewedAt": run.review.get("reviewedAt"),
            "skuMapDigest": run.review.get("skuMapDigest"),
        },
        "approval": None if run.approval is None else {
            "approverDisplayName": run.approval.get("approverDisplayName"),
            "approvedAt": run.approval.get("approvedAt"),
            "evidencePolicy": run.approval.get("evidencePolicy"),
            "nonProduction": run.approval.get("nonProduction"),
        },
        "publication": None if run.publication is None else {
            name: run.publication.get(name)
            for name in ("artifact", "current", "currentSnapshotId", "publishedAt", "publishedBy")
        },
        "actions": {
            "canReview": can_review,
            "canApprove": can_approve,
            "canPublish": can_publish,
            "waiting": waiting,
        },
    }


def _diff_view(diff: dict[str, Any] | None) -> dict[str, Any] | None:
    if diff is None:
        return None
    rates = []
    for entry in diff.get("rates") if isinstance(diff.get("rates"), list) else []:
        if not isinstance(entry, dict):
            continue
        rates.append({
            name: _text(entry.get(name))
            for name in ("rateKey", "status", "old", "new", "change", "percentChange")
        } | {"assumed": entry.get("assumed") is True})
    return {
        "baselineSnapshotId": _text(diff.get("baselineSnapshotId")),
        **{
            f"{status}Count": diff.get(f"{status}Count")
            if type(diff.get(f"{status}Count")) is int else None
            for status in ("added", "removed", "changed", "unchanged")
        },
        "rates": rates,
    }
