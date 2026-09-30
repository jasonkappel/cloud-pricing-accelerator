import type {
  AssumptionSensitivity,
  AssumptionStressScenario,
  CloudName,
  Comparison,
  Gap,
} from "../api";
import { DISPLAY, capitalize } from "../glossary";
import { aboutMoney, calendarDate, localDateTime, money } from "../format";
import { presentationCanonicalValue, presentationGapPrompt } from "../presentation";
import { describeAnswer } from "./answerLabels";
import { regionLabel, showsAws, showsAzure } from "./modeCopy";
import type { Mode } from "./storage";

// Every figure below is a payload value, formatted. Nothing is added, subtracted, or ranked here.
// Every cloud's result shares one template, one weight, and one color.

export function singleCloud(mode: Mode): CloudName | null {
  if (mode === "azure") {
    return "Azure";
  }
  if (mode === "aws") {
    return "AWS";
  }
  return null;
}

export function totalFor(comparison: Comparison, cloud: CloudName): string | null {
  return cloud === "AWS" ? comparison.aws_monthly_total : comparison.azure_monthly_total;
}

// Placeholder prices the reader of this mode should see. A single-cloud reader sees only their cloud's.
export function visibleSensitivities(comparison: Comparison, mode: Mode): AssumptionSensitivity[] {
  const cloud = singleCloud(mode);
  return comparison.assumption_sensitivities.filter(
    (item) => cloud === null || item.provider === cloud,
  );
}

export function hasAnswer(comparison: Comparison): boolean {
  return comparison.verdict_confidence !== "Draft";
}

export function Verdict({ comparison, mode }: { comparison: Comparison; mode: Mode }) {
  if (!hasAnswer(comparison)) {
    return (
      <p className="cp-verdict">
        We can&apos;t give a number yet. Something still blocks pricing; see the notes below.
      </p>
    );
  }
  const cloud = singleCloud(mode);
  if (cloud !== null) {
    const total = totalFor(comparison, cloud);
    return (
      <p className="cp-verdict">
        {total === null ? (
          <>{cloud} could not be priced for this estimate.</>
        ) : (
          <>
            <strong>About {aboutMoney(total)}/month on {cloud}</strong> at {DISPLAY.listView}.
          </>
        )}
      </p>
    );
  }
  const delta = comparison.headline_delta;
  const azure = totalFor(comparison, "Azure");
  const aws = totalFor(comparison, "AWS");
  if (comparison.cheaper_cloud === null || delta === null || azure === null || aws === null) {
    return (
      <p className="cp-verdict">
        At {DISPLAY.listView}, <strong>Azure and AWS cost the same</strong> each month.
      </p>
    );
  }
  return (
    <p className="cp-verdict">
      At {DISPLAY.listView},{" "}
      <strong>
        Azure is about {aboutMoney(azure)}/month and AWS is about {aboutMoney(aws)}/month
      </strong>
      , a difference of about {aboutMoney(delta)}/month.
    </p>
  );
}

export function PlaceholderCallout({ comparison, mode }: { comparison: Comparison; mode: Mode }) {
  if (comparison.verdict_confidence !== "Placeholder") {
    return null;
  }
  const items = visibleSensitivities(comparison, mode);
  if (items.length === 0) {
    return null;
  }
  const compare = singleCloud(mode) === null;
  return (
    <aside aria-label={capitalize(DISPLAY.demoAssumptions)} className="cp-callout">
      <p className="cp-callout-title">
        {items.length === 1
          ? `This includes one ${DISPLAY.demoAssumption}:`
          : `This includes ${items.length} ${DISPLAY.demoAssumptions}:`}
      </p>
      <ul>
        {items.map((item) => (
          <li key={item.id}>
            {item.label} on {item.provider}, modeled at {money(item.assumed_monthly_amount)}/month.{" "}
            {compare ? (
              item.changes_cheaper_cloud || comparison.cheaper_cloud === null ? (
                <>That placeholder is big enough to change the result.</>
              ) : (
                <>The result holds even if that price were zero or doubled.</>
              )
            ) : (
              <>
                At zero the estimate would be {money(scenarioTotal(item.at_zero, item.provider))}
                ; doubled, {money(scenarioTotal(item.at_double, item.provider))}.
              </>
            )}
          </li>
        ))}
      </ul>
    </aside>
  );
}

