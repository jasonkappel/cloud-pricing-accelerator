import type { Mode } from "./storage";

export const MODE_COPY: Record<Mode, { title: string; gives: string; short: string }> = {
  both: {
    title: "Compare Azure and AWS",
    gives: "You get each cloud's monthly total at public list prices, the difference, and how sure we are.",
    short: "Compare Azure and AWS",
  },
  azure: {
    title: "Azure only",
    gives: "You get a monthly estimate for Azure at public list prices.",
    short: "Azure only",
  },
  aws: {
    title: "AWS only",
    gives: "You get a monthly estimate for AWS at public list prices.",
    short: "AWS only",
  },
};

// Whether a mode shows AWS names and figures. Pricing always runs for both clouds.
export function showsAws(mode: Mode): boolean {
  return mode !== "azure";
}

export function showsAzure(mode: Mode): boolean {
  return mode !== "aws";
}

export function regionLabel(mode: Mode): string {
  if (mode === "azure") {
    return "Azure East US 2";
  }
  if (mode === "aws") {
    return "AWS US East (N. Virginia, us-east-1)";
  }
  return "Azure East US 2 and AWS US East (N. Virginia, us-east-1)";
}
