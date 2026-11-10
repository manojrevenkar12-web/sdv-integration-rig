// Shapes of web/public/data.json, written by `python -m sdvrig report`.

export interface FlasherSummary {
  scenarios: number;
  passed: number;
  checks_failed: Record<string, number>;
  bricked: number;
  families: Record<string, { passed: number; of: number }>;
  median_update_ms: number | null;
  block_retries: number;
  restarts: number;
}

export interface RunResult {
  ecu: string;
  flasher: string;
  target_version: string;
  previous_version: string;
  outcome: "updated" | "rolled_back" | "refused" | "failed";
  reason: string | null;
  running_version: string;
  duration_ms: number;
  steps: string[];
  step_times_ms: number[];
  events: string[];
}

export interface ShowcaseRun {
  record: { checks: Record<string, boolean>; details: Record<string, string>; result: RunResult; passed: boolean };
  frames_per_50ms: number[];
  faults: string;
}

export interface Showcase {
  scenario: { id: string; family: string; ecu: string; faults: { kind: string; off_ms?: number }[] };
  runs: { robust: ShowcaseRun; baseline: ShowcaseRun };
}

export interface MonitorSummary {
  defective_releases: number;
  caught: number;
  healthy_releases: number;
  false_halts: number;
  median_days_to_halt: number | null;
  median_fleet_share_on_bad_release: number | null;
  median_bad_exposure_avoided: number | null;
  localization_correct: number | null;
  localization_checked: number | null;
}

export interface BenchRun {
  hardware: string;
  ambient_c: number;
  version: string;
  role: string;
  dtcs: string[];
  as_expected: boolean;
}

export interface Ticket {
  scenario: string;
  owner: string;
  title: string;
  reproduced: boolean | null;
  evidence: { z: number; rate_ratio: number; exposed_events: number; expected_events: number; day: number };
  runs: BenchRun[];
}

export interface DecoderCheck {
  decoder: string;
  expected?: number;
  equal?: number;
  agrees?: boolean;
  skipped?: string;
}

export interface Crosscheck {
  ecu: string;
  bus: string;
  fd: boolean;
  messages: number;
  can_frames: number;
  checks: DecoderCheck[];
}

export interface BenchFlash {
  image_kib: number;
  bus: string;
  fd: boolean;
  simulated_ms: number;
  wall_ms: number;
  throughput_kib_s: number;
}

export interface DashboardData {
  flash: { scenarios: number; summary: { robust: FlasherSummary; baseline: FlasherSummary } } | null;
  fleet: {
    scenarios: number;
    summary: Record<"stratified" | "pooled" | "before_after" | "none", MonitorSummary>;
    triage: { tickets: number; bench_repro_attempted: number; bench_repro_confirmed: number; soak_test_needed: number };
    tickets: Ticket[];
  } | null;
  crosscheck: Crosscheck[] | null;
  bench: { flash: Record<string, BenchFlash>; fleet: { vehicle_days_per_s: number } } | null;
  tests: { total: number; passed: number; failed: number; skipped: number } | null;
  showcase: Showcase[] | null;
}
