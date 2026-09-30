import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";

import type { ApplicationDetail, Capabilities, CloudName, Comparison } from "../api";
import { DISPLAY, capitalize } from "../glossary";
import { localDateTime, money, number, percent } from "../format";
import {
  presentationWarnings,
} from "../presentation";
import {
  Disclaimer,
  NotIncludedLine,
  PlaceholderCallout,
  ResolvedAnswers,
  Stamp,
  Totals,
  Verdict,
  hasAnswer,
  scenarioTotal,
  singleCloud,
  visibleSensitivities,
} from "./answerParts";
import { showsAws, showsAzure } from "./modeCopy";
import type { Mode } from "./storage";

type DoorId = "scope" | "confidence" | "breakeven";

const TOP_DRIVERS = 5;

interface AnswerStepProps {
  capabilities: Capabilities;
  detail: ApplicationDetail;
  mode: Mode;
}

export function AnswerStep({ capabilities, detail, mode }: AnswerStepProps) {
  const comparison = detail.comparison;
  const applicationId = detail.application.id;
  const [open, setOpen] = useState<DoorId | null>(null);
  const panelRef = useRef<HTMLElement>(null);
  const cloud = singleCloud(mode);
  const showAws = showsAws(mode);

  useEffect(() => {
    if (open !== null) {
      panelRef.current?.focus();
    }
  }, [open]);

  const doors: { id: DoorId; label: string; hint: string }[] = [
    { id: "scope", label: "Scope", hint: "What's in the number and what isn't" },
    { id: "confidence", label: "Confidence", hint: "How much had to be decided" },
    cloud === null
      ? { id: "breakeven", label: "Breakeven", hint: "What would change the result" }
      : { id: "breakeven", label: "What moves your number?", hint: "Other public prices and placeholders" },
  ];
  const warnings = presentationWarnings(comparison.warnings, showAws);

  return (
    <div className="cp-page">
      <div>
        <p className="cp-muted cp-small">{detail.application.name}</p>
        <Verdict comparison={comparison} mode={mode} />
      </div>
      <PlaceholderCallout comparison={comparison} mode={mode} />
      <Totals comparison={comparison} mode={mode} />
      <NotIncludedLine comparison={comparison} />
      {!hasAnswer(comparison) && warnings.length > 0 && (
        <ul className="cp-callout">
          {warnings.map((warning) => <li key={warning}>{warning}</li>)}
        </ul>
      )}
      <Disclaimer />

      <div className="cp-doors">
        {doors.map((door) => (
          <button
            aria-controls={`door-panel-${door.id}`}
            aria-expanded={open === door.id}
            className="cp-door"
            key={door.id}
            onClick={() => setOpen((current) => (current === door.id ? null : door.id))}
            type="button"
          >
            <strong>{door.label}</strong>
            <span>{door.hint}</span>
          </button>
        ))}
      </div>
      {doors.map((door) => (
        <section
          aria-label={door.label}
          className="cp-panel cp-card"
          hidden={open !== door.id}
          id={`door-panel-${door.id}`}
          key={door.id}
          ref={open === door.id ? panelRef : undefined}
          tabIndex={-1}
        >
          {open === door.id && door.id === "scope" && (
            <ScopePanel comparison={comparison} detail={detail} mode={mode} />
          )}
          {open === door.id && door.id === "confidence" && (
            <ConfidencePanel detail={detail} mode={mode} />
          )}
          {open === door.id && door.id === "breakeven" && (
            cloud === null
              ? <BreakevenPanel comparison={comparison} />
              : <MovesPanel cloud={cloud} comparison={comparison} mode={mode} />
          )}
        </section>
      ))}

      <Stamp comparison={comparison} mode={mode} nonProduction={capabilities.priceBook.nonProduction} />
      <div className="cp-row">
        {comparison.can_export ? (
          <Link className="cp-button cp-button-primary" to={`/estimates/${applicationId}/export`}>
            Export
          </Link>
        ) : (
          <span className="cp-muted">Export unlocks when the estimate is ready.</span>
        )}
        <Link className="cp-button" to={`/comparisons?applicationId=${applicationId}`}>
          Builder detail
        </Link>
      </div>
    </div>
  );
}

