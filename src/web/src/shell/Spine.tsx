import { Link } from "react-router-dom";

export type StepId = "mode" | "upload" | "resolve" | "answer" | "export";

export const STEPS: Array<{ id: StepId; label: string }> = [
  { id: "mode", label: "Mode" },
  { id: "upload", label: "Upload" },
  { id: "resolve", label: "Resolve" },
  { id: "answer", label: "Answer" },
  { id: "export", label: "Export" },
];

export function isStepId(value: string | undefined): value is StepId {
  return STEPS.some((step) => step.id === value);
}

interface SpineProps {
  current: StepId;
  // Steps the user may jump back (or forward) to.
  reachable: StepId[];
  // Steps that are finished.
  done: StepId[];
  hrefFor: (step: StepId) => string;
  nothingToResolve?: boolean;
}

export function Spine({ current, reachable, done, hrefFor, nothingToResolve = false }: SpineProps) {
  return (
    <nav aria-label="Estimate progress" className="cp-spine no-print">
      <ol>
        {STEPS.map((step, index) => {
          const isCurrent = step.id === current;
          const isDone = done.includes(step.id) && !isCurrent;
          const canVisit = !isCurrent && reachable.includes(step.id);
          const label = step.id === "resolve" && nothingToResolve ? "Nothing to resolve" : step.label;
          const marker = (
            <span aria-hidden="true" className="cp-spine-marker">
              {isDone ? "✓" : index + 1}
            </span>
          );
          const status = isCurrent ? "current step" : isDone ? "done" : canVisit ? "available" : "not reached";
          return (
            <li
              className={`cp-spine-step${isCurrent ? " current" : ""}${isDone ? " done" : ""}`}
              key={step.id}
            >
              {canVisit ? (
                <Link to={hrefFor(step.id)}>
                  {marker}
                  <span>{label}</span>
                  <span className="visually-hidden">, {status}</span>
                </Link>
              ) : (
                <span aria-current={isCurrent ? "step" : undefined} className="cp-spine-static">
                  {marker}
                  <span>{label}</span>
                  <span className="visually-hidden">, {status}</span>
                </span>
              )}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
