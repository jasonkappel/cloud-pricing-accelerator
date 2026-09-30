"""Adversarial acceptance tests for the design rules in the README.

Each case is tested here or in the named existing test:

1. A line where Azure costs more, shown plainly: test_a_line_where_azure_costs_more_is_shown_plainly.
2. Missing IOPS held as Unpriced, not zero: test_missing_iops_is_unpriced_not_zero.
3. A model completion with an unseen number rejected: PENDING until the Foundry facade is built (it is on
   the roadmap). The numeral-rejection check itself is not tested here. What is guarded today is the
   precondition: no model is in the API, so no completion can reach a price
   (test_no_model_path_can_originate_a_number).
4. Windows not double-counted: test_windows_is_priced_once.
5. A boot disk not priced as Premium SSD v2: test_boot_disk_is_never_priced_as_premium_ssd_v2.
6. A stale or partial snapshot blocked: test_published_pricebook.py (test_a_stale_published_snapshot_fails_closed,
   test_a_missing_staged_extract_fails_closed, test_a_forged_manifest_line_fails_closed) and
   test_pricing.py::test_unpublished_pricebook_blocks_pricing.
7. An attempt to mutate an approved SkuMap refused: no route writes a SkuMap
   (test_no_route_can_write_a_skumap); changed bytes fail the digest
   (test_pricing.py::test_unapproved_skumap_digest_blocks_pricing,
   test_published_pricebook.py::test_a_skumap_changed_since_approval_fails_closed,
   test_pricebook_approval.py::test_skumap_change_blocks_a_reviewed_run_until_approval_only).
8. A versioned Published artifact mutation rejected on read: test_published_pricebook.py
   (test_an_artifact_changed_after_publication_fails_closed,
   test_an_artifact_rewritten_after_its_etag_check_fails_closed, test_a_tampered_staged_extract_fails_closed,
   test_a_pointer_rolled_back_to_a_replaced_snapshot_fails_closed).
9. An RBAC-bypass attempt on a mutating call refused: test_auth.py (test_roles_are_deny_by_default,
   test_appservice_without_platform_auth_fails_closed, test_non_entra_or_unreadable_principal_is_401) and
   test_pricebook_approval.py::test_roles_are_enforced_per_action.
"""

import ast
import io
import json
from decimal import Decimal
from pathlib import Path

from openpyxl import load_workbook

from main import app

API_ROOT = Path(__file__).resolve().parents[1]
MODEL_PACKAGES = ("openai", "azure.ai", "semantic_kernel", "langchain", "anthropic", "autogen")


