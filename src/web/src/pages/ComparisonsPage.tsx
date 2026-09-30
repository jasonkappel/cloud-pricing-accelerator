import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import {
  downloadComparison,
  downloadPricedWorkbook,
  getApplication,
  listApplications,
  type ApplicationDetail,
} from "../api";
import { PageHeader } from "../components/PageHeader";
import { StatusCard } from "../components/StatusCard";
import {
  presentationEvidenceUrls,
  presentationLineEvidence,
  presentationWarnings,
} from "../presentation";

function formatMoney(value: string | null) {
  if (value === null) {
    return "Unpriced";
  }
  const [whole, fraction = "00"] = value.split(".");
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `$${grouped}.${fraction.padEnd(2, "0").slice(0, 2)}`;
}

export function ComparisonsPage() {
  const [searchParams] = useSearchParams();
  const [detail, setDetail] = useState<ApplicationDetail | null>(null);
  const [error, setError] = useState("");
  const [isWorkbookExporting, setIsWorkbookExporting] = useState(false);
  const [workbookDownloadStatus, setWorkbookDownloadStatus] = useState("");
  const comparisonExportController = useRef<AbortController | null>(null);
  const workbookExportController = useRef<AbortController | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    comparisonExportController.current?.abort();
    comparisonExportController.current = null;
    workbookExportController.current?.abort();
    workbookExportController.current = null;
    setDetail(null);
    setError("");
    setIsWorkbookExporting(false);
    setWorkbookDownloadStatus("");
    const requestedId = searchParams.get("applicationId");
    const load = requestedId
      ? getApplication(requestedId, controller.signal)
      : listApplications(controller.signal).then((applications) => {
          if (!applications[0]) {
            throw new Error("Upload an Intake before opening Comparisons.");
          }
          return getApplication(applications[0].id, controller.signal);
        });
    load.then(setDetail).catch((requestError: unknown) => {
      if (requestError instanceof DOMException && requestError.name === "AbortError") {
        return;
      }
      setError(requestError instanceof Error ? requestError.message : "The Comparison could not be loaded.");
    });
    return () => {
      controller.abort();
      comparisonExportController.current?.abort();
      workbookExportController.current?.abort();
    };
  }, [searchParams]);

  async function handleExport() {
    if (!detail) {
      return;
    }
    setError("");
    comparisonExportController.current?.abort();
    const controller = new AbortController();
    comparisonExportController.current = controller;
    try {
      await downloadComparison(detail.application.id, controller.signal);
    } catch (requestError) {
      if (requestError instanceof DOMException && requestError.name === "AbortError") {
        return;
      }
      setError(requestError instanceof Error ? requestError.message : "Export failed.");
    } finally {
      if (comparisonExportController.current === controller) {
        comparisonExportController.current = null;
      }
    }
  }

  async function handleWorkbookExport() {
    if (!detail) {
      return;
    }
    setError("");
    setWorkbookDownloadStatus("");
    setIsWorkbookExporting(true);
    workbookExportController.current?.abort();
    const controller = new AbortController();
    workbookExportController.current = controller;
    try {
      await downloadPricedWorkbook(
        detail.application.id,
        detail.application.intake_file_name,
        controller.signal,
      );
      setWorkbookDownloadStatus("Download started. Check your browser's Downloads.");
    } catch (requestError) {
      if (requestError instanceof DOMException && requestError.name === "AbortError") {
        return;
      }
      setError(
        requestError instanceof Error
          ? requestError.message
          : "Workbook export failed.",
      );
    } finally {
      if (workbookExportController.current === controller) {
        setIsWorkbookExporting(false);
        workbookExportController.current = null;
      }
    }
  }

  const comparison = detail?.comparison;
  const showAws = comparison?.presentation.show_aws ?? false;
  const visibleScenarios = comparison?.commercial_scenarios.filter(
    (scenario) => showAws || scenario.provider === "Azure",
  ).sort((left, right) => Number(left.provider === "AWS") - Number(right.provider === "AWS")) ?? [];
  const sensitivities = comparison?.breakeven_sensitivities ?? [];
  const hasDemoAssumptions = comparison?.line_items.some((line) => line.demo_assumption) ?? false;
  const visibleWarnings = presentationWarnings(
    comparison?.warnings ?? [],
    showAws,
  );
  return (
    <>
      <PageHeader
        eyebrow="Deterministic output"
        title="Comparisons"
        description="Every displayed number comes from the approved structured payload and run hash."
      />
      {error && <p className="error" role="alert">{error}</p>}
      {!detail || !comparison ? (
        <section className="panel"><p className="empty-state">No Comparison is available.</p></section>
      ) : (
        <>
          <p className="benchmark-banner">
            {showAws ? "Public cloud comparison" : "Azure public pricing scenarios"} · priced as of{" "}
            {comparison.priced_as_of}
          </p>
          {visibleWarnings.map((warning) => (
            <p className="warning" key={warning}>{warning}</p>
          ))}
          {comparison.license_assessments.length > 0 && (
            <section className="panel">
              <h3>License assessments</h3>
              <div className="exclusion-list">
                {comparison.license_assessments.map((assessment) => (
                  <article key={`${assessment.unit_id}-${assessment.product}`}>
                    <strong>{assessment.unit_id}: {assessment.product}</strong>
                    <span>{assessment.blocks_pricing ? "Blocks pricing" : "Eligible treatment recorded"}</span>
                    <p><strong>Azure:</strong> {assessment.azure_treatment}</p>
                    {showAws && <p><strong>AWS:</strong> {assessment.aws_treatment}</p>}
                    {presentationEvidenceUrls(assessment.evidence_urls, showAws).map((url) => (
                      <p key={url}><a href={url} rel="noreferrer" target="_blank">{url}</a></p>
                    ))}
                  </article>
                ))}
              </div>
            </section>
          )}
          <div className="card-grid">
            {visibleScenarios.map((scenario) => (
              <StatusCard
                key={scenario.id}
                title={scenario.label}
                value={scenario.available ? formatMoney(scenario.monthly_total) : "Unavailable"}
              >
                {scenario.available
                  ? `Covered ${formatMoney(scenario.covered_monthly_total)} · Uncovered ${formatMoney(scenario.uncovered_monthly_total)}`
                  : scenario.unavailable_reason}
              </StatusCard>
            ))}
            <StatusCard title="CompletenessGate" value={comparison.state}>
              Run hash: <code>{comparison.run_hash.slice(0, 12)}</code>
            </StatusCard>
          </div>
          <section className="panel">
            <h3>Commitment assumptions</h3>
            <p>
              {comparison.assumptions.commitment_term_years}-year ·{" "}
              {comparison.assumptions.commitment_payment_option} ·{" "}
              {comparison.assumptions.commitment_utilization_percent}% utilization ·{" "}
              {comparison.assumptions.commitment_tenancy} tenancy
            </p>
            <p className="assumption">
              Commitments are workload-isolated. Software, storage, network, and other
              uncovered meters remain at public List rates.
            </p>
          </section>
          {comparison.state === "ReviewBaseline" && (
            <section className="panel">
              <h3>Enterprise breakeven sensitivity</h3>
              <p className="assumption">
                Hypothetical public-price sensitivity only—not an EA, EDP, private offer,
                or actual contracted price.
              </p>
              {showAws && hasDemoAssumptions && (
                <p className="assumption">
                  Azure RHEL demo assumptions affect these modeled parity figures.
                  Target discounts are sensitivities, not provider verdicts.
                </p>
              )}
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Public basis</th>
                      <th>Reference discount</th>
                      <th>{showAws ? "Target discount to parity" : "Azure discount to parity"}</th>
                      <th>{showAws ? "Additional target advantage" : "Additional Azure advantage"}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {sensitivities.flatMap((sensitivity) =>
                      sensitivity.available
                        ? sensitivity.points.map((point) => (
                            <tr key={`${sensitivity.id}-${point.reference_discount_percent}`}>
                              <td>{sensitivity.label}</td>
                              <td>{point.reference_discount_percent}%</td>
                              <td>
                                {showAws
                                  ? `${sensitivity.target_provider ?? "Equal"}: ${point.target_discount_to_parity_percent}%`
                                  : sensitivity.target_provider === "Azure"
                                    ? `${point.target_discount_to_parity_percent}%`
                                    : "Already at or below parity"}
                              </td>
                              <td>
                                {showAws || sensitivity.target_provider === "Azure"
                                  ? `${point.additional_discount_advantage_percent} points`
                                  : "0.00 points"}
                              </td>
                            </tr>
                          ))
                        : [
                            <tr key={`${sensitivity.id}-unavailable`}>
                              <td>{sensitivity.label}</td>
                              <td colSpan={3}>
                                Unavailable until the complete public-price basis is approved.
                              </td>
                            </tr>,
                          ],
                    )}
                  </tbody>
                </table>
              </div>
            </section>
          )}
          {showAws && comparison.state === "ReviewBaseline" && (
            <section className="panel">
              <h3>Headline delta</h3>
              <p className="headline-delta">
                {hasDemoAssumptions
                  ? `Modeled public-list difference: ${formatMoney(comparison.headline_delta)} per month. Demo assumptions affect this comparison; do not use it as a final figure.`
                  : comparison.cheaper_cloud
                  ? `Public-list monthly totals: Azure ${formatMoney(comparison.azure_monthly_total)}, AWS ${formatMoney(comparison.aws_monthly_total)}. Difference: ${formatMoney(comparison.headline_delta)} per month.`
                  : "The public-list monthly totals are equal."}
              </p>
              <p>This informs the platform decision; it does not make one.</p>
            </section>
          )}
          {showAws && comparison.state === "ReviewBaseline" && (
            <section className="panel">
              <h3>Ranked cost drivers</h3>
              <ol className="driver-list">
                {comparison.cost_drivers.map((driver) => (
                  <li key={driver.line_id}>
                    <strong>{driver.component}</strong>
                    <span>
                      {hasDemoAssumptions
                        ? `Modeled difference ${formatMoney(driver.delta)}`
                        : `Difference ${formatMoney(driver.delta)}`} ·{" "}
                      {driver.share_of_absolute_delta_percent}% of absolute variance
                      {comparison.line_items.some((line) => line.id === driver.line_id && line.demo_assumption)
                        ? " · Demo assumption"
                        : ""}
                    </span>
                  </li>
                ))}
              </ol>
            </section>
          )}
          <section className="panel">
            <h3>Line items</h3>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Unit / component</th>
                    <th>Match</th>
                    <th>Status</th>
                    {showAws && <th>AWS List</th>}
                    <th>{showAws ? "Azure List" : "Azure"}</th>
                    <th>{showAws ? "Formula / evidence" : "Evidence"}</th>
                  </tr>
                </thead>
                <tbody>
                  {comparison.line_items.map((line) => (
                    <tr key={line.id}>
                      <td><strong>{line.unit_name}</strong><br />{line.component}</td>
                      <td>{line.match_class}</td>
                      <td>
                        <span className="status">
                          {line.status}{line.demo_assumption ? " · Demo assumption" : ""}
                        </span>
                      </td>
                      {showAws && <td>{formatMoney(line.aws_amount)}</td>}
                      <td className={showAws && line.higher_cloud === "Azure" ? "cost-higher" : ""}>
                        {formatMoney(line.azure_amount)}
                      </td>
                      <td>
                        {showAws && <code>{line.formula}</code>}
                        <p>{presentationLineEvidence(line, showAws)}</p>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
          <section className="panel">
            <h3>ExcludedCostLedger</h3>
            <div className="exclusion-list">
              {comparison.excluded_cost_ledger.map((entry) => (
                <article key={entry.id}>
                  <strong>{entry.category}</strong>
                  <span>{entry.materiality}</span>
                  <p>{entry.rationale}</p>
                  <code>{entry.policy_id}</code>
                </article>
              ))}
            </div>
          </section>
          <section className="panel evidence-grid">
            <h3>Reconciliation evidence</h3>
            <dl>
              <dt>PriceBook snapshot</dt>
              <dd>{comparison.pricebook_snapshot_id}</dd>
              <dt>PriceBook hash</dt>
              <dd><code>{comparison.pricebook_content_hash}</code></dd>
              <dt>Source published snapshot</dt>
              <dd>{comparison.source_pricebook_snapshot_id}</dd>
              <dt>Source published hash</dt>
              <dd><code>{comparison.source_pricebook_content_hash}</code></dd>
              <dt>SkuMap approval</dt>
              <dd>
                {comparison.skumap_approval.approver} ·{" "}
                {new Date(comparison.skumap_approval.approved_at).toISOString()}
                {comparison.skumap_approval.non_production ? " · Non-production" : ""}
              </dd>
              <dt>SkuMap digest</dt>
              <dd><code>{comparison.skumap_content_digest}</code></dd>
              <dt>Calculator rule</dt>
              <dd>{comparison.calculator_rule_version}</dd>
              <dt>Run hash</dt>
              <dd><code>{comparison.run_hash}</code></dd>
            </dl>
          </section>
          <div className="action-row">
            <Link to={`/review?applicationId=${detail.application.id}`}>Resolve Gaps</Link>
            <button
              disabled={!comparison.can_export || isWorkbookExporting}
              onClick={handleWorkbookExport}
              type="button"
            >
              {isWorkbookExporting ? "Preparing workbook..." : "Download priced workbook"}
            </button>
            <button disabled={!comparison.can_export} onClick={handleExport} type="button">
              Download evidence JSON
            </button>
          </div>
          <p aria-live="polite" className="download-status" role="status">
            {workbookDownloadStatus}
          </p>
        </>
      )}
    </>
  );
}
