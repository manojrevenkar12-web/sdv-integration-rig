import type { ReactNode } from "react";

// Wide tables scroll sideways on narrow screens instead of squeezing their columns.
export function TableScroll({ children, label }: { children: ReactNode; label: string }) {
  return (
    <div role="region" aria-label={label} tabIndex={0} style={{ overflowX: "auto" }}>
      {children}
    </div>
  );
}
