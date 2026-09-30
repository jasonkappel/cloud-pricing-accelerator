from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from decimal import InvalidOperation
from pathlib import Path

from .approval_keys import load_key_vault_verifier
from .aws import AWS_OFFERS, collect_aws_offer, collect_compute_savings_plan
from .azure import collect_azure_rows
from .blob_stage import stage_validated_run
from .derive import SPEC_PATH, derive_from_artifact, derive_run, write_outputs
from .core import HarvestError, canonical_json, validate_snapshot_id, write_ndjson
from .http import SafeHttpClient
from .reconcile import reconcile_representative_application
from .snapshot import (
    approve_snapshot,
    build_staged_snapshot,
    publish_snapshot,
    validate_snapshot,
)


COLLECTOR_VERSION = "1.0.0"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Streaming public price harvester")
    commands = parser.add_subparsers(dest="command", required=True)

    collect = commands.add_parser("collect")
    collect.add_argument("--run-dir", type=Path, required=True)
    collect.add_argument("--snapshot-id", required=True)
    collect.add_argument("--azure-region", default="eastus2")
    collect.add_argument("--aws-region", default="us-east-1")

    validate = commands.add_parser("validate")
    validate.add_argument("--run-dir", type=Path, required=True)
    validate.add_argument("--previous-artifact", type=Path)
    validate.add_argument("--current-pointer", type=Path)
    validate.add_argument("--bootstrap", action="store_true")
    _add_verifier_arguments(validate)

    approve = commands.add_parser("approve")
    approve.add_argument("--run-dir", type=Path, required=True)
    approve.add_argument("--approval-record", type=Path, required=True)
    approve.add_argument("--sku-map", type=Path, required=True)
    _add_verifier_arguments(approve)

    publish = commands.add_parser("publish")
    publish.add_argument("--run-dir", type=Path, required=True)
    publish.add_argument("--store-dir", type=Path, required=True)
    publish.add_argument("--expected-pointer-hash")
    _add_verifier_arguments(publish)

    stage_blob = commands.add_parser("stage-blob")
    stage_blob.add_argument("--run-dir", type=Path, required=True)
    stage_blob.add_argument("--account-url", required=True)
    stage_blob.add_argument("--container", default="staged-runs")
    stage_blob.add_argument(
        "--credential-mode", choices=("local", "managed-identity"),
        default="managed-identity",
    )
    stage_blob.add_argument("--managed-identity-client-id")
    stage_blob.add_argument("--spec", type=Path, default=SPEC_PATH)
    stage_blob.add_argument("--previous-extract", type=Path)

    derive = commands.add_parser("derive")
    derive_source = derive.add_mutually_exclusive_group(required=True)
    derive_source.add_argument("--run-dir", type=Path)
    derive_source.add_argument("--artifact", type=Path)
    derive.add_argument("--output-dir", type=Path)
    derive.add_argument("--previous-extract", type=Path)
    derive.add_argument("--spec", type=Path, default=SPEC_PATH)
    _add_verifier_arguments(derive)

    reconcile = commands.add_parser("reconcile")
    reconcile.add_argument("--artifact", type=Path, required=True)
    reconcile.add_argument("--output", type=Path, required=True)
    reconcile.add_argument(
        "--enterprise-discount-percent",
        type=_decimal_argument,
        default=Decimal("10"),
    )
    _add_verifier_arguments(reconcile)
    return parser


def _add_verifier_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--approval-key-id",
        help="Versioned Key Vault key ID that signs RS256 approvals; "
        "without it, approvals must be HMAC-signed with HARVESTER_APPROVAL_HMAC_KEY.",
    )
    parser.add_argument(
        "--verifier-identity-client-id",
        help="Managed identity client ID used to read the approval key's public half.",
    )


def _verifier(args: argparse.Namespace):
    if not args.approval_key_id:
        if args.verifier_identity_client_id:
            raise HarvestError("--verifier-identity-client-id requires --approval-key-id.")
        return None
    credential = None
    if args.verifier_identity_client_id:
        from azure.identity import ManagedIdentityCredential

        credential = ManagedIdentityCredential(client_id=args.verifier_identity_client_id)
    return load_key_vault_verifier(args.approval_key_id, credential=credential)