function ScopePanel({
  comparison,
  detail,
  mode,
}: {
  comparison: Comparison;
  detail: ApplicationDetail;
  mode: Mode;
}) {
  const cloud = singleCloud(mode);
  const summary = detail.intake_summary;
  const unpriced = comparison.line_items.filter((line) => line.status === "Unpriced");
  return (
    <>
      <h2>Scope</h2>
      {cloud === null ? (
        <>
          <h3>{capitalize(DISPLAY.costDrivers)}</h3>
          {comparison.cost_drivers.length === 0 ? (
            <p className="cp-muted">No line differs between the clouds.</p>
          ) : (
            <table className="cp-table">
              <thead>
                <tr>
                  <th scope="col">Line</th>
                  <th className="cp-num" scope="col">Difference per month</th>
                  <th className="cp-num" scope="col">Share of the difference</th>
                </tr>
              </thead>
              <tbody>
                {comparison.cost_drivers.slice(0, TOP_DRIVERS).map((driver) => (
                  <tr key={driver.line_id}>
                    <td>{driver.component}</td>
                    <td className="cp-num">{money(driver.delta)}</td>
                    <td className="cp-num">{percent(driver.share_of_absolute_delta_percent)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <p className="cp-muted cp-small">In the order the pricing engine ranks them.</p>
        </>
      ) : (
        <>
          <h3>Priced lines on {cloud}</h3>
          <table className="cp-table">
            <thead>
              <tr>
                <th scope="col">Unit</th>
                <th scope="col">Line</th>
                <th className="cp-num" scope="col">Per month</th>
              </tr>
            </thead>
            <tbody>
              {comparison.line_items
                .filter((line) => line.status === "Priced")
                .map((line) => (
                  <tr key={line.id}>
                    <td>{line.unit_name}</td>
                    <td>{line.component}</td>
                    <td className="cp-num">
                      {money(cloud === "AWS" ? line.aws_amount : line.azure_amount)}
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        </>
      )}

      <h3>Included</h3>
      {summary ? (
        <p>
          {number(summary.server_count)} servers ({number(summary.server_vcpu)} vCPU),{" "}
          {number(summary.database_count)} databases, and {number(summary.storage_count)} storage
          volumes ({number(summary.storage_allocated_gb)} GB allocated) from the {DISPLAY.intake}.
        </p>
      ) : (
        <p className="cp-muted">Servers, databases, and storage from the {DISPLAY.intake}.</p>
      )}
      {unpriced.length > 0 && (
        <>
          <h3>Not priced</h3>
          <ul>
            {unpriced.map((line) => (
              <li key={line.id}>
                {line.unit_name}, {line.component}: {line.unpriced_reason ?? "no price available"}
              </li>
            ))}
          </ul>
        </>
      )}

      <h3>{DISPLAY.excludedCostLedger}</h3>
      {comparison.excluded_cost_ledger.length === 0 ? (
        <p className="cp-muted">Nothing is excluded.</p>
      ) : (
        <table className="cp-table">
          <thead>
            <tr>
              <th scope="col">Cost</th>
              <th scope="col">Size</th>
              <th scope="col">Why it's left out</th>
            </tr>
          </thead>
          <tbody>
            {comparison.excluded_cost_ledger.map((item) => (
              <tr key={item.id}>
                <td>{item.category}</td>
                <td>{item.materiality}</td>
                <td>{item.rationale}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}

function ConfidencePanel({ detail, mode }: { detail: ApplicationDetail; mode: Mode }) {
  const comparison = detail.comparison;
  const showAws = showsAws(mode);
  const showAzure = showsAzure(mode);
  const sensitivities = visibleSensitivities(comparison, mode);
  return (
    <>
      <h2>Confidence</h2>
      <h3>Your answers</h3>
      <ResolvedAnswers gaps={detail.gaps} mode={mode} />

      <h3>{capitalize(DISPLAY.demoAssumptions)}</h3>
      {sensitivities.length === 0 ? (
        <p className="cp-muted">None. Every priced line uses a published list price.</p>
      ) : (
        sensitivities.map((item) => (
          <div key={item.id}>
            <p>
              {item.label} on {item.provider}, modeled at {money(item.assumed_monthly_amount)}/month.
              Stress test on {DISPLAY.listView}:
            </p>
            <table className="cp-table">
              <thead>
                <tr>
                  <th scope="col">If that price were</th>
                  {showAzure && <th className="cp-num" scope="col">Azure per month</th>}
                  {showAws && <th className="cp-num" scope="col">AWS per month</th>}
                  {showAws && showAzure && <th className="cp-num" scope="col">Difference</th>}
                </tr>
              </thead>
              <tbody>
                {([["Zero", item.at_zero], ["Doubled", item.at_double]] as const).map(([label, scenario]) => (
                  <tr key={label}>
                    <th scope="row">{label} ({money(scenario.assumed_monthly_amount)})</th>
                    {showAzure && <td className="cp-num">{money(scenarioTotal(scenario, "Azure"))}</td>}
                    {showAws && <td className="cp-num">{money(scenarioTotal(scenario, "AWS"))}</td>}
                    {showAws && showAzure && <td className="cp-num">{money(scenario.headline_delta)}</td>}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))
      )}

      <details>
        <summary>Builder detail</summary>
        <dl className="cp-stamp">
          <div><dt>Run hash</dt><dd><code>{comparison.run_hash}</code></dd></div>
          <div><dt>Price book hash</dt><dd><code>{comparison.pricebook_content_hash}</code></dd></div>
          <div><dt>Mapping digest</dt><dd><code>{comparison.skumap_approval.content_digest}</code></dd></div>
          <div>
            <dt>Mapping approved by</dt>
            <dd>
              {comparison.skumap_approval.approver}, {localDateTime(comparison.skumap_approval.approved_at)}
              {comparison.skumap_approval.non_production ? " (non-production)" : ""}
            </dd>
          </div>
          <div><dt>Calculator rules</dt><dd>{comparison.calculator_rule_version}</dd></div>
        </dl>
      </details>
    </>
  );
}

function BreakevenPanel({ comparison }: { comparison: Comparison }) {
  return (
    <>
      <h2>Breakeven</h2>
      <p className="cp-muted">
        The discount one cloud would need to match the other, computed by the pricing engine. These are
        public prices only, not your contracted prices.
      </p>
      {comparison.breakeven_sensitivities.map((item) => (
        <div key={item.id}>
          <h3>{item.label}</h3>
          {!item.available ? (
            <p className="cp-muted">{item.unavailable_reason ?? "Not available for this estimate."}</p>
          ) : (
            <>
              <table className="cp-table">
                <thead>
                  <tr>
                    <th className="cp-num" scope="col">If {item.reference_provider ?? "the reference cloud"} discounts</th>
                    <th className="cp-num" scope="col">{item.target_provider ?? "The other cloud"} needs</th>
                    <th className="cp-num" scope="col">Extra discount beyond {item.reference_provider ?? "the reference"}</th>
                  </tr>
                </thead>
                <tbody>
                  {item.points.map((point) => (
                    <tr key={point.reference_discount_percent}>
                      <td className="cp-num">{percent(point.reference_discount_percent)}</td>
                      <td className="cp-num">{percent(point.target_discount_to_parity_percent)}</td>
                      <td className="cp-num">{percent(point.additional_discount_advantage_percent)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="cp-muted cp-small">{item.disclosure}</p>
            </>
          )}
        </div>
      ))}
      <CommercialViews clouds={["Azure", "AWS"]} comparison={comparison} />
    </>
  );
}

function MovesPanel({ cloud, comparison, mode }: { cloud: CloudName; comparison: Comparison; mode: Mode }) {
  const sensitivities = visibleSensitivities(comparison, mode);
  return (
    <>
      <h2>What moves your number?</h2>
      <CommercialViews clouds={[cloud]} comparison={comparison} />
      {sensitivities.length > 0 && (
        <>
          <h3>{capitalize(DISPLAY.demoAssumptions)}</h3>
          <ul>
            {sensitivities.map((item) => (
              <li key={item.id}>
                {item.label}: at zero, {money(scenarioTotal(item.at_zero, cloud))}/month; doubled,{" "}
                {money(scenarioTotal(item.at_double, cloud))}/month.
              </li>
            ))}
          </ul>
        </>
      )}
    </>
  );
}

function CommercialViews({ clouds, comparison }: { clouds: CloudName[]; comparison: Comparison }) {
  const scenarios = comparison.commercial_scenarios.filter((item) => clouds.includes(item.provider));
  if (scenarios.length === 0) {
    return null;
  }
  return (
    <>
      <h3>Other public prices</h3>
      <table className="cp-table">
        <thead>
          <tr>
            <th scope="col">Price type</th>
            <th className="cp-num" scope="col">Per month</th>
          </tr>
        </thead>
        <tbody>
          {scenarios.map((scenario) => (
            <tr key={scenario.id}>
              <td>{scenario.label}</td>
              <td className="cp-num">
                {scenario.available
                  ? money(scenario.monthly_total)
                  : scenario.unavailable_reason ?? "Not available"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
