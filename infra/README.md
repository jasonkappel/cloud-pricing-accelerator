# infra

Bicep for the Cloud Pricing Accelerator. Deployment steps are in the [root README](../README.md#deploy-to-azure).

## Layout

- `main.bicep`: subscription-scope entry point. Creates one resource group and, inside it, Log Analytics,
  Application Insights, Key Vault, Storage, Azure SQL, a Linux B1 App Service plan, the API app (Python
  3.12), the web app (Node 24), a user-assigned managed identity used only by the web app for sign-in, the
  network (below), the price book storage account, the price harvester VM, and its monthly schedule (a
  Logic App). Both apps get App Service
  Authentication (`authsettingsV2`); it is enabled once the three auth parameters are set.
  `deployFunction` is locked to `false`.
- `main.parameters.json`: non-tenant defaults (`location`, `presentAwsPricing`, feature flags).
- `modules/`: one module per resource type, plus `role-assignments.bicep` (Key Vault access for the
  deployer) and `api-approval-access.bicep` (the API's approval permissions, below).
  `harvester-cloud-init.yaml` is the harvester VM's first-boot script.

## Parameters

| Parameter | Required | Meaning |
| --- | --- | --- |
| `location` | yes | Azure region for every resource. |
| `environmentName` | no (`pricing-dev`) | 3-12 lowercase letters, digits, or hyphens. Used in every resource name with a 6-character hash of the subscription, environment name, and region. |
| `resourceGroupName` | no (`rg-cloud-pricing-<environmentName>`) | Resource group to create or reuse. |
| `deployedBy` | yes | Name of the person or pipeline; recorded as a tag and as the SQL Entra admin display name. |
| `deployerObjectId` | yes | Entra object ID of the deployer (`az ad signed-in-user show --query id -o tsv`). |
| `deployerPrincipalType` | no (`User`) | `User`, `ServicePrincipal`, or `Group`. |
| `presentAwsPricing` | no (`false`; `main.parameters.json` sets `true`) | Sets the API's `PRESENT_AWS_PRICING`. |
| `authTenantId` | no (empty) | Entra tenant for sign-in. |
| `apiClientId` | no (empty) | Client ID of the `cloud-pricing-api` registration from `scripts/setup-entra.ps1`. |
| `webClientId` | no (empty) | Client ID of the `cloud-pricing-web` registration from `scripts/setup-entra.ps1`. |
| `harvesterAdminSshPublicKey` | yes | SSH public key for the harvester VM. Linux requires one, but no inbound path exists, so the private key is never used. Generate it once and keep the `.pub` file: Azure cannot change a VM's SSH key, so later deployments must pass the same key (the root README shows how to read it back from the VM). |
| `harvesterVmSize` | no (`Standard_D4s_v3`) | Harvester VM size. Must support Trusted Launch (Gen2) and SCSI disks: first boot finds the data disk at SCSI LUN 0, so NVMe-only sizes do not work. |
| `harvestMonthDay` | no (`2`) | Day of the month (1-28, UTC) the scheduled harvest runs. |
| `harvestHourUtc` | no (`6`) | Hour (UTC) the scheduled harvest starts. |
| `alertEmail` | no (empty) | Email notified when a scheduled harvest fails. Empty: the alert still fires in Azure Monitor. |
| `vnetAddressPrefix` | no (`10.60.0.0/24`) | VNet address space. Change it if it overlaps a network you will peer with. |

Sign-in turns on only when all three auth values are set. Until then the API has `AUTH_MODE=appservice` with
platform authentication off, so every `/api` call returns 503 (fail closed).

Outputs: `resourceGroupName`, `apiAppName`, `webAppName`, `webUrl`, `apiUrl`, `webIdentityPrincipalId`
(read by `scripts/setup-entra.ps1` for the federated credential), `authConfigured`, `harvesterVmName`, `harvestScheduleName`,
`harvesterIdentityClientId`, `pricebookStorageAccount`, `pricebookBlobEndpoint`, and `harvesterEgressIp`
(the NAT gateway address the public price feeds see).
`scripts/deploy-apps.ps1` reads them to deploy the code.

Always run `az deployment sub what-if` with the same arguments first and review every predicted change.

## Network

One VNet (`vnetAddressPrefix`, default `10.60.0.0/24`) with three subnets:

| Subnet | Size | Holds | Rules |
| --- | --- | --- | --- |
| `snet-app` | /26 | VNet integration for both apps (delegated to `Microsoft.Web/serverFarms`, `Microsoft.Web` service endpoint) | All app outbound traffic routes through the VNet, so the apps resolve the private DNS zones. |
| `snet-private-endpoints` | /27 | Private endpoints for Key Vault and the price book storage account | Inbound from the VNet only. |
| `snet-harvester` | /27 | The harvester VM's NIC (no public IP) | No inbound at all. Outbound: HTTPS to the private endpoints and to the internet (through the NAT gateway), plus the Azure WireServer. Everything else denied. No default outbound access. |

- **The API accepts traffic only from `snet-app`.** App Service access restrictions allow that subnet
  (identified by its service endpoint) and deny everything else, so a direct call to the API's public host
  name gets 403 before authentication runs. The web app reaches it through its VNet integration. The deploy
  (SCM) site keeps its own policy: Entra-only (basic authentication is disabled), so `deploy-apps.ps1` still
  works from your machine.
- **Private DNS:** `privatelink.blob.<storage suffix>` and `privatelink.vaultcore.azure.net`, linked to the
  VNet. Key Vault and both storage accounts have public network access disabled.
- **Harvester egress** uses a NAT gateway on its subnet only, so the VM has no public IP. NSGs cannot filter
  by host name; the harvester's HTTP client enforces its own allowlist of the two public price feeds. If
  host-level egress control is mandatory, add Azure Firewall (not included).
- The original evidence storage account (`st...`) keeps its container-level retention policy and has no
  private endpoint because nothing uses it yet. Container-level retention cannot coexist with blob
  versioning, which is why price books have their own account.

## Price harvester VM

- Ubuntu 24.04, Trusted Launch, `harvesterVmSize`, a user-assigned identity (`id-harvester-...`), a 64 GiB
  OS disk, and an independent 128 GiB Premium SSD data disk at LUN 0. Deleting the VM detaches the data disk
  rather than deleting it.
- The identity has Storage Blob Data Contributor on the `staged-runs` container only, Storage Blob Data
  Reader on `published-pricebooks` and `publication-control` (to validate each refresh against the current
  Published snapshot), Key Vault Reader on the `approval-signing` key only (its public half, to verify
  approvals; no crypto operations), and Tag Contributor on the VM itself to record its harvest result. It
  cannot write `published-pricebooks` or `publication-control`; publishing is the API's job after a
  signed-in human approves.
- First boot (`modules/harvester-cloud-init.yaml`) formats the data disk only if it is empty and
  unpartitioned, mounts it by UUID at `/var/lib/pricing-harvester`, and refuses anything unexpected. Every
  collection must first run `/usr/local/sbin/check-harvester-disk`. Cloud-init runs on first boot only, not on
  redeployment. Azure cannot change `customData` on an existing VM, so this file is frozen: editing it breaks
  redeployment over an existing VM. Change the VM through `scripts/deploy-harvester.ps1` (Run Command) instead.
- No inbound path exists. Administer it with Azure Run Command, for example:

  ```powershell
  az vm run-command invoke --resource-group <rg> --name <harvesterVmName> --command-id RunShellScript `
    --scripts 'cloud-init status --wait; /usr/local/sbin/check-harvester-disk; df -h /var/lib/pricing-harvester'
  az vm deallocate --resource-group <rg> --name <harvesterVmName>
  ```

- The VM starts running after deployment. `scripts/deploy-harvester.ps1` installs the harvester and
  deallocates it; the disks and NAT gateway still cost money while it is deallocated.
- Ubuntu's default Azure package mirror uses HTTP, which the NSG blocks; the installer points apt at
  `https://archive.ubuntu.com/ubuntu/`.

## Harvest schedule

`modules/harvest-schedule.bicep` creates a Consumption Logic App (`logic-harvest-...`) with a system identity.
Its permissions are scoped to the harvester VM: Tag Contributor, and a custom role
(`Harvest schedule VM power (<resource group>)`) that can only read, start, and deallocate the VM. It
deliberately lacks Virtual Machine Contributor, which would allow Run Command (root on the VM). Creating
the custom role needs Owner or User Access Administrator. One run at a time; the first run waits for the
schedule (no run on deployment). Each month it:

1. Fails with `VmAlreadyRunning` if the VM is not stopped or deallocated (someone is using it). If the power
   state can't be read, it fails without touching the VM.
2. Tags the VM `harvest-request=<run id>`, `harvest-result=pending`, and `harvest-request-expires` (one hour
   ahead), then starts it.
3. On boot, `pricing-harvest.service` reads the tags from instance metadata. Only a pending, unexpired
   request starts a harvest, so starting the VM by hand never harvests. The VM first claims the request
   (`harvest-result=running <run id>`), so a request starts at most one harvest. It then waits for the data
   disk, runs the disk check, `collect`, `validate`, `derive` (the rate extract and its diff against the approved
   baseline), and `stage-blob` as the unprivileged `pricing-harvester`
   user (bounded to 5 hours), records `harvest-result=succeeded|failed <run id> <detail>` (the harvester
   identity has Tag Contributor on the VM), and powers off.
4. Polls every 10 minutes for up to 6 hours (a failed poll just polls again), then deallocates the VM whatever happened and waits until it
   is deallocated.
5. Fails the run unless the tag says `succeeded` for this run. A request this run left open (`pending` or
   `running`) is closed as `failed <run id> no-result`, but only after a
   successful tag read; a result the VM recorded is never overwritten, and an unclosed request expires.

A failed run raises the `alert-logic-harvest-...-failed` metric alert (and emails `alertEmail` if set). A
failed run can still leave a complete staged run (for example, if staging finished but reporting did not);
check `staged-runs` before assuming nothing was staged. The result tag is an operational signal only;
approval verifies the staged bytes. Run logs go to Log Analytics; the VM's own log is
`journalctl -u pricing-harvest` (read it with Run Command). To harvest now, run the Logic App's trigger in
the portal (**Overview > Run trigger**) with the VM deallocated. `scripts/deploy-harvester.ps1` refuses a
running VM and closes any unfinished request before it starts the VM.

## Snapshot approval

The API records SkuMap reviews and snapshot approvals from the web app's Price book page (see
`src/api/README.md`). Key Vault holds an RSA 3072 key, `approval-signing` (sign and verify only, not
exportable); ARM creates it only if it is missing, so a redeploy keeps the version the API is pinned to
through `APPROVAL_SIGNING_KEY_ID`. `api-approval-access.bicep` gives the API's system-assigned identity:

- Key Vault Crypto User on that key only.
- Storage Blob Data Reader on `staged-runs`.
- A custom role on `publication-control` with blob read, write, and add only (no deletes, moves, tags, or
  container management), with an ABAC condition: writes only under `approvals/` and to `current.json`.
- The same custom role on `published-pricebooks`, to create Published artifacts. Append-only artifacts and
  the pointer compare-and-swap are enforced by the API's conditional writes, not by RBAC; versioning and
  soft delete keep every overwritten version recoverable.
- Creating the custom role needs Owner or User Access Administrator, as for the harvest schedule's VM power
  role. The API runs gunicorn with `--timeout 600` because publishing copies a large artifact.

The API reaches Key Vault and the price book account through their private endpoints over its VNet
integration.

## Rules

- Managed identity everywhere. No stored secrets beyond Key Vault; web sign-in uses a managed-identity
  federated credential instead of a client secret.
- Key Vault, both storage accounts, and SQL have public network access disabled. Key Vault and the price
  book account are reachable only through their private endpoints. The API uses them only for snapshot
  approval; estimates are still held in memory.
- Dev, test, and prod are separate environments (`environmentName`) with their own parameters.
- Published PriceBooks are non-production until WORM retention is on; versioning and soft delete are not immutability. The later
  production WORM policy duration is a compliance decision (see `docs/PREREQUISITES.md`); parameterize it,
  do not hard-code it.
