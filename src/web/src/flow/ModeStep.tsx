import { type FormEvent, useId, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { MODE_COPY, regionLabel } from "./modeCopy";
import {
  MODES,
  getPendingMode,
  getPendingRegionConfirmed,
  setEstimateMode,
  setPendingMode,
  setPendingRegionConfirmed,
  type Mode,
} from "./storage";

interface ModeStepProps {
  // The starting selection. For a new estimate this is the API's defaultMode.
  defaultMode: Mode;
  // Set when changing the view of an existing estimate; the region is already recorded.
  applicationId?: string;
  nextHref?: string;
  onModeChange?: (mode: Mode) => void;
}

export function ModeStep({ defaultMode, applicationId, nextHref, onModeChange }: ModeStepProps) {
  const navigate = useNavigate();
  const location = useLocation();
  const isNew = applicationId === undefined;
  const [mode, setMode] = useState<Mode>((isNew ? getPendingMode() : null) ?? defaultMode);
  const [regionConfirmed, setRegionConfirmed] = useState(isNew && getPendingRegionConfirmed());
  const [error, setError] = useState("");
  const headingId = useId();

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (isNew) {
      if (!regionConfirmed) {
        setError("Confirm the pricing region to continue.");
        return;
      }
      setPendingMode(mode);
      setPendingRegionConfirmed(true);
      navigate(`/estimates/new/upload${location.search}`);
      return;
    }
    setEstimateMode(applicationId, mode);
    onModeChange?.(mode);
    if (nextHref) {
      navigate(nextHref);
    }
  }

  return (
    <form className="cp-page" onSubmit={handleSubmit}>
      <div>
        <h1 className="cp-title" id={headingId}>What do you want to know?</h1>
        <p className="cp-lede">
          Pricing always runs for both clouds, so you can change this later without uploading again.
        </p>
      </div>
      <div aria-labelledby={headingId} className="cp-card-grid" role="radiogroup">
        {MODES.map((option) => (
          <button
            aria-checked={mode === option}
            className="cp-choice"
            key={option}
            onClick={() => setMode(option)}
            role="radio"
            type="button"
          >
            <strong>{MODE_COPY[option].title}</strong>
            <span>{MODE_COPY[option].gives}</span>
          </button>
        ))}
      </div>

      {isNew && (
        <section className="cp-card">
          <h2>Pricing region</h2>
          <p className="cp-muted cp-small">
            This deployment has one approved region. Confirm it to price your estimate there.
          </p>
          <label className="cp-row">
            <input
              checked={regionConfirmed}
              onChange={(event) => setRegionConfirmed(event.currentTarget.checked)}
              type="checkbox"
            />
            <span>Price in {regionLabel(mode)}</span>
          </label>
        </section>
      )}

      {error && <p className="cp-error" role="alert">{error}</p>}
      <div className="cp-row">
        <button className="cp-button cp-button-primary" type="submit">
          {isNew ? "Continue to upload" : "Use this view"}
        </button>
      </div>
    </form>
  );
}
