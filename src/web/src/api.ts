export type ComparisonState = "DraftBenchmark" | "ReviewBaseline";
export type GapStatus = "Open" | "Resolved";
export type GapKind =
  | "RuntimeHours"
  | "ApprovedRegions"
  | "StoragePerformance"
  | "LicenseEligibility";
export type LineStatus = "Priced" | "Unpriced" | "Excluded";
export type CloudName = "AWS" | "Azure";
export type VerdictConfidence = "Final" | "Placeholder" | "Draft";
export type WorkbookView = "azure" | "aws" | "both";
export type DefaultMode = "both" | "azure";

export interface Application {
  id: string;
  name: string;
  intake_file_name: string;
  comparison_state: ComparisonState;
  created_at: string;
}

export interface ApplicationSummary extends Application {
  expires_at: string;
  open_question_count: number;
  first_open_prompt: string | null;
  verdict_confidence: VerdictConfidence;
  cheaper_cloud: CloudName | null;
  headline_delta: string | null;
  headline_delta_percent: string | null;
  aws_monthly_total: string | null;
  azure_monthly_total: string | null;
  priced_as_of: string;
  excluded_cost_categories: string[];
  placeholder_providers: CloudName[];
}

export interface Capabilities {
  productLabel: string;
  applicationMaxAgeHours: string;
  defaultMode: DefaultMode;
  priceBook: {
    source: "demo-extract" | "published-blob";
    snapshotId: string;
    pricedAsOf: string;
    nonProduction: boolean;
    stale: boolean;
    staleAfterDays: number;
  };
  available: string[];
  deferred: string[];
}

export interface IntakeSummary {
  server_count: number;
  server_vcpu: number;
  database_count: number;
  storage_count: number;
  storage_allocated_gb: string;
  open_question_count: number;
}

export interface AssumptionStressScenario {
  multiplier: string;
  assumed_monthly_amount: string;
  aws_monthly_total: string;
  azure_monthly_total: string;
  cheaper_cloud: CloudName | null;
  headline_delta: string;
  changes_cheaper_cloud: boolean;
}

export interface AssumptionSensitivity {
  id: string;
  label: string;
  provider: CloudName;
  line_ids: string[];
  commercial_view: "List";
  assumed_monthly_amount: string;
  at_zero: AssumptionStressScenario;
  at_double: AssumptionStressScenario;
  changes_cheaper_cloud: boolean;
}

export interface Gap {
  id: string;
  unit_id: string | null;
  kind: GapKind;
  license_product: string | null;
  prompt: string;
  reason: string;
  material: boolean;
  status: GapStatus;
  raw_value: unknown;
  canonical_value: Record<string, unknown> | null;
  resolved_by: string | null;
  resolved_at: string | null;
}

export interface UnitSummary {
  id: string;
  name: string;
  environment: string;
}

export interface LineItem {
  id: string;
  service_definition_id: string;
  skumap_component_id: string;
  unit_id: string;
  unit_name: string;
  component: string;
  role: string;
  match_class: string;
  status: LineStatus;
  quantity: string | null;
  aws_quantity: string | null;
  azure_quantity: string | null;
  quantity_unit: string | null;
  aws_rate: string | null;
  azure_rate: string | null;
  aws_amount: string | null;
  azure_amount: string | null;
  aws_commercial: ProviderCommercialAmounts;
  azure_commercial: ProviderCommercialAmounts;
  higher_cloud: "AWS" | "Azure" | null;
  demo_assumption: boolean;
  formula: string;
  evidence: string;
  unpriced_reason: string | null;
  exclusion_policy_id: string | null;
}

export interface ProviderCommercialAmounts {
  list_rate: string | null;
  list_amount: string | null;
  savings_plan_rate: string | null;
  savings_plan_quantity: string | null;
  savings_plan_covered_amount: string | null;
  savings_plan_uncovered_amount: string | null;
  savings_plan_amount: string | null;
  reservation_rate: string | null;
  reservation_quantity: string | null;
  reservation_covered_amount: string | null;
  reservation_uncovered_amount: string | null;
  reservation_amount: string | null;
}

export interface CommercialScenario {
  id: string;
  provider: "AWS" | "Azure";
  label: string;
  offer: "List" | "SavingsPlan" | "Reservation";
  monthly_total: string | null;
  covered_monthly_total: string | null;
  uncovered_monthly_total: string | null;
  term_years: number | null;
  payment_option: string | null;
  utilization_percent: string | null;
  tenancy: string | null;
  public_price_only: boolean;
  available: boolean;
  unavailable_reason: string | null;
}

export interface BreakevenPoint {
  reference_discount_percent: string;
  target_discount_to_parity_percent: string;
  additional_discount_advantage_percent: string;
}

