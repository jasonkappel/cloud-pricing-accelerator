# Prerequisites and human stop-points

These are the decisions and people that deployment scripts cannot supply. Clear them before you deploy,
and have the named humans ready for the two approval gates. Hand this checklist to whoever owns the Azure
tenant and the FinOps relationship.

## Before you deploy

### Azure subscription and landing zone
- [ ] An Azure subscription approved for this workload, and rights to create resource groups and role
      assignments in it (Owner, or Contributor plus User Access Administrator).
- [ ] A deployment region. The template creates its own VNet, subnets, private endpoints (Key Vault and
      the price book storage account), and private DNS zones. If your landing zone requires an existing
      hub VNet or central DNS, plan that change to `infra/` first.
- [ ] Agreement that the price book covers one fixed region pair: Azure East US 2 and AWS US East
      (N. Virginia). Other regions need a harvest spec and SkuMap change (see `src/harvester/README.md`).
- [ ] Outbound access approved for the harvester VM, through its NAT gateway, to `prices.azure.com` and
      `pricing.us-east-1.amazonaws.com`. The harvester is not fully private; it must call two public feeds.
      Its HTTP client allows only those hosts; add Azure Firewall if you need host-level egress control.

### Identity and roles (Entra ID)
- [ ] Someone who can create app registrations and grant tenant-wide admin consent (for example
  Application Administrator or Cloud Application Administrator) to run `scripts/setup-entra.ps1`, which
  creates the web and API app registrations.
- [ ] Users assigned to the app roles: Estimator for everyone who builds estimates, plus SkuMapReviewer and
      SnapshotApprover for the price book sign-offs. Sign-in requires a role assignment.
- [ ] Managed identities for every service-to-service call, with least-privilege RBAC on Blob and Key
      Vault.
- [ ] PIM and access reviews on the approval roles.

### Data governance
- [ ] Data-handling posture: no intake content, workbook notes, contacts, or PII may enter Application
      Insights or logs. Telemetry redacted. Customer-managed keys where required.
- [ ] Data residency, Blob versioning, and soft-delete policy confirmed. A compliance-approved
      WORM retention duration is a later prerequisite for frozen Decision evidence and production use,
      not for Published prices that are explicitly labeled non-production.
- [ ] Threat model and data classification for the workbook intake completed.

## The two credibility approval gates (name these humans now)

This tool harvests public prices from two clouds, unattended. That needs human accountability, not just
automation. Both roles below need a named human, and they are not optional. One qualified person may hold
both roles.

- [ ] **Snapshot approver.** Identify and authorize, through the SnapshotApprover app role, the named
      human (ideally your FinOps lead) who approves each Published PriceBook before it can price.
- [ ] **SkuMap reviewer.** Identify and authorize, through the SkuMapReviewer app role, the AWS-qualified
      reviewer who signs the mapping version's content digest.

## Ongoing operational owners (name these before go-live)

- [ ] Refresh owner: watches the monthly harvest, receives the failed-harvest alert (set `alertEmail` at
      deploy time), and follows `docs/OPERATOR-GUIDE.md` to recover. A failed harvest leaves the current
      Published snapshot in place; pricing stops (by design) only when that snapshot is past its maximum
      age (`PRICEBOOK_MAX_AGE_DAYS`, default 45) or no longer matches the approved SkuMap.
- [ ] SkuMap review owner: who re-reviews the mappings a feed change touches (route the change to the
      affected SkuMaps, not a full-catalog re-approval each cycle).
- [ ] Retention/cost owner: who owns the snapshot + evidence retention policy and the storage cost as the
      full-catalog snapshots accumulate.

## Cost of the footprint

This tool prices cloud workloads, so price its own. The README's "Monthly cost" section estimates one
environment (App Service, SQL, versioned Blob, on-demand harvester VM, private endpoints, Key Vault,
monitoring). Confirm it for your region and agreements, and right-size the non-production environments.

## Day-to-day operation

Once deployed, the monthly harvest, review, and publish loop is in [`OPERATOR-GUIDE.md`](OPERATOR-GUIDE.md).