def _decimal_argument(value: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("must be a decimal number") from exc


def main() -> None:
    args = build_parser().parse_args()
    try:
        result = _run(args)
    except HarvestError as exc:
        raise SystemExit(f"harvester failed: {exc}") from exc
    print(canonical_json(result))


def _run(args: argparse.Namespace) -> dict:
    if args.command == "collect":
        return _collect(args)
    if args.command == "validate":
        return validate_snapshot(
            args.run_dir,
            previous_artifact=args.previous_artifact,
            bootstrap=args.bootstrap,
            current_pointer=args.current_pointer,
            approval_verifier=_verifier(args),
        )
    if args.command == "approve":
        return approve_snapshot(
            args.run_dir,
            approval_record_path=args.approval_record,
            sku_map_path=args.sku_map,
            approval_verifier=_verifier(args),
        )
    if args.command == "publish":
        return publish_snapshot(
            args.run_dir,
            store_dir=args.store_dir,
            expected_pointer_hash=args.expected_pointer_hash,
            approval_verifier=_verifier(args),
        )
    if args.command == "stage-blob":
        return stage_validated_run(
            args.run_dir,
            account_url=args.account_url,
            container=args.container,
            credential_mode=args.credential_mode,
            managed_identity_client_id=args.managed_identity_client_id,
            spec_path=args.spec,
            previous_extract=args.previous_extract,
        )
    if args.command == "derive":
        return _derive(args)
    if args.command == "reconcile":
        report = reconcile_representative_application(
            args.artifact,
            enterprise_discount_percent=args.enterprise_discount_percent,
            approval_verifier=_verifier(args),
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            f"{canonical_json(report)}\n", encoding="utf-8", newline="\n"
        )
        return {
            "report": str(args.output),
            "reportDigest": report["reportDigest"],
        }
    raise AssertionError("unreachable")


def _derive(args: argparse.Namespace) -> dict:
    if args.run_dir is not None:
        if args.approval_key_id:
            raise HarvestError("--approval-key-id applies only to --artifact.")
        if args.output_dir is not None:
            raise HarvestError("--output-dir applies only to --artifact; a run writes into its run directory.")
        return derive_run(args.run_dir, spec_path=args.spec, previous_extract=args.previous_extract)
    if args.output_dir is None:
        raise HarvestError("--artifact requires --output-dir.")
    extract, report = derive_from_artifact(
        args.artifact,
        spec_path=args.spec,
        previous_extract=args.previous_extract,
        approval_verifier=_verifier(args),
    )
    write_outputs(args.output_dir, extract, report)
    return {
        "extractSnapshotId": report["extractSnapshotId"],
        "extractDigest": report["extractDigest"],
        "rateCount": report["rateCount"],
        "changedCount": (report["diff"] or {}).get("changedCount"),
    }


def _collect(args: argparse.Namespace) -> dict:
    validate_snapshot_id(args.snapshot_id)
    captured_at = datetime.now(UTC).isoformat()
    args.run_dir.mkdir(parents=True, exist_ok=False)
    source_paths: list[Path] = []
    with SafeHttpClient() as client:
        azure_path = args.run_dir / "sources" / "azure.ndjson"
        write_ndjson(
            azure_path,
            collect_azure_rows(client, region=args.azure_region),
        )
        source_paths.append(azure_path)
        for offer in AWS_OFFERS:
            path = args.run_dir / "sources" / f"aws-{offer}.ndjson"
            write_ndjson(
                path,
                collect_aws_offer(
                    client,
                    offer=offer,
                    region=args.aws_region,
                    work_dir=args.run_dir,
                ),
            )
            source_paths.append(path)
        savings_plan_path = args.run_dir / "sources" / "aws-savings-plans.ndjson"
        write_ndjson(
            savings_plan_path,
            collect_compute_savings_plan(
                client,
                region=args.aws_region,
                work_dir=args.run_dir,
            ),
        )
        source_paths.append(savings_plan_path)
    manifest = build_staged_snapshot(
        snapshot_id=args.snapshot_id,
        captured_at=captured_at,
        azure_region=args.azure_region,
        aws_region=args.aws_region,
        source_paths=source_paths,
        run_dir=args.run_dir,
        collector_version=COLLECTOR_VERSION,
    )
    return {
        "snapshotId": manifest["snapshotId"],
        "validationStatus": manifest["validationStatus"],
        "rowCount": manifest["rowCount"],
        "contentHash": manifest["contentHash"],
    }


if __name__ == "__main__":
    main()