export interface BreakevenSensitivity {
  id: string;
  label: string;
  offer: "List" | "SavingsPlan" | "Reservation";
  aws_public_monthly_total: string | null;
  azure_public_monthly_total: string | null;
  reference_provider: "AWS" | "Azure" | null;
  target_provider: "AWS" | "Azure" | null;
  points: BreakevenPoint[];
  public_price_only: boolean;
  actual_contracted_price: boolean;
  workload_isolated: boolean;
  available: boolean;
  unavailable_reason: string | null;
  disclosure: string;
}

export interface LicenseAssessment {
  unit_id: string;
  product: string;
  azure_treatment: string;
  aws_treatment: string;
  blocks_pricing: boolean;
  evidence_urls: string[];
}

export interface CostDriver {
  line_id: string;
  component: string;
  delta: string;
  higher_cloud: "AWS" | "Azure";
  share_of_absolute_delta_percent: string;
}

export interface ExcludedCost {
  id: string;
  category: string;
  materiality: string;
  rationale: string;
  policy_id: string;
}

export interface AssumptionSet {
  currency: string;
  horizon_months: number;
  commercial_view: "List";
  commitment_term_years: number;
  commitment_payment_option: string;
  commitment_utilization_percent: string;
  commitment_tenancy: string;
  azure_region: string | null;
  aws_region: string | null;
  pricebook_snapshot_id: string;
}

export interface Comparison {
  state: ComparisonState;
  product_label: string;
  commercial_view: "List";
  priced_as_of: string;
  pricebook_snapshot_id: string;
  pricebook_content_hash: string;
  source_pricebook_snapshot_id: string | null;
  source_pricebook_content_hash: string | null;
  source_urls: string[];
  rate_sources: Array<{
    rate_key: string;
    source_type: string;
    provider: string;
    sku: string | null;
    meter: string | null;
    row_id: string | null;
    row_ids: string[];
    note: string | null;
  }>;
  skumap_content_digest: string;
  skumap_approval: {
    approver: string;
    approved_at: string;
    content_digest: string;
    non_production: boolean;
  };
  calculator_rule_version: string;
  presentation: {
    show_aws: boolean;
  };
  provisional: boolean;
  aws_monthly_total: string | null;
  azure_monthly_total: string | null;
  commercial_scenarios: CommercialScenario[];
  breakeven_sensitivities: BreakevenSensitivity[];
  license_assessments: LicenseAssessment[];
  headline_delta: string | null;
  headline_delta_percent: string | null;
  cheaper_cloud: "AWS" | "Azure" | null;
  verdict_confidence: VerdictConfidence;
  assumption_sensitivities: AssumptionSensitivity[];
  line_items: LineItem[];
  cost_drivers: CostDriver[];
  excluded_cost_ledger: ExcludedCost[];
  assumptions: AssumptionSet;
  run_hash: string;
  can_export: boolean;
  warnings: string[];
}

export interface ApplicationDetail {
  application: Application;
  compute_units: UnitSummary[];
  database_units: UnitSummary[];
  storage_units: UnitSummary[];
  gaps: Gap[];
  comparison: Comparison;
  expires_at: string | null;
  intake_summary: IntakeSummary | null;
}

export interface GapResolution {
  resolved_by: string;
  runtime_hours_month?: string;
  azure_region?: string;
  aws_region?: string;
  target_iops?: number;
  target_mbps?: string;
  active_software_assurance?: boolean;
  azure_hybrid_benefit_eligible?: boolean;
  aws_license_mobility_eligible?: boolean;
  acquired_before_2019_10_01?: boolean;
  passive_secondary?: boolean;
  passive_use_only?: boolean;
  perpetual_license?: boolean;
  eligible_product_version?: boolean;
  azure_sql_deployment_model?:
    | "SqlVm"
    | "SqlDatabaseProvisionedVCore"
    | "SqlManagedInstanceProvisionedVCore"
    | "SqlDatabaseServerless"
    | "SqlDatabaseDtu";
}

interface RuntimeConfig {
  apiBaseUrl: string;
}

const DOWNLOAD_URL_REVOKE_DELAY_MS = 60_000;

let runtimeConfigPromise: Promise<RuntimeConfig> | undefined;

