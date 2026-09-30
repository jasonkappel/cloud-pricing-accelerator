"""Sign-in and role checks.

AUTH_MODE is required and has no default:

- ``appservice``: App Service Authentication validates the Entra access token (tenant, audience, and
  calling application) before the request reaches this code, strips any client-supplied ``X-MS-*``
  headers, and injects ``X-MS-CLIENT-PRINCIPAL``. The platform sets ``WEBSITE_AUTH_ENABLED=True`` only
  while that validation is on; without it the header could be forged, so every protected route fails
  closed with 503.
- ``local``: developer machines only. The principal comes from ``LOCAL_PRINCIPAL_*`` settings. Startup
  fails if any Azure hosting indicator is present.

Roles are never inferred. A principal without the required app role gets 403.
"""

import base64
import binascii
import json
import os
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from app.models import PrincipalRecord

AUTH_MODE_ENV = "AUTH_MODE"
PRINCIPAL_HEADER = "x-ms-client-principal"
PRINCIPAL_IDP_HEADER = "x-ms-client-principal-idp"
MAX_PRINCIPAL_HEADER_BYTES = 16 * 1024

AZURE_HOSTING_INDICATORS = (
    "WEBSITE_SITE_NAME",
    "WEBSITE_INSTANCE_ID",
    "WEBSITE_AUTH_ENABLED",
    "IDENTITY_ENDPOINT",
    "MSI_ENDPOINT",
    "CONTAINER_APP_NAME",
    "FUNCTIONS_WORKER_RUNTIME",
)

OID_CLAIMS = ("http://schemas.microsoft.com/identity/claims/objectidentifier", "oid")
TENANT_CLAIMS = ("http://schemas.microsoft.com/identity/claims/tenantid", "tid")
NAME_CLAIMS = ("name", "preferred_username")
ROLE_CLAIMS = ("roles", "http://schemas.microsoft.com/ws/2008/06/identity/claims/role")


class AuthConfigurationError(RuntimeError):
    pass


class AuthMode(StrEnum):
    APPSERVICE = "appservice"
    LOCAL = "local"


class Role(StrEnum):
    ESTIMATOR = "Estimator"
    SNAPSHOT_APPROVER = "SnapshotApprover"
    SKUMAP_REVIEWER = "SkuMapReviewer"


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    object_id: str
    name: str
    roles: frozenset[str]

    def record(self) -> PrincipalRecord:
        return PrincipalRecord(
            tenant_id=self.tenant_id,
            object_id=self.object_id,
            name=self.name,
        )


def auth_mode() -> AuthMode:
    raw = os.getenv(AUTH_MODE_ENV)
    if raw is None or not raw.strip():
        raise AuthConfigurationError(
            "AUTH_MODE is required: 'appservice' in Azure or 'local' on a developer machine."
        )
    try:
        mode = AuthMode(raw.strip())
    except ValueError as error:
        raise AuthConfigurationError(
            f"AUTH_MODE '{raw}' is not supported; use 'appservice' or 'local'."
        ) from error
    if mode == AuthMode.LOCAL:
        present = [name for name in AZURE_HOSTING_INDICATORS if os.getenv(name)]
        if present:
            raise AuthConfigurationError(
                "AUTH_MODE=local is refused on a hosted environment "
                f"({', '.join(present)} is set)."
            )
        _local_principal()
    return mode


def _local_principal() -> Principal:
    object_id = os.getenv("LOCAL_PRINCIPAL_OID", "").strip()
    name = os.getenv("LOCAL_PRINCIPAL_NAME", "").strip()
    if not object_id or not name:
        raise AuthConfigurationError(
            "AUTH_MODE=local requires LOCAL_PRINCIPAL_OID and LOCAL_PRINCIPAL_NAME."
        )
    roles = frozenset(
        role.strip()
        for role in os.getenv("LOCAL_PRINCIPAL_ROLES", "").split(",")
        if role.strip()
    )
    unknown = roles - {role.value for role in Role}
    if unknown:
        raise AuthConfigurationError(
            f"LOCAL_PRINCIPAL_ROLES has unknown roles: {', '.join(sorted(unknown))}."
        )
    return Principal(
        tenant_id=os.getenv("LOCAL_PRINCIPAL_TENANT", "local").strip() or "local",
        object_id=object_id,
        name=name,
        roles=roles,
    )


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


