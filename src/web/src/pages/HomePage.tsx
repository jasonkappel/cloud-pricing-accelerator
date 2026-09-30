import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { listApplications, type ApplicationSummary } from "../api";
import { NotIncludedCategories } from "../flow/answerParts";
import { useCapabilities } from "../flow/useCapabilities";
import { getSeenEstimates, markExpired, rememberEstimate, type SeenEstimate } from "../flow/storage";
import { DISPLAY, capitalize, estimateCount, hoursLabel, questionCount } from "../glossary";
import { calendarDate } from "../format";
import {
  ConfidenceNote,
  PriceBookCard,
  SummaryVerdict,
  keptUntil,
  modeFor,
  modeLabel,
  needsYou,
} from "./homeParts";

// Reconciles the browser's remembered estimates with the API list. Anything remembered but no longer
// held by the API is expired. Only {id, name, expiresAt} is stored; comparison data never is.
export function useEstimateList() {
  const [items, setItems] = useState<ApplicationSummary[] | null>(null);
  const [expired, setExpired] = useState<SeenEstimate[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    listApplications(controller.signal)
      .then((loaded) => {
        if (controller.signal.aborted) {
          return;
        }
        const liveIds = new Set(loaded.map((item) => item.id));
        for (const seen of getSeenEstimates()) {
          if (!liveIds.has(seen.id)) {
            markExpired(seen.id);
          }
        }
        for (const item of [...loaded].reverse()) {
          rememberEstimate({ id: item.id, name: item.name, expiresAt: item.expires_at });
        }
        setExpired(getSeenEstimates().filter((seen) => seen.expired));
        setItems(loaded);
      })
      .catch((requestError: unknown) => {
        if (!controller.signal.aborted) {
          setError(requestError instanceof Error ? requestError.message : "Estimates could not be loaded.");
        }
      });
    return () => controller.abort();
  }, []);

  return { items, expired, error };
}

export function HomePage() {
  const { capabilities, error: capabilitiesError } = useCapabilities();
  const { items, expired, error } = useEstimateList();

  if (error || capabilitiesError) {
    return <p className="cp-error" role="alert">{error || capabilitiesError}</p>;
  }
  if (!items || !capabilities) {
    return <p className="cp-muted">Loading…</p>;
  }

  const waiting = items.filter(needsYou);
  const ready = items.filter((item) => !needsYou(item));
  const keptLine = `Estimates are kept for ${hoursLabel(capabilities.applicationMaxAgeHours)}. The export is the lasting record.`;

  if (items.length === 0) {
    return (
      <section className="cp-page">
        <section className="cp-card">
          <h1 className="cp-title">Price your first app</h1>
          <p className="cp-lede">
            Upload an {DISPLAY.intake} and get a comparison of Azure and AWS at {DISPLAY.listView}.
          </p>
          <div className="cp-row">
            <Link className="cp-button cp-button-primary" to="/estimates/new/mode">New estimate</Link>
            <Link className="cp-button" to="/estimates/new/mode?sample=1">Try a sample run</Link>
          </div>
        </section>
        <ol aria-label="How it works" className="cp-card-grid">
          <li className="cp-card"><strong>1. Upload</strong><span className="cp-muted"> the {DISPLAY.intake}.</span></li>
          <li className="cp-card"><strong>2. Answer</strong><span className="cp-muted"> any {DISPLAY.gaps} we can&apos;t read from it.</span></li>
          <li className="cp-card"><strong>3. Get the answer</strong><span className="cp-muted">, with the proof underneath.</span></li>
        </ol>
        <section className="cp-card">
          <h2>What you&apos;ll need</h2>
          <p>An {DISPLAY.intake}. Nothing else: list prices are already loaded.</p>
        </section>
        <PriceBookCard capabilities={capabilities} />
        <ExpiredList items={expired} />
        <p className="cp-muted cp-small">{keptLine}</p>
      </section>
    );
  }

  return (
    <section className="cp-page">
      <div className="cp-row cp-row-between">
        <h1 className="cp-title">
          {waiting.length === 0
            ? "Nothing needs you right now"
            : `${estimateCount(waiting.length)} ${waiting.length === 1 ? "needs" : "need"} you`}
        </h1>
        <Link className="cp-button cp-button-primary" to="/estimates/new/mode">New estimate</Link>
      </div>

      {waiting.length > 0 && (
        <section aria-labelledby="needs-you" className="cp-card">
          <h2 id="needs-you">Needs you</h2>
          <ul className="cp-list">
            {waiting.map((item) => {
              const mode = modeFor(item, capabilities);
              return (
                <li className="cp-list-row" key={item.id}>
                  <div>
                    <strong>{item.name}</strong>
                    <p className="cp-muted cp-small">
                      {item.open_question_count > 0
                        ? `On Resolve: ${questionCount(item.open_question_count)} open.${
                            item.first_open_prompt && mode !== "azure" ? ` ${item.first_open_prompt}` : ""
                          }`
                        : "Something still blocks pricing. Open the Builder review for the reason."}
                    </p>
                    <p className="cp-muted cp-small">{keptUntil(item.expires_at)}</p>
                  </div>
                  <Link
                    className="cp-button cp-button-primary"
                    to={item.open_question_count > 0
                      ? `/estimates/${item.id}/resolve`
                      : `/review?applicationId=${item.id}`}
                  >
                    Resume
                  </Link>
                </li>
              );
            })}
          </ul>
        </section>
      )}

      {ready.length > 0 && (
        <section aria-labelledby="recent-answers" className="cp-card">
          <h2 id="recent-answers">Recent answers</h2>
          <ul className="cp-list">
            {ready.map((item) => {
              const mode = modeFor(item, capabilities);
              return (
                <li className="cp-list-row" key={item.id}>
                  <div>
                    <strong>{item.name}</strong>
                    <p><SummaryVerdict mode={mode} summary={item} /> <ConfidenceNote mode={mode} summary={item} /></p>
                    <NotIncludedCategories categories={item.excluded_cost_categories} />
                    <p className="cp-muted cp-small">
                      {modeLabel(mode)} · Prices as of {calendarDate(item.priced_as_of)} · {keptUntil(item.expires_at)}
                    </p>
                  </div>
                  <div className="cp-row">
                    <Link className="cp-button" to={`/estimates/${item.id}/answer`}>Open</Link>
                    <Link className="cp-button" to={`/estimates/${item.id}/export`}>Export</Link>
                  </div>
                </li>
              );
            })}
          </ul>
        </section>
      )}

      <PriceBookCard capabilities={capabilities} />
      <ExpiredList items={expired} />
      <p className="cp-muted cp-small">{keptLine}</p>
    </section>
  );
}

export function ExpiredList({ items }: { items: SeenEstimate[] }) {
  if (items.length === 0) {
    return null;
  }
  return (
    <section aria-labelledby="expired-estimates" className="cp-card">
      <h2 id="expired-estimates">{capitalize(DISPLAY.applications)} no longer held</h2>
      <ul className="cp-list">
        {items.map((item) => (
          <li className="cp-list-row" key={item.id}>
            <div>
              <strong>{item.name}</strong>
              <p className="cp-muted cp-small">Expired. Re-upload the intake to rebuild it.</p>
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}