function getRuntimeConfig(): Promise<RuntimeConfig> {
  if (import.meta.env.DEV) {
    const apiBaseUrl = import.meta.env.VITE_API_BASE_URL?.trim();
    if (!apiBaseUrl) {
      return Promise.reject(new Error("VITE_API_BASE_URL is required for local development"));
    }
    return Promise.resolve({ apiBaseUrl });
  }

  runtimeConfigPromise ??= fetch("/config.json")
    .then((response) => {
      if (!response.ok) {
        throw new Error(`Runtime configuration failed with status ${response.status}`);
      }
      return response.json() as Promise<RuntimeConfig>;
    })
    .then((config) => {
      // An empty apiBaseUrl means the web server proxies /api on the same origin.
      if (typeof config.apiBaseUrl !== "string") {
        throw new Error("Runtime configuration is missing apiBaseUrl");
      }
      return { apiBaseUrl: config.apiBaseUrl.trim() };
    })
    .catch((error: unknown) => {
      runtimeConfigPromise = undefined;
      throw error;
    });

  return runtimeConfigPromise;
}

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export function isNotFound(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

let refreshPromise: Promise<boolean> | undefined;

function refreshSignIn(): Promise<boolean> {
  refreshPromise ??= fetch("/.auth/refresh", {
    headers: { "X-Requested-With": "XMLHttpRequest" },
    redirect: "manual",
  })
    .then((response) => response.ok)
    .catch(() => false)
    .finally(() => {
      window.setTimeout(() => {
        refreshPromise = undefined;
      }, 0);
    });
  return refreshPromise;
}

export function signInUrl(): string {
  const back = `${window.location.pathname}${window.location.search}${window.location.hash}`;
  return `/.auth/login/aad?post_login_redirect_uri=${encodeURIComponent(back)}`;
}

export const SIGN_OUT_URL = "/.auth/logout?post_logout_redirect_uri=%2F";

/** True on the deployed, same-origin app where App Service Authentication handles sign-in. */
export async function usesPlatformSignIn(): Promise<boolean> {
  const { apiBaseUrl } = await getRuntimeConfig();
  return apiBaseUrl === "";
}

function needsSignIn(response: Response): boolean {
  return response.status === 401 || response.type === "opaqueredirect";
}

/** Calls the API. On the same-origin (signed-in) deployment a lapsed session is refreshed once,
 * then the whole page goes to sign-in. The browser never holds a token itself. */
async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const { apiBaseUrl } = await getRuntimeConfig();
  if (apiBaseUrl) {
    return fetch(`${apiBaseUrl}${path}`, init);
  }
  const headers = new Headers(init?.headers);
  headers.set("X-Requested-With", "XMLHttpRequest");
  const send = () => fetch(path, { ...init, headers, redirect: "manual" });
  let response = await send();
  if (!needsSignIn(response)) {
    return response;
  }
  if (!await refreshSignIn()) {
    window.location.assign(signInUrl());
    throw new ApiError("Your sign-in has expired. Redirecting to sign in.", 401);
  }
  response = await send();
  if (needsSignIn(response)) {
    // A fresh session that is still refused points at access or configuration, not an expired
    // sign-in, so redirecting again would loop.
    throw new ApiError(
      "You are signed in, but the pricing API did not accept your sign-in. Ask an administrator to check your access.",
      401,
    );
  }
  return response;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await apiFetch(path, init);
  if (!response.ok) {
    let detail = `Request failed with status ${response.status}`;
    try {
      const payload = await response.json() as { detail?: string };
      detail = payload.detail ?? detail;
    } catch {
      // The status remains the useful error when a non-JSON proxy response is returned.
    }
    throw new ApiError(detail, response.status);
  }
  return response.json() as Promise<T>;
}

function triggerDownload(blob: Blob, fileName: string) {
  const href = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = href;
  link.download = fileName;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(href), DOWNLOAD_URL_REVOKE_DELAY_MS);
}

export function listApplications(signal?: AbortSignal): Promise<ApplicationSummary[]> {
  return request<ApplicationSummary[]>("/api/applications", { signal });
}

export interface SignedInUser {
  name: string;
  objectId: string;
  tenantId: string;
  roles: string[];
}

export function getMe(signal?: AbortSignal): Promise<SignedInUser> {
  return request<SignedInUser>("/api/me", { signal });
}

export function getCapabilities(signal?: AbortSignal): Promise<Capabilities> {
  return request<Capabilities>("/api/capabilities", { signal });
}

export type StagedRunState =
  | "AwaitingSkuMapReview"
  | "AwaitingApproval"
  | "Approved"
  | "Published"
  | "Blocked";

export interface StagedRunSummary {
  snapshotId: string;
  runId: string;
  state: StagedRunState;
  stagedAt: string | null;
  capturedAt: string | null;
  problems: string[];
}

export interface StagedRateChange {
  rateKey: string | null;
  status: string | null;
  old: string | null;
  new: string | null;
  change: string | null;
  percentChange: string | null;
  assumed: boolean;
}