export function scenarioTotal(scenario: AssumptionStressScenario, cloud: CloudName): string {
  return cloud === "AWS" ? scenario.aws_monthly_total : scenario.azure_monthly_total;
}

export function Totals({ comparison, mode }: { comparison: Comparison; mode: Mode }) {
  if (!hasAnswer(comparison)) {
    return null;
  }
  const clouds: CloudName[] = [];
  if (showsAzure(mode)) {
    clouds.push("Azure");
  }
  if (showsAws(mode)) {
    clouds.push("AWS");
  }
  return (
    <dl aria-label="Monthly totals" className="cp-totals">
      {clouds.map((cloud) => (
        <div key={cloud}>
          <dt>{cloud}, per month</dt>
          <dd className="cp-num">{money(totalFor(comparison, cloud))}</dd>
        </div>
      ))}
    </dl>
  );
}

export function NotIncludedLine({ comparison }: { comparison: Comparison }) {
  return (
    <NotIncludedCategories
      categories={comparison.excluded_cost_ledger.map((item) => item.category)}
    />
  );
}

export function NotIncludedCategories({ categories }: { categories: string[] }) {
  return (
    <p className="cp-not-included">
      <strong>{DISPLAY.excludedCostLedger}:</strong>{" "}
      {categories.length > 0 ? categories.join(", ") : "nothing is excluded from this estimate"}.
    </p>
  );
}

export function Disclaimer() {
  return (
    <p className="cp-disclaimer">
      This informs a platform decision. It does not make one. {capitalize(DISPLAY.listView)} run-rate
      benchmark; your contracted prices will differ.
    </p>
  );
}

export function Stamp({
  comparison,
  mode,
  nonProduction,
}: {
  comparison: Comparison;
  mode: Mode;
  nonProduction: boolean;
}) {
  return (
    <dl aria-label="Price book stamp" className="cp-stamp">
      <div>
        <dt>Region</dt>
        <dd>{regionLabel(mode)}</dd>
      </div>
      <div>
        <dt>{capitalize(DISPLAY.priceBook)}</dt>
        <dd>{comparison.pricebook_snapshot_id}</dd>
      </div>
      <div>
        <dt>Prices as of</dt>
        <dd>{calendarDate(comparison.priced_as_of)}</dd>
      </div>
      {nonProduction && (
        <div>
          <dt>Status</dt>
          <dd>Non-production prices</dd>
        </div>
      )}
    </dl>
  );
}

// The Resolve answers with who confirmed them, masked for the mode. Shared by Confidence and Summary.
export function ResolvedAnswers({ gaps, mode }: { gaps: Gap[]; mode: Mode }) {
  const showAws = showsAws(mode);
  const answered = gaps.filter((gap) => gap.status === "Resolved");
  if (answered.length === 0) {
    return <p className="cp-muted">Nothing had to be decided; the {DISPLAY.intake} was complete.</p>;
  }
  return (
    <ul>
      {answered.map((gap) => {
        const parts = describeAnswer(
          gap,
          presentationCanonicalValue(gap.canonical_value, showAws),
          showAws,
        );
        return (
          <li key={gap.id}>
            <strong>{presentationGapPrompt(gap, showAws)}</strong>
            {parts.length === 1 ? (
              <> {parts[0].value}</>
            ) : (
              <ul className="cp-answer-list">
                {parts.map((part) => (
                  <li key={part.label}>
                    {part.label} <strong>{part.value}</strong>
                  </li>
                ))}
              </ul>
            )}
            <span className="cp-muted cp-small">
              {" "}Confirmed by {gap.resolved_by ?? "unknown"}
              {gap.resolved_at ? `, ${localDateTime(gap.resolved_at)}` : ""}.
            </span>
          </li>
        );
      })}
    </ul>
  );
}
