import { type FormEvent, useCallback, useEffect, useId, useState } from "react";

import {
  getStagedRun,
  listStagedRuns,
  recordSnapshotDecision,
  type SnapshotDecisionKind,
  type StagedRunDetail,
  type StagedRunList,
  type StagedRunState,
  type StagedRunSummary,
} from "../api";
import { localDateTime } from "../format";
import { useCapabilities } from "../flow/useCapabilities";
import { DISPLAY, capitalize } from "../glossary";
import { PriceBookCard } from "./homeParts";

const STATE_LABELS: Record<StagedRunState, string> = {
  AwaitingSkuMapReview: "Waiting for SkuMap review",
  AwaitingApproval: "Waiting for approval",
  Approved: "Approved, not yet published",
  Published: "Published",
  Blocked: "Blocked",
};

const REGION_LABELS: Record<string, string> = { azureRegion: "Azure", awsRegion: "AWS" };

function when(iso: string | null | undefined): string {
  return iso ? localDateTime(iso) : "Not recorded";
}

function text(value: string | number | null | undefined): string {
  return value === null || value === undefined ? "Not recorded" : String(value);
}

export function PriceBookPage() {
  const { capabilities, error, reload } = useCapabilities();
  // The staged snapshots stay reachable when pricing is refused, so the first (or a replacement) snapshot can be
  // reviewed, approved, and published.
  return (
    <section className="cp-page">
      <div>
        <h1 className="cp-title">{capitalize(DISPLAY.priceBook)}</h1>
        <p className="cp-lede">
          Every estimate is priced from one {DISPLAY.priceBook} snapshot, stamped on every page and file.
        </p>
      </div>
      {error ? (
        <section className="cp-card" aria-labelledby="current-heading">
          <h2 id="current-heading">Current {DISPLAY.priceBook}</h2>
          <p className="cp-error" role="alert">{error}</p>
        </section>
      ) : capabilities ? (
        <PriceBookCard capabilities={capabilities} />
      ) : (
        <p className="cp-muted">Loading…</p>
      )}
      <StagedSnapshots onDecided={reload} />
      <section className="cp-card">
        <h2>How it changes</h2>
        <p>
          A new snapshot comes only from the in-tenant price harvester. It is staged, then a SkuMapReviewer
          reviews it and a SnapshotApprover approves it (one person may hold both roles). Publishing is a separate
          step. There is no refresh button on purpose.
        </p>
      </section>
    </section>
  );
}

