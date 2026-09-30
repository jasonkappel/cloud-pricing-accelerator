# Price harvester

The harvester collects the approved public Azure and AWS catalogs without loading the AWS offers into
memory. Azure is processed one API page at a time. AWS catalogs are streamed to temporary disk, products
are indexed in SQLite, and normalized terms are written as NDJSON. Raw catalogs, normalized rows, and
published snapshots are Git-ignored.

## Trust workflow

Collection, validation, approval, publication, and reconciliation are separate commands:

```powershell
python -m src.harvester collect `
  --run-dir src\harvester\data\runs\demo-001 `
  --snapshot-id demo-001

python -m src.harvester validate `
  --run-dir src\harvester\data\runs\demo-001 `
  --bootstrap

python -m src.harvester approve `
  --run-dir src\harvester\data\runs\demo-001 `
  --approval-record src\harvester\data\approvals\demo-001.json `
  --sku-map samples\skumap_seed_v1.json

python -m src.harvester publish `
  --run-dir src\harvester\data\runs\demo-001 `
  --store-dir src\harvester\data\published
```

The approval record is produced outside the collector boundary and contains immutable actor IDs,
the `SnapshotApprover` and `SkuMapReviewer` roles, timestamp, snapshot hash, SkuMap hash, and an
`HMAC-SHA256` signature. `HARVESTER_APPROVAL_HMAC_KEY` must be supplied through a secure environment
boundary and must not be passed on the command line or committed. The POC uses HMAC assertions; production
must replace them with Entra-authenticated approval records while preserving the same digest bindings.

For a non-bootstrap refresh, pass both the current Published artifact and `current.json` to validation:

```powershell
python -m src.harvester validate `
  --run-dir src\harvester\data\runs\demo-002 `
  --previous-artifact src\harvester\data\published\demo-001.pricebook.ndjson `
  --current-pointer src\harvester\data\published\current.json
```

Pass the SHA-256 of that same `current.json` to `publish --expected-pointer-hash`. Validation records the
baseline snapshot and pointer hash. Publication creates the artifact exclusively, then holds an exclusive
filesystem lock while rechecking and atomically replacing the pointer. Production Blob publication must
use ETag `If-Match`/`If-None-Match` conditions instead of the filesystem lock.

Validation verifies the canonical row count and hash, required catalog coverage, duplicate normalized IDs,
and changes against the previous Published snapshot. Missing previous state must be explicitly declared
with `--bootstrap`. Failed or unknown validation cannot be approved or published. Every artifact consumer
streams and verifies canonical order, row identities, count, and content hash before using any price.

The POC uses a filesystem store with the same artifact-first and pointer-swap semantics required for Blob.
A non-production deployment may admit only separately human-approved, digest-verified
`MutablePilot` Published artifacts on private versioned/soft-deleted Blob, explicitly labeled
non-production. This is not immutable evidence. Production and frozen Decision evidence
still require WORM retention and a separately authorized publisher identity.
Collection only permits HTTPS requests to `prices.azure.com` and
`pricing.us-east-1.amazonaws.com`; redirects and arbitrary URLs are rejected.

## On the harvester VM

`scripts/deploy-harvester.ps1` installs this package on the harvester VM through Azure Run Command:
`vm/install.sh` creates the `pricing-harvester` system user, a virtual environment under
`/opt/pricing-harvester`, `/etc/pricing-harvester/harvest.env` (price book account URL, identity client ID,
regions, and the versioned approval key ID), and `pricing-harvest.service`. The monthly schedule (`infra/modules/harvest-schedule.bicep`) tags
the VM with a request and starts it; `vm/harvest-start` runs only for a pending, unexpired request (`vm_signal.py`
reads the tags from instance metadata) and claims it first, then runs with the VM's managed identity:
`published_fetch` (downloads `publication-control/current.json` and the artifact it names, byte for byte),
`collect`, `validate`, `derive`, and `stage-blob`. When a snapshot is Published, `validate` gets
`--previous-artifact`, `--current-pointer`, and `--approval-key-id`, so it verifies the artifact's RS256
approval with the Key Vault key's public half before trusting it, and the SkuMap rate diff is against the
extract derived from that artifact. Only when nothing is Published does it declare `--bootstrap` and diff
against the packaged baseline extract. `vm/harvest-finish` records the result on the VM tag and powers off. Files under `vm/` must keep LF
line endings (`.gitattributes`).

