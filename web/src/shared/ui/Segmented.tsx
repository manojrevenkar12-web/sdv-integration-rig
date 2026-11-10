import { motion } from "framer-motion";
import { color, font, motion as m, radius, size, space, weight } from "../theme/tokens";

export function Segmented<T extends string>({ options, value, onChange, label }: {
  options: { value: T; label: string }[];
  value: T;
  onChange: (v: T) => void;
  label: string;
}) {
  return (
    <div role="radiogroup" aria-label={label} style={{ display: "inline-flex", gap: space.xs, flexWrap: "wrap" }}>
      {options.map((o) => {
        const on = o.value === value;
        return (
          <button
            key={o.value}
            type="button"
            role="radio"
            aria-checked={on}
            onClick={() => onChange(o.value)}
            style={{
              position: "relative",
              font: "inherit",
              fontFamily: font.body,
              fontSize: size.small,
              fontWeight: weight.medium,
              color: on ? color.sheet : color.ink,
              background: "transparent",
              border: `1px solid ${color.rule}`,
              borderRadius: radius.control,
              padding: `${space.xs}px ${space.md}px`,
              cursor: "pointer",
            }}
          >
            {on && (
              <motion.span
                layoutId={`seg-${label}`}
                transition={{ duration: m.quickSeconds, ease: m.ease }}
                style={{ position: "absolute", inset: 0, background: color.ink, borderRadius: radius.control - 1 }}
              />
            )}
            <span style={{ position: "relative" }}>{o.label}</span>
          </button>
        );
      })}
    </div>
  );
}
