import { type DragEvent, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { resolveGap, uploadIntake, type ApplicationDetail } from "../api";
import { DISPLAY, capitalize, questionCount } from "../glossary";
import { calendarDate, number } from "../format";
import { presentationGapPrompt } from "../presentation";
import { regionLabel, showsAws } from "./modeCopy";
import {
  PILOT_REGION,
  clearPending,
  getConfirmedBy,
  carryRegionConfirmation,
  getPendingRegionConfirmed,
  markNothingToResolve,
  rememberEstimate,
  setEstimateMode,
  type Mode,
} from "./storage";

export const SAMPLE_INTAKE_PATH = "/synthetic_intake_completed.xlsx";
const SAMPLE_INTAKE_NAME = "synthetic_intake_completed.xlsx";

function isAcceptable(file: File | undefined): file is File {
  return Boolean(file && file.name.toLowerCase().endsWith(".xlsx") && file.size > 0);
}

export function UploadStep({ mode }: { mode: Mode }) {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const isSample = searchParams.get("sample") === "1";
  const [file, setFile] = useState<File | null>(null);
  const [error, setError] = useState("");
  const [isUploading, setIsUploading] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const inFlight = useRef(false);

  function select(candidate: File | undefined) {
    if (!isAcceptable(candidate)) {
      setFile(null);
      setError("Choose an OpenXML .xlsx intake workbook.");
      return false;
    }
    setFile(candidate);
    setError("");
    return true;
  }

  function handleDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    if (isUploading) {
      return;
    }
    if (inputRef.current) {
      inputRef.current.value = "";
    }
    if (event.dataTransfer.files.length !== 1) {
      setFile(null);
      setError("Drop one OpenXML .xlsx intake workbook.");
      return;
    }
    select(event.dataTransfer.files[0]);
  }

  async function upload(candidate: File) {
    if (inFlight.current) {
      return;
    }
    inFlight.current = true;
    setIsUploading(true);
    setError("");
    try {
      const detail = await uploadIntake(candidate);
      const applicationId = detail.application.id;
      setEstimateMode(applicationId, mode);
      rememberEstimate({
        id: applicationId,
        name: detail.application.name,
        expiresAt: detail.expires_at ?? "",
      });

      // Post the region confirmed in Mode once, when this session already knows "Confirmed by".
      // Otherwise Resolve asks for the name and records the region with it.
      let current = detail;
      let regionError = "";
      const regionGap = detail.gaps.find(
        (gap) => gap.kind === "ApprovedRegions" && gap.status === "Open",
      );
      const confirmedBy = getConfirmedBy();
      if (regionGap && getPendingRegionConfirmed() && !confirmedBy) {
        carryRegionConfirmation(applicationId);
      }
      if (regionGap && getPendingRegionConfirmed() && confirmedBy) {
        try {
          current = await resolveGap(applicationId, regionGap.id, {
            resolved_by: confirmedBy,
            azure_region: PILOT_REGION.azureRegion,
            aws_region: PILOT_REGION.awsRegion,
          });
        } catch (requestError) {
          regionError = requestError instanceof Error
            ? requestError.message
            : "The region could not be recorded.";
        }
      }
      clearPending();
      if (!current.gaps.some((gap) => gap.status === "Open")) {
        markNothingToResolve(applicationId);
      }
      navigate(
        regionError ? `/estimates/${applicationId}/resolve` : `/estimates/${applicationId}/upload`,
        { replace: true, state: regionError ? { regionError } : undefined },
      );
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "The intake workbook was rejected.");
      inFlight.current = false;
      setIsUploading(false);
    }
  }

  async function uploadSample() {
    setError("");
    try {
      const response = await fetch(SAMPLE_INTAKE_PATH);
      if (!response.ok) {
        throw new Error(`The sample workbook could not be loaded (status ${response.status}).`);
      }
      const blob = await response.blob();
      await upload(new File([blob], SAMPLE_INTAKE_NAME, {
        type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      }));
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "The sample workbook could not be loaded.");
    }
  }

  return (
    <div className="cp-page">
      <div>
        <h1 className="cp-title">Upload the {DISPLAY.intake}</h1>
        <p className="cp-lede">
          We read servers, databases, and storage from the workbook, check it is safe to open, and list
          anything we need you to answer.
        </p>
      </div>
      {isSample && (
        <section className="cp-card">
          <h2>Sample run</h2>
          <p className="cp-muted">
            Uses the synthetic sample {DISPLAY.intake} that ships with this accelerator. It contains no real
            customer data.
          </p>
          <button
            className="cp-button cp-button-primary"
            disabled={isUploading}
            onClick={uploadSample}
            type="button"
          >
            {isUploading ? "Checking the workbook…" : "Upload the sample workbook"}
          </button>
        </section>
      )}
      <section className="cp-card">
        <form
          className="cp-page"
          onSubmit={(event) => {
            event.preventDefault();
            if (!isAcceptable(file ?? undefined)) {
              setError("Choose an OpenXML .xlsx intake workbook.");
              return;
            }
            void upload(file as File);
          }}
        >
          <div
            aria-disabled={isUploading}
            className={`file-picker${isUploading ? " disabled" : ""}`}
            onDragOver={(event) => event.preventDefault()}
            onDrop={handleDrop}
          >
            <label className="visually-hidden" htmlFor="intake-file">{capitalize(DISPLAY.intake)}</label>
            <input
              accept=".xlsx"
              className="visually-hidden"
              disabled={isUploading}
              id="intake-file"
              onChange={(event) => {
                if (!select(event.currentTarget.files?.[0])) {
                  event.currentTarget.value = "";
                }
              }}
              ref={inputRef}
              tabIndex={-1}
              type="file"
            />
            <button
              className="cp-button"
              disabled={isUploading}
              onClick={() => {
                const input = inputRef.current;
                if (input) {
                  input.value = "";
                  input.click();
                }
              }}
              type="button"
            >
              Choose .xlsx file
            </button>
            <span aria-live="polite" className="file-picker-name">
              {file?.name ?? "or drag and drop the workbook here"}
            </span>
          </div>
          <p className="cp-muted cp-small">
            OpenXML .xlsx only. Macros, external links, encrypted files, formulas, and oversized packages
            are rejected before anything is read.
          </p>
          <div className="cp-row">
            <button className="cp-button cp-button-primary" disabled={isUploading || !file} type="submit">
              {isUploading ? "Checking the workbook…" : "Upload"}
            </button>
          </div>
        </form>
      </section>
      {error && <p className="cp-error" role="alert">{error}</p>}
    </div>
  );
}

