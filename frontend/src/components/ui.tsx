// Shared building blocks: cards, buttons, money, stat tiles, status pills, empty states.

import type { ButtonHTMLAttributes, ReactNode } from "react";
import { inr, num } from "../lib/format";
import { Icon, type IconName } from "./Icon";

export function Card({ title, subtitle, icon, children, actions, className = "" }: {
  title?: ReactNode;
  subtitle?: ReactNode;
  icon?: IconName;
  children: ReactNode;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <section
      className={`rounded-2xl border border-slate-200/80 bg-white p-4 shadow-sm sm:p-5 dark:border-slate-800 dark:bg-slate-900 ${className}`}
    >
      {(title ?? actions) && (
        <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
          <div className="flex items-start gap-3">
            {icon && (
              <span className="mt-0.5 rounded-xl bg-brand-50 p-2 text-brand-600 dark:bg-brand-500/15 dark:text-brand-200">
                <Icon name={icon} />
              </span>
            )}
            <div>
              {title && <h2 className="text-base font-semibold text-slate-900 dark:text-white">{title}</h2>}
              {subtitle && <p className="mt-0.5 text-sm text-slate-500 dark:text-slate-400">{subtitle}</p>}
            </div>
          </div>
          {actions && <div className="flex flex-wrap gap-2">{actions}</div>}
        </div>
      )}
      {children}
    </section>
  );
}

type Variant = "primary" | "secondary" | "danger" | "ghost" | "success";
const variants: Record<Variant, string> = {
  primary: "bg-brand-500 text-white shadow-sm hover:bg-brand-600",
  success: "bg-emerald-600 text-white shadow-sm hover:bg-emerald-700",
  secondary:
    "border border-slate-200 bg-white text-slate-800 hover:bg-slate-50 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100 dark:hover:bg-slate-700",
  danger: "bg-red-600 text-white shadow-sm hover:bg-red-700",
  ghost: "text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800",
};

export function Button({ variant = "secondary", className = "", ...rest }: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: Variant;
}) {
  return (
    <button
      type="button"
      {...rest}
      className={`inline-flex min-h-10 items-center justify-center gap-2 rounded-xl px-4 py-2 text-sm font-medium transition focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 disabled:cursor-not-allowed disabled:opacity-50 ${variants[variant]} ${className}`}
    />
  );
}

/** Profit/loss shown with a sign AND a word, never colour alone. */
export function Money({ value, className = "", word = false }: { value: number; className?: string; word?: boolean }) {
  const tone =
    value > 0 ? "text-good-text dark:text-emerald-400" : value < 0 ? "text-red-600 dark:text-red-400" : "text-slate-700 dark:text-slate-200";
  const sign = value > 0 ? "+" : value < 0 ? "−" : "";
  return (
    <span className={`${tone} ${className}`}>
      {sign}
      {inr(Math.abs(value))}
      {word && value !== 0 && <span className="ml-1 text-sm font-medium">{value > 0 ? "profit" : "loss"}</span>}
    </span>
  );
}

export function Stat({ label, value, hint, money = true }: {
  label: ReactNode;
  value: number;
  hint?: ReactNode;
  money?: boolean;
}) {
  return (
    <div className="rounded-2xl border border-slate-200/80 bg-white p-4 shadow-sm dark:border-slate-800 dark:bg-slate-900">
      <div className="text-sm text-slate-500 dark:text-slate-400">{label}</div>
      <div className="mt-1 text-xl font-semibold">
        {money ? <Money value={value} /> : <span>{num(value, 0)}</span>}
      </div>
      {hint && <div className="mt-1 text-xs text-slate-500 dark:text-slate-400">{hint}</div>}
    </div>
  );
}

