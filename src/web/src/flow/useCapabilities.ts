import { useCallback, useEffect, useState } from "react";

import { getCapabilities, type Capabilities } from "../api";

let cached: Promise<Capabilities> | undefined;

function load(): Promise<Capabilities> {
  cached ??= getCapabilities().catch((error: unknown) => {
    cached = undefined;
    throw error;
  });
  return cached;
}

export function useCapabilities(): {
  capabilities: Capabilities | null;
  error: string;
  reload: () => void;
} {
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [error, setError] = useState("");
  const [generation, setGeneration] = useState(0);

  useEffect(() => {
    let active = true;
    load()
      .then((value) => {
        if (active) {
          setCapabilities(value);
          setError("");
        }
      })
      .catch((requestError: unknown) => {
        if (active) {
          setCapabilities(null);
          setError(
            requestError instanceof Error ? requestError.message : "Settings could not be loaded.",
          );
        }
      });
    return () => {
      active = false;
    };
  }, [generation]);

  // After a publication the current PriceBook (or its refusal) may have changed.
  const reload = useCallback(() => {
    cached = undefined;
    setGeneration((value) => value + 1);
  }, []);

  return { capabilities, error, reload };
}
