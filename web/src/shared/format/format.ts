export const pct = (x: number | null | undefined) => (x == null ? "–" : `${Math.round(x * 100)}%`);
export const seconds = (ms: number) => `${(ms / 1000).toFixed(1)} s`;
export const thousands = (n: number) => n.toLocaleString("en-GB");

const STEP_LABELS: Record<string, string> = {
  identify: "Identify",
  "prepare-vehicle": "Quiet the vehicle",
  "programming-session": "Programming session",
  "security-access": "Unlock",
  preconditions: "Check supply",
  erase: "Erase",
  "request-download": "Request download",
  transfer: "Transfer",
  "transfer-exit": "Transfer exit",
  "check-memory": "Check SHA-256",
  activate: "Activate slot",
  reset: "Reset",
  "self-test": "Self-test",
  commit: "Commit",
  "roll-back": "Roll back",
  reconnect: "Wait for ECU",
  "restore-vehicle": "Restore vehicle",
};
export const stepLabel = (s: string) => STEP_LABELS[s] ?? s;

const OUTCOME: Record<string, string> = {
  updated: "Updated",
  rolled_back: "Rolled back",
  refused: "Refused",
  failed: "Failed",
};
export const outcomeLabel = (s: string) => OUTCOME[s] ?? s;

const CHECKS: Record<string, string> = {
  outcome: "Reported the right outcome",
  no_brick: "Every ECU still runs",
  truthful_report: "Report matches what the ECU runs",
  no_stray_dtcs: "No fault code it caused",
  vehicle_restored: "Vehicle back to normal",
};
export const checkLabel = (s: string) => CHECKS[s] ?? s;

const FAMILIES: Record<string, string> = {
  clean: "Clean bench",
  interference: "Interference",
  bus_off_burst: "Bus-off bursts",
  power_cut_transfer: "Power cut, transfer",
  power_cut_erase: "Power cut, erase",
  power_cut_exit: "Power cut, verify",
  power_cut_after_reset: "Power cut, after reset",
  brownout_dip: "Voltage dip",
  brownout_persistent: "Low battery",
  harness_fault: "Harness fault",
  harness_fault_long: "Harness fault, long",
  bad_build: "Build fails self-test",
  combined: "Combined faults",
  flaky_supply: "Supply fails every try",
  wrong_key: "Wrong security key",
};
export const familyLabel = (s: string) => FAMILIES[s] ?? s;
