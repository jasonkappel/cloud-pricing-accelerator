import { MODE_COPY } from "../flow/modeCopy";
import { useCapabilities } from "../flow/useCapabilities";
import { DISPLAY, hoursLabel } from "../glossary";

export function SettingsPage() {
  const { capabilities, error } = useCapabilities();
  if (error) {
    return <p className="cp-error" role="alert">{error}</p>;
  }
  if (!capabilities) {
    return <p className="cp-muted">Loading…</p>;
  }
  return (
    <section className="cp-page">
      <div>
        <h1 className="cp-title">Settings</h1>
        <p className="cp-lede">Read-only. These are set on the server and cannot be changed here.</p>
      </div>
      <section className="cp-card">
        <dl className="cp-stamp">
          <div>
            <dt>Starting mode</dt>
            <dd>{MODE_COPY[capabilities.defaultMode].title}. All three modes are always offered.</dd>
          </div>
          <div>
            <dt>Estimates kept for</dt>
            <dd>{hoursLabel(capabilities.applicationMaxAgeHours)}. The export is the lasting record.</dd>
          </div>
          <div>
            <dt>Product</dt>
            <dd>Cloud Pricing Accelerator, {capabilities.productLabel}</dd>
          </div>
        </dl>
      </section>
      <section className="cp-card">
        <h2>Disclaimer</h2>
        <p>
          Estimates use {DISPLAY.listView} for a single run-rate month. They exclude the costs listed as
          {" "}{DISPLAY.excludedCostLedger} and do not reflect contracted discounts. This informs a platform
          decision. It does not make one.
        </p>
      </section>
    </section>
  );
}
