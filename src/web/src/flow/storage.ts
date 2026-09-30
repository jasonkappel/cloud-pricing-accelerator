export type Mode = "both" | "azure" | "aws";

export const MODES: Mode[] = ["both", "azure", "aws"];

const PENDING_MODE_KEY = "cp.mode.pending";
const PENDING_REGION_KEY = "cp.region.pending";
const CONFIRMED_BY_KEY = "cp.confirmedBy";
const REGION_CONFIRMED_PREFIX = "cp.region.confirmed.";
const SEEN_KEY = "cp.seen";
const NOTHING_TO_RESOLVE_PREFIX = "cp.nothingToResolve.";
const MAX_SEEN = 25;

export const PILOT_REGION = {
  azureRegion: "eastus2",
  awsRegion: "us-east-1",
  label: "Azure East US 2 and AWS US East (N. Virginia, us-east-1)",
} as const;

export interface SeenEstimate {
  id: string;
  name: string;
  expiresAt: string;
  expired?: boolean;
}

function read(storage: Storage, key: string): string | null {
  try {
    return storage.getItem(key);
  } catch {
    return null;
  }
}

function write(storage: Storage, key: string, value: string | null) {
  try {
    if (value === null) {
      storage.removeItem(key);
    } else {
      storage.setItem(key, value);
    }
  } catch {
    // Storage can be unavailable (private mode, quota); the flow still works without it.
  }
}

export function isMode(value: unknown): value is Mode {
  return value === "both" || value === "azure" || value === "aws";
}

export function getPendingMode(): Mode | null {
  const value = read(sessionStorage, PENDING_MODE_KEY);
  return isMode(value) ? value : null;
}

export function setPendingMode(mode: Mode) {
  write(sessionStorage, PENDING_MODE_KEY, mode);
}

export function getPendingRegionConfirmed(): boolean {
  return read(sessionStorage, PENDING_REGION_KEY) === "confirmed";
}

export function setPendingRegionConfirmed(confirmed: boolean) {
  write(sessionStorage, PENDING_REGION_KEY, confirmed ? "confirmed" : null);
}

export function clearPending() {
  write(sessionStorage, PENDING_MODE_KEY, null);
  write(sessionStorage, PENDING_REGION_KEY, null);
}

// The region confirmed at Mode, carried to Resolve when no "Confirmed by" was known at upload.
export function carryRegionConfirmation(applicationId: string) {
  write(sessionStorage, `${REGION_CONFIRMED_PREFIX}${applicationId}`, "confirmed");
}

export function hasCarriedRegionConfirmation(applicationId: string): boolean {
  return read(sessionStorage, `${REGION_CONFIRMED_PREFIX}${applicationId}`) === "confirmed";
}

export function getEstimateMode(applicationId: string): Mode | null {
  const value = read(localStorage, `cp.mode.${applicationId}`);
  return isMode(value) ? value : null;
}

export function setEstimateMode(applicationId: string, mode: Mode) {
  write(localStorage, `cp.mode.${applicationId}`, mode);
}

export function getConfirmedBy(): string {
  return read(sessionStorage, CONFIRMED_BY_KEY) ?? "";
}

export function setConfirmedBy(name: string) {
  const trimmed = name.trim();
  write(sessionStorage, CONFIRMED_BY_KEY, trimmed.length > 0 ? trimmed : null);
}

export function markNothingToResolve(applicationId: string) {
  write(localStorage, `${NOTHING_TO_RESOLVE_PREFIX}${applicationId}`, "1");
}

export function hadNothingToResolve(applicationId: string): boolean {
  return read(localStorage, `${NOTHING_TO_RESOLVE_PREFIX}${applicationId}`) === "1";
}

export function getSeenEstimates(): SeenEstimate[] {
  const raw = read(localStorage, SEEN_KEY);
  if (!raw) {
    return [];
  }
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) {
      return [];
    }
    return parsed.filter(
      (item): item is SeenEstimate =>
        typeof item === "object"
        && item !== null
        && typeof (item as SeenEstimate).id === "string"
        && typeof (item as SeenEstimate).name === "string"
        && typeof (item as SeenEstimate).expiresAt === "string",
    );
  } catch {
    return [];
  }
}

function saveSeen(items: SeenEstimate[]) {
  write(localStorage, SEEN_KEY, JSON.stringify(items.slice(0, MAX_SEEN)));
}

export function rememberEstimate(item: SeenEstimate) {
  const others = getSeenEstimates().filter((entry) => entry.id !== item.id);
  saveSeen([{ id: item.id, name: item.name, expiresAt: item.expiresAt }, ...others]);
}

// The API no longer holds this estimate. Keep the row so Home can say so; drop its view state.
export function markExpired(applicationId: string) {
  const items = getSeenEstimates();
  if (!items.some((entry) => entry.id === applicationId)) {
    return;
  }
  saveSeen(
    items.map((entry) => (entry.id === applicationId ? { ...entry, expired: true } : entry)),
  );
  write(localStorage, `cp.mode.${applicationId}`, null);
  write(localStorage, `${NOTHING_TO_RESOLVE_PREFIX}${applicationId}`, null);
}

export function forgetEstimate(applicationId: string) {
  saveSeen(getSeenEstimates().filter((entry) => entry.id !== applicationId));
  write(localStorage, `cp.mode.${applicationId}`, null);
  write(localStorage, `${NOTHING_TO_RESOLVE_PREFIX}${applicationId}`, null);
}
