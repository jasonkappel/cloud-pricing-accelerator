# Operator guide: the monthly price harvest

Estimates are priced from one Published price book snapshot. A new snapshot comes only from the in-tenant
price harvester. A person reviews it, approves it, and publishes it on the web app's **Price book** page.
This guide covers that monthly loop. The background is in `infra/README.md` (Harvest schedule, Snapshot
approval), `src/harvester/README.md`, and `src/api/README.md` (Price book approval).

`<rg>`, `<harvesterVmName>`, `<apiAppName>`, and the Logic App below are the deployment outputs
`resourceGroupName`, `harvesterVmName`, `apiAppName`, and `harvestScheduleName` (`az deployment sub show
--name <deployment> --query properties.outputs`).

## The monthly loop

1. **Harvest (automatic).** A Logic App starts the harvester VM on day `harvestMonthDay` (default 2) at
   `harvestHourUtc` (default 06:00 UTC). The VM collects Azure and AWS public list prices, validates them,
   derives the rate extract, stages the run in the `staged-runs` container, and powers off. A run takes
   about 20 to 30 minutes. The Logic App then deallocates the VM.
2. **SkuMap review (SkuMapReviewer).** Open **Price book**, pick the run under **Staged snapshots**
   ("Waiting for SkuMap review"), check it (see "Read a staged snapshot"), tick the statement, and select
   **Record SkuMap review**.
3. **Approve (SnapshotApprover).** The run now shows "Waiting for approval". Check it again, tick the
   statement, and select **Approve snapshot**.
4. **Publish (SnapshotApprover).** Tick the statement and select **Publish snapshot**. It can take a few
   minutes. New estimates then price from this snapshot. The **Price book** card on the home page shows the
   rate extract's ID, which is the harvest's snapshot ID followed by `-demo-extract-v2`, and **Prices as
   of**, the date the prices were captured.

The review and the approval record the person's name and sign-in ID and are signed with the deployment's
Key Vault key; the publication record stores the publisher's name and sign-in ID. Nothing publishes on its
own, and there is no refresh button.

If publishing is interrupted after the new snapshot became current, the run says "This snapshot is current,
but publishing did not finish". Pricing stops until the record is complete: open the same run and select
**Publish snapshot** again. Don't harvest again for this.

Publish every month. Age counts from the capture date (**Prices as of**), not the publish date. The home
page flags the current snapshot as stale once it is more than 30 days old, and pricing stops (HTTP 503)
once it is more than `pricebookMaxAgeDays` old (default 45). If a month's harvest fails or isn't
published, that leaves about two weeks after the next one was due.

### The first snapshot