## Deriving the rate extract

The pricing engine reads a small rate extract (`samples/pricebook_seed_v1.json` shape), not the whole
snapshot. `derive` builds it from a snapshot using `rate-extract-spec.json`:

- Every SkuMap rate key names its rows with explicit equality selectors (provider, service, term, unit,
  optional product family, and dimensions such as `armSkuName` or `usagetype`). The region comes from the
  snapshot scope, which must equal the spec scope.
- Each selector must match **exactly one** row. Zero or several matches fail the run and list every
  offending selector; the cheapest candidate is never chosen. For example, Premium SSD v2 IOPS and
  throughput are tiered meters, so their selectors name the billed tier (`tierMinimumUnits`).
- Values are computed with `decimal` only: a row price, a difference (RHEL minus Linux), a unit scaling
  (hourly to monthly, GiBps to MiBps), or a term normalization (upfront / 26,280 hours plus the recurring
  hourly rate). A negative or non-finite value fails. Assumed rates (the Azure RHEL uplift) come from the
  spec, stay labeled `DemoAssumption`, and are never presented as harvested.
- `rate-extract-report.json` records the spec digest, the selected row for every selector, the exact and
  rounded values, the extract digest, and, with `--previous-extract`, a per-rate old/new/change/percent diff.

```powershell
# From a Validated run (writes rate-extract.json and rate-extract-report.json into the run)
python -m src.harvester derive --run-dir src\harvester\data\runs\demo-001 `
  --previous-extract samples\pricebook_seed_v1.json

# From a Published artifact (verifies its hash and approval first; needs HARVESTER_APPROVAL_HMAC_KEY)
python -m src.harvester derive --artifact src\harvester\data\published\trust-20260922e.pricebook.ndjson `
  --output-dir $env:TEMP\extract --previous-extract samples\pricebook_seed_v1.json
```

Deriving from the `trust-20260922e` rows in `tests/fixtures/` reproduces the approved demo extract byte
for byte (digest `496711e3…14f5`, the value in `samples/pricebook_extract_approval_v1.json`);
`tests/test_derive.py` checks this. The bundled extract names its approver "Sample Approver". With the
original signed artifact available locally, set `HARVESTER_FULL_ARTIFACT_TEST=1` to check that its rates
and manifest match apart from that name (about a minute). A run extract is `Validated` with no publisher; approval and
publishing bind it. Changing regions or instance sizes means editing the spec, and
`scripts/deploy-harvester.ps1` refuses regions that differ from the spec scope.

## Phase-2a private Blob staging (not publication)

After `validate`, optionally copy that **Validated** run into an existing **private** Blob container:

```powershell
python -m src.harvester stage-blob `
  --run-dir src\harvester\data\runs\demo-001 `
  --account-url $env:HARVESTER_BLOB_ACCOUNT_URL `
  --credential-mode local
