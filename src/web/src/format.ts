// Formatting only. Every value here comes from the API payload as a decimal string; nothing is
// summed, subtracted, or derived. Rounding for "about" figures uses integer string math so a
// display value can never drift from the payload through floating point.

function splitDecimal(value: string): { negative: boolean; whole: string; fraction: string } | null {
  const match = /^(-)?(\d+)(?:\.(\d+))?$/.exec(value.trim());
  if (!match) {
    return null;
  }
  return { negative: match[1] === "-", whole: match[2], fraction: match[3] ?? "" };
}

function group(whole: string): string {
  return whole.replace(/^0+(?=\d)/, "").replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

// Exact money, two decimals, as the payload states it.
export function money(value: string | null | undefined, fallback = "Not priced"): string {
  if (value === null || value === undefined) {
    return fallback;
  }
  const parts = splitDecimal(value);
  if (!parts) {
    return value;
  }
  const cents = parts.fraction.padEnd(2, "0").slice(0, 2);
  return `${parts.negative ? "-" : ""}$${group(parts.whole)}.${cents}`;
}

// Whole-dollar money for totals where cents add noise; rounds half up from the payload string.
export function wholeMoney(value: string | null | undefined, fallback = "Not priced"): string {
  if (value === null || value === undefined) {
    return fallback;
  }
  const rounded = roundToStep(value, 1n);
  return rounded === null ? value : `$${group(rounded)}`;
}

function roundToStep(value: string, step: bigint): string | null {
  const parts = splitDecimal(value);
  if (!parts || parts.negative) {
    return null;
  }
  const scale = 10n ** BigInt(parts.fraction.length);
  const scaled = BigInt(parts.whole) * scale + BigInt(parts.fraction || "0");
  const stepScaled = step * scale;
  const roundedSteps = (scaled * 2n + stepScaled) / (stepScaled * 2n);
  return (roundedSteps * step).toString();
}

// "about $4,200": a display rounding of one payload value. Small amounts keep more precision so the
// rounding never hides the size of a difference.
export function aboutMoney(value: string): string {
  const parts = splitDecimal(value);
  if (!parts || parts.negative) {
    return money(value);
  }
  const whole = BigInt(parts.whole);
  const step = whole < 100n ? 1n : whole < 1000n ? 10n : 100n;
  const rounded = roundToStep(value, step);
  if (rounded === null) {
    return money(value);
  }
  if (rounded === "0") {
    return "less than $1";
  }
  return `$${group(rounded)}`;
}

// "roughly 18%" from a payload percentage string; "less than 1%" when it rounds to zero.
export function aboutPercent(value: string): string {
  const parts = splitDecimal(value);
  if (!parts || parts.negative) {
    return `${value}%`;
  }
  const rounded = roundToStep(value, 1n);
  if (rounded === null) {
    return `${value}%`;
  }
  if (rounded === "0") {
    return "less than 1%";
  }
  return `roughly ${rounded}%`;
}

export function percent(value: string): string {
  return `${value}%`;
}

export function number(value: string | number): string {
  const text = String(value);
  const parts = splitDecimal(text);
  if (!parts) {
    return text;
  }
  const fraction = parts.fraction.replace(/0+$/, "");
  return `${parts.negative ? "-" : ""}${group(parts.whole)}${fraction ? `.${fraction}` : ""}`;
}

export function localTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return iso;
  }
  return date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

export function localDateTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return iso;
  }
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

// A calendar date such as "2026-09-22" is shown as written, without time-zone shifting.
export function calendarDate(value: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) {
    return value;
  }
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const month = months[Number(match[2]) - 1];
  return month ? `${month} ${Number(match[3])}, ${match[1]}` : value;
}
