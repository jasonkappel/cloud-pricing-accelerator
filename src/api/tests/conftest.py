import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

API_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = API_ROOT.parents[1]
sys.path.insert(0, str(API_ROOT))

TEST_AUTH_ENV = {
    "AUTH_MODE": "local",
    "LOCAL_PRINCIPAL_OID": "00000000-0000-0000-0000-00000000e571",
    "LOCAL_PRINCIPAL_NAME": "Test Estimator",
    "LOCAL_PRINCIPAL_ROLES": "Estimator",
}
os.environ.update(TEST_AUTH_ENV)

from app.data import application_repository  # noqa: E402
from main import app  # noqa: E402


@pytest.fixture(autouse=True)
def local_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in TEST_AUTH_ENV.items():
        monkeypatch.setenv(name, value)


@pytest.fixture(autouse=True)
def clear_repository() -> None:
    application_repository.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def repository_root() -> Path:
    return REPOSITORY_ROOT
