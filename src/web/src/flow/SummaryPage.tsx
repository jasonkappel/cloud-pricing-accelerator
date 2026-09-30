import { Link, Navigate, useParams } from "react-router-dom";

import { DISPLAY, capitalize } from "../glossary";
import { localDateTime } from "../format";
import {
  Disclaimer,
  NotIncludedLine,
  PlaceholderCallout,
  ResolvedAnswers,
  Stamp,
  Totals,
  Verdict,
} from "./answerParts";
import { getEstimateMode, type Mode } from "./storage";
import { useCapabilities } from "./useCapabilities";
import { useEstimate } from "./useEstimate";

export function SummaryPage() {
  const { id = "" } = useParams();
  const { capabilities, error: capabilitiesError } = useCapabilities();
  const { detail, expired, error } = useEstimate(id);

  if (expired) {
    return (
      <section className="cp-page">
        <h1 className="cp-title">This estimate has expired</h1>
        <p className="cp-lede">Re-upload the {DISPLAY.intake} to rebuild it.</p>
      </section>
    );
  }
  if (error || capabilitiesError) {
    return <p className="cp-error" role="alert">{error || capabilitiesError}</p>;
  }
  if (!detail || !capabilities) {
    return <p className="cp-muted">Loading…</p>;
  }
  if (detail.gaps.some((gap) => gap.status === "Open")) {
    return <Navigate replace to={`/estimates/${id}/resolve`} />;
  }

  const comparison = detail.comparison;
  const mode: Mode = getEstimateMode(id) ?? capabilities.defaultMode;

  if (!comparison.can_export) {
    return (
      <section className="cp-page">
        <h1 className="cp-title">The summary is not available yet</h1>
        <p className="cp-lede">It unlocks with export, once the estimate is ready.</p>
        <Link className="cp-button" to={`/estimates/${id}/answer`}>Back to the answer</Link>
      </section>
    );
  }

  return (
    <article className="cp-page cp-summary">
      <div className="cp-row no-print">
        <Link className="cp-button" to={`/estimates/${id}/export`}>Back to export</Link>
        <button className="cp-button cp-button-primary" onClick={() => window.print()} type="button">
          Print or save as PDF
        </button>
      </div>
      <header>
        <p className="cp-muted cp-small">
          Cloud Pricing Accelerator · {capitalize(DISPLAY.listView)} run-rate benchmark
        </p>
        <h1 className="cp-title">{detail.application.name}</h1>
      </header>
      <Verdict comparison={comparison} mode={mode} />
      <PlaceholderCallout comparison={comparison} mode={mode} />
      <Totals comparison={comparison} mode={mode} />
      <NotIncludedLine comparison={comparison} />
      <section aria-labelledby="summary-answers">
        <h2 className="cp-section-title" id="summary-answers">Answers used in this estimate</h2>
        <ResolvedAnswers gaps={detail.gaps} mode={mode} />
      </section>
      {comparison.excluded_cost_ledger.length > 0 && (
        <table className="cp-table">
          <thead>
            <tr>
              <th scope="col">{DISPLAY.excludedCostLedger}</th>
              <th scope="col">Why it's left out</th>
            </tr>
          </thead>
          <tbody>
            {comparison.excluded_cost_ledger.map((item) => (
              <tr key={item.id}>
                <td>{item.category}</td>
                <td>{item.rationale}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <Stamp comparison={comparison} mode={mode} nonProduction={capabilities.priceBook.nonProduction} />
      <p className="cp-muted cp-small">
        Run hash <code>{comparison.run_hash}</code>. Printed {localDateTime(new Date().toISOString())}.
      </p>
      <Disclaimer />
    </article>
  );
}
