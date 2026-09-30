import { Link } from "react-router-dom";

import type { ApplicationSummary, Capabilities } from "../api";
import { DISPLAY, capitalize } from "../glossary";
import { aboutMoney, calendarDate, localDateTime, localTime } from "../format";
import { MODE_COPY } from "../flow/modeCopy";
import { getEstimateMode, type Mode } from "../flow/storage";


export function modeFor(summary: { id: string }, capabilities: Capabilities): Mode {
  return getEstimateMode(summary.id) ?? capabilities.defaultMode;
}

export function needsYou(summary: ApplicationSummary): boolean {
  return summary.open_question_count > 0 || summary.verdict_confidence === "Draft";
}

export function keptUntil(expiresAt: string): string {
  if (!expiresAt) {
    return "";
  }
  const expires = new Date(expiresAt);
  const sameDay = expires.toDateString() === new Date().toDateString();
  return `Kept until ${sameDay ? localTime(expiresAt) : localDateTime(expiresAt)}`;
}

// The Answer grammar for a list row, from the list summary's payload values.
export function SummaryVerdict({ summary, mode }: { summary: ApplicationSummary; mode: Mode }) {
  if (mode !== "both") {
    const cloud = mode === "azure" ? "Azure" : "AWS";
    const total = mode === "azure" ? summary.azure_monthly_total : summary.aws_monthly_total;
    return (
      <span>
        {total === null
          ? `${cloud} could not be priced.`
          : `About ${aboutMoney(total)}/month on ${cloud} at ${DISPLAY.listView}.`}
      </span>
    );
  }
  const { azure_monthly_total: azure, aws_monthly_total: aws, headline_delta: delta } = summary;
  if (summary.cheaper_cloud === null || delta === null || azure === null || aws === null) {
    return <span>At {DISPLAY.listView}, Azure and AWS cost the same each month.</span>;
  }
  return (
    <span>
      At {DISPLAY.listView},{" "}
      <strong>
        Azure is about {aboutMoney(azure)}/month and AWS is about {aboutMoney(aws)}/month
      </strong>
      , a difference of about {aboutMoney(delta)}/month.
    </span>
  );
}

export function ConfidenceNote({ summary, mode }: { summary: ApplicationSummary; mode: Mode }) {
  if (summary.verdict_confidence !== "Placeholder") {
    return null;
  }
  // Flag only placeholders inside the figures this view shows, as the Answer screen does.
  const visible = summary.placeholder_providers.some((provider) =>
    mode === "both" || (mode === "azure" ? provider === "Azure" : provider === "AWS"),
  );
  if (!visible) {
    return null;
  }
  return <span className="cp-badge">Includes a {DISPLAY.demoAssumption}</span>;
}

export function PriceBookCard({ capabilities }: { capabilities: Capabilities }) {
  const book = capabilities.priceBook;
  // The 30-day rule is decided by the API; the browser only shows the result.
  const stale = book.stale;
  return (
    <section aria-labelledby="price-book-card" className="cp-card">
      <h2 id="price-book-card">{capitalize(DISPLAY.priceBook)}</h2>
      <dl className="cp-stamp">
        <div><dt>Snapshot</dt><dd>{book.snapshotId}</dd></div>
        <div><dt>Prices as of</dt><dd>{calendarDate(book.pricedAsOf)}</dd></div>
        <div>
          <dt>Kind</dt>
          <dd>
            {book.source === "demo-extract"
              ? "Frozen demo set (non-production)"
              : `Published harvest${book.nonProduction ? " (non-production)" : ""}`}
          </dd>
        </div>
      </dl>
      {stale && (
        <p className="cp-callout cp-small">
          This harvest is more than {book.staleAfterDays} days old. Prices change only through a new harvest
          and a named approver.
        </p>
      )}
      <Link className="cp-small" to="/price-book">About the {DISPLAY.priceBook}</Link>
    </section>
  );
}

export function modeLabel(mode: Mode): string {
  return MODE_COPY[mode].short;
}
