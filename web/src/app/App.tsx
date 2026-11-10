import { Decoders } from "../features/decoders/Decoders";
import { FlashChecks } from "../features/flashing/FlashChecks";
import { FleetMonitors, Tickets } from "../features/fleet/FleetMonitors";
import { FlashTrace } from "../features/trace/FlashTrace";
import { pct } from "../shared/format/format";
import type { DashboardData } from "../shared/data/types";
import { useDashboard } from "../shared/data/useDashboard";
import { color, leading, measure, size, space, weight } from "../shared/theme/tokens";
import { Button } from "../shared/ui/Button";
import { Command, Notice } from "../shared/ui/Notice";
import { Section } from "../shared/ui/Section";
import { PageSkeleton } from "../shared/ui/Skeleton";

const REPO = "https://github.com/manojrevenkar12-web/sdv-integration-rig";

function Missing({ command, what }: { command: string; what: string }) {
  return (
    <Notice tone="empty">
      No {what} yet. Run <Command>{command}</Command>, then <Command>python -m sdvrig report</Command>.
    </Notice>
  );
}

export function Dashboard({ data }: { data: DashboardData }) {
  const flash = data.flash;
  const fleet = data.fleet;
  return (
    <>
      <header>
        <h1 style={{ fontSize: size.h1, lineHeight: leading.tight, fontWeight: weight.semibold, margin: 0, maxWidth: measure.text }}>
          The same power cut, two flashers.
        </h1>
        <p style={{ fontSize: size.lead, maxWidth: measure.text, color: color.graphite, margin: `${space.md}px 0 ${space.xl}px` }}>
          {flash
            ? `On a simulated zonal vehicle, ${flash.summary.robust.passed} of ${flash.scenarios} held-out flashing scenarios pass every check on the vehicle's own state. The happy-path script passes ${flash.summary.baseline.passed}.`
            : "A simulated zonal vehicle with ISO-TP, UDS and DoIP built from the standards."}
        </p>
      </header>
      {data.showcase?.length ? <FlashTrace showcase={data.showcase} /> : <Missing command="python -m sdvrig showcase" what="flash traces" />}

      <Section id="checks" title="Checks on the vehicle afterwards"
        intro="After every scenario the harness reads the vehicle itself, not the flasher's report: what each ECU runs, whether the trial boot was committed, which fault codes appeared, and whether every ECU is back in its default session with communication on.">
        {flash ? <FlashChecks flash={flash} /> : <Missing command="python -m sdvrig flash-eval --set heldout" what="held-out flashing run" />}
      </Section>

      <Section id="fleet" title="Stopping a bad release in the fleet"
        intro={fleet
          ? `${fleet.scenarios} held-out winters of 8,000 vehicles. Early rollout waves lean to the Nordic region and cold snaps raise some fault codes on any software, so comparing new against old naively halts healthy releases. The stratified monitor compares vehicles only within the same day, region, temperature band and hardware revision, and halted ${fleet.summary.stratified.caught} of ${fleet.summary.stratified.defective_releases} bad releases with ${pct(fleet.summary.stratified.median_fleet_share_on_bad_release)} of the fleet installed, and no healthy one.`
          : undefined}>
        {fleet ? (
          <>
            <FleetMonitors fleet={fleet} />
            <h3 style={{ fontSize: size.lead, fontWeight: weight.semibold, margin: `${space.xl}px 0 0` }}>
              From a halt to a bench reproduction
            </h3>
            <p style={{ maxWidth: measure.text, color: color.graphite }}>
              {fleet.triage.bench_repro_confirmed} of {fleet.triage.bench_repro_attempted} localised halts reproduced in the
              simulated climate chamber; {fleet.triage.soak_test_needed} tickets asked for a soak test because no single
              condition isolated the defect. Open a ticket to see the four bench runs.
            </p>
            {fleet.tickets.length ? <Tickets tickets={fleet.tickets} /> : <Notice tone="empty">No ticket had a bench reproduction.</Notice>}
          </>
        ) : <Missing command="python -m sdvrig fleet-eval --set heldout" what="held-out fleet run" />}
      </Section>

      <Section id="decoders" title="Checked by code that shares nothing with the rig"
        intro="Every flash is written to pcap captures (SocketCAN for the buses, Ethernet and TCP for DoIP). scapy decodes both; a small Rust tool reassembles ISO-TP from the bus captures, including the CAN FD escapes scapy 2.6.1 does not implement.">
        {data.crosscheck ? <Decoders crosscheck={data.crosscheck} bench={data.bench} /> : <Missing command="python -m sdvrig crosscheck" what="decoder cross-check" />}
      </Section>

      <footer style={{ marginTop: space.xxl, paddingTop: space.lg, color: color.graphite, fontSize: size.small }}>
        <a href={REPO}>Source on GitHub</a>
        {data.tests && <span style={{ marginLeft: space.lg }}>{data.tests.passed} tests passing</span>}
        <span style={{ marginLeft: space.lg }}>Every number on this page is generated from reports/*.json.</span>
      </footer>
    </>
  );
}

export function App({ fetcher }: { fetcher?: typeof fetch } = {}) {
  const { state, retry } = useDashboard(fetcher);
  return (
    <main style={{ maxWidth: measure.page, margin: "0 auto", padding: `${space.xl}px ${space.lg}px` }}>
      {state.status === "loading" && <PageSkeleton />}
      {state.status === "error" && (
        <Notice tone="error" action={<Button onClick={retry}>Try again</Button>}>
          The results could not be loaded ({state.message}). Generate them with <Command>python -m sdvrig report</Command>,
          which writes web/public/data.json.
        </Notice>
      )}
      {state.status === "ready" && <Dashboard data={state.data} />}
    </main>
  );
}
