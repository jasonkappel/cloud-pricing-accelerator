# Cloud Pricing Accelerator API

FastAPI application shell for the public-list run-rate benchmark.

## Run locally

Use Python 3.12 and install the dependencies from `requirements.txt`, then run:

```powershell
$env:PORT = "3000"
$env:AUTH_MODE = "local"
$env:LOCAL_PRINCIPAL_OID = "00000000-0000-0000-0000-000000000001"
$env:LOCAL_PRINCIPAL_NAME = "Local Developer"
$env:LOCAL_PRINCIPAL_ROLES = "Estimator"
uvicorn main:app --host 0.0.0.0 --port $env:PORT
```

Production command:

```powershell
gunicorn -w 1 -k uvicorn.workers.UvicornWorker main:app --bind "0.0.0.0:$env:PORT"
```

## Sign-in and roles

`AUTH_MODE` is required; a missing or unknown value fails startup.

- `appservice` (Azure): App Service Authentication validates the Entra access token (tenant, audience,
  and calling application) before the request arrives, strips client-supplied `X-MS-*` headers, and
  injects `X-MS-CLIENT-PRINCIPAL`. The API reads the tenant, object ID, name, and roles from that header.
  If the platform has not set `WEBSITE_AUTH_ENABLED=True`, every `/api` route returns 503, because the
  header could otherwise be forged. A missing or unreadable principal returns 401.
- `local` (developer machines): the principal comes from `LOCAL_PRINCIPAL_OID`, `LOCAL_PRINCIPAL_NAME`, and
  comma-separated `LOCAL_PRINCIPAL_ROLES` (optional `LOCAL_PRINCIPAL_TENANT`). Startup fails if any Azure
  hosting variable (for example `WEBSITE_SITE_NAME` or `IDENTITY_ENDPOINT`) is present. CORS is enabled only
  in this mode.

Roles are app roles on the `cloud-pricing-api` registration and are never inferred: `Estimator` is
required for every `/api/applications` and `/api/intakes` route (403 otherwise). `SnapshotApprover` and
`SkuMapReviewer` gate the price book approval steps below. `/healthz` and `/readyz` stay anonymous.
`GET /api/me` returns the signed-in `name`, `objectId`, `tenantId`, and `roles`; it and
`/api/capabilities` need sign-in but no role.

The verified identity is recorded additively as `application.created_by` and `gap.resolved_by_principal`
(`tenant_id`, `object_id`, `name`). The typed `resolved_by` ("Confirmed by") stays a separate attestation.
Both are part of the run-hash input.

## Price book approval