// Friendly words for machine states; each pill pairs colour with a word.
const STATES: Record<string, { label: string; tone: string }> = {
  RUNNING: { label: "Running", tone: "bg-emerald-100 text-emerald-800 dark:bg-emerald-500/15 dark:text-emerald-300" },
  STOPPED: { label: "Stopped", tone: "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300" },
  ERROR: { label: "Error", tone: "bg-red-100 text-red-800 dark:bg-red-500/15 dark:text-red-300" },
  COMPLETE: { label: "Done", tone: "bg-emerald-100 text-emerald-800 dark:bg-emerald-500/15 dark:text-emerald-300" },
  REJECTED: { label: "Rejected", tone: "bg-red-100 text-red-800 dark:bg-red-500/15 dark:text-red-300" },
  CANCELLED: { label: "Cancelled", tone: "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300" },
  OPEN: { label: "Waiting to fill", tone: "bg-amber-100 text-amber-900 dark:bg-amber-500/15 dark:text-amber-200" },
  PENDING: { label: "Sending", tone: "bg-amber-100 text-amber-900 dark:bg-amber-500/15 dark:text-amber-200" },
  TRIGGER_PENDING: { label: "Stop loss set", tone: "bg-brand-50 text-brand-700 dark:bg-brand-500/15 dark:text-brand-200" },
  done: { label: "Finished", tone: "bg-emerald-100 text-emerald-800 dark:bg-emerald-500/15 dark:text-emerald-300" },
  failed: { label: "Failed", tone: "bg-red-100 text-red-800 dark:bg-red-500/15 dark:text-red-300" },
  running: { label: "Running", tone: "bg-brand-50 text-brand-700 dark:bg-brand-500/15 dark:text-brand-200" },
};

export function Pill({ value }: { value: string }) {
  const s = STATES[value] ?? { label: value, tone: "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300" };
  return <span className={`inline-block whitespace-nowrap rounded-full px-2.5 py-0.5 text-xs font-medium ${s.tone}`}>{s.label}</span>;
}

const MEDIUM = { label: "Medium risk", tone: "bg-amber-100 text-amber-900 dark:bg-amber-500/15 dark:text-amber-200" };
const RISK: Record<string, { label: string; tone: string }> = {
  low: { label: "Low risk", tone: "bg-emerald-100 text-emerald-800 dark:bg-emerald-500/15 dark:text-emerald-300" },
  medium: { label: "Medium risk", tone: "bg-amber-100 text-amber-900 dark:bg-amber-500/15 dark:text-amber-200" },
  high: { label: "High risk", tone: "bg-red-100 text-red-800 dark:bg-red-500/15 dark:text-red-300" },
};

export function RiskBadge({ level }: { level: string }) {
  const r = RISK[level] ?? MEDIUM;
  return (
    <span className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-medium ${r.tone}`}>
      <Icon name="alert" className="h-3.5 w-3.5" /> {r.label}
    </span>
  );
}

export function Empty({ children, icon = "info" }: { children: ReactNode; icon?: IconName }) {
  return (
    <div className="flex flex-col items-center gap-2 py-8 text-center text-sm text-slate-500 dark:text-slate-400">
      <Icon name={icon} className="h-8 w-8 text-slate-300 dark:text-slate-600" />
      <div className="max-w-sm">{children}</div>
    </div>
  );
}

export function Notice({ tone = "info", children }: { tone?: "info" | "warn" | "danger" | "good"; children: ReactNode }) {
  const tones = {
    info: "border-brand-200 bg-brand-50 text-brand-700 dark:border-brand-500/30 dark:bg-brand-500/10 dark:text-brand-200",
    warn: "border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-100",
    danger: "border-red-300 bg-red-50 text-red-900 dark:border-red-500/30 dark:bg-red-500/10 dark:text-red-100",
    good: "border-emerald-300 bg-emerald-50 text-emerald-900 dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-100",
  };
  const icon: Record<typeof tone, IconName> = { info: "info", warn: "alert", danger: "alert", good: "check" };
  return (
    <div role={tone === "danger" || tone === "warn" ? "alert" : "status"} className={`flex gap-3 rounded-2xl border p-4 text-sm ${tones[tone]}`}>
      <Icon name={icon[tone]} className="mt-0.5 h-5 w-5 shrink-0" />
      <div>{children}</div>
    </div>
  );
}

export function PageTitle({ title, subtitle }: { title: string; subtitle?: ReactNode }) {
  return (
    <div className="mb-1">
      <h1 className="text-2xl font-bold tracking-tight text-slate-900 dark:text-white">{title}</h1>
      {subtitle && <p className="mt-1 text-slate-600 dark:text-slate-400">{subtitle}</p>}
    </div>
  );
}

export const inputClass =
  "w-full rounded-xl border border-slate-300 bg-white px-3 py-2.5 text-sm shadow-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-500/30 dark:border-slate-700 dark:bg-slate-950";
