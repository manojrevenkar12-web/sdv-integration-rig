import { AnimatePresence, motion } from "framer-motion";
import { useState } from "react";
import { pct } from "../../shared/format/format";
import { TableScroll } from "../../shared/ui/TableScroll";
import type { DashboardData, Ticket } from "../../shared/data/types";
import { color, font, motion as m, radius, size, space, stroke, weight } from "../../shared/theme/tokens";

type Fleet = NonNullable<DashboardData["fleet"]>;

const cell = { padding: `${space.sm}px ${space.md}px`, borderBottom: `${stroke.hair}px solid ${color.rule}` };
const num = { ...cell, fontFamily: font.figures, textAlign: "right" as const, fontVariantNumeric: "tabular-nums", whiteSpace: "nowrap" as const };
const ROWS: { key: keyof Fleet["summary"]; label: string }[] = [
  { key: "stratified", label: "Stratified (this repo)" },
  { key: "pooled", label: "Pooled new against old" },
  { key: "before_after", label: "Fleet before and after" },
  { key: "none", label: "No monitor" },
];
const ROLE: Record<string, string> = {
  room: "Bench, 20 °C",
  field: "Field condition",
  "control-hw": "Other hardware",
  "control-previous": "Previous release",
};

export function FleetMonitors({ fleet }: { fleet: Fleet }) {
  const s = fleet.summary;
  return (
    <TableScroll label="Monitors compared">
    <table style={{ width: "100%" }}>
      <thead>
        <tr style={{ color: color.graphite, fontSize: size.small }}>
          <th style={{ ...cell, textAlign: "left", fontWeight: weight.medium }}>Monitor</th>
          <th style={{ ...num, fontWeight: weight.medium }}>Bad releases halted</th>
          <th style={{ ...num, fontWeight: weight.medium }}>Healthy releases halted</th>
          <th style={{ ...num, fontWeight: weight.medium }}>Median days to halt</th>
          <th style={{ ...num, fontWeight: weight.medium }}>Fleet on the bad release</th>
        </tr>
      </thead>
      <tbody>
        {ROWS.map(({ key, label }) => {
          const r = s[key];
          const mine = key === "stratified";
          return (
            <tr key={key} style={{ fontWeight: mine ? weight.semibold : weight.regular }}>
              <td style={{ ...cell, whiteSpace: "nowrap" }}>{label}</td>
              <td style={num}>{r.caught} / {r.defective_releases}</td>
              <td style={{ ...num, color: r.false_halts > 0 ? color.hv : color.ink }}>
                {r.false_halts} / {r.healthy_releases}
              </td>
              <td style={num}>{r.median_days_to_halt ?? "–"}</td>
              <td style={num}>{pct(r.median_fleet_share_on_bad_release)}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
    </TableScroll>
  );
}

function TicketRow({ t }: { t: Ticket }) {
  const [open, setOpen] = useState(false);
  return (
    <li style={{ borderBottom: `${stroke.hair}px solid ${color.rule}`, listStyle: "none" }}>
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
        style={{ font: "inherit", width: "100%", textAlign: "left", background: "none", border: "none",
                 padding: `${space.md}px 0`, cursor: "pointer", color: color.ink, display: "flex",
                 justifyContent: "space-between", gap: space.md, flexWrap: "wrap" }}
      >
        <span>
          <span style={{ fontWeight: weight.semibold }}>{t.title}</span>
          <span style={{ display: "block", color: color.graphite, fontSize: size.small }}>
            {t.owner}; {t.scenario}; rate ratio {t.evidence.rate_ratio} with {t.evidence.exposed_events} events, z = {t.evidence.z}
          </span>
        </span>
        <span style={{ color: t.reproduced ? color.canL : color.hv, whiteSpace: "nowrap" }}>
          {t.reproduced ? "Reproduced on the bench" : "Not reproduced"}
        </span>
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: m.quickSeconds, ease: m.ease }}
            style={{ overflow: "hidden" }}
          >
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: space.sm,
                          paddingBottom: space.md }}>
              {t.runs.map((r) => (
                <div key={r.role} style={{ background: color.sheet, borderRadius: radius.panel, padding: space.md,
                                           borderTop: `${stroke.wire}px solid ${r.dtcs.length ? color.hv : color.canL}` }}>
                  <div style={{ fontWeight: weight.medium }}>{ROLE[r.role] ?? r.role}</div>
                  <div style={{ fontSize: size.small, color: color.graphite }}>
                    {r.hardware}, {r.ambient_c} °C, software {r.version}
                  </div>
                  <div style={{ fontFamily: font.figures, marginTop: space.xs }}>
                    {r.dtcs.length ? r.dtcs.join(", ") : "no fault code"}
                  </div>
                </div>
              ))}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </li>
  );
}

export function Tickets({ tickets }: { tickets: Ticket[] }) {
  return (
    <ul style={{ padding: 0, margin: 0 }}>
      {tickets.map((t) => <TicketRow key={t.scenario + t.title} t={t} />)}
    </ul>
  );
}
