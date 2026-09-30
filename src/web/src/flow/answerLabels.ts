import type { Gap, GapResolution } from "../api";

// One source for question wording, so the Resolve form and the Confidence summary never drift apart.
// Values from the payload are shown as given; this module only swaps keys and codes for words.

export type SqlModel = NonNullable<GapResolution["azure_sql_deployment_model"]>;

export const RUNTIME_PRESETS = [
  { value: "730", label: "Always on", detail: "730 hours a month" },
  { value: "220", label: "Business hours", detail: "220 hours a month" },
] as const;

export const LICENSE_QUESTIONS = [
  ["active_software_assurance", "Active Software Assurance or a qualifying subscription?"],
  ["azure_hybrid_benefit_eligible", "Eligible for Azure Hybrid Benefit?"],
  ["acquired_before_2019_10_01", "Licenses acquired, or trued up, before October 1, 2019?"],
  ["perpetual_license", "Perpetual licenses?"],
  ["eligible_product_version", "Product version eligible for the legacy bring-your-own-license path?"],
] as const;

export const SQL_MODELS: { value: SqlModel; label: string }[] = [
  { value: "SqlVm", label: "SQL Server on Azure VM" },
  { value: "SqlDatabaseProvisionedVCore", label: "Azure SQL Database, provisioned vCore" },
  { value: "SqlManagedInstanceProvisionedVCore", label: "Azure SQL Managed Instance, provisioned vCore" },
  { value: "SqlDatabaseServerless", label: "Azure SQL Database, serverless" },
  { value: "SqlDatabaseDtu", label: "Azure SQL Database, DTU" },
];

export const PASSIVE_SECONDARY_QUESTION = "Is there a passive secondary?";
export const PASSIVE_USE_ONLY_QUESTION = "Is the secondary strictly passive, never serving reads?";
export const SQL_MODEL_QUESTION = "Azure SQL deployment model";

export function licenseMobilityQuestion(showAws: boolean): string {
  return showAws
    ? "Eligible for AWS SQL Server License Mobility?"
    : "Eligible for License Mobility on the other cloud?";
}

const REGION_NAMES: Record<string, string> = {
  eastus2: "Azure East US 2",
  "us-east-1": "AWS US East (N. Virginia, us-east-1)",
};

export interface AnswerPart {
  label: string;
  value: string;
}

// `value` must already be masked for the mode (no aws_* keys when AWS is hidden).
export function describeAnswer(
  gap: Gap,
  value: Record<string, unknown> | null,
  showAws: boolean,
): AnswerPart[] {
  if (!value) {
    return [];
  }
  switch (gap.kind) {
    case "RuntimeHours":
      return runtimeParts(value);
    case "ApprovedRegions":
      return regionParts(value);
    case "StoragePerformance":
      return storageParts(value);
    case "LicenseEligibility":
      return licenseParts(gap, value, showAws);
    default:
      return fallbackParts(value);
  }
}

function runtimeParts(value: Record<string, unknown>): AnswerPart[] {
  const hours = text(value.runtime_hours_month);
  const preset = RUNTIME_PRESETS.find((item) => item.value === hours);
  return [
    {
      label: "Runtime",
      value: preset ? `${preset.label}, ${preset.detail}` : `${hours} hours a month`,
    },
  ];
}

function regionParts(value: Record<string, unknown>): AnswerPart[] {
  const parts: AnswerPart[] = [];
  if ("azure_region" in value) {
    parts.push({ label: "Azure region", value: regionName(value.azure_region) });
  }
  if ("aws_region" in value) {
    parts.push({ label: "AWS region", value: regionName(value.aws_region) });
  }
  return parts;
}

function storageParts(value: Record<string, unknown>): AnswerPart[] {
  return [
    { label: "Target IOPS", value: text(value.target_iops) },
    { label: "Target throughput", value: `${text(value.target_mbps)} MB/s` },
  ];
}

function licenseParts(
  gap: Gap,
  value: Record<string, unknown>,
  showAws: boolean,
): AnswerPart[] {
  const parts: AnswerPart[] = LICENSE_QUESTIONS.map(([key, label]) => ({
    label,
    value: yesNo(value[key]),
  }));
  // Windows answers carry forced SQL-only values the user was never asked; show only what was asked.
  if (gap.license_product !== "SQL Server") {
    return parts;
  }
  if ("aws_license_mobility_eligible" in value) {
    parts.push({
      label: licenseMobilityQuestion(showAws),
      value: yesNo(value.aws_license_mobility_eligible),
    });
  }
  parts.push({ label: PASSIVE_SECONDARY_QUESTION, value: yesNo(value.passive_secondary) });
  if (value.passive_secondary === true) {
    parts.push({ label: PASSIVE_USE_ONLY_QUESTION, value: yesNo(value.passive_use_only) });
  }
  const model = SQL_MODELS.find((item) => item.value === value.azure_sql_deployment_model);
  parts.push({
    label: SQL_MODEL_QUESTION,
    value: model ? model.label : text(value.azure_sql_deployment_model),
  });
  return parts;
}

function fallbackParts(value: Record<string, unknown>): AnswerPart[] {
  return Object.entries(value).map(([key, entry]) => ({
    label: humanize(key),
    value: typeof entry === "boolean" ? yesNo(entry) : text(entry),
  }));
}

function regionName(code: unknown): string {
  const raw = text(code);
  return REGION_NAMES[raw] ?? raw;
}

function yesNo(entry: unknown): string {
  if (entry === true) {
    return "Yes";
  }
  if (entry === false) {
    return "No";
  }
  return "Not answered";
}

function text(entry: unknown): string {
  if (entry === null || entry === undefined) {
    return "Not answered";
  }
  return typeof entry === "object" ? JSON.stringify(entry) : String(entry);
}

function humanize(key: string): string {
  const words = key.replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}
