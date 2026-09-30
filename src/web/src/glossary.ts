// Display glossary (UX spec section 9). The guided flow and the Audience-facing summary use these
// labels. Builder detail pages, the API, and the audit file keep the domain's canonical terms.
export const DISPLAY = {
  application: "estimate",
  applications: "estimates",
  intake: "intake workbook",
  gap: "question",
  gaps: "questions",
  resolveGap: "answer a question",
  ready: "Ready",
  draft: "Draft, questions open",
  excludedCostLedger: "Not included",
  listView: "public list prices",
  priceBook: "price book",
  costDrivers: "why the numbers differ",
  demoAssumption: "placeholder price",
  demoAssumptions: "placeholder prices",
} as const;

export function capitalize(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

export function stateLabel(state: "DraftBenchmark" | "ReviewBaseline"): string {
  return state === "ReviewBaseline" ? DISPLAY.ready : DISPLAY.draft;
}

export function questionCount(count: number): string {
  return `${count} ${count === 1 ? DISPLAY.gap : DISPLAY.gaps}`;
}

export function estimateCount(count: number): string {
  return `${count} ${count === 1 ? DISPLAY.application : DISPLAY.applications}`;
}

// applicationMaxAgeHours arrives as a decimal string such as "1" or "1.5".
export function hoursLabel(value: string): string {
  return `${value} ${value === "1" ? "hour" : "hours"}`;
}