function StagedSnapshots({ onDecided }: { onDecided: () => void }) {
  const [list, setList] = useState<StagedRunList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<StagedRunSummary | null>(null);
  const [openCount, setOpenCount] = useState(0);

  const load = useCallback((signal?: AbortSignal) => {
    setError(null);
    listStagedRuns(signal)
      .then(setList)
      .catch((reason: unknown) => {
        if (!signal?.aborted) {
          setError(reason instanceof Error ? reason.message : "Could not load staged snapshots.");
        }
      });
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    load(controller.signal);
    return () => controller.abort();
  }, [load]);

  return (
    <section className="cp-card" aria-labelledby="staged-heading">
      <h2 id="staged-heading">Staged snapshots</h2>
      {error && <p className="cp-error" role="alert">{error}</p>}
      {!error && !list && <p className="cp-muted">Loading…</p>}
      {list && !list.configured && (
        <p className="cp-muted">Snapshot review and approval is not configured on this deployment.</p>
      )}
      {list?.configured && list.runs.length === 0 && (
        <p className="cp-muted">No staged snapshots. The harvester stages one after each run.</p>
      )}
      {list?.configured && list.runs.length > 0 && (
        <table className="cp-table">
          <thead>
            <tr>
              <th scope="col">Snapshot</th>
              <th scope="col">Staged</th>
              <th scope="col">State</th>
              <th scope="col"><span className="visually-hidden">Open</span></th>
            </tr>
          </thead>
          <tbody>
            {list.runs.map((run) => (
              <tr key={`${run.snapshotId}/${run.runId}`}>
                <td>{run.snapshotId}</td>
                <td>{when(run.stagedAt)}</td>
                <td><span className="cp-badge">{STATE_LABELS[run.state]}</span></td>
                <td>
                  <button
                    type="button"
                    className="cp-button"
                    aria-pressed={selected?.runId === run.runId && selected.snapshotId === run.snapshotId}
                    onClick={() => { setSelected(run); setOpenCount((count) => count + 1); }}
                  >
                    Open
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {selected && (
        <StagedRunPanel
          key={`${selected.snapshotId}/${selected.runId}/${openCount}`}
          summary={selected}
          onDecided={() => { load(); onDecided(); }}
        />
      )}
    </section>
  );
}

function StagedRunPanel({ summary, onDecided }: { summary: StagedRunSummary; onDecided: () => void }) {
  const [run, setRun] = useState<StagedRunDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  // After a refused decision, show what the API holds now so stale digests are never resent.
  const refresh = () => {
    getStagedRun(summary.snapshotId, summary.runId)
      .then(setRun)
      .catch(() => undefined)
      .finally(onDecided);
  };

  useEffect(() => {
    const controller = new AbortController();
    getStagedRun(summary.snapshotId, summary.runId, controller.signal)
      .then(setRun)
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) {
          setError(reason instanceof Error ? reason.message : "Could not load the staged snapshot.");
        }
      });
    return () => controller.abort();
  }, [summary.snapshotId, summary.runId]);

  if (error) {
    return <p className="cp-error" role="alert">{error}</p>;
  }
  if (!run) {
    return <p className="cp-muted">Loading…</p>;
  }
  const diff = run.extract.diff;
  return (
    <article className="cp-staged-run" aria-label={`Staged snapshot ${run.snapshotId}`}>
      <h3>{run.snapshotId}: {STATE_LABELS[run.state]}</h3>
      {run.problems.length > 0 && (
        <div className="cp-error" role="alert">
          <p>This snapshot cannot be reviewed or approved:</p>
          <ul>{run.problems.map((problem) => <li key={problem}>{problem}</li>)}</ul>
        </div>
      )}
      <dl className="cp-stamp">
        <div><dt>Captured</dt><dd>{when(run.capturedAt)}</dd></div>
        <div><dt>Validated</dt><dd>{when(run.validatedAt)}</dd></div>
        <div><dt>Price rows</dt><dd>{text(run.rowCount)}</dd></div>
        <div>
          <dt>Regions</dt>
          <dd>{run.scope ? Object.entries(run.scope).map(([key, value]) => `${REGION_LABELS[key] ?? key} ${String(value)}`).join(", ") : "Not recorded"}</dd>
        </div>
        <div><dt>Previous snapshot</dt><dd>{run.baselineSnapshotId ?? "None (first snapshot)"}</dd></div>
        <div><dt>Rate extract</dt><dd>{text(run.extractSnapshotId)}</dd></div>
      </dl>

      <h4>Validation report</h4>
      <p>
        Status: {text(run.validation.status)}.{" "}
        {run.validation.bootstrap
          ? "First snapshot, so there is no row-level comparison."
          : `Rows added ${text(run.validation.rowChanges.added)}, retired ${text(run.validation.rowChanges.retired)}, changed ${text(run.validation.rowChanges.changed)}; material rate changes ${text(run.validation.rowChanges.materialRateChangeCount)}.`}
      </p>
      {run.validation.failures.length > 0 && (
        <ul>{run.validation.failures.map((failure) => <li key={failure}>{failure}</li>)}</ul>
      )}

      <h4>Coverage</h4>
      <ul className="cp-checklist">
        {run.validation.coverage.map((item) => (
          <li key={item.check}>
            <span className={item.covered ? "cp-check-icon cp-check-ok" : "cp-check-icon cp-check-caution"} aria-hidden="true">
              {item.covered ? "✓" : "!"}
            </span>
            {item.check}: {item.covered ? "found" : "missing"}
          </li>
        ))}
      </ul>

      <h4>Rate changes</h4>
      {diff === null ? (
        <p className="cp-muted">No previous rate extract to compare with.</p>
      ) : (
        <>
          <p>
            Compared with {text(diff.baselineSnapshotId)}: {text(diff.changedCount)} changed,{" "}
            {text(diff.addedCount)} added, {text(diff.removedCount)} removed, {text(diff.unchangedCount)} unchanged.
            Rates are shown exactly as derived.
          </p>
          <table className="cp-table">
            <thead>
              <tr>
                <th scope="col">Rate</th>
                <th scope="col">Before</th>
                <th scope="col">After</th>
                <th scope="col">Change</th>
                <th scope="col">Change %</th>
                <th scope="col">Status</th>
              </tr>
            </thead>
            <tbody>
              {diff.rates.map((rate) => (
                <tr key={rate.rateKey ?? ""}>
                  <td>
                    {text(rate.rateKey)}
                    {rate.assumed && <> <span className="cp-badge">{DISPLAY.demoAssumption}</span></>}
                  </td>
                  <td>{rate.old ?? "None"}</td>
                  <td>{rate.new ?? "None"}</td>
                  <td>{rate.change ?? "None"}</td>
                  <td>{rate.percentChange ?? "None"}</td>
                  <td>{text(rate.status)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      <h4>Sign-off</h4>
      <dl className="cp-stamp">
        <div>
          <dt>SkuMap review</dt>
          <dd>{run.review ? `${run.review.reviewerDisplayName}, ${when(run.review.reviewedAt)}` : "Not yet reviewed"}</dd>
        </div>
        <div>
          <dt>Approval</dt>
          <dd>
            {run.approval
              ? `${run.approval.approverDisplayName}, ${when(run.approval.approvedAt)}${run.approval.nonProduction ? " (non-production evidence)" : ""}`
              : "Not yet approved"}
          </dd>
        </div>
        <div>
          <dt>Publication</dt>
          <dd>
            {run.state === "Published" && run.publication
              ? `${text(run.publication.publishedBy)}, ${when(run.publication.publishedAt)}${run.publication.current ? " (current price book)" : ` (current price book is ${text(run.publication.currentSnapshotId)})`}`
              : "Not yet published"}
          </dd>
        </div>
      </dl>
      <details>
        <summary>Digests being signed</summary>
        <dl className="cp-stamp cp-digests">
          <div><dt>Stage manifest</dt><dd>{text(run.stageManifestDigest)}</dd></div>
          <div><dt>Rate extract</dt><dd>{text(run.extractDigest)}</dd></div>
          <div><dt>Evidence</dt><dd>{text(run.evidenceDigest)}</dd></div>
          <div><dt>SkuMap</dt><dd>{run.skuMapDigest}</dd></div>
          <div><dt>Price rows</dt><dd>{text(run.contentHash)}</dd></div>
        </dl>
      </details>
      {run.actions.waiting && <p className="cp-muted">{run.actions.waiting}</p>}
      {run.actions.canReview && (
        <DecisionForm
          run={run}
          kind="skumap-review"
          onDone={(next) => { setRun(next); onDecided(); }}
          onFailed={refresh}
        />
      )}
      {run.actions.canApprove && (
        <DecisionForm
          run={run}
          kind="approval"
          onDone={(next) => { setRun(next); onDecided(); }}
          onFailed={refresh}
        />
      )}
      {run.actions.canPublish && (
        <DecisionForm
          run={run}
          kind="publish"
          onDone={(next) => { setRun(next); onDecided(); }}
          onFailed={refresh}
        />
      )}
    </article>
  );
}

const SIGNED_NOTE =
  "Your name and sign-in ID are recorded from your sign-in and signed with this deployment's approval key.";

const DECISION_COPY: Record<
  SnapshotDecisionKind,
  { attest: string; button: string; busy: string; note: string }
> = {
  "skumap-review": {
    attest: "I reviewed the SkuMap mappings against this snapshot's rate extract, coverage, and rate changes.",
    button: "Record SkuMap review",
    busy: "Recording…",
    note: SIGNED_NOTE,
  },
  approval: {
    attest: "I checked the validation report, coverage, and rate changes above, and I approve this snapshot.",
    button: "Approve snapshot",
    busy: "Recording…",
    note: SIGNED_NOTE,
  },
  publish: {
    attest: "I want this approved snapshot to become the current price book for new estimates.",
    button: "Publish snapshot",
    busy: "Publishing… this can take a few minutes",
    note: "Your name and sign-in ID are recorded with the publication. Publishing is refused if the current price book changed since this run was validated.",
  },
};

function DecisionForm({
  run,
  kind,
  onDone,
  onFailed,
}: {
  run: StagedRunDetail;
  kind: SnapshotDecisionKind;
  onDone: (run: StagedRunDetail) => void;
  onFailed: () => void;
}) {
  const checkboxId = useId();
  const [attested, setAttested] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const copy = DECISION_COPY[kind];

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!attested || busy) {
      return;
    }
    setBusy(true);
    setError(null);
    recordSnapshotDecision(run, kind)
      .then(onDone)
      .catch((reason: unknown) => {
        setError(reason instanceof Error ? reason.message : "The decision was not recorded.");
        setAttested(false);
        onFailed();
      })
      .finally(() => setBusy(false));
  };

  return (
    <form className="cp-decision" onSubmit={submit}>
      <p className="cp-small">{copy.note}</p>
      <label htmlFor={checkboxId}>
        <input
          id={checkboxId}
          type="checkbox"
          checked={attested}
          onChange={(event) => setAttested(event.target.checked)}
        />{" "}
        {copy.attest}
      </label>
      {error && <p className="cp-error" role="alert">{error}</p>}
      <button type="submit" className="cp-button cp-button-primary" disabled={!attested || busy}>
        {busy ? copy.busy : copy.button}
      </button>
    </form>
  );
}
