import { Route, Routes } from "react-router-dom";

import { EstimateFlow } from "./flow/EstimateFlow";
import { SummaryPage } from "./flow/SummaryPage";
import { ApplicationsPage } from "./pages/ApplicationsPage";
import { ComparisonsPage } from "./pages/ComparisonsPage";
import { EstimatesPage } from "./pages/EstimatesPage";
import { HomePage } from "./pages/HomePage";
import { IntakesPage } from "./pages/IntakesPage";
import { NotFoundPage } from "./pages/NotFoundPage";
import { PriceBookPage } from "./pages/PriceBookPage";
import { ReviewPage } from "./pages/ReviewPage";
import { SettingsPage } from "./pages/SettingsPage";
import { Nav } from "./shell/Nav";

export function App() {
  return (
    <div className="cp-shell">
      <Nav />
      <main className="cp-main content">
        <Routes>
          <Route path="/" element={<HomePage />} />
          <Route path="/estimates" element={<EstimatesPage />} />
          <Route path="/estimates/:id/summary" element={<SummaryPage />} />
          <Route path="/estimates/:id" element={<EstimateFlow />} />
          <Route path="/estimates/:id/:step" element={<EstimateFlow />} />
          <Route path="/price-book" element={<PriceBookPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          {/* Builder detail: the original pages stay mounted unchanged. */}
          <Route path="/applications" element={<ApplicationsPage />} />
          <Route path="/intakes" element={<IntakesPage />} />
          <Route path="/comparisons" element={<ComparisonsPage />} />
          <Route path="/review" element={<ReviewPage />} />
          <Route path="*" element={<NotFoundPage />} />
        </Routes>
      </main>
    </div>
  );
}
