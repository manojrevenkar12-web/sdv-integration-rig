import type { ReactNode } from "react";
import { color, measure, radius, space } from "../theme/tokens";

// For error and empty states: says what happened and what to do next.
export function Notice({ tone, children, action }: { tone: "error" | "empty"; children: ReactNode; action?: ReactNode }) {
  return (
    <div
      role={tone === "error" ? "alert" : "status"}
      style={{
        maxWidth: measure.text,
        borderLeft: `4px solid ${tone === "error" ? color.hv : color.shield}`,
        background: color.sheet,
        borderRadius: radius.panel,
        padding: `${space.md}px ${space.lg}px`,
      }}
    >
      <div>{children}</div>
      {action && <div style={{ marginTop: space.md }}>{action}</div>}
    </div>
  );
}

export function Command({ children }: { children: string }) {
  return <code style={{ background: color.grid, padding: `0 ${space.xs}px`, borderRadius: radius.panel }}>{children}</code>;
}
