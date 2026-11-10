import { checkLabel, familyLabel } from "../../shared/format/format";
import type { DashboardData } from "../../shared/data/types";
import { color, font, meter, size, space, stroke, weight } from "../../shared/theme/tokens";

type Flash = NonNullable<DashboardData["flash"]>;

const cell = { padding: `${space.sm}px ${space.md}px`, borderBottom: `${stroke.hair}px solid ${color.rule}` };
const num = { ...cell, fontFamily: font.figures, textAlign: "right" as const, fontVariantNumeric: "tabular-nums", whiteSpace: "nowrap" as const };

function Meter({ passed, of, tone }: { passed: number; of: number; tone: string }) {
  return (
    <span aria-label={`${passed} of ${of}`} style={{ display: "inline-flex", gap: meter.gap, verticalAlign: "middle" }}>
      {Array.from({ length: of }, (_, i) => (
        <span key={i} style={{ width: meter.tick, height: meter.height, background: i < passed ? tone : color.grid }} />
      ))}
    </span>
  );
}

export function FlashChecks({ flash }: { flash: Flash }) {
  const { robust, baseline } = flash.summary;
  const checks = ["outcome", "truthful_report", "no_stray_dtcs", "vehicle_restored", "no_brick"];
  return (
    <div style={{ display: "grid", gap: space.xl, gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))" }}>
      <table style={{ width: "100%" }}>
        <caption style={{ textAlign: "left", fontWeight: weight.semibold, paddingBottom: space.sm }}>
          Scenarios that failed each check, of {flash.scenarios}
        </caption>
        <thead>
          <tr style={{ color: color.graphite, fontSize: size.small }}>
            <th style={{ ...cell, textAlign: "left", fontWeight: weight.medium }}>Check</th>
            <th style={{ ...num, fontWeight: weight.medium }}>Robust</th>
            <th style={{ ...num, fontWeight: weight.medium }}>Baseline</th>
          </tr>
        </thead>
        <tbody>
          {checks.map((c) => (
            <tr key={c}>
              <td style={cell}>{checkLabel(c)}</td>
              <td style={num}>{robust.checks_failed[c] ?? 0}</td>
              <td style={{ ...num, color: (baseline.checks_failed[c] ?? 0) > 0 ? color.hv : color.ink }}>
                {baseline.checks_failed[c] ?? 0}
              </td>
            </tr>
          ))}
          <tr>
            <td style={{ ...cell, fontWeight: weight.semibold }}>Passed every check</td>
            <td style={{ ...num, fontWeight: weight.semibold }}>{robust.passed}</td>
            <td style={{ ...num, fontWeight: weight.semibold }}>{baseline.passed}</td>
          </tr>
        </tbody>
      </table>
      <table style={{ width: "100%" }}>
        <caption style={{ textAlign: "left", fontWeight: weight.semibold, paddingBottom: space.sm }}>
          Passes by fault family
        </caption>
        <tbody>
          {Object.keys(robust.families).map((f) => (
            <tr key={f} style={{ fontSize: size.small }}>
              <td style={{ ...cell, padding: `${space.xs}px ${space.sm}px ${space.xs}px 0` }}>{familyLabel(f)}</td>
              <td style={{ ...cell, padding: `${space.xs}px ${space.sm}px` }}>
                <Meter {...robust.families[f]} tone={color.canL} />
              </td>
              <td style={{ ...cell, padding: `${space.xs}px 0 ${space.xs}px ${space.sm}px` }}>
                <Meter {...baseline.families[f]} tone={color.shield} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
