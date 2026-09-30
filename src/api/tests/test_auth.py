import base64
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import create_app
from app.auth import AuthConfigurationError, auth_mode, parse_client_principal
from app.data import application_repository
from app.intake import parse_intake
from app.models import Application, PrincipalRecord
from app.pricing import pricing_engine

TENANT = "11111111-1111-1111-1111-111111111111"
ESTIMATOR_OID = "22222222-2222-2222-2222-222222222222"
APPROVER_OID = "33333333-3333-3333-3333-333333333333"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _principal_header(oid: str, name: str, roles: list[str], tenant: str = TENANT) -> str:
    claims = [
        {"typ": "http://schemas.microsoft.com/identity/claims/objectidentifier", "val": oid},
        {"typ": "http://schemas.microsoft.com/identity/claims/tenantid", "val": tenant},
        {"typ": "name", "val": name},
        *({"typ": "roles", "val": role} for role in roles),
    ]
    payload = {"auth_typ": "aad", "claims": claims, "name_typ": "name", "role_typ": "roles"}
    return base64.b64encode(json.dumps(payload).encode()).decode()


def _headers(oid: str, name: str, roles: list[str]) -> dict[str, str]:
    return {
        "X-MS-CLIENT-PRINCIPAL": _principal_header(oid, name, roles),
        "X-MS-CLIENT-PRINCIPAL-IDP": "aad",
    }