export function UploadFoundStep({ detail, mode }: { detail: ApplicationDetail; mode: Mode }) {
  const summary = detail.intake_summary;
  const openGaps = detail.gaps.filter((gap) => gap.status === "Open");
  const regionGap = detail.gaps.find((gap) => gap.kind === "ApprovedRegions");
  const applicationId = detail.application.id;
  const nextHref = `/estimates/${applicationId}/${openGaps.length > 0 ? "resolve" : "answer"}`;
  const showAws = showsAws(mode);

  return (
    <div className="cp-page">
      <div>
        <h1 className="cp-title">{detail.application.name}</h1>
        <p className="cp-lede">From {detail.application.intake_file_name}.</p>
      </div>

      <section aria-labelledby="found-heading" className="cp-card">
        <h2 id="found-heading">What we found</h2>
        {summary ? (
          <dl className="cp-facts">
            <div>
              <dt>Servers</dt>
              <dd>{number(summary.server_count)}</dd>
              <dd className="cp-muted cp-small">{number(summary.server_vcpu)} vCPU</dd>
            </div>
            <div>
              <dt>Databases</dt>
              <dd>{number(summary.database_count)}</dd>
            </div>
            <div>
              <dt>Storage</dt>
              <dd>{number(summary.storage_count)}</dd>
              <dd className="cp-muted cp-small">{number(summary.storage_allocated_gb)} GB allocated</dd>
            </div>
            <div>
              <dt>Questions for you</dt>
              <dd>{number(summary.open_question_count)}</dd>
            </div>
          </dl>
        ) : (
          <p className="cp-muted">The API did not return an intake summary.</p>
        )}
      </section>

      <section aria-labelledby="checked-heading" className="cp-card">
        <h2 id="checked-heading">What we checked</h2>
        <ul className="cp-checklist">
          <li>
            <span aria-hidden="true" className="cp-check-icon cp-check-ok">✓</span>
            <span><span className="visually-hidden">Passed: </span>
              The workbook is a safe OpenXML file: no macros, external links, encryption, or formulas.
            </span>
          </li>
          <li>
            <span aria-hidden="true" className="cp-check-icon cp-check-ok">✓</span>
            <span><span className="visually-hidden">Passed: </span>
              Prices come from the {DISPLAY.priceBook} {detail.comparison.pricebook_snapshot_id}, as of{" "}
              {calendarDate(detail.comparison.priced_as_of)}.
            </span>
          </li>
          {regionGap?.status === "Resolved" && (
            <li>
              <span aria-hidden="true" className="cp-check-icon cp-check-ok">✓</span>
              <span><span className="visually-hidden">Passed: </span>
                Region confirmed: {regionLabel(mode)}.
              </span>
            </li>
          )}
          {openGaps.map((gap) => (
            <li key={gap.id}>
              <span aria-hidden="true" className="cp-check-icon cp-check-caution">!</span>
              <span><span className="visually-hidden">Needs an answer: </span>
                {presentationGapPrompt(gap, showAws)}
              </span>
            </li>
          ))}
        </ul>
      </section>

      <div className="cp-row">
        <Link className="cp-button cp-button-primary" to={nextHref}>
          {openGaps.length > 0
            ? `Answer ${questionCount(openGaps.length)}`
            : "Ready to price. See the answer"}
        </Link>
      </div>
    </div>
  );
}
