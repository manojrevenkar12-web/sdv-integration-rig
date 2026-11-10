import type { ReactNode } from "react";
import { color, leading, measure, size, space, weight } from "../theme/tokens";

export function Section({ title, intro, children, id }: { title: string; intro?: ReactNode; children: ReactNode; id?: string }) {
  return (
    <section aria-labelledby={id} style={{ marginTop: space.xxl }}>
      <h2 id={id} style={{ fontSize: size.h2, fontWeight: weight.semibold, lineHeight: leading.tight, margin: 0 }}>
        {title}
      </h2>
      {intro && (
        <p style={{ maxWidth: measure.text, color: color.graphite, margin: `${space.sm}px 0 ${space.lg}px` }}>{intro}</p>
      )}
      {children}
    </section>
  );
}
