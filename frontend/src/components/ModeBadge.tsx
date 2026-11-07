import type { Mode } from "../api/types";

/** Always-visible trading mode: practice (blue) or real money (red, pulsing). */
export function ModeBadge({ mode }: { mode: Mode | undefined }) {
  if (!mode) {
    return (
      <span className="rounded-full bg-slate-200 px-3 py-1 text-xs font-bold text-slate-700" data-testid="mode-badge">
        Connecting…
      </span>
    );
  }
  const live = mode === "live";
  return (
    <span
      data-testid="mode-badge"
      aria-label={live ? "Real money mode" : "Practice mode, no real money"}
      title={live ? "Real orders, real money" : "Pretend money. No order reaches Zerodha."}
      className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-3 py-1 text-xs font-bold tracking-wide text-white ${
        live ? "animate-pulse bg-red-600 ring-2 ring-red-300" : "bg-blue-600"
      }`}
    >
      <span className="h-1.5 w-1.5 rounded-full bg-white" />
      {live ? "REAL MONEY" : "PRACTICE MODE"}
    </span>
  );
}
