import type { DashboardData } from "./types";

export const DATA_URL = `${import.meta.env.BASE_URL}data.json`;

export async function loadDashboard(fetcher: typeof fetch = fetch): Promise<DashboardData> {
  const res = await fetcher(DATA_URL);
  if (!res.ok) throw new Error(`data.json answered ${res.status}`);
  return (await res.json()) as DashboardData;
}
