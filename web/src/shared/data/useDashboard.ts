import { useCallback, useEffect, useState } from "react";
import { loadDashboard } from "./load";
import type { DashboardData } from "./types";

export type LoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; data: DashboardData };

export function useDashboard(fetcher?: typeof fetch) {
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const load = useCallback(() => {
    setState({ status: "loading" });
    loadDashboard(fetcher)
      .then((data) => setState({ status: "ready", data }))
      .catch((err: Error) => setState({ status: "error", message: err.message }));
  }, [fetcher]);
  useEffect(load, [load]);
  return { state, retry: load };
}
