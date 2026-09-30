import { type FormEvent, useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";

import { resolveGap, type ApplicationDetail, type Gap, type GapResolution } from "../api";
import { DISPLAY, questionCount } from "../glossary";
import { presentationGapPrompt, presentationGapReason } from "../presentation";
import {
  LICENSE_QUESTIONS,
  PASSIVE_SECONDARY_QUESTION,
  PASSIVE_USE_ONLY_QUESTION,
  RUNTIME_PRESETS,
  SQL_MODELS,
  SQL_MODEL_QUESTION,
  licenseMobilityQuestion,
  type SqlModel,
} from "./answerLabels";
import { regionLabel, showsAws } from "./modeCopy";
import {
  PILOT_REGION,
  getConfirmedBy,
  hasCarriedRegionConfirmation,
  setConfirmedBy,
  type Mode,
} from "./storage";

type YesNo = "" | "yes" | "no";

const DECIMAL = /^\d+(\.\d+)?$/;
const INTEGER = /^\d+$/;

interface ResolveStepProps {
  detail: ApplicationDetail;
  mode: Mode;
  onUpdated: (detail: ApplicationDetail) => void;
}

export function ResolveStep({ detail, mode, onUpdated }: ResolveStepProps) {
  const navigate = useNavigate();
  const location = useLocation();
  const regionError = (location.state as { regionError?: string } | null)?.regionError ?? "";
  const [confirmedBy, setConfirmedByValue] = useState(getConfirmedBy());
  const openGaps = detail.gaps.filter((gap) => gap.status === "Open");
  const applicationId = detail.application.id;
  const showAws = showsAws(mode);
  const hadOpen = useRef(openGaps.length > 0);
  // One answer saves at a time, so a slower earlier response can never overwrite a newer detail.
  const [savingGapId, setSavingGapId] = useState<string | null>(null);
  const savingRef = useRef(false);
  const currentApplication = useRef(applicationId);
  currentApplication.current = applicationId;

  async function submitAnswer(gapId: string, body: GapResolution): Promise<void> {
    if (savingRef.current) {
      throw new Error("Another answer is still saving. Try again in a moment.");
    }
    savingRef.current = true;
    setSavingGapId(gapId);
    const forApplication = applicationId;
    try {
      const updated = await resolveGap(forApplication, gapId, body);
      if (currentApplication.current === forApplication) {
        onUpdated(updated);
      }
    } finally {
      savingRef.current = false;
      setSavingGapId(null);
    }
  }

  useEffect(() => {
    if (hadOpen.current && openGaps.length === 0) {
      navigate(`/estimates/${applicationId}/answer`);
    }
  }, [applicationId, navigate, openGaps.length]);

  if (openGaps.length === 0) {
    return (
      <div className="cp-page">
        <h1 className="cp-title">Nothing to resolve</h1>
        <p className="cp-lede">Every question for this estimate is answered.</p>
        <div className="cp-row">
          <Link className="cp-button cp-button-primary" to={`/estimates/${applicationId}/answer`}>
            See the answer
          </Link>
        </div>
      </div>
    );
  }

  return (
    <div className="cp-page">
      <div>
        <h1 className="cp-title">{questionCount(openGaps.length)} before we can price it.</h1>
        <p className="cp-lede">
          We never guess a missing fact. Each answer is recorded with your name and time.
        </p>
      </div>
      {regionError && (
        <p className="cp-error" role="alert">
          The region you confirmed was not recorded: {regionError} Confirm it below.
        </p>
      )}
      <section className="cp-card cp-field">
        <label htmlFor="resolve-confirmed-by">Confirmed by</label>
        <p className="cp-muted cp-small" id="resolve-confirmed-by-help">
          Asked once for this browser session and recorded with every answer.
        </p>
        <input
          aria-describedby="resolve-confirmed-by-help"
          autoComplete="name"
          id="resolve-confirmed-by"
          maxLength={200}
          onBlur={() => setConfirmedBy(confirmedBy)}
          onChange={(event) => setConfirmedByValue(event.currentTarget.value)}
          required
          value={confirmedBy}
        />
      </section>
      {openGaps.map((gap, index) => (
        <QuestionCard
          applicationId={applicationId}
          confirmedBy={confirmedBy}
          gap={gap}
          index={index + 1}
          key={gap.id}
          mode={mode}
          onSubmit={submitAnswer}
          saving={savingGapId === gap.id}
          locked={savingGapId !== null}
          showAws={showAws}
          total={openGaps.length}
        />
      ))}
      <p className="cp-muted cp-small">
        Need the full {DISPLAY.gap} detail? <Link to={`/review?applicationId=${applicationId}`}>Open the Builder review</Link>.
      </p>
    </div>
  );
}

interface QuestionCardProps {
  applicationId: string;
  confirmedBy: string;
  gap: Gap;
  index: number;
  total: number;
  mode: Mode;
  showAws: boolean;
  saving: boolean;
  locked: boolean;
  onSubmit: (gapId: string, body: GapResolution) => Promise<void>;
}

function QuestionCard({
  applicationId,
  confirmedBy,
  gap,
  index,
  total,
  mode,
  showAws,
  saving,
  locked,
  onSubmit,
}: QuestionCardProps) {
  const [error, setError] = useState("");
  const [runtimeChoice, setRuntimeChoice] = useState<"" | "730" | "220" | "custom">("");
  const [customHours, setCustomHours] = useState("");
  const [iops, setIops] = useState("");
  const [mbps, setMbps] = useState("");
  const carriedRegion = gap.kind === "ApprovedRegions" && hasCarriedRegionConfirmation(applicationId);
  const [regionConfirmed, setRegionConfirmed] = useState(carriedRegion);
  const [license, setLicense] = useState<Record<string, YesNo>>({});
  const [sqlModel, setSqlModel] = useState<SqlModel | "">("");
  const headingId = `question-${gap.id}`;
  const isSql = gap.license_product === "SQL Server";

  function answerFields(): Omit<GapResolution, "resolved_by"> | string {
    if (gap.kind === "RuntimeHours") {
      if (runtimeChoice === "") {
        return "Choose how many hours a month this runs.";
      }
      if (runtimeChoice !== "custom") {
        return { runtime_hours_month: runtimeChoice };
      }
      const value = customHours.trim();
      if (!DECIMAL.test(value) || !withinHours(value)) {
        return "Enter custom hours between 1 and 744.";
      }
      return { runtime_hours_month: value };
    }
    if (gap.kind === "StoragePerformance") {
      if (!INTEGER.test(iops.trim()) || !DECIMAL.test(mbps.trim())) {
        return "Enter the target IOPS as a whole number and the throughput in MB/s.";
      }
      return { target_iops: Number(iops.trim()), target_mbps: mbps.trim() };
    }
    if (gap.kind === "LicenseEligibility") {
      const required = [
        ...LICENSE_QUESTIONS.map(([key]) => key),
        ...(isSql ? ["aws_license_mobility_eligible", "passive_secondary"] : []),
        ...(isSql && license.passive_secondary === "yes" ? ["passive_use_only"] : []),
      ];
      if (required.some((key) => !license[key])) {
        return "Answer every license question.";
      }
      if (isSql && sqlModel === "") {
        return "Choose the Azure SQL deployment model.";
      }
      const yes = (key: string) => license[key] === "yes";
      return {
        active_software_assurance: yes("active_software_assurance"),
        azure_hybrid_benefit_eligible: yes("azure_hybrid_benefit_eligible"),
        acquired_before_2019_10_01: yes("acquired_before_2019_10_01"),
        perpetual_license: yes("perpetual_license"),
        eligible_product_version: yes("eligible_product_version"),
        ...(isSql
          ? {
              aws_license_mobility_eligible: yes("aws_license_mobility_eligible"),
              passive_secondary: yes("passive_secondary"),
              // Only a passive secondary can be passive-only; the API rejects it otherwise.
              passive_use_only: yes("passive_secondary") && yes("passive_use_only"),
              azure_sql_deployment_model: sqlModel as SqlModel,
            }
          : {}),
      };
    }
    if (!regionConfirmed) {
      return "Confirm the pricing region.";
    }
    return { azure_region: PILOT_REGION.azureRegion, aws_region: PILOT_REGION.awsRegion };
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const name = confirmedBy.trim();
    if (name.length === 0) {
      setError("Enter your name in Confirmed by first.");
      return;
    }
    const fields = answerFields();
    if (typeof fields === "string") {
      setError(fields);
      return;
    }
    setConfirmedBy(name);
    setError("");
    try {
      await onSubmit(gap.id, { resolved_by: name, ...fields });
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "The answer could not be saved.");
    }
  }

  function yesNo(key: string, label: string) {
    const groupName = `${gap.id}-${key}`;
    return (
      <fieldset className="cp-fieldset" key={key}>
        <legend>{label}</legend>
        <div className="cp-radio-row">
          {(["yes", "no"] as const).map((value) => (
            <label key={value}>
              <input
                checked={license[key] === value}
                name={groupName}
                onChange={() => setLicense((current) => ({ ...current, [key]: value }))}
                type="radio"
                value={value}
              />
              {value === "yes" ? "Yes" : "No"}
            </label>
          ))}
        </div>
        {!license[key] && <span className="cp-muted cp-small">Not answered</span>}
      </fieldset>
    );
  }

  return (
    <section aria-labelledby={headingId} className="cp-card">
      <form className="cp-page" noValidate onSubmit={handleSubmit}>
        <div>
          <p className="cp-muted cp-small">Question {index} of {total}</p>
          <h2 id={headingId}>{presentationGapPrompt(gap, showAws)}</h2>
          <p className="cp-muted">{presentationGapReason(gap, showAws)}</p>
        </div>

        {gap.kind === "RuntimeHours" && (
          <fieldset className="cp-fieldset">
            <legend>Hours a month</legend>
            <div className="cp-card-grid">
              {RUNTIME_PRESETS.map((preset) => (
                <button
                  aria-pressed={runtimeChoice === preset.value}
                  className="cp-choice"
                  key={preset.value}
                  onClick={() => setRuntimeChoice(preset.value)}
                  type="button"
                >
                  <strong>{preset.label}</strong>
                  <span>{preset.detail}</span>
                </button>
              ))}
              <button
                aria-pressed={runtimeChoice === "custom"}
                className="cp-choice"
                onClick={() => setRuntimeChoice("custom")}
                type="button"
              >
                <strong>Custom</strong>
                <span>1 to 744 hours a month</span>
              </button>
            </div>
            {runtimeChoice === "custom" && (
              <div className="cp-field">
                <label htmlFor={`${gap.id}-hours`}>Custom hours a month</label>
                <input
                  id={`${gap.id}-hours`}
                  inputMode="decimal"
                  max="744"
                  min="1"
                  onChange={(event) => setCustomHours(event.currentTarget.value)}
                  step="0.25"
                  type="number"
                  value={customHours}
                />
              </div>
            )}
          </fieldset>
        )}

        {gap.kind === "StoragePerformance" && (
          <div className="cp-card-grid">
            <div className="cp-field">
              <label htmlFor={`${gap.id}-iops`}>Target IOPS</label>
              <input
                id={`${gap.id}-iops`}
                inputMode="numeric"
                min="0"
                onChange={(event) => setIops(event.currentTarget.value)}
                step="1"
                type="number"
                value={iops}
              />
            </div>
            <div className="cp-field">
              <label htmlFor={`${gap.id}-mbps`}>Target throughput (MB/s)</label>
              <input
                id={`${gap.id}-mbps`}
                inputMode="decimal"
                min="0"
                onChange={(event) => setMbps(event.currentTarget.value)}
                step="0.1"
                type="number"
                value={mbps}
              />
            </div>
          </div>
        )}

        {gap.kind === "LicenseEligibility" && (
          <>
            {LICENSE_QUESTIONS.map(([key, label]) => yesNo(key, label))}
            {isSql && (
              <>
                {yesNo("aws_license_mobility_eligible", licenseMobilityQuestion(showAws))}
                {yesNo("passive_secondary", PASSIVE_SECONDARY_QUESTION)}
                {license.passive_secondary === "yes"
                  && yesNo("passive_use_only", PASSIVE_USE_ONLY_QUESTION)}
                <div className="cp-field">
                  <label htmlFor={`${gap.id}-sql-model`}>{SQL_MODEL_QUESTION}</label>
                  <select
                    id={`${gap.id}-sql-model`}
                    onChange={(event) => setSqlModel(event.currentTarget.value as SqlModel | "")}
                    value={sqlModel}
                  >
                    <option disabled value="">Choose a deployment model</option>
                    {SQL_MODELS.map((model) => (
                      <option key={model.value} value={model.value}>{model.label}</option>
                    ))}
                  </select>
                </div>
              </>
            )}
          </>
        )}

        {gap.kind === "ApprovedRegions" && (
          <>
            {carriedRegion && (
              <p className="cp-muted cp-small">
                You confirmed this region when you started. Save it to record it with your name.
              </p>
            )}
            <label className="cp-row">
              <input
                checked={regionConfirmed}
                onChange={(event) => setRegionConfirmed(event.currentTarget.checked)}
                type="checkbox"
              />
              <span>Price in {regionLabel(mode)}</span>
            </label>
          </>
        )}

        {error && <p className="cp-error" role="alert">{error}</p>}
        <div className="cp-row">
          <button className="cp-button cp-button-primary" disabled={locked} type="submit">
            {saving ? "Saving…" : "Save answer"}
          </button>
        </div>
      </form>
    </section>
  );
}

// Range check on the typed input only; the value is sent to the API as the user wrote it.
function withinHours(value: string): boolean {
  const [whole, fraction = ""] = value.split(".");
  const hours = Number(whole);
  if (hours < 1) {
    return false;
  }
  if (hours > 744) {
    return false;
  }
  return !(hours === 744 && /[1-9]/.test(fraction));
}