def _upload(client, content: bytes) -> dict:
    response = client.post(
        "/api/intakes",
        files={
            "file": (
                "intake.xlsx",
                io.BytesIO(content),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _resolve(client, application_id: str, gap_id: str, body: dict) -> dict:
    response = client.post(
        f"/api/applications/{application_id}/gaps/{gap_id}/resolve",
        json={"resolved_by": "Reviewer", **body},
    )
    assert response.status_code == 200, response.text
    return response.json()["comparison"]


def _review_baseline(client, repository_root: Path) -> dict:
    content = (repository_root / "samples" / "synthetic_intake_completed.xlsx").read_bytes()
    application_id = _upload(client, content)["application"]["id"]
    _resolve(
        client,
        application_id,
        "P1.regions",
        {"azure_region": "eastus2", "aws_region": "us-east-1"},
    )
    comparison = _resolve(client, application_id, "compute-3.runtime", {"runtime_hours_month": "220"})
    assert comparison["state"] == "ReviewBaseline"
    return comparison


def _amount(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def test_a_line_where_azure_costs_more_is_shown_plainly(client, repository_root: Path) -> None:
    comparison = _review_baseline(client, repository_root)
    priced = [line for line in comparison["line_items"] if line["status"] == "Priced"]

    azure_higher_lines = [
        line for line in priced if _amount(line["azure_amount"]) > _amount(line["aws_amount"])
    ]
    assert azure_higher_lines, "the sample must keep at least one line where Azure costs more"
    for line in priced:
        aws, azure = _amount(line["aws_amount"]), _amount(line["azure_amount"])
        expected = "Azure" if azure > aws else "AWS" if aws > azure else None
        assert line["higher_cloud"] == expected, line["id"]
    assert any(driver["higher_cloud"] == "Azure" for driver in comparison["cost_drivers"])


def test_missing_iops_is_unpriced_not_zero(client, repository_root: Path) -> None:
    workbook = load_workbook(repository_root / "samples" / "synthetic_intake_completed.xlsx")
    workbook["4 Storage"]["H2"] = ""
    buffer = io.BytesIO()
    workbook.save(buffer)
    detail = _upload(client, buffer.getvalue())
    application_id = detail["application"]["id"]
    assert "storage-1.performance" in {gap["id"] for gap in detail["gaps"]}

    for comparison in (
        _resolve(client, application_id, "P1.regions", {"azure_region": "eastus2", "aws_region": "us-east-1"}),
        _resolve(client, application_id, "compute-3.runtime", {"runtime_hours_month": "220"}),
    ):
        iops = [line for line in comparison["line_items"] if line["skumap_component_id"] == "block-iops"]
        assert [line["status"] for line in iops] == ["Unpriced"]
        assert iops[0]["aws_amount"] is None and iops[0]["azure_amount"] is None
        assert iops[0]["unpriced_reason"]
        assert comparison["state"] == "DraftBenchmark"
        assert comparison["headline_delta"] is None
        assert comparison["cheaper_cloud"] is None
        assert comparison["can_export"] is False
    response = client.get(f"/api/applications/{application_id}/comparison/export")
    assert response.status_code == 409


def test_windows_is_priced_once(client, repository_root: Path) -> None:
    rates = json.loads((repository_root / "samples" / "pricebook_seed_v1.json").read_text())["rates"]
    comparison = _review_baseline(client, repository_root)
    windows_units = {
        line["unit_id"]
        for line in comparison["line_items"]
        if line["skumap_component_id"] == "vm-win-compute"
    }
    assert windows_units

    for unit_id in windows_units:
        lines = [line for line in comparison["line_items"] if line["unit_id"] == unit_id]
        compute = [line for line in lines if line["skumap_component_id"] == "vm-win-compute"]
        uplift = [line for line in lines if line["skumap_component_id"] == "vm-win-oslicense"]
        assert len(compute) == 1 and len(uplift) == 1, unit_id
        for provider in ("aws", "azure"):
            base = Decimal(compute[0][f"{provider}_rate"])
            shapes = [
                key.split(".")[3]
                for key, value in rates.items()
                if key.startswith(f"{provider}.vm.linux.") and key.endswith(".hour")
                and Decimal(value) == base
            ]
            assert len(shapes) == 1, (unit_id, provider, shapes)
            windows_rate = Decimal(rates[f"{provider}.vm.windows.{shapes[0]}.hour"])
            assert base + Decimal(uplift[0][f"{provider}_rate"]) == windows_rate
            quantity = Decimal(compute[0][f"{provider}_quantity"])
            total = Decimal(compute[0][f"{provider}_amount"]) + Decimal(uplift[0][f"{provider}_amount"])
            assert abs(total - windows_rate * quantity) <= Decimal("0.01"), (unit_id, provider)


def test_boot_disk_is_never_priced_as_premium_ssd_v2(client, repository_root: Path) -> None:
    skumap = json.loads((repository_root / "samples" / "skumap_seed_v1.json").read_text())
    boot_disks = [
        component
        for component in _components(skumap)
        if str(component.get("componentId", "")).endswith("-bootdisk")
    ]
    assert boot_disks
    for component in boot_disks:
        azure_meter = component["azure"]["meterQuery"]
        assert "Premium SSD" in azure_meter and "v2" not in azure_meter, component["componentId"]

    comparison = _review_baseline(client, repository_root)
    assert not any(
        line["skumap_component_id"].endswith("-bootdisk") and line["status"] == "Priced"
        for line in comparison["line_items"]
    )
    assert "PILOT-BOOT-BACKUP-EXCLUSION-V1" in {item["id"] for item in comparison["excluded_cost_ledger"]}
    premium_v2_lines = {
        line["skumap_component_id"]
        for line in comparison["line_items"]
        if line["skumap_component_id"].startswith("block-")
    }
    assert premium_v2_lines <= {"block-capacity", "block-iops", "block-throughput"}


def _components(node):
    if isinstance(node, dict):
        if "componentId" in node:
            yield node
        for value in node.values():
            yield from _components(value)
    elif isinstance(node, list):
        for value in node:
            yield from _components(value)


def test_no_model_path_can_originate_a_number() -> None:
    requirements = (API_ROOT / "requirements.txt").read_text().lower()
    assert not any(package.replace(".", "-") in requirements for package in MODEL_PACKAGES)

    for source in [API_ROOT / "main.py", *(API_ROOT / "app").rglob("*.py")]:
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert not name.startswith(MODEL_PACKAGES), f"{source.name} imports {name}"


def test_no_route_can_write_a_skumap() -> None:
    writes = [
        path
        for path, operations in app.openapi()["paths"].items()
        if set(operations) & {"post", "put", "patch", "delete"}
    ]
    assert "/api/intakes" in writes
    # skumap-review records a reviewer's sign-off on a staged run; it never writes SkuMap bytes.
    assert all("skumap" not in path.lower() or path.endswith("/skumap-review") for path in writes)
