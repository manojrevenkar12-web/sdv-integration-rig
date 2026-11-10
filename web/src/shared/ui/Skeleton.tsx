import { color, radius, space } from "../theme/tokens";

export function SkeletonBlock({ height, width = "100%" }: { height: number; width?: number | string }) {
  return (
    <div
      aria-hidden
      style={{ height, width, background: color.grid, borderRadius: radius.panel, marginBottom: space.md }}
    />
  );
}

export function PageSkeleton() {
  return (
    <div role="status" aria-label="Loading results">
      <SkeletonBlock height={48} width="60%" />
      <SkeletonBlock height={20} width="40%" />
      <SkeletonBlock height={220} />
      <SkeletonBlock height={160} />
      <SkeletonBlock height={160} />
    </div>
  );
}
