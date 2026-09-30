from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from src.harvester import vm_signal
from src.harvester.core import HarvestError

VM_ID = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-harvest"
CLIENT_ID = "11111111-2222-3333-4444-555555555555"
LIVE = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def pending(request: str) -> dict[str, str]:
    return {"harvest-request": request, "harvest-result": "pending", "harvest-request-expires": LIVE}


class FakeTransport:
    def __init__(self, tags: dict[str, str], environment: str = "AzurePublicCloud") -> None:
        self.tags = tags
        self.environment = environment
        self.calls: list[urllib.request.Request] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> bytes:
        self.calls.append(request)
        url = request.full_url
        if url.startswith(vm_signal.IMDS):
            assert request.get_header("Metadata") == "true"
            if "/instance/compute" in url:
                return json.dumps(
                    {
                        "resourceId": VM_ID,
                        "azEnvironment": self.environment,
                        "tagsList": [{"name": k, "value": v} for k, v in self.tags.items()],
                    }
                ).encode()
            if "/identity/oauth2/token" in url:
                return json.dumps({"access_token": "token"}).encode()
        return b"{}"


def test_pending_request_only_when_result_is_pending() -> None:
    transport = FakeTransport(pending("08584abc"))
    assert vm_signal.wait_for_pending_request(wait_seconds=0, transport=transport) == "08584abc"


@pytest.mark.parametrize(
    "tags",
    [
        {},
        {"harvest-request": "08584abc"},
        {"harvest-request": "08584abc", "harvest-result": "succeeded 08584abc harvest-1"},
        {"harvest-request": "bad id;rm", "harvest-result": "pending", "harvest-request-expires": LIVE},
        {"harvest-request": "08584abc", "harvest-result": "pending"},
        {"harvest-request": "08584abc", "harvest-result": "pending", "harvest-request-expires": "2000-01-01T00:00:00Z"},
        {"harvest-request": "08584abc", "harvest-result": "pending", "harvest-request-expires": "soon"},
    ],
)
def test_maintenance_boot_is_not_a_harvest(tags: dict[str, str]) -> None:
    transport = FakeTransport(tags)
    sleeps: list[float] = []
    assert (
        vm_signal.wait_for_pending_request(
            wait_seconds=30, transport=transport, sleep=sleeps.append, poll_seconds=15
        )
        is None
    )
    assert sleeps == [15, 15]


def test_waits_for_lagging_tags() -> None:
    transport = FakeTransport({})

    def sleep(_: float) -> None:
        transport.tags = pending("req-1")

    assert vm_signal.wait_for_pending_request(wait_seconds=60, transport=transport, sleep=sleep) == "req-1"


def test_retries_unavailable_metadata_until_deadline() -> None:
    transport = FakeTransport(pending("req-2"))
    failures = [OSError("not ready")]

    def flaky(request: urllib.request.Request, timeout: float) -> bytes:
        if failures:
            raise failures.pop()
        return transport(request, timeout)

    assert vm_signal.wait_for_pending_request(wait_seconds=30, transport=flaky, sleep=lambda _: None) == "req-2"

    def down(request: urllib.request.Request, timeout: float) -> bytes:
        raise OSError("down")

    with pytest.raises(HarvestError):
        vm_signal.wait_for_pending_request(wait_seconds=30, transport=down, sleep=lambda _: None)


def test_report_merges_result_tag_on_own_vm() -> None:
    transport = FakeTransport({})
    value = vm_signal.report_result(
        status="succeeded",
        request="req-1",
        detail="harvest-20261002T060000",
        client_id=CLIENT_ID,
        transport=transport,
    )
    assert value == "succeeded req-1 harvest-20261002T060000"
    token_call = transport.calls[1]
    assert f"client_id={CLIENT_ID}" in token_call.full_url
    assert "resource=https%3A%2F%2Fmanagement.azure.com%2F" in token_call.full_url
    patch = transport.calls[-1]
    assert patch.get_method() == "PATCH"
    assert patch.full_url == (
        f"https://management.azure.com{VM_ID}/providers/Microsoft.Resources/tags/default"
        "?api-version=2021-04-01"
    )
    assert patch.get_header("Authorization") == "Bearer token"
    assert json.loads(patch.data) == {
        "operation": "Merge",
        "properties": {"tags": {"harvest-result": value}},
    }


def test_report_uses_sovereign_arm_endpoint() -> None:
    transport = FakeTransport({}, environment="AzureUSGovernmentCloud")
    vm_signal.report_result(
        status="failed", request="r", detail="collect", client_id=CLIENT_ID, transport=transport
    )
    assert transport.calls[-1].full_url.startswith("https://management.usgovcloudapi.net/")


def test_claimed_request_is_not_pending_again() -> None:
    claim = vm_signal.report_result(
        status="running", request="req-1", detail="", client_id=CLIENT_ID, transport=FakeTransport({})
    )
    assert claim == "running req-1"
    tags = {"harvest-request": "req-1", "harvest-result": claim}
    assert vm_signal.pending_request({"resourceId": VM_ID, "tagsList": [
        {"name": k, "value": v} for k, v in tags.items()
    ]}) is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"status": "done"},
        {"request": "a b"},
        {"detail": "x;y"},
        {"client_id": "not-a-guid"},
    ],
)
def test_report_rejects_bad_input(kwargs: dict[str, str]) -> None:
    arguments = {
        "status": "failed",
        "request": "req-1",
        "detail": "collect",
        "client_id": CLIENT_ID,
        **kwargs,
    }
    with pytest.raises(HarvestError):
        vm_signal.report_result(transport=FakeTransport({}), **arguments)


def test_report_refuses_unknown_cloud() -> None:
    with pytest.raises(HarvestError, match="Unknown Azure environment"):
        vm_signal.report_result(
            status="failed",
            request="r",
            detail="",
            client_id=CLIENT_ID,
            transport=FakeTransport({}, environment="SomewhereElse"),
        )
