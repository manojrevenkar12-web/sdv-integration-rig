import { motion, useReducedMotion } from "framer-motion";
import { useState } from "react";
import { outcomeLabel, seconds, stepLabel } from "../../shared/format/format";
import type { Showcase, ShowcaseRun } from "../../shared/data/types";
import { color, font, motion as m, size, space, stroke, svgText, weight } from "../../shared/theme/tokens";
import { Segmented } from "../../shared/ui/Segmented";

// Geometry of the trace drawing, in viewBox units.
const VIEW_W = 1000;
const LABEL_W = 150;
const PLOT_W = VIEW_W - LABEL_W - 20;
const LANE_H = 92;
const BAND_Y = 26;
const SPARK_H = 30;
const AXIS_H = 28;
const MIN_LABEL_W = 70;
const OFF_H = 6;

const STEP_FILL: Record<string, string> = {
  transfer: color.canH,
  reconnect: color.hv,
  "roll-back": color.hv,
  commit: color.canL,
  "self-test": color.canL,
};

function faultTimes(run: ShowcaseRun): { kind: string; ms: number }[] {
  if (!run.faults || run.faults === "none") return [];
  return run.faults.split(", ").map((f) => {
    const [kind, at] = f.split("@");
    return { kind, ms: parseFloat(at) };
  });
}

function Lane({ run, label, y, x }: { run: ShowcaseRun; label: string; y: number; x: (ms: number) => number }) {
  const r = run.record.result;
  const end = r.duration_ms;
  const segments = r.steps.map((step, i) => ({
    step,
    from: r.step_times_ms[i],
    to: i + 1 < r.steps.length ? r.step_times_ms[i + 1] : end,
  }));
  const peak = Math.max(1, ...run.frames_per_50ms);
  const spark = run.frames_per_50ms
    .map((n, i) => `${i === 0 ? "M" : "L"}${x(i * 50).toFixed(1)},${(y + BAND_Y + stroke.lane + SPARK_H - (n / peak) * SPARK_H).toFixed(1)}`)
    .join(" ");
  const good = run.record.passed;
  return (
    <g>
      <text x={0} y={y + BAND_Y + 13} fontFamily={font.body} fontSize={svgText.label} fontWeight={weight.semibold} fill={color.ink}>
        {label}
      </text>
      <text x={0} y={y + BAND_Y + 32} fontFamily={font.body} fontSize={svgText.note} fill={good ? color.canL : color.hv}>
        {good ? "every check passed" : `${Object.values(run.record.checks).filter((ok) => !ok).length} checks failed`}
      </text>
      {segments.map((s, i) => {
        const w = Math.max(1, x(s.to) - x(s.from));
        const fill = STEP_FILL[s.step] ?? (i % 2 ? color.shield : color.ink);
        return (
          <g key={`${s.step}-${i}`}>
            <rect x={x(s.from)} y={y + BAND_Y} width={w} height={stroke.lane} fill={fill}>
              <title>{`${stepLabel(s.step)}: ${seconds(s.from)} – ${seconds(s.to)}`}</title>
            </rect>
            {w > MIN_LABEL_W && (
              <text x={x(s.from) + 4} y={y + BAND_Y - 6} fontFamily={font.figures} fontSize={svgText.tick} fill={color.graphite}>
                {stepLabel(s.step)}
              </text>
            )}
          </g>
        );
      })}
      <path d={spark} fill="none" stroke={color.graphite} strokeWidth={stroke.hair} />
      <text x={x(end) + 6} y={y + BAND_Y + 13} fontFamily={font.figures} fontSize={svgText.note} fill={good ? color.ink : color.hv}>
        {outcomeLabel(r.outcome)}
      </text>
    </g>
  );
}

