import { FormEvent, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import {
  getApplication,
  listApplications,
  resolveGap,
  type ApplicationDetail,
  type Gap,
} from "../api";
import { PageHeader } from "../components/PageHeader";
import { StatusCard } from "../components/StatusCard";
import {
  presentationCanonicalValue,
  presentationEvidenceUrls,
  presentationGapPrompt,
  presentationGapReason,
} from "../presentation";

export function ReviewPage() {
  const [searchParams] = useSearchParams();
  const [detail, setDetail] = useState<ApplicationDetail | null>(null);
  const [error, setError] = useState("");
  const [isSaving, setIsSaving] = useState(false);
  const activeApplicationId = useRef<string | null>(null);
  const mutationController = useRef<AbortController | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    mutationController.current?.abort();
    mutationController.current = null;
    setIsSaving(false);
    setDetail(null);
    setError("");
    const requestedId = searchParams.get("applicationId");
    activeApplicationId.current = requestedId;
    const load = requestedId
      ? getApplication(requestedId, controller.signal)
      : listApplications(controller.signal).then((applications) => {
          if (!applications[0]) {
            throw new Error("Upload an Intake before opening the review workspace.");
          }
          return getApplication(applications[0].id, controller.signal);
        });
    load.then((loaded) => {
      if (!controller.signal.aborted) {
        activeApplicationId.current = loaded.application.id;
        setDetail(loaded);
      }
    }).catch((requestError: unknown) => {
      if (requestError instanceof DOMException && requestError.name === "AbortError") {
        return;
      }
      setError(requestError instanceof Error ? requestError.message : "The review could not be loaded.");
    });
    return () => {
      controller.abort();
      mutationController.current?.abort();
      activeApplicationId.current = null;
    };
  }, [searchParams]);

  async function handleResolve(event: FormEvent<HTMLFormElement>, gap: Gap) {
    event.preventDefault();
    if (!detail) {
      return;
    }
    setError("");
    setIsSaving(true);
    mutationController.current?.abort();
    const controller = new AbortController();
    mutationController.current = controller;
    const applicationId = detail.application.id;
    const form = new FormData(event.currentTarget);
    try {
      const updated = await resolveGap(applicationId, gap.id, {
        resolved_by: String(form.get("resolvedBy") ?? ""),
        ...(gap.kind === "RuntimeHours"
          ? { runtime_hours_month: String(form.get("runtimeHours") ?? "") }
          : gap.kind === "StoragePerformance"
            ? {
                target_iops: Number(form.get("targetIops")),
                target_mbps: String(form.get("targetMbps") ?? ""),
              }
            : gap.kind === "LicenseEligibility"
              ? {
                  active_software_assurance: form.get("activeSa") === "yes",
                  azure_hybrid_benefit_eligible: form.get("azureAhb") === "yes",
                  acquired_before_2019_10_01: form.get("pre2019") === "yes",
                  perpetual_license: form.get("perpetualLicense") === "yes",
                  eligible_product_version: form.get("eligibleVersion") === "yes",
                  ...(gap.license_product === "SQL Server"
                    ? {
                        aws_license_mobility_eligible:
                          form.get("awsMobility") === "yes",
                        passive_secondary:
                          form.get("passiveSecondary") === "yes",
                        passive_use_only: form.get("passiveOnly") === "yes",
                        azure_sql_deployment_model: String(
                          form.get("sqlDeploymentModel") ?? "",
                        ) as
                          | "SqlVm"
                          | "SqlDatabaseProvisionedVCore"
                          | "SqlManagedInstanceProvisionedVCore"
                          | "SqlDatabaseServerless"
                          | "SqlDatabaseDtu",
                      }
                    : {}),
                }
            : { azure_region: "eastus2", aws_region: "us-east-1" }),
      }, controller.signal);
      if (
        !controller.signal.aborted
        && activeApplicationId.current === applicationId
      ) {
        setDetail(updated);
      }
    } catch (requestError) {
      if (requestError instanceof DOMException && requestError.name === "AbortError") {
        return;
      }
      setError(requestError instanceof Error ? requestError.message : "The Gap could not be resolved.");
    } finally {
      if (mutationController.current === controller) {
        mutationController.current = null;
        setIsSaving(false);
      }
    }
  }

  return (
    <>
      <PageHeader
        eyebrow="Reviewer workspace"
        title="Review"
        description="Resolve typed material Gaps before the CompletenessGate unlocks a ReviewBaseline."
      />
      {error && <p className="error" role="alert">{error}</p>}
      {!detail ? (
        <section className="panel">
          <p className="empty-state">No Application is available for review.</p>
        </section>
      ) : (
        <>
          <div className="card-grid">
            <StatusCard title="Application" value={detail.application.name}>
              {detail.compute_units.length} ComputeUnits, {detail.database_units.length} DatabaseUnits,
              and {detail.storage_units.length} StorageUnits.
            </StatusCard>
            <StatusCard title="CompletenessGate" value={detail.comparison.state}>
              {detail.comparison.can_export
                ? "All material Gaps are resolved; export is unlocked."
                : detail.gaps.some((gap) => gap.status === "Open")
                  ? "Headline and export remain locked while material Gaps are open."
                  : "All Gaps are resolved, but unsupported licensing or pricing mappings still block export."}
            </StatusCard>
            <StatusCard title="Open material Gaps" value={String(detail.gaps.filter((gap) => gap.status === "Open").length)}>
              Clarifications become canonical only through typed, unit-explicit forms.
            </StatusCard>
          </div>
          <section className="panel">
            <div className="section-heading">
              <h3>Clarifications</h3>
              <span>{detail.gaps.length}</span>
            </div>
            <div className="gap-list">
              {detail.gaps.map((gap) => (
                <article className="gap-card" key={gap.id}>
                  <div>
                    <span className="status">{gap.status}</span>
                    <h4>{presentationGapPrompt(gap, detail.comparison.presentation.show_aws)}</h4>
                    <p>{presentationGapReason(gap, detail.comparison.presentation.show_aws)}</p>
                  </div>
                  {gap.status === "Open" ? (
                    <form className="inline-form" onSubmit={(event) => handleResolve(event, gap)}>
                      {gap.kind === "RuntimeHours" ? (
                        <label>
                          Runtime
                          <input
                            max="744"
                            min="1"
                            name="runtimeHours"
                            required
                            step="0.25"
                            type="number"
                          />
                          <span>hours/month</span>
                        </label>
                      ) : gap.kind === "StoragePerformance" ? (
                        <>
                          <label>
                            Target IOPS
                            <input min="0" name="targetIops" required step="1" type="number" />
                          </label>
                          <label>
                            Target throughput
                            <input min="0" name="targetMbps" required step="0.1" type="number" />
                            <span>MB/s</span>
                          </label>
                        </>
                      ) : gap.kind === "LicenseEligibility" ? (
                        <>
                          {[
                            ["activeSa", "Active Software Assurance or qualifying subscription"],
                            ["azureAhb", "Azure Hybrid Benefit eligible"],
                            ["pre2019", "License acquired or true-up before October 1, 2019"],
                            ["perpetualLicense", "Perpetual license"],
                            ["eligibleVersion", "Product version eligible for the legacy BYOL path"],
                          ].map(([name, label]) => (
                            <label key={name}>
                              {label}
                              <select defaultValue="" name={name} required>
                                <option disabled value="">Choose an answer</option>
                                <option value="no">No</option>
                                <option value="yes">Yes</option>
                              </select>
                            </label>
                          ))}
                          {gap.license_product === "SQL Server" && (
                            <>
                              {[
                                [
                                  "awsMobility",
                                  detail.comparison.presentation.show_aws
                                    ? "AWS SQL License Mobility eligible"
                                    : "Secondary-cloud SQL License Mobility eligible",
                                ],
                                ["passiveSecondary", "Passive secondary"],
                                ["passiveOnly", "Secondary is strictly passive-only"],
                              ].map(([name, label]) => (
                                <label key={name}>
                                  {label}
                                  <select defaultValue="" name={name} required>
                                    <option disabled value="">Choose an answer</option>
                                    <option value="no">No</option>
                                    <option value="yes">Yes</option>
                                  </select>
                                </label>
                              ))}
                              <label>
                                Azure SQL deployment model
                                <select
                                  defaultValue=""
                                  name="sqlDeploymentModel"
                                  required
                                >
                                  <option disabled value="">Choose a deployment model</option>
                                  <option value="SqlVm">SQL Server on Azure VM</option>
                                  <option value="SqlDatabaseProvisionedVCore">Azure SQL Database provisioned vCore</option>
                                  <option value="SqlManagedInstanceProvisionedVCore">Azure SQL Managed Instance provisioned vCore</option>
                                  <option value="SqlDatabaseServerless">Azure SQL Database serverless</option>
                                  <option value="SqlDatabaseDtu">Azure SQL Database DTU</option>
                                </select>
                              </label>
                            </>
                          )}
                        </>
                      ) : (
                        <p className="assumption">
                          {detail.comparison.presentation.show_aws
                            ? "PriceBook pair: Azure East US 2 and AWS US East (N. Virginia)."
                            : "PriceBook: Azure East US 2 with a hidden comparison-region mapping."}
                        </p>
                      )}
                      <label>
                        Confirmed by
                        <input autoComplete="name" name="resolvedBy" required />
                      </label>
                      <button disabled={isSaving} type="submit">Confirm canonical value</button>
                    </form>
                  ) : (
                    <pre>
                      {JSON.stringify(
                        presentationCanonicalValue(
                          gap.canonical_value,
                          detail.comparison.presentation.show_aws,
                        ),
                        null,
                        2,
                      )}
                    </pre>
                  )}
                </article>
              ))}
            </div>
          </section>
          {detail.comparison.license_assessments.length > 0 && (
            <section className="panel">
              <h3>License assessments</h3>
              <div className="exclusion-list">
                {detail.comparison.license_assessments.map((assessment) => (
                  <article key={`${assessment.unit_id}-${assessment.product}`}>
                    <strong>{assessment.unit_id}: {assessment.product}</strong>
                    <span>{assessment.blocks_pricing ? "Blocks pricing" : "Eligible treatment recorded"}</span>
                    <p><strong>Azure:</strong> {assessment.azure_treatment}</p>
                    {detail.comparison.presentation.show_aws && (
                      <p><strong>AWS:</strong> {assessment.aws_treatment}</p>
                    )}
                    {presentationEvidenceUrls(
                      assessment.evidence_urls,
                      detail.comparison.presentation.show_aws,
                    ).map((url) => (
                      <p key={url}><a href={url} rel="noreferrer" target="_blank">{url}</a></p>
                    ))}
                  </article>
                ))}
              </div>
            </section>
          )}
          <section className="notice">
            <strong>Direct user-to-API control action</strong>
            <p>
              Gap resolution is deterministic and never routed through a model. Each answer is recorded
              with your signed-in identity and requires the Estimator role.
            </p>
          </section>
          <Link
            className="button-link"
            to={`/comparisons?applicationId=${detail.application.id}`}
          >
            Inspect comparison
          </Link>
        </>
      )}
    </>
  );
}
