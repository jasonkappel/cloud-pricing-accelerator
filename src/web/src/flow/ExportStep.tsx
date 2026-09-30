import { useState } from "react";
import { Link } from "react-router-dom";

import {
  downloadComparison,
  downloadPricedWorkbook,
  type ApplicationDetail,
  type Capabilities,
  type WorkbookView,
} from "../api";
import { DISPLAY, capitalize, hoursLabel } from "../glossary";
import { NotIncludedLine, Stamp } from "./answerParts";
import type { Mode } from "./storage";

interface ExportStepProps {
  capabilities: Capabilities;
  detail: ApplicationDetail;
  mode: Mode;
}

const MODE_TO_VIEW: Record<Mode, WorkbookView> = { both: "both", azure: "azure", aws: "aws" };

export function ExportStep({ capabilities, detail, mode }: ExportStepProps) {
  const comparison = detail.comparison;
  const applicationId = detail.application.id;
  const canExport = comparison.can_export;
  const [busy, setBusy] = useState<"" | "workbook" | "audit">("");
  const [error, setError] = useState("");
  const workbookView = MODE_TO_VIEW[mode];

  async function run(kind: "workbook" | "audit") {
    setBusy(kind);
    setError("");
    try {
      if (kind === "workbook") {
        await downloadPricedWorkbook(applicationId, detail.application.intake_file_name, undefined, workbookView);
      } else {
        await downloadComparison(applicationId);
      }
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "The download failed.");
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="cp-page">
      <div>
        <h1 className="cp-title">Export</h1>
        <p className="cp-lede">
          The export is the lasting record. Estimates in this accelerator are kept for{" "}
          {hoursLabel(capabilities.applicationMaxAgeHours)}.
        </p>
      </div>
      {!canExport && (
        <p className="cp-callout" role="status">
          Export is locked until the estimate is ready. <Link to={`/estimates/${applicationId}/answer`}>Back to the answer</Link>.
        </p>
      )}
      <div className="cp-card-grid">
        <section className="cp-card">
          <h2>Summary</h2>
          <p>A one-page summary to print or save as PDF.</p>
          {canExport ? (
            <Link className="cp-button cp-button-primary" to={`/estimates/${applicationId}/summary`}>
              Open the printable summary
            </Link>
          ) : (
            <button className="cp-button" disabled type="button">Open the printable summary</button>
          )}
        </section>
        <section className="cp-card">
          <h2>Workbook</h2>
          <p>
            Your {DISPLAY.intake} with every priced line, formula, and source added.
            {mode === "aws" && " Shows AWS only."}
            {mode === "azure" && " Shows Azure only."}
          </p>
          <button
            className="cp-button cp-button-primary"
            disabled={!canExport || busy !== ""}
            onClick={() => void run("workbook")}
            type="button"
          >
            {busy === "workbook" ? "Preparing…" : "Download workbook"}
          </button>
        </section>
        <section className="cp-card">
          <h2>Audit file</h2>
          <p>The full comparison as JSON, with the run hash, for anyone who needs to check the math.</p>
          <button
            className="cp-button"
            disabled={!canExport || busy !== ""}
            onClick={() => void run("audit")}
            type="button"
          >
            {busy === "audit" ? "Preparing…" : "Download audit file"}
          </button>
        </section>
      </div>
      {error && <p className="cp-error" role="alert">{error}</p>}

      <section className="cp-card">
        <h2>Always included</h2>
        <ul>
          <li>The label &ldquo;{capitalize(DISPLAY.listView)} run-rate benchmark&rdquo;.</li>
          <li>The {DISPLAY.excludedCostLedger} list, next to every figure.</li>
          <li>Every {DISPLAY.demoAssumption}, named as one.</li>
          <li>The region, {DISPLAY.priceBook}, and prices-as-of date below.</li>
        </ul>
        <NotIncludedLine comparison={comparison} />
      </section>
      <Stamp comparison={comparison} mode={mode} nonProduction={capabilities.priceBook.nonProduction} />
    </div>
  );
}
