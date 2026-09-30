import type { Gap, LineItem } from "./api";

export function presentationEvidenceUrls(
  urls: string[],
  showAws: boolean,
): string[] {
  return showAws
    ? urls
    : urls.filter((url) => /azure|microsoft/i.test(url));
}

export function presentationLineEvidence(
  line: LineItem,
  showAws: boolean,
): string {
  if (showAws) {
    return line.unpriced_reason ?? line.evidence;
  }
  if (line.unpriced_reason) {
    return /aws|us-east/i.test(line.unpriced_reason)
      ? "This Azure line remains unavailable until its material pricing or eligibility evidence is resolved."
      : line.unpriced_reason;
  }
  return line.demo_assumption
    ? "Azure demo assumption; see Pricing Evidence in the downloaded workbook."
    : "Approved Azure PriceBook meter; see Pricing Evidence in the downloaded workbook.";
}

export function presentationGapPrompt(gap: Gap, showAws: boolean): string {
  if (showAws) {
    return gap.prompt;
  }
  if (gap.kind === "ApprovedRegions") {
    return "Confirm the approved Azure pricing region.";
  }
  if (gap.kind === "LicenseEligibility") {
    return `Confirm ${gap.license_product ?? "license"} eligibility for Azure pricing.`;
  }
  if (gap.kind === "StoragePerformance") {
    return "Confirm the required Azure storage performance.";
  }
  return gap.prompt;
}

export function presentationGapReason(gap: Gap, showAws: boolean): string {
  if (showAws) {
    return gap.reason;
  }
  if (gap.kind === "ApprovedRegions") {
    return "The Azure PriceBook region must be confirmed before pricing can complete.";
  }
  if (gap.kind === "LicenseEligibility") {
    return "Azure benefit eligibility must be explicitly confirmed; unknown eligibility fails closed.";
  }
  if (gap.kind === "StoragePerformance") {
    return "Azure storage performance meters require explicit IOPS and throughput targets.";
  }
  return gap.reason;
}

export function presentationCanonicalValue(
  value: Record<string, unknown> | null,
  showAws: boolean,
): Record<string, unknown> | null {
  if (showAws || value === null) {
    return value;
  }
  return Object.fromEntries(
    Object.entries(value).filter(([key]) => !key.startsWith("aws_")),
  );
}

export function presentationWarnings(
  warnings: string[],
  showAws: boolean,
): string[] {
  if (showAws) {
    return warnings;
  }
  return warnings.map((warning) =>
    warning
      .replace(
        "for approved East US 2 and us-east-1 mappings",
        "for the approved East US 2 mapping",
      )
      .replace(/\bAWS\b/g, "secondary-cloud"),
  );
}