export interface StagedRunDetail extends StagedRunSummary {
  contentHash: string | null;
  rowCount: number | null;
  validatedAt: string | null;
  scope: Record<string, string> | null;
  baselineSnapshotId: string | null;
  stageManifestDigest: string | null;
  extractDigest: string | null;
  evidenceDigest: string | null;
  extractSnapshotId: string | null;
  skuMapDigest: string;
  validation: {
    status: string | null;
    failures: string[];
    coverage: { check: string; covered: boolean }[];
    bootstrap: boolean;
    rowChanges: Record<"added" | "retired" | "changed" | "materialRateChangeCount", number | null>;
  };
  extract: {
    specVersion: string | null;
    rateCount: number | null;
    assumedRateCount: number | null;
    diff: {
      baselineSnapshotId: string | null;
      addedCount: number | null;
      removedCount: number | null;
      changedCount: number | null;
      unchangedCount: number | null;
      rates: StagedRateChange[];
    } | null;
  };
  review: { reviewerDisplayName: string; reviewedAt: string; skuMapDigest: string } | null;
  approval: {
    approverDisplayName: string;
    approvedAt: string;
    evidencePolicy: string;
    nonProduction: boolean;
  } | null;
  publication: {
    artifact: string | null;
    current: boolean;
    currentSnapshotId: string | null;
    publishedAt: string | null;
    publishedBy: string | null;
  } | null;
  actions: {
    canReview: boolean;
    canApprove: boolean;
    canPublish: boolean;
    waiting: string | null;
  };
}

export interface StagedRunList {
  configured: boolean;
  runs: StagedRunSummary[];
}

export type SnapshotDecisionKind = "skumap-review" | "approval" | "publish";

function stagedRunPath(snapshotId: string, runId: string): string {
  return `/api/price-book/staged/${encodeURIComponent(snapshotId)}/${encodeURIComponent(runId)}`;
}

export function listStagedRuns(signal?: AbortSignal): Promise<StagedRunList> {
  return request<StagedRunList>("/api/price-book/staged", { signal });
}

export function getStagedRun(
  snapshotId: string,
  runId: string,
  signal?: AbortSignal,
): Promise<StagedRunDetail> {
  return request<StagedRunDetail>(stagedRunPath(snapshotId, runId), { signal });
}

// The body repeats the digests the person saw, so the API refuses if anything changed since.
export function recordSnapshotDecision(
  run: StagedRunDetail,
  kind: SnapshotDecisionKind,
): Promise<StagedRunDetail> {
  return request<StagedRunDetail>(`${stagedRunPath(run.snapshotId, run.runId)}/${kind}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(
      kind === "publish"
        ? { evidenceDigest: run.evidenceDigest, attested: true }
        : {
            stageManifestDigest: run.stageManifestDigest,
            extractDigest: run.extractDigest,
            evidenceDigest: run.evidenceDigest,
            skuMapDigest: run.skuMapDigest,
            attested: true,
          },
    ),
  });
}

export function getApplication(
  applicationId: string,
  signal?: AbortSignal,
): Promise<ApplicationDetail> {
  return request<ApplicationDetail>(`/api/applications/${applicationId}`, { signal });
}

export function uploadIntake(file: File): Promise<ApplicationDetail> {
  const form = new FormData();
  form.append("file", file);
  return request<ApplicationDetail>("/api/intakes", {
    method: "POST",
    body: form,
  });
}

export function resolveGap(
  applicationId: string,
  gapId: string,
  resolution: GapResolution,
  signal?: AbortSignal,
): Promise<ApplicationDetail> {
  return request<ApplicationDetail>(
    `/api/applications/${applicationId}/gaps/${encodeURIComponent(gapId)}/resolve`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(resolution),
      signal,
    },
  );
}

export async function downloadComparison(
  applicationId: string,
  signal?: AbortSignal,
): Promise<void> {
  const response = await apiFetch(
    `/api/applications/${applicationId}/comparison/export`,
    { signal },
  );
  if (!response.ok) {
    let detail = `Export failed with status ${response.status}`;
    try {
      const payload = await response.json() as { detail?: string };
      detail = payload.detail ?? detail;
    } catch {
      // The HTTP status remains actionable when an intermediary returns non-JSON.
    }
    throw new Error(detail);
  }
  const blob = await response.blob();
  triggerDownload(blob, `comparison-${applicationId}.json`);
}

export async function downloadPricedWorkbook(
  applicationId: string,
  originalFileName: string,
  signal?: AbortSignal,
  view?: WorkbookView,
): Promise<void> {
  const query = view ? `?view=${view}` : "";
  const response = await apiFetch(
    `/api/applications/${applicationId}/comparison/workbook${query}`,
    { signal },
  );
  if (!response.ok) {
    let detail = `Workbook export failed with status ${response.status}`;
    try {
      const payload = await response.json() as { detail?: string };
      detail = payload.detail ?? detail;
    } catch {
      // The HTTP status remains actionable when an intermediary returns non-JSON.
    }
    throw new Error(detail);
  }
  const blob = await response.blob();
  triggerDownload(
    blob,
    `${originalFileName.replace(/\.xlsx$/i, "")}-priced.xlsx`,
  );
}