```

The account URL is always explicit; `--container` defaults to `staged-runs`. The
`publication-control` and `published-pricebooks` containers are not valid staging targets.
Use `--credential-mode managed-identity` (the default) when deployed; optionally provide
`--managed-identity-client-id` for a user-assigned identity. Local mode explicitly uses
`DefaultAzureCredential`; deployed mode uses `ManagedIdentityCredential` only. The identity needs
**Storage Blob Data Contributor** on `staged-runs` only, including read-container-properties and
create-blob permissions; grant these via Azure RBAC, not account keys or connection strings. The
container must already exist, with anonymous public access disabled. Account URLs must be Azure Blob
HTTPS account endpoints. No resources or roles are provisioned by this command.

The run must also contain `rate-extract.json` and `rate-extract-report.json` from `derive`; the command
refuses to stage an extract that does not bind this run (its digest, source snapshot and content hash, stage
manifest digest, `Validated` status, and no publisher). It then re-derives the extract from the
validated rows with the installed spec (and `--previous-extract`, for the diff) and refuses any difference, so
an extract and report rewritten together are still caught. The receipt records the extract digest.
The command checks the validated manifest binding and SHA-256/row count before transfer. It streams
canonical rows (including multi-GB snapshots) and metadata into exclusive, non-overwriting names under
`staging/<snapshotId>/<attemptId>/`; each retry uses a fresh attempt prefix, so an interrupted
transfer cannot strand the validated run. The adapter reads each uploaded object back to verify
its SHA-256 and length against the local artifact before writing `receipt.json` **last** with the
run identifiers, digests, byte lengths, verified ETags, validation timestamp, and baseline metadata.
A duplicate name, tamper, public container,
missing RBAC permission, or interrupted upload fails without a completion receipt. Partial objects
remain for operator investigation; retry the same validated run after resolving the issue.
Exclusive create prevents this adapter from overwriting names. Blob versioning and 30-day soft
delete aid recovery, but **the collector identity has delete/write permissions on staging**;
this is not WORM or protection from a compromised collector. Consumers must ignore attempts
without a receipt and independently verify the full bytes and ETags rather than trust metadata.

**The harvester never publishes to Blob.** `stage-blob` does not approve, publish, change
`current.json`, or write the published container; the `publish` command is filesystem-only. Blob
publication is the API's (see `src/api/README.md`): it reuses `build_published_manifest` and
`published_header` from `snapshot.py`, so the Published artifact is byte-compatible with
`verify_published_artifact`.

Every verifying command (`validate`, `approve`, `publish`, `derive --artifact`, `reconcile`) accepts
`--approval-key-id <versioned Key Vault key URL>` (with `--verifier-identity-client-id` for a managed
identity) to verify RS256 approvals signed by the web app. Without it, approvals must be HMAC-signed with
`HARVESTER_APPROVAL_HMAC_KEY`. `approve` also accepts the web app's `{schemaVersion, runId, record}` wrapper.

`published_admission.admit_published_blob` is a **read-only preparation path** for a future
published snapshot. It requires an explicit trusted approval verifier and either a WORM verifier
or an opt-in `mutable-pilot` storage-policy verifier (versioning plus soft delete) and an independent
freshness/rollback verifier backed outside the mutable publication account. The non-production labels are
bound into the human-signed approval record and copied into the Published manifest, allowing an
already staged, validated run to be approved without reharvesting. The deployed API does not call
it; the API verifies Published snapshots itself (see `src/api/README.md`). Given the required verifiers, it bounds the pointer and artifact downloads,
checks Blob ETags, reuses the
canonical published-artifact verifier, enforces the `SnapshotApprover` and `SkuMapReviewer`
roles and nonempty actor IDs even when a different signature verifier is supplied, and rejects pointer or content mismatches. It uses
an operator-provided scratch directory and removes temporary files after verification.
Test fixtures use the filesystem HMAC approval only to exercise the admission contract:
that prototype is **not** an Entra approval mechanism or permission to publish.

The web app records the approval in `publication-control/approvals/{snapshotId}/approval.json`,
signed with the Key Vault key (see `src/api/README.md`). Approval records may carry optional fields:
`extractDigest` (the approved rate extract's SHA-256), `runId` (the staged run), `evidenceDigest` (the
SHA-256 of that run's canonical receipt), and `keyId` (the signing key). A record with `keyId` must carry
all three bindings. `publish` recomputes the run's `rate-extract.json` digest and refuses a mismatch, then
copies the fields into the Published manifest as `approvedExtractDigest`, `approvedStagedRunId`,
`approvedEvidenceDigest`, and `approvalKeyId`; `verify_published_artifact` rebuilds and checks them, so
the bindings survive publication.

The web/API demo uses a small source-controlled rate extract from authenticated snapshot
`trust-20260922e`. It remains explicitly non-production and calls out the Azure RHEL software-price
assumption, which is outside the current Azure Retail Prices harvest scope.

`coverage-matrix.json` records which required meter categories are covered by each feed. Categories without
an approved source remain `UnpricedUnlessExcluded`; the harvester does not turn missing shared-platform,
load-balancer, security, observability, or support pricing into zero.

## Reconciliation

```powershell
python -m src.harvester reconcile `
  --artifact src\harvester\data\published\demo-001.pricebook.ndjson `
  --output src\harvester\data\reports\demo-001.json
```

The representative POC report uses approved explicit selectors for a 4-vCPU/16-GiB Linux VM and 200 GB of
block storage. It records exact source row IDs, units, and arithmetic and requires at least one
line where the Azure price is higher, so the check covers both directions. The Committed view uses the pinned defaults: the three-year, all-upfront, 100%-utilized AWS
Compute Savings Plan and the Azure three-year savings-plan rate. It never selects the cheapest candidate
dynamically. The Enterprise view records its common discount and the additional provider-specific discount
gap that would change which total is lower.
