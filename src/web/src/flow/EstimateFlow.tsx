import { useCallback, useState } from "react";
import { Link, Navigate, useLocation, useParams } from "react-router-dom";

import type { ApplicationDetail } from "../api";
import { Spine, isStepId, type StepId } from "../shell/Spine";
import { AnswerStep } from "./AnswerStep";
import { ExportStep } from "./ExportStep";
import { ModeStep } from "./ModeStep";
import { ResolveStep } from "./ResolveStep";
import {
  getEstimateMode,
  getPendingMode,
  getPendingRegionConfirmed,
  hadNothingToResolve,
  type Mode,
} from "./storage";
import { UploadFoundStep, UploadStep } from "./UploadStep";
import { useCapabilities } from "./useCapabilities";
import { useEstimate } from "./useEstimate";

export function EstimateFlow() {
  const { id = "", step } = useParams();
  if (!isStepId(step)) {
    return <Navigate replace to={id === "new" ? "/estimates/new/mode" : `/estimates/${id}/answer`} />;
  }
  if (id === "new") {
    return <NewEstimateFlow step={step} />;
  }
  return <ExistingEstimateFlow applicationId={id} step={step} />;
}

function NewEstimateFlow({ step }: { step: StepId }) {
  const { capabilities, error } = useCapabilities();
  const { search } = useLocation();
  const pendingMode = getPendingMode();
  const readyForUpload = pendingMode !== null && getPendingRegionConfirmed();

  if (step !== "mode" && step !== "upload") {
    return <Navigate replace to="/estimates/new/mode" />;
  }
  if (step === "upload" && !readyForUpload) {
    return <Navigate replace to={`/estimates/new/mode${search}`} />;
  }

  return (
    <section className="cp-page">
      <Spine
        current={step}
        done={step === "upload" ? ["mode"] : []}
        hrefFor={(target) => `/estimates/new/${target}`}
        reachable={readyForUpload ? ["mode", "upload"] : ["mode"]}
      />
      {error && <p className="cp-error" role="alert">{error}</p>}
      {step === "mode" ? (
        capabilities ? (
          <ModeStep defaultMode={capabilities.defaultMode} />
        ) : (
          !error && <p className="cp-muted">Loading…</p>
        )
      ) : (
        <UploadStep mode={pendingMode ?? "both"} />
      )}
    </section>
  );
}

function ExistingEstimateFlow({ applicationId, step }: { applicationId: string; step: StepId }) {
  const { capabilities, error: capabilitiesError } = useCapabilities();
  const { detail, setDetail, expired, error } = useEstimate(applicationId);
  const [modeOverride, setModeOverride] = useState<Mode | null>(null);

  const handleUpdated = useCallback((updated: ApplicationDetail) => setDetail(updated), [setDetail]);

  if (expired) {
    return (
      <section className="cp-page">
        <h1 className="cp-title">This estimate has expired</h1>
        <p className="cp-lede">
          Estimates are kept in memory for a limited time. Re-upload the intake workbook to rebuild it.
          The export is the lasting record.
        </p>
        <div className="cp-row">
          <Link className="cp-button cp-button-primary" to="/estimates/new/mode">New estimate</Link>
          <Link className="cp-button" to="/">Home</Link>
        </div>
      </section>
    );
  }
  if (error || capabilitiesError) {
    return (
      <section className="cp-page">
        <p className="cp-error" role="alert">{error || capabilitiesError}</p>
      </section>
    );
  }
  if (!detail || !capabilities) {
    return <p className="cp-muted">Loading…</p>;
  }

  const mode: Mode = modeOverride ?? getEstimateMode(applicationId) ?? capabilities.defaultMode;
  const openGaps = detail.gaps.filter((gap) => gap.status === "Open");
  const nothingToResolve = openGaps.length === 0 && hadNothingToResolve(applicationId);
  const answerReachable = openGaps.length === 0;

  // A deep link past Resolve with questions still open goes back to Resolve.
  if ((step === "answer" || step === "export") && !answerReachable) {
    return <Navigate replace to={`/estimates/${applicationId}/resolve`} />;
  }

  const reachable: StepId[] = ["mode", "upload", "resolve"];
  if (answerReachable) {
    reachable.push("answer", "export");
  }
  const done: StepId[] = ["mode", "upload"];
  if (answerReachable) {
    done.push("resolve");
  }
  if (step === "export") {
    done.push("answer");
  }

  return (
    <section className="cp-page">
      <Spine
        current={step}
        done={done}
        hrefFor={(target) => `/estimates/${applicationId}/${target}`}
        nothingToResolve={nothingToResolve}
        reachable={reachable}
      />
      {step === "mode" && (
        <ModeStep
          applicationId={applicationId}
          defaultMode={mode}
          nextHref={`/estimates/${applicationId}/${answerReachable ? "answer" : "resolve"}`}
          onModeChange={setModeOverride}
        />
      )}
      {step === "upload" && <UploadFoundStep detail={detail} mode={mode} />}
      {step === "resolve" && (
        <ResolveStep detail={detail} mode={mode} onUpdated={handleUpdated} />
      )}
      {step === "answer" && <AnswerStep capabilities={capabilities} detail={detail} mode={mode} />}
      {step === "export" && (
        <ExportStep capabilities={capabilities} detail={detail} mode={mode} />
      )}
    </section>
  );
}
