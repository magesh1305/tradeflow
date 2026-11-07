const inrFmt = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  maximumFractionDigits: 2,
  minimumFractionDigits: 2,
});
const numFmt = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2 });

export const inr = (v: number | null | undefined): string =>
  v === null || v === undefined || Number.isNaN(v) ? "—" : inrFmt.format(v);

export const num = (v: number | null | undefined, digits = 2): string =>
  v === null || v === undefined || Number.isNaN(v)
    ? "—"
    : new Intl.NumberFormat("en-IN", { maximumFractionDigits: digits }).format(v);

export const qty = (v: number): string => numFmt.format(v);

/** Tailwind classes for profit / loss colouring. */
export const pnlClass = (v: number): string =>
  v > 0 ? "text-emerald-600 dark:text-emerald-400" : v < 0 ? "text-rose-600 dark:text-rose-400" : "";

const timeFmt = new Intl.DateTimeFormat("en-IN", {
  timeZone: "Asia/Kolkata",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});
const dateTimeFmt = new Intl.DateTimeFormat("en-IN", {
  timeZone: "Asia/Kolkata",
  day: "2-digit",
  month: "short",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

export const istTime = (iso: string): string => timeFmt.format(new Date(iso));
export const istDateTime = (iso: string): string => dateTimeFmt.format(new Date(iso));

/** Today's date in IST as YYYY-MM-DD. */
export function todayIst(now: Date = new Date()): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Kolkata" }).format(now);
}