@pytest.fixture
def appservice(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_MODE", "appservice")
    monkeypatch.setenv("WEBSITE_AUTH_ENABLED", "True")
    return TestClient(create_app())


def _upload(client: TestClient, repository_root: Path, headers: dict[str, str]):
    fixture = repository_root / "samples" / "synthetic_intake_completed.xlsx"
    with fixture.open("rb") as workbook:
        return client.post(
            "/api/intakes",
            files={"file": (fixture.name, workbook, XLSX)},
            headers=headers,
        )


@pytest.mark.parametrize("value", [None, "", "  ", "none", "AppService", "anonymous"])
def test_auth_mode_is_required_and_known(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv("AUTH_MODE", raising=False)
    else:
        monkeypatch.setenv("AUTH_MODE", value)
    with pytest.raises(AuthConfigurationError):
        create_app()


@pytest.mark.parametrize(
    "indicator",
    ["WEBSITE_SITE_NAME", "WEBSITE_INSTANCE_ID", "WEBSITE_AUTH_ENABLED", "IDENTITY_ENDPOINT"],
)
def test_local_mode_is_refused_when_hosted(
    monkeypatch: pytest.MonkeyPatch, indicator: str
) -> None:
    monkeypatch.setenv(indicator, "present")
    with pytest.raises(AuthConfigurationError, match="refused on a hosted"):
        create_app()


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("LOCAL_PRINCIPAL_OID", "", "requires LOCAL_PRINCIPAL_OID"),
        ("LOCAL_PRINCIPAL_NAME", "", "requires LOCAL_PRINCIPAL_OID"),
        ("LOCAL_PRINCIPAL_ROLES", "Estimator,Admin", "unknown roles"),
    ],
)
def test_local_mode_requires_a_valid_principal(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str, message: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(AuthConfigurationError, match=message):
        auth_mode()


def test_appservice_without_platform_auth_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTH_MODE", "appservice")
    monkeypatch.delenv("WEBSITE_AUTH_ENABLED", raising=False)
    client = TestClient(create_app())
    forged = _headers(ESTIMATOR_OID, "Forged", ["Estimator"])
    assert client.get("/api/me", headers=forged).status_code == 503
    assert client.get("/api/applications", headers=forged).status_code == 503
    assert client.get("/healthz").status_code == 200


def test_health_stays_anonymous(appservice: TestClient) -> None:
    assert appservice.get("/healthz").status_code == 200
    assert appservice.get("/readyz").status_code in {200, 503}


@pytest.mark.parametrize(
    "path",
    ["/api/me", "/api/capabilities", "/api/applications"],
)
def test_missing_principal_is_401(appservice: TestClient, path: str) -> None:
    assert appservice.get(path).status_code == 401


def test_non_entra_or_unreadable_principal_is_401(appservice: TestClient) -> None:
    header = _principal_header(ESTIMATOR_OID, "Someone", ["Estimator"])
    assert appservice.get(
        "/api/me",
        headers={"X-MS-CLIENT-PRINCIPAL": header, "X-MS-CLIENT-PRINCIPAL-IDP": "github"},
    ).status_code == 401
    assert appservice.get(
        "/api/me",
        headers={"X-MS-CLIENT-PRINCIPAL": "not base64!", "X-MS-CLIENT-PRINCIPAL-IDP": "aad"},
    ).status_code == 401
    no_oid = base64.b64encode(
        json.dumps({"auth_typ": "aad", "claims": [{"typ": "name", "val": "x"}]}).encode()
    ).decode()
    assert appservice.get(
        "/api/me",
        headers={"X-MS-CLIENT-PRINCIPAL": no_oid, "X-MS-CLIENT-PRINCIPAL-IDP": "aad"},
    ).status_code == 401


def test_roles_are_deny_by_default(appservice: TestClient, repository_root: Path) -> None:
    approver = _headers(APPROVER_OID, "Snapshot Approver", ["SnapshotApprover"])
    me = appservice.get("/api/me", headers=approver)
    assert me.status_code == 200
    assert me.json() == {
        "name": "Snapshot Approver",
        "objectId": APPROVER_OID,
        "tenantId": TENANT,
        "roles": ["SnapshotApprover"],
    }
    assert appservice.get("/api/capabilities", headers=approver).status_code == 200
    assert appservice.get("/api/applications", headers=approver).status_code == 403
    assert _upload(appservice, repository_root, approver).status_code == 403
    nobody = _headers(APPROVER_OID, "No Roles", [])
    assert appservice.get("/api/applications", headers=nobody).status_code == 403
    assert application_repository.list() == []


def test_signed_in_identity_is_recorded_and_hashed(
    appservice: TestClient, repository_root: Path
) -> None:
    estimator = _headers(ESTIMATOR_OID, "Estimator One", ["Estimator"])
    response = _upload(appservice, repository_root, estimator)
    assert response.status_code == 201, response.text
    detail = response.json()
    assert detail["application"]["created_by"] == {
        "tenant_id": TENANT,
        "object_id": ESTIMATOR_OID,
        "name": "Estimator One",
    }
    application_id = detail["application"]["id"]
    resolved = appservice.post(
        f"/api/applications/{application_id}/gaps/P1.regions/resolve",
        json={"resolved_by": "Typed Attestation", "azure_region": "eastus2", "aws_region": "us-east-1"},
        headers=estimator,
    )
    assert resolved.status_code == 200, resolved.text
    gap = next(item for item in resolved.json()["gaps"] if item["id"] == "P1.regions")
    assert gap["resolved_by"] == "Typed Attestation"
    assert gap["resolved_by_principal"]["object_id"] == ESTIMATOR_OID


def test_recorded_identity_is_bound_into_the_run_hash(repository_root: Path) -> None:
    content = (repository_root / "samples" / "synthetic_intake_completed.xlsx").read_bytes()
    normalized = parse_intake(content)
    base = Application(name=normalized.application_name, intake_file_name="fixture.xlsx")

    def run_hash(application: Application) -> str:
        return pricing_engine.compare(
            application,
            normalized.compute_units,
            normalized.database_units,
            normalized.storage_units,
            normalized.gaps,
        ).run_hash

    signed = base.model_copy(
        update={
            "created_by": PrincipalRecord(tenant_id=TENANT, object_id=ESTIMATOR_OID, name="A")
        }
    )
    other = base.model_copy(
        update={
            "created_by": PrincipalRecord(tenant_id=TENANT, object_id=APPROVER_OID, name="A")
        }
    )
    assert run_hash(base) == run_hash(base.model_copy())
    assert len({run_hash(base), run_hash(signed), run_hash(other)}) == 3


def test_principal_name_is_treated_as_untrusted_text(
    appservice: TestClient, repository_root: Path
) -> None:
    hostile = _headers(ESTIMATOR_OID, '=HYPERLINK("x")', ["Estimator"])
    assert parse_client_principal(hostile["X-MS-CLIENT-PRINCIPAL"]).name.startswith("=")
    detail = _upload(appservice, repository_root, hostile).json()
    assert detail["application"]["created_by"]["name"] == "'=HYPERLINK(\"x\")"


def test_local_mode_uses_configured_principal(client: TestClient) -> None:
    me = client.get("/api/me")
    assert me.status_code == 200
    assert me.json()["name"] == "Test Estimator"
    assert me.json()["roles"] == ["Estimator"]
