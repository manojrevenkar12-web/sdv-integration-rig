import type { ButtonHTMLAttributes } from "react";
import { color, font, radius, size, space, weight } from "../theme/tokens";

export function Button(props: ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      type="button"
      {...props}
      style={{
        font: "inherit",
        fontFamily: font.body,
        fontSize: size.body,
        fontWeight: weight.medium,
        color: color.sheet,
        background: color.ink,
        border: "none",
        borderRadius: radius.control,
        padding: `${space.sm}px ${space.md}px`,
        cursor: "pointer",
        ...props.style,
      }}
    />
  );
}
