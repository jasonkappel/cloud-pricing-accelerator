from __future__ import annotations

import json
from pathlib import Path

import pytest
from azure.core.exceptions import ResourceNotFoundError

from src.harvester.core import HarvestError, canonical_json
from src.harvester.published_fetch import artifact_from_pointer, fetch_current


class FakeDownload:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.size = len(data)

    def readall(self) -> bytes:
        return self.data

    def readinto(self, handle) -> int:
        handle.write(self.data)
        return self.size


class FakeContainer:
    def __init__(self, blobs: dict[str, bytes]) -> None:
        self.blobs = blobs

    def download_blob(self, name: str, **_: object) -> FakeDownload:
        if name not in self.blobs:
            raise ResourceNotFoundError(name)
        return FakeDownload(self.blobs[name])


class FakeService:
    def __init__(self, containers: dict[str, dict[str, bytes]]) -> None:
        self.containers = containers

    def get_container_client(self, name: str) -> FakeContainer:
        return FakeContainer(self.containers.get(name, {}))


def pointer(snapshot_id: str = "harvest-1", artifact: str | None = None) -> bytes:
    return (
        canonical_json(
            {
                "snapshotId": snapshot_id,
                "contentHash": "a" * 64,
                "artifact": artifact or f"{snapshot_id}.pricebook.ndjson",
                "previousSnapshotId": None,
            }
        )
        + "\n"
    ).encode()


def test_nothing_published_means_bootstrap(tmp_path: Path) -> None:
    result = fetch_current(FakeService({}), tmp_path / "baseline")
    assert result == {"published": False}
    assert list((tmp_path / "baseline").iterdir()) == []


def test_fetches_pointer_bytes_and_named_artifact(tmp_path: Path) -> None:
    data = pointer()
    service = FakeService(
        {
            "publication-control": {"current.json": data},
            "published-pricebooks": {"harvest-1.pricebook.ndjson": b"manifest\nrow\n"},
        }
    )
    result = fetch_current(service, tmp_path / "baseline")
    assert result["published"] is True
    # The pointer is written byte for byte, because validate hashes it.
    assert (tmp_path / "baseline" / "current.json").read_bytes() == data
    assert (tmp_path / "baseline" / "harvest-1.pricebook.ndjson").read_bytes() == b"manifest\nrow\n"


def test_missing_artifact_fails_closed(tmp_path: Path) -> None:
    service = FakeService({"publication-control": {"current.json": pointer()}})
    with pytest.raises(HarvestError, match="missing artifact"):
        fetch_current(service, tmp_path / "baseline")


def test_existing_output_directory_is_refused(tmp_path: Path) -> None:
    (tmp_path / "baseline").mkdir()
    with pytest.raises(FileExistsError):
        fetch_current(FakeService({}), tmp_path / "baseline")


@pytest.mark.parametrize(
    "data",
    [
        b"not json",
        b"[]",
        json.dumps({"snapshotId": "harvest-1"}).encode(),
        pointer(artifact="../escape.pricebook.ndjson"),
        pointer(artifact="harvest-2.pricebook.ndjson"),
        pointer(snapshot_id="../bad"),
    ],
)
def test_untrusted_pointer_is_rejected(data: bytes) -> None:
    with pytest.raises(HarvestError):
        artifact_from_pointer(data)


def test_oversized_pointer_is_rejected(tmp_path: Path) -> None:
    service = FakeService({"publication-control": {"current.json": b" " * 9000}})
    with pytest.raises(HarvestError, match="too large"):
        fetch_current(service, tmp_path / "baseline")