def _first_claim(claims: list[dict[str, object]], types: tuple[str, ...]) -> str | None:
    for claim_type in types:
        for claim in claims:
            if claim.get("typ") == claim_type and isinstance(claim.get("val"), str):
                value = str(claim["val"]).strip()
                if value:
                    return value
    return None


def parse_client_principal(encoded: str) -> Principal:
    if len(encoded) > MAX_PRINCIPAL_HEADER_BYTES:
        raise _unauthorized("The sign-in principal is too large.")
    try:
        payload = json.loads(base64.b64decode(encoded, validate=True))
    except (binascii.Error, ValueError, UnicodeDecodeError) as error:
        raise _unauthorized("The sign-in principal is not readable.") from error
    if not isinstance(payload, dict) or payload.get("auth_typ") != "aad":
        raise _unauthorized("Sign in with Microsoft Entra ID.")
    claims = payload.get("claims")
    if not isinstance(claims, list) or not all(isinstance(item, dict) for item in claims):
        raise _unauthorized("The sign-in principal has no claims.")
    object_id = _first_claim(claims, OID_CLAIMS)
    tenant_id = _first_claim(claims, TENANT_CLAIMS)
    if object_id is None or tenant_id is None:
        raise _unauthorized("The sign-in principal has no object or tenant ID.")
    name_type = payload.get("name_typ")
    name_types = ((name_type,) if isinstance(name_type, str) else ()) + NAME_CLAIMS
    role_type = payload.get("role_typ")
    role_types = ((role_type,) if isinstance(role_type, str) else ()) + ROLE_CLAIMS
    roles = frozenset(
        str(claim["val"]).strip()
        for claim in claims
        if claim.get("typ") in role_types and isinstance(claim.get("val"), str)
    )
    return Principal(
        tenant_id=tenant_id,
        object_id=object_id,
        name=_first_claim(claims, name_types) or object_id,
        roles=roles,
    )


def current_principal(request: Request) -> Principal:
    try:
        mode = auth_mode()
    except AuthConfigurationError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)
        ) from error
    if mode == AuthMode.LOCAL:
        return _local_principal()
    if os.getenv("WEBSITE_AUTH_ENABLED", "").strip().casefold() != "true":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Sign-in is not configured for this API yet.",
        )
    if request.headers.get(PRINCIPAL_IDP_HEADER, "").casefold() != "aad":
        raise _unauthorized("Sign in with Microsoft Entra ID.")
    encoded = request.headers.get(PRINCIPAL_HEADER)
    if not encoded:
        raise _unauthorized("Sign in to continue.")
    return parse_client_principal(encoded)


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]


def require_role(role: Role):
    def dependency(principal: CurrentPrincipal) -> Principal:
        if role.value not in principal.roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This action needs the {role.value} role.",
            )
        return principal

    return dependency


EstimatorPrincipal = Annotated[Principal, Depends(require_role(Role.ESTIMATOR))]


def require_any_role(*roles: Role):
    def dependency(principal: CurrentPrincipal) -> Principal:
        if not any(role.value in principal.roles for role in roles):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This page needs one of the "
                + ", ".join(role.value for role in roles)
                + " roles.",
            )
        return principal

    return dependency


SkuMapReviewerPrincipal = Annotated[Principal, Depends(require_role(Role.SKUMAP_REVIEWER))]
SnapshotApproverPrincipal = Annotated[Principal, Depends(require_role(Role.SNAPSHOT_APPROVER))]
PriceBookViewerPrincipal = Annotated[
    Principal,
    Depends(require_any_role(Role.ESTIMATOR, Role.SKUMAP_REVIEWER, Role.SNAPSHOT_APPROVER)),
]
