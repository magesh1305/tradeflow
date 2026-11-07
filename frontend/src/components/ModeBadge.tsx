import type { Mode } from "../api/types";

/** Always-visible trading mode: PAPER in blue, LIVE in red. */
export function ModeBadge({ mode }: { mode: Mode | undefined }) {
  if (!mode) {
    return (
      <span className="rounded-md bg-slate-300 px-3 py-1 text-sm font-bold text-slate-800" data-testid="mode-badge">
        MODE ?
      </span>
    );
  }
  const live = mode === "live";
  return (
    <span
      data-testid="mode-badge"
      aria-label={`${mode} trading mode`}
      className={`rounded-md px-3 py-1 text-base font-extrabold tracking-widest text-white ${
        live ? "animate-pulse bg-red-600 ring-2 ring-red-300" : "bg-blue-600"
      }`}
    >
      {live ? "LIVE" : "PAPER"}
    </span>
  );
}
