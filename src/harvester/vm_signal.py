"""Harvest request and result signalling between the schedule (Logic App) and the harvester VM.

The Logic App tags the VM with ``harvest-request=<run id>`` and ``harvest-result=pending`` before
starting it. On boot the VM reads its tags from the Instance Metadata Service; only a pending request
starts a harvest, so a maintenance boot never harvests. The VM claims the request first
(``harvest-result=running <request>``), so a request can start at most one harvest even if the schedule
never closes it. When the run ends the VM writes
``harvest-result=<succeeded|failed> <request> <detail>`` through Azure Resource Manager with its
managed identity (Tag Contributor on the VM only) and powers off. The result tag is an operational
signal for alerting, never a trust signal: approval and publication verify the staged bytes.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
import urllib.parse
import urllib.request
from typing import Any, Callable

from .core import HarvestError

IMDS = "http://169.254.169.254/metadata"
REQUEST_TAG = "harvest-request"
RESULT_TAG = "harvest-result"
EXPIRES_TAG = "harvest-request-expires"
PENDING = "pending"
_REQUEST = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_DETAIL = re.compile(r"^[A-Za-z0-9._:/-]{0,120}$")
_ARM_BY_ENVIRONMENT = {
    "azurepubliccloud": "https://management.azure.com",
    "azureusgovernmentcloud": "https://management.usgovcloudapi.net",
    "azurechinacloud": "https://management.chinacloudapi.cn",
}
_TAGS_API_VERSION = "2021-04-01"

Transport = Callable[[urllib.request.Request, float], bytes]


def _default_transport(request: urllib.request.Request, timeout: float) -> bytes:
    # IMDS must never go through a proxy; ARM is reached directly over the NAT gateway.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        return response.read()


def _imds_json(path: str, transport: Transport) -> Any:
    request = urllib.request.Request(f"{IMDS}/{path}", headers={"Metadata": "true"})
    try:
        return json.loads(transport(request, 10.0))
    except (OSError, ValueError) as exc:
        raise HarvestError(f"Instance metadata unavailable: {exc}") from exc


def read_compute(transport: Transport = _default_transport) -> dict[str, Any]:
    compute = _imds_json("instance/compute?api-version=2021-02-01", transport)
    if not isinstance(compute, dict) or not isinstance(compute.get("resourceId"), str):
        raise HarvestError("Instance metadata has no resource ID.")
    return compute


def _tags(compute: dict[str, Any]) -> dict[str, str]:
    tags = compute.get("tagsList")
    if not isinstance(tags, list):
        return {}
    return {
        str(tag.get("name")): str(tag.get("value"))
        for tag in tags
        if isinstance(tag, dict) and tag.get("name") is not None
    }


def _expired(value: str | None, now: datetime) -> bool:
    try:
        expires = datetime.strptime(value or "", "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return now >= expires


def pending_request(compute: dict[str, Any], now: datetime | None = None) -> str | None:
    """A request is live only while pending and unexpired, so one the schedule never closed cannot
    make a later maintenance boot harvest."""
    tags = _tags(compute)
    request = tags.get(REQUEST_TAG, "")
    if tags.get(RESULT_TAG) != PENDING or not _REQUEST.fullmatch(request):
        return None
    if _expired(tags.get(EXPIRES_TAG), now or datetime.now(timezone.utc)):
        return None
    return request


def wait_for_pending_request(
    *,
    wait_seconds: int,
    transport: Transport = _default_transport,
    sleep: Callable[[float], None] = time.sleep,
    poll_seconds: int = 15,
) -> str | None:
    """IMDS tags can lag a tag update by a short time, so poll before deciding this is a maintenance boot."""
    deadline = max(wait_seconds, 0)
    waited = 0
    while True:
        try:
            request = pending_request(read_compute(transport))
        except HarvestError:
            # Instance metadata can be briefly unavailable early in boot; fail only at the deadline.
            if waited >= deadline:
                raise
            request = None
        if request is not None or waited >= deadline:
            return request
        sleep(poll_seconds)
        waited += poll_seconds


def _arm_endpoint(compute: dict[str, Any]) -> str:
    environment = str(compute.get("azEnvironment") or "AzurePublicCloud").lower()
    endpoint = _ARM_BY_ENVIRONMENT.get(environment)
    if endpoint is None:
        raise HarvestError(f"Unknown Azure environment in instance metadata: {environment}.")
    return endpoint


def _arm_token(resource: str, client_id: str, transport: Transport) -> str:
    query = urllib.parse.urlencode(
        {"api-version": "2018-02-01", "resource": f"{resource}/", "client_id": client_id}
    )
    body = _imds_json(f"identity/oauth2/token?{query}", transport)
    token = body.get("access_token") if isinstance(body, dict) else None
    if not isinstance(token, str) or not token:
        raise HarvestError("Managed identity returned no access token.")
    return token


def result_value(status: str, request: str, detail: str) -> str:
    if status not in ("running", "succeeded", "failed"):
        raise HarvestError("Result status must be running, succeeded, or failed.")
    if not _REQUEST.fullmatch(request):
        raise HarvestError("Invalid harvest request ID.")
    if not _DETAIL.fullmatch(detail):
        raise HarvestError("Invalid result detail.")
    return f"{status} {request} {detail}".rstrip()


def report_result(
    *,
    status: str,
    request: str,
    detail: str,
    client_id: str,
    transport: Transport = _default_transport,
) -> str:
    value = result_value(status, request, detail)
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", client_id or ""):
        raise HarvestError("HARVESTER_CLIENT_ID must be the harvester identity's client ID.")
    compute = read_compute(transport)
    endpoint = _arm_endpoint(compute)
    token = _arm_token(endpoint, client_id, transport)
    resource_id = compute["resourceId"]
    if not re.fullmatch(
        r"/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft\.Compute/virtualMachines/[^/]+",
        resource_id,
        flags=re.IGNORECASE,
    ):
        raise HarvestError("Instance metadata resource ID is not a virtual machine.")
    url = (
        f"{endpoint}{resource_id}/providers/Microsoft.Resources/tags/default"
        f"?api-version={_TAGS_API_VERSION}"
    )
    payload = json.dumps(
        {"operation": "Merge", "properties": {"tags": {RESULT_TAG: value}}}
    ).encode("utf-8")
    request_obj = urllib.request.Request(
        url,
        data=payload,
        method="PATCH",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        transport(request_obj, 30.0)
    except OSError as exc:
        raise HarvestError(f"Could not record the harvest result: {exc}") from exc
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Harvest request and result tags")
    commands = parser.add_subparsers(dest="command", required=True)
    pending = commands.add_parser("pending", help="Print the pending request ID, or nothing.")
    pending.add_argument("--wait-seconds", type=int, default=0)
    report = commands.add_parser("report", help="Record the harvest result on the VM.")
    report.add_argument("--request", required=True)
    report.add_argument("--status", required=True, choices=("running", "succeeded", "failed"))
    report.add_argument("--detail", default="")
    report.add_argument("--client-id", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "pending":
            request = wait_for_pending_request(wait_seconds=args.wait_seconds)
            if request:
                print(request)
            return 0
        print(
            report_result(
                status=args.status,
                request=args.request,
                detail=args.detail,
                client_id=args.client_id,
            )
        )
        return 0
    except HarvestError as exc:
        print(f"harvest signal failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
