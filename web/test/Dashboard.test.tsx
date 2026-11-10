import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App, Dashboard } from "../src/app/App";
import type { DashboardData } from "../src/shared/data/types";

import raw from "../public/data.json";

const data = raw as unknown as DashboardData;
const ok = (body: unknown) => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) } as Response);

afterEach(cleanup);

describe("dashboard states", () => {
  it("shows a skeleton while loading", () => {
    render(<App fetcher={() => new Promise<Response>(() => {})} />);
    expect(screen.getByRole("status", { name: "Loading results" })).toBeTruthy();
  });

  it("explains a load failure and retries", async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce({ ok: false, status: 404 } as Response)
      .mockImplementationOnce(() => ok(data));
    render(<App fetcher={fetcher as unknown as typeof fetch} />);
    expect((await screen.findByRole("alert")).textContent).toContain("404");
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(screen.getByRole("heading", { level: 1 })).toBeTruthy());
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it("tells you which command fills an empty section", () => {
    const empty: DashboardData = { flash: null, fleet: null, crosscheck: null, bench: null, tests: null, showcase: null };
    render(<Dashboard data={empty} />);
    expect(screen.getByText(/flash-eval --set heldout/)).toBeTruthy();
    expect(screen.getByText(/fleet-eval --set heldout/)).toBeTruthy();
    expect(screen.getByText(/sdvrig crosscheck/)).toBeTruthy();
  });
});

describe("dashboard content", () => {
  it("states the held-out flashing numbers from the data", () => {
    render(<Dashboard data={data} />);
    const r = data.flash!.summary.robust;
    expect(screen.getByText(new RegExp(`${r.passed} of ${data.flash!.scenarios} held-out`))).toBeTruthy();
  });

  it("switches the trace between scenarios", () => {
    render(<Dashboard data={data} />);
    const radios = screen.getAllByRole("radio");
    expect(radios[0].getAttribute("aria-checked")).toBe("true");
    fireEvent.click(radios[1]);
    expect(radios[1].getAttribute("aria-checked")).toBe("true");
  });

  it("opens a ticket to show its bench runs", () => {
    render(<Dashboard data={data} />);
    const first = screen.getAllByRole("button", { expanded: false })[0];
    fireEvent.click(first);
    expect(first.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText("Field condition")).toBeTruthy();
  });
});
