import { thousands } from "../../shared/format/format";
import { TableScroll } from "../../shared/ui/TableScroll";
import type { DashboardData } from "../../shared/data/types";
import { color, font, size, space, stroke, weight } from "../../shared/theme/tokens";

const cell = { padding: `${space.sm}px ${space.md}px`, borderBottom: `${stroke.hair}px solid ${color.rule}` };
const num = { ...cell, fontFamily: font.figures, textAlign: "right" as const, fontVariantNumeric: "tabular-nums", whiteSpace: "nowrap" as const };
const DECODER: Record<string, string> = {
  "doip/scapy": "scapy DoIP, TCP reassembled",
  "isotp/scapy": "scapy CAN and ISO-TP",
  "isotp/rust": "isotp-trace (Rust)",
};

export function Decoders({ crosscheck, bench }: { crosscheck: NonNullable<DashboardData["crosscheck"]>; bench: DashboardData["bench"] }) {
  const decoders = crosscheck[0].checks.map((c) => c.decoder);
  return (
    <div style={{ display: "grid", gap: space.xl, gridTemplateColumns: "1fr" }}>
      <TableScroll label="Decoder agreement">
      <table style={{ width: "100%" }}>
        <caption style={{ textAlign: "left", fontWeight: weight.semibold, paddingBottom: space.sm }}>
          Messages decoded exactly as the tester saw them
        </caption>
        <thead>
          <tr style={{ color: color.graphite, fontSize: size.small }}>
            <th style={{ ...cell, textAlign: "left", fontWeight: weight.medium }}>ECU</th>
            {decoders.map((d) => <th key={d} style={{ ...num, fontWeight: weight.medium }}>{DECODER[d] ?? d}</th>)}
          </tr>
        </thead>
        <tbody>
          {crosscheck.map((x) => (
            <tr key={x.ecu}>
              <td style={{ ...cell, whiteSpace: "nowrap" }}>{x.ecu} <span style={{ color: color.graphite, fontSize: size.small }}>{x.fd ? "CAN FD" : "CAN"}</span></td>
              {x.checks.map((c) => (
                <td key={c.decoder} style={{ ...num, color: c.agrees ? color.ink : color.hv }}>
                  {c.skipped ? "not built" : `${c.equal} / ${c.expected}`}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      </TableScroll>
      {bench && (
        <TableScroll label="Flash timing">
        <table style={{ width: "100%" }}>
          <caption style={{ textAlign: "left", fontWeight: weight.semibold, paddingBottom: space.sm }}>
            One flash, simulated time and wall time
          </caption>
          <thead>
            <tr style={{ color: color.graphite, fontSize: size.small }}>
              <th style={{ ...cell, textAlign: "left", fontWeight: weight.medium }}>ECU</th>
              <th style={{ ...num, fontWeight: weight.medium }}>Image</th>
              <th style={{ ...num, fontWeight: weight.medium }}>Vehicle time</th>
              <th style={{ ...num, fontWeight: weight.medium }}>Wall time</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(bench.flash).map(([ecu, b]) => (
              <tr key={ecu}>
                <td style={cell}>{ecu}</td>
                <td style={num}>{b.image_kib} KiB</td>
                <td style={num}>{(b.simulated_ms / 1000).toFixed(2)} s</td>
                <td style={num}>{Math.round(b.wall_ms)} ms</td>
              </tr>
            ))}
            <tr>
              <td style={cell} colSpan={3}>Fleet simulation and monitoring</td>
              <td style={num}>{thousands(bench.fleet.vehicle_days_per_s)} vehicle-days/s</td>
            </tr>
          </tbody>
        </table>
        </TableScroll>
      )}
    </div>
  );
}