export function FlashTrace({ showcase }: { showcase: Showcase[] }) {
  const [pick, setPick] = useState(showcase[0].scenario.id);
  const reduce = useReducedMotion();
  const sc = showcase.find((s) => s.scenario.id === pick) ?? showcase[0];
  const runs = [
    { key: "robust", label: "Robust flasher", run: sc.runs.robust },
    { key: "baseline", label: "Happy-path script", run: sc.runs.baseline },
  ];
  const maxMs = Math.max(...runs.map((r) => r.run.record.result.duration_ms)) * 1.08;
  const x = (ms: number) => LABEL_W + (ms / maxMs) * PLOT_W;
  const height = runs.length * LANE_H + AXIS_H;
  const faults = faultTimes(sc.runs.robust);
  const off = sc.scenario.faults.find((f) => f.off_ms)?.off_ms;
  const ticks = Array.from({ length: Math.floor(maxMs / 1000) + 1 }, (_, i) => i * 1000);
  const names: Record<string, string> = {
    power_cut_transfer: `Power cut mid-transfer (${sc.scenario.ecu})`,
    bad_build: `Build fails its self-test (${sc.scenario.ecu})`,
  };

  return (
    <figure style={{ margin: 0 }}>
      <div style={{ marginBottom: space.md }}>
        <Segmented
          label="Scenario"
          value={pick}
          onChange={setPick}
          options={showcase.map((s) => ({ value: s.scenario.id, label: names[s.scenario.family] ?? s.scenario.id }))}
        />
      </div>
      <svg viewBox={`0 0 ${VIEW_W} ${height}`} width="100%" role="img"
           aria-label={`Timeline of ${names[sc.scenario.family] ?? sc.scenario.id}, both flashers`}>
        <defs>
          <clipPath id="draw">
            <motion.rect
              key={pick}
              x={0}
              y={0}
              height={height}
              initial={{ width: reduce ? VIEW_W : LABEL_W }}
              animate={{ width: VIEW_W }}
              transition={{ duration: reduce ? 0 : m.drawSeconds, ease: m.ease }}
            />
          </clipPath>
        </defs>
        {ticks.map((t) => (
          <g key={t}>
            <line x1={x(t)} x2={x(t)} y1={0} y2={height - AXIS_H} stroke={color.grid} strokeWidth={stroke.hair} />
            <text x={x(t)} y={height - 8} fontFamily={font.figures} fontSize={svgText.tick} fill={color.graphite} textAnchor="middle">
              {t / 1000} s
            </text>
          </g>
        ))}
        <g clipPath="url(#draw)">
          {runs.map((r, i) => (
            <Lane key={r.key} run={r.run} label={r.label} y={i * LANE_H} x={x} />
          ))}
          {faults.map((f) => (
            <g key={f.kind + f.ms}>
              <line x1={x(f.ms)} x2={x(f.ms)} y1={4} y2={height - AXIS_H} stroke={color.hv} strokeWidth={stroke.wire} />
              {off && (
                <g>
                  <rect x={x(f.ms)} y={height - AXIS_H - OFF_H} width={x(f.ms + off) - x(f.ms)} height={OFF_H} fill={color.hv} opacity={0.35} />
                  <text x={x(f.ms + off) + 6} y={height - AXIS_H} fontFamily={font.figures} fontSize={svgText.tick} fill={color.hv}>
                    ECU without power
                  </text>
                </g>
              )}
              <text x={x(f.ms) + 6} y={14} fontFamily={font.body} fontSize={svgText.note} fontWeight={weight.medium} fill={color.hv}>
                {f.kind === "power_cut" ? `Power cut, ${seconds(off ?? 0)} off` : f.kind}
              </text>
            </g>
          ))}
        </g>
      </svg>
      <figcaption style={{ fontSize: size.small, color: color.graphite, marginTop: space.sm }}>
        Coloured bands are the UDS steps each flasher took; the thin line under each is CAN traffic on the
        ECU's bus. Simulated time, from dev scenario {sc.scenario.id}.
      </figcaption>
    </figure>
  );
}