The harvester stages each run under `staging/{snapshotId}/{runId}/` and cannot approve or publish. The
API shows staged runs to a signed-in SkuMapReviewer or SnapshotApprover, records their decisions, and
publishes an Approved run when a SnapshotApprover asks. With `PRICEBOOK_SOURCE=published-blob` (the
Azure deployment's default), estimates then price from the current Published snapshot, as described below.

`PRICEBOOK_APPROVAL_MODE` selects the backend; a missing value means `off`, and any invalid setting fails
startup.

| Mode | Settings | Use |
|---|---|---|
| `off` | none | `GET /api/price-book/staged` returns `configured: false`; decisions return 503. |
| `azure` | `PRICEBOOK_BLOB_ENDPOINT` (https blob account endpoint), `APPROVAL_SIGNING_KEY_ID` (a versioned Key Vault key URL), optional `PRICEBOOK_STAGING_CONTAINER` (`staged-runs`), `PRICEBOOK_CONTROL_CONTAINER` (`publication-control`), and `PRICEBOOK_PUBLISHED_CONTAINER` (`published-pricebooks`); the three must differ | The API's system-assigned managed identity reads staging, signs with the Key Vault key (RS256), writes records under `approvals/` and the `current.json` pointer in the control container, and creates Published artifacts. |
| `local` | `PRICEBOOK_LOCAL_ROOT` (an existing folder holding `<container>/...`), `APPROVAL_HMAC_KEY` (32+ characters) | Developer machines only: requires `AUTH_MODE=local` and no Azure hosting variables. HMAC, compatible with the harvester's test verifier. |

Endpoints (any of the three roles can read; each decision needs its own role):

- `GET /api/price-book/staged`: `{configured, runs}` with each run's state: `AwaitingSkuMapReview`,
  `AwaitingApproval`, `Approved`, `Published`, or `Blocked` (with `problems`).
- `GET /api/price-book/staged/{snapshotId}/{runId}`: the verified run: counts, scope, validation report,
  coverage, the rate-extract diff (rates as the report's exact strings), the digests to be signed, the
  review, approval, and `publication` (`artifact`, `current`, `currentSnapshotId`, `publishedAt`,
  `publishedBy`), and server-computed `actions` (`canReview`, `canApprove`, `canPublish`, `waiting`).
- `POST .../skumap-review` (SkuMapReviewer) and `POST .../approval` (SnapshotApprover): body
  `{stageManifestDigest, extractDigest, evidenceDigest, skuMapDigest, attested: true}`. The digests must
  equal what the server just verified, or the request returns 409; unknown fields return 422.
  `evidenceDigest` is the SHA-256 of the canonical staging receipt, which lists every staged file's
  SHA-256, so it covers everything the page showed.
- `POST .../publish` (SnapshotApprover, any member): body `{evidenceDigest, attested: true}`. The run must
  be Approved, its `evidenceDigest` unchanged, and `publication-control/current.json` still the baseline
  the run was validated against (its SHA-256 and snapshot ID; no pointer for a bootstrap run); otherwise
  409. The API re-verifies the approval signature, builds the Published manifest with the harvester's own
  `build_published_manifest` (packaged with the API), and assembles
  `published-pricebooks/<snapshotId>.pricebook.ndjson`: the manifest line, then the staged rows copied
  server-side (Put Block From URL, each block pinned to the rows' receipt ETag), committed only if the
  artifact does not exist. A write-once `approvals/<snapshotId>/publication-intent.json` then pins the
  artifact's ETag (plus requester, content hash, and predecessor). It then replaces `current.json` by
  compare-and-swap (`If-None-Match: *` for the first publish, the read ETag afterwards) with
  `{snapshotId, contentHash, artifact, previousSnapshotId}`, and writes
  `approvals/<snapshotId>/publication.json` last. A lost pointer race returns 409; a retry reuses the
  artifact only under the intent's ETag, or, when no intent was written, only after its rows hash to the
  approved `contentHash`. A different artifact under the same snapshot ID blocks the run. If the record
  write fails, the run stays Approved (current, not Published); publishing it again, or publishing its
  successor, completes the record from the intent, the pointer, and the artifact ETag alone, without
  re-validating the staged run (which the harvester can still change). A successor is refused while its
  predecessor's record can't be completed. Large artifacts need a long request timeout (infra sets
  gunicorn `--timeout 600`).

Every read re-verifies the run: the receipt, each small file's SHA-256, size, and ETag, the rows file's
size and hash metadata, and the bindings between them: the rows file's SHA-256 must be the snapshot
`contentHash`, and every digest and the row count must be present. Before signing a decision, the API
streams the rows file under its receipt ETag and hashes the bytes, so it never signs on blob metadata
alone (publish recomputes the hash again). JSON is parsed strictly, and a validation report or rate diff with an unexpected shape blocks
the run instead of failing the request. Any mismatch blocks the run. The server builds each record from those verified values
and the signed-in identity, never from the request body. Both records sign the `runId` and
`evidenceDigest`, so an approval cannot be moved to another staged run. The approval also binds the reviewed SkuMap
digest and reviewer, and is write-once per
snapshot ID. A SkuMap change after review blocks an unapproved run. Records are marked
`evidencePolicy: MutablePilot` and `nonProduction: true` until WORM evidence exists. Stored approvals
verify with the harvester's `_verify_approval_record`.

Dependencies: `azure-identity`, `azure-keyvault-keys`, and `azure-storage-blob`, used only in `azure` mode.

The API exposes `/healthz`, `/readyz`, and the estimate workflow under `/api`:

- `POST /api/intakes` requires a bounded `Content-Length`, rejects unsafe OpenXML/XML and worksheet
  dimensions before normalization, and performs blocking workbook work outside the event loop.
- `POST /api/applications/{id}/gaps/{gap_id}/resolve` records typed clarification values.
- `GET /api/applications/{id}` returns the normalized facts and deterministic Comparison.
- `GET /api/applications/{id}/comparison/export` exports canonical JSON only after the CompletenessGate.
- `GET /api/applications/{id}/comparison/workbook` returns the uploaded `.xlsx` with `Pricing Summary`,
  `Pricing Detail`, `Pricing Assumptions`, `Pricing Exclusions`, and `Pricing Evidence` sheets added. Optional `view=azure|aws|both`
  picks a layout; without it the layout follows `PRESENT_AWS_PRICING`. The `aws` layout mirrors the
  Azure-only layout cell for cell with AWS amounts, and marks only AWS placeholder lines. Any other value
  returns 422.
- `GET /api/capabilities` also reports `applicationMaxAgeHours` (decimal string), `defaultMode` (`both`
  when `PRESENT_AWS_PRICING=true`, otherwise `azure`; a starting selection, not a limit), and `priceBook`
  (`snapshotId`, `pricedAsOf`, `nonProduction`, `stale`, `staleAfterDays`). `stale` is true only for a
  Published harvest more than 30 days old; frozen demo sets never go stale.

The repository retains Applications for `APPLICATION_MAX_AGE_HOURS` (a positive decimal, default `1`, at
least one minute and at most `8760`); an unset or empty value uses the default, and any other invalid value,
including whitespace only, fails API startup. It limits retained source workbooks to 100 MiB, caches each
deterministic Comparison, and returns HTTP 429 with `Retry-After` when full. Create and Gap-resolution
updates calculate before commit so failed pricing cannot partially mutate a record.

Additive display-support fields (all `decimal`, computed in the engine, and covered by the run hash):

- Comparison `verdict_confidence`: `Draft` (DraftBenchmark), `Placeholder` (ReviewBaseline with at least
  one priced `demo_assumption` line), or `Final`.
- Comparison `headline_delta_percent`: `headline_delta` as a percentage of the higher List total, in
  ReviewBaseline only. It equals the 0% List-parity breakeven point.
- Comparison `assumption_sensitivities`: one entry per placeholder component (today, the Azure RHEL
  uplift) with its provider, line ids, assumed List monthly amount, and the List totals, cheaper cloud, and
  delta with that amount at zero (`at_zero`) and doubled (`at_double`), plus whether either changes the
  cheaper cloud. An unmapped placeholder component fails closed.
- `GET /api/applications` rows add `expires_at`, `open_question_count`, `first_open_prompt`,
  `verdict_confidence`, `cheaper_cloud`, `headline_delta`, `headline_delta_percent`, both monthly totals,
  `priced_as_of`, `excluded_cost_categories` (so any figure on a list row shows what is not included), and
  `placeholder_providers` (the clouds with a priced placeholder line).
- Application detail adds `expires_at` and `intake_summary` (server count and vCPU, database count,
  storage count and allocated GB, open question count), so the browser never sums unit facts.

The evidence JSON export carries every comparison field, including the three display-support fields above,
and is enough on its own to recompute `run_hash`. The run hash input leaves out `application.comparison_state`
because that value is the engine's own prior output, not an input. The run hash also binds the outputs
(state, `can_export`, both monthly totals, `headline_delta`, `cheaper_cloud`, cost drivers, and the
ExcludedCostLedger), so an edited result no longer matches its hash.

The priced workbook preserves the original Intake sheets, which carry no formulas: intake rejects a
formula in any package part (parsed, so the encoding can't hide it), including conditional-formatting
and data-validation rules, table totals functions, and defined names other than a plain print area,
print titles, or filter range. It accepts
only `.xml` and `.rels` parts, requires the workbook at `xl/workbook.xml`, and requires every sheet to
point to a worksheet under `xl/worksheets/`. The export refuses a source workbook that still has formula
cells, conditional formatting, data validation, calculated table columns, or defined names, returning
422. Monthly extensions and totals are Excel formulas;
approved rates, assumptions, snapshot identity, source hash, SkuMap approval, and run hash are visible in
the added sheets. Formula totals are reconciled to authoritative server-computed totals. Unpriced and
excluded components remain visible rather than being treated as zero. The export rejects drawings, charts,
and pivot content rather than silently losing unsupported workbook objects during export.

Pricing uses the digest-approved SkuMap and one of two PriceBook sources, chosen by `PRICEBOOK_SOURCE`.
`published-blob` (the Azure deployment's default) prices from the current Published harvest. It reads
`current.json` and the publication record the API wrote, checks the artifact's ETag is the one published,
and verifies the artifact's manifest line with the harvester's own check against this deployment's approval
key. The signed approval binds the rate extract digest and the SkuMap digest: the staged `rate-extract.json`
prices only if its digest matches, and only while the API's SkuMap is the one that was approved. A snapshot
whose `pricedAsOf` is more than `PRICEBOOK_MAX_AGE_DAYS` (default 45) old refuses to price. The whole chain
is verified again every `PRICEBOOK_REFRESH_SECONDS` (default 60), so a moved pointer loads the new snapshot
and changed evidence stops pricing even under an unchanged pointer; each comparison prices from one
snapshot. A pointer to a Published snapshot that a later publication record supersedes is refused
(rollback guard), as is a pointer whose retained versions show it left this snapshot and came back, two
publications that each replaced nothing, or any publication record that is missing, malformed, or (from
blob versioning) deleted or overwritten. Publishing refuses to rebuild a missing pointer once anything
was published. The guard holds only while that history can't be purged: an account owner who deletes
blob versions can still roll back until WORM retention is on. To go back to earlier prices,
publish a new harvest. Any failure returns 503 with the reason, never demo rates or a stale snapshot, and is retried
after 5 seconds. `/api/capabilities` refuses the same snapshot a comparison would refuse. Comparisons
already created keep the snapshot they were priced with (it is stamped on them) until the Application
expires. `demo-extract` (the local default) prices from the clearly labeled demo extract of authenticated
snapshot `trust-20260922e`, whose separate approval record binds the exact extract digest to the source
snapshot ID and hash; pricing fails closed if either changes. Any other value fails at startup. Under both
sources the RHEL software uplift remains a visible demo assumption because that charge is outside the
current Azure Retail Prices harvest scope. Published snapshots stay labeled non-production until WORM
retention exists. Windows AHB/BYOL and SQL Server
license labels open a typed material eligibility Gap covering Software Assurance, AHB, AWS License
Mobility, license vintage, and passive-secondary use. Dedicated Host and SQL Server price mappings remain
fail-closed until approved provider meters are added. The API also returns two-sided breakeven sensitivity
for matching public List, Savings Plan, and Reservation bases at 0%, 10%, and 20% reference discounts.
These values are workload-isolated hypothetical sensitivities, not EA, EDP, private-offer, or contracted
prices. `PRESENT_AWS_PRICING` is a strict `true`/`false` presentation flag and defaults to `false`;
the API and evidence JSON retain the complete calculations regardless of the presentation mode. The
deployed configuration enables six public-price scenarios (both clouds' List, three-year
Savings Plan, and three-year Reservation) in the web and workbook while retaining commitment and
breakeven evidence. The workbook shows covered commitment rates and billed quantities beside
server-computed amounts and flags edited inputs that no longer reconcile. The rates come from the
current Published snapshot (or, with `PRICEBOOK_SOURCE=demo-extract`, the bundled demo extract), never
from contracted EA/EDP rates. Changing `PRESENT_AWS_PRICING` requires an app-setting update, not a full
infrastructure redeployment. WORM evidence and Foundry narration remain deferred.
