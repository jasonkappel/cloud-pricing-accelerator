import { useEffect, useState } from "react";

import { getApplication, isNotFound, type ApplicationDetail } from "../api";
import { markExpired, rememberEstimate } from "./storage";

export interface EstimateState {
  detail: ApplicationDetail | null;
  setDetail: (detail: ApplicationDetail) => void;
  expired: boolean;
  error: string;
}

// Loads one estimate from the API. Nothing about the comparison is cached in the browser; only
// {id, name, expiresAt} is remembered so Home can show a row as expired after a 404.
export function useEstimate(applicationId: string): EstimateState {
  const [detail, setDetail] = useState<ApplicationDetail | null>(null);
  const [expired, setExpired] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    setDetail(null);
    setExpired(false);
    setError("");
    getApplication(applicationId, controller.signal)
      .then((loaded) => {
        if (controller.signal.aborted) {
          return;
        }
        rememberEstimate({
          id: loaded.application.id,
          name: loaded.application.name,
          expiresAt: loaded.expires_at ?? "",
        });
        setDetail(loaded);
      })
      .catch((requestError: unknown) => {
        if (controller.signal.aborted) {
          return;
        }
        if (isNotFound(requestError)) {
          markExpired(applicationId);
          setExpired(true);
          return;
        }
        setError(requestError instanceof Error ? requestError.message : "The estimate could not be loaded.");
      });
    return () => controller.abort();
  }, [applicationId]);

  return { detail, setDetail, expired, error };
}