A new deployment has nothing Published, so pricing returns 503 until the first snapshot is Published. The
first harvest has no previous snapshot to compare with ("First snapshot, so there is no row-level
comparison"), and its rate changes are compared with the packaged baseline extract. Don't wait for the
schedule: run it now (see "Run a harvest now"), then review, approve, and publish it.

## Who approves

Sign-in roles are app roles on the `cloud-pricing-api` enterprise application (**Enterprise applications >
cloud-pricing-api > Users and groups**):

| Role | Does |
|---|---|
| `SkuMapReviewer` | Records the SkuMap review: the mappings from neutral units to priced meters still hold for this snapshot. Should know AWS and Azure pricing. |
| `SnapshotApprover` | Approves the snapshot and publishes an approved one. |
| `Estimator` | Uploads and views estimates. Cannot sign off snapshots. |

One person may hold both sign-off roles. The review and the approval are still recorded separately. A
two-person rule is deferred to v2.

## Read a staged snapshot

Select **Open** on a staged run. Each time it loads, the page re-checks the staged files against their
receipt; before a review or approval is signed, the API also re-hashes the full price rows.

- **Problems box (red).** The run is **Blocked** and cannot be reviewed, approved, or published. Blocking
  problems include a changed or missing staged file, a validation failure, missing coverage, a SkuMap that
  changed after review, or another run of the same snapshot already approved. Don't try to fix a Blocked
  run; harvest again.
- **Captured / Validated / Price rows / Regions.** When the prices were read, how many rows, and which
  Azure and AWS regions. Check that the regions are the approved ones.
- **Previous snapshot.** The Published snapshot this run was compared with.
- **Validation report.** Status must be `Validated`. For a refresh it counts price rows added, retired, and
  changed since the previous snapshot, and the number of **material rate changes**: rows whose price moved
  by 10% or more. Material changes do not block approval. A large count, or many rows retired, deserves a
  closer look at the rate changes below.
- **Coverage.** Each required catalog check must say "found". A missing one blocks the run.
- **Rate changes.** The rates the pricing engine actually uses (one per SkuMap rate key), compared with the
  previous rate extract: before, after, change, change %, and status (`changed`, `added`, `removed`,
  `unchanged`). Rates are shown as the engine uses them, rounded to each rate's decimal places in
  `rate-extract-spec.json`, so a very small movement can show as `unchanged`; the unrounded values are in
  the run's `rate-extract-report.json` (`selections.<rate key>.exact`). A rate marked "placeholder price"
  (the Azure RHEL uplift) is not harvested. Ask why for any large change, and for any `removed` rate: a
  removed rate means estimates that need it will not price.
- **Sign-off and digests.** Who reviewed, approved, and published, and the digests being signed. The
  sign-off is refused (HTTP 409) if any digest changed since the page loaded; reload and check again.

Publishing is refused (409) if the current price book changed since the run was validated, for example
because another run was published in the meantime. Harvest again so the new run compares with the current
snapshot.

A SkuMap change (a new `samples/skumap_seed_v1.json` deployed with the API) stops the current Published
snapshot from pricing ("approved against a different SkuMap"): estimates return 503 until a snapshot
reviewed against the new SkuMap is Published. A staged run not yet reviewed can be reviewed, approved, and
published as usual; one reviewed but not yet approved is Blocked ("The SkuMap changed after it was
reviewed"), so harvest again. Have a staged run ready before any SkuMap change.

## A failed harvest

The Logic App's **Runs history** is the authority: a run that is not **Succeeded** failed. It also raises
the alert `alert-logic-harvest-...-failed` (and emails `alertEmail`, if it was set at deployment). Open the
failed run to see why:

- `VmAlreadyRunning`: the VM was running when the run started (someone was using it). Nothing was
  requested.
- `HarvestNotStarted`: the power state could not be read or the request could not be written. The VM was
  not started.
- Otherwise the VM ran. Read the VM tags (`az vm show -g <rg> -n <harvesterVmName> --query tags`) and match
  the run ID in `harvest-result` to the failed run; the tag can still hold an earlier run's result.
  `failed <run id> <step>` names the step that failed (`claim`, `disk-check`, `baseline`, `collect`,
  `validate`, `derive`, `stage`), with `:<snapshot>` after `disk-check`. A harvest that hits its 5-hour
  limit also records its step. `failed <run id> no-result` means the VM recorded no result: it never
  claimed the request, or it was still running when the Logic App gave up after 6 hours and deallocated
  it.

Before assuming nothing was staged, check the **Staged snapshots** list: a run can fail after staging
finished. Then read the harvester's own log with Run Command. Starting the VM by hand does not harvest,
unless an unexpired `pending` request is still on the VM (for example after `HarvestNotStarted`): if
`harvest-result` is `pending`, wait until the time in `harvest-request-expires` has passed first.

```powershell
az vm start -g <rg> -n <harvesterVmName>
az vm run-command invoke -g <rg> -n <harvesterVmName> --command-id RunShellScript `
  --scripts 'journalctl -u pricing-harvest --no-pager -n 200' --query "value[0].message" -o tsv
az vm deallocate -g <rg> -n <harvesterVmName>
```

Always deallocate it again. A running VM costs about $140 a month and blocks the next scheduled run.

Common causes:

- A price API outage or throttling: run it again.
- A rate selector that no longer matches exactly one price row after a provider catalog change (the log
  lists each offending selector; the run fails at `derive`). Fix it in
  `src/harvester/rate-extract-spec.json`, have the SkuMapReviewer check it, run
  `scripts/deploy-harvester.ps1`, then run a harvest (see "Run a harvest now"). Once a snapshot is
  Published, the new selector must match **both** the new catalog and the Published snapshot, because each
  harvest re-derives the Published snapshot's extract with the installed spec to compare rates. A selector
  that matches only the new catalog fails every harvest at `validate`; that case needs an engineering
  change, not a rerun. Before the first publish, it only has to match the new catalog.

## Run a harvest now

1. Make sure the VM is stopped: `az vm show -g <rg> -n <harvesterVmName> -d --query powerState`. If it
   is running, check the Logic App's **Runs history** first: a run still in progress is a harvest, so
   wait for it. Deallocate (`az vm deallocate -g <rg> -n <harvesterVmName>`) only when no run is in
   progress and nobody is using the VM.
2. In the portal, open the Logic App (`harvestScheduleName`) and select **Overview > Run trigger**.
3. When the run succeeds, review, approve, and publish as above.

To change the schedule, redeploy the infrastructure with new `harvestMonthDay` (1 to 28) and
`harvestHourUtc` (0 to 23) values. After changing harvester code, run `scripts/deploy-harvester.ps1`: it
refuses a running VM and closes a `pending` request before it starts the VM. It does not harvest.

## Roll back

There is no repoint to an older snapshot. The API refuses a pointer to a Published snapshot that a later
one superseded. To replace a bad snapshot, harvest again and publish the new run. A harvest captures the
public list prices on its date, so it picks up a provider's correction. If the bad price is still live,
the new harvest will carry it too: wait for the provider, then harvest again.

To stop estimates from a bad snapshot in the meantime, stop the API app (`az webapp stop -g <rg> -n
<apiAppName>`): there is no fallback to demo rates or an older snapshot. Stopping it also discards every
estimate held in memory, and it takes down the Price book page. Start it again (`az webapp start`) to
review, approve, and publish the replacement; the bad snapshot prices again until you publish. Each estimate
is stamped with the snapshot it was priced from, so check the stamp on workbooks already downloaded.

The rollback guard relies on blob versioning. Deleting a pointer or publication-record version stops
pricing rather than restoring old prices (soft delete keeps deleted versions for 30 days). An account owner
who purges the pointer's history and the later publication records could still roll the pointer back until
WORM retention is on (see `docs/PREREQUISITES.md`). Published snapshots stay labeled non-production until
then.
