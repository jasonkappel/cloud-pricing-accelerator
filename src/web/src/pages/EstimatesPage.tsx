import { Link } from "react-router-dom";

import { NotIncludedCategories } from "../flow/answerParts";
import { useCapabilities } from "../flow/useCapabilities";
import { DISPLAY, capitalize, hoursLabel, questionCount, stateLabel } from "../glossary";
import { ConfidenceNote, SummaryVerdict, keptUntil, modeFor, modeLabel, needsYou } from "./homeParts";
import { ExpiredList, useEstimateList } from "./HomePage";

export function EstimatesPage() {
  const { capabilities, error: capabilitiesError } = useCapabilities();
  const { items, expired, error } = useEstimateList();

  if (error || capabilitiesError) {
    return <p className="cp-error" role="alert">{error || capabilitiesError}</p>;
  }
  if (!items || !capabilities) {
    return <p className="cp-muted">Loading…</p>;
  }

  return (
    <section className="cp-page">
      <div className="cp-row cp-row-between">
        <div>
          <h1 className="cp-title">{capitalize(DISPLAY.applications)}</h1>
          <p className="cp-lede">
            Every {DISPLAY.application} the API is holding right now. Estimates are kept for{" "}
            {hoursLabel(capabilities.applicationMaxAgeHours)}.
          </p>
        </div>
        <Link className="cp-button cp-button-primary" to="/estimates/new/mode">New estimate</Link>
      </div>

      {items.length === 0 ? (
        <p className="cp-empty">No {DISPLAY.applications} yet.</p>
      ) : (
        <ul className="cp-list cp-card">
          {items.map((item) => {
            const mode = modeFor(item, capabilities);
            const waiting = needsYou(item);
            return (
              <li className="cp-list-row" key={item.id}>
                <div>
                  <strong>{item.name}</strong>{" "}
                  <span className="cp-badge">{stateLabel(item.comparison_state)}</span>
                  <p>
                    {waiting
                      ? item.open_question_count > 0
                        ? `${questionCount(item.open_question_count)} open.`
                        : "Something still blocks pricing."
                      : <><SummaryVerdict mode={mode} summary={item} /> <ConfidenceNote mode={mode} summary={item} /></>}
                  </p>
                  {!waiting && <NotIncludedCategories categories={item.excluded_cost_categories} />}
                  <p className="cp-muted cp-small">
                    {modeLabel(mode)} · {item.intake_file_name} · {keptUntil(item.expires_at)}
                  </p>
                </div>
                <div className="cp-row">
                  <Link
                    className="cp-button cp-button-primary"
                    to={`/estimates/${item.id}/${item.open_question_count > 0 ? "resolve" : "answer"}`}
                  >
                    {waiting ? "Resume" : "Open"}
                  </Link>
                  <Link className="cp-button" to={`/review?applicationId=${item.id}`}>Builder review</Link>
                  <Link className="cp-button" to={`/comparisons?applicationId=${item.id}`}>Builder comparison</Link>
                </div>
              </li>
            );
          })}
        </ul>
      )}

      <ExpiredList items={expired} />

      <section className="cp-card">
        <h2>Builder detail</h2>
        <p className="cp-muted">The original Builder pages remain available.</p>
        <div className="cp-row">
          <Link to="/applications">Applications</Link>
          <Link to="/intakes">Intakes</Link>
          <Link to="/review">Review</Link>
          <Link to="/comparisons">Comparisons</Link>
        </div>
      </section>
    </section>
  );
}
