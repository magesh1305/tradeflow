// The daily trading plan. Submit it every morning: no strategy starts until you do.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import type { SettingsInfo } from "../api/types";
import { useToast } from "../app/toasts";
import { Icon } from "../components/Icon";
import { usePlanSubmit } from "../components/PlanSubmit";
import { SimplePlan } from "../components/SimplePlan";
import { Term } from "../components/Term";
import { Button, Card, Notice, PageTitle, Pill, RiskBadge, inputClass } from "../components/ui";
import { istDateTime, istTime } from "../lib/format";
import { FormError, fromForm, toForm, type PlanForm, type StrategyForm } from "../lib/plan";

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="text-sm">
      <span className="mb-1 block font-medium text-slate-800 dark:text-slate-200">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-xs text-slate-500 dark:text-slate-400">{hint}</span>}
    </label>
  );
}

function NumberInput({ value, onChange, placeholder = "No limit", step = "any" }: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  step?: string;
}) {
  return (
    <input
      className={inputClass}
      type="number"
      inputMode="decimal"
      min={0}
      step={step}
      placeholder={placeholder}
      value={value}
      onChange={(e) => {
        onChange(e.target.value);
      }}
    />
  );
}

function StrategyCard({ id, info, value, onChange }: {
  id: string;
  info: SettingsInfo["strategies"][number] | undefined;
  value: StrategyForm;
  onChange: (v: StrategyForm) => void;
}) {
  const set = (k: keyof StrategyForm, v: string | boolean) => {
    onChange({ ...value, [k]: v });
  };
  return (
    <div className={`rounded-xl border p-4 transition ${value.enabled ? "border-brand-200 bg-brand-50/30 dark:border-brand-500/30 dark:bg-brand-500/5" : "border-slate-200 dark:border-slate-800"}`}>
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <label className="flex cursor-pointer items-center gap-3">
          <input
            type="checkbox"
            className="h-5 w-5 accent-brand-500"
            checked={value.enabled}
            onChange={(e) => {
              set("enabled", e.target.checked);
            }}
          />
          <span className="grid h-9 w-9 place-items-center rounded-xl bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300">
            <Icon name="robot" />
          </span>
          <span className="font-semibold text-slate-900 dark:text-white">{info?.title || id}</span>
        </label>
        {info?.risk_level && <RiskBadge level={info.risk_level} />}
        {info && <Pill value={info.state} />}
        <span className={`ml-auto text-sm font-medium ${value.enabled ? "text-brand-700 dark:text-brand-200" : "text-slate-500"}`}>
          {value.enabled ? "Trades today" : "Off today"}
        </span>
      </div>
      <div className={`grid grid-cols-2 gap-4 md:grid-cols-3 ${value.enabled ? "" : "opacity-60"}`}>
        <Field label="Lots per trade" hint="Size of each trade.">
          <NumberInput
            value={value.lots}
            step="1"
            placeholder={info?.has_lots === false ? "Not used" : "Keep current"}
            onChange={(v) => {
              set("lots", v);
            }}
          />
        </Field>
        <Field label="Most trades today" hint="Robot stops opening new trades after this.">
          <NumberInput value={value.max_trades_per_day} step="1" onChange={(v) => { set("max_trades_per_day", v); }} />
        </Field>
        <Field label="Loss limit for this robot ₹" hint="Reached: this robot stops and closes its trades.">
          <NumberInput value={value.loss_limit} onChange={(v) => { set("loss_limit", v); }} />
        </Field>
        <Field label="Profit target for this robot ₹" hint="Reached: no new trades for this robot.">
          <NumberInput value={value.profit_target} onChange={(v) => { set("profit_target", v); }} />
        </Field>
        <Field label="Most lots in one order">
          <NumberInput value={value.max_lots_per_order} step="1" onChange={(v) => { set("max_lots_per_order", v); }} />
        </Field>
        <Field label="Most money to use ₹" hint="Estimated money Zerodha blocks for its trades.">
          <NumberInput value={value.max_capital} onChange={(v) => { set("max_capital", v); }} />
        </Field>
      </div>
    </div>
  );
}

function PlanEditor({ info }: { info: SettingsInfo }) {
  const [form, setForm] = useState<PlanForm>(() => toForm(info.plan));
  const toast = useToast();
  const { submit, pending, dialog } = usePlanSubmit(info);

  const a = form.account;
  const setAccount = (k: keyof PlanForm["account"], v: string) => {
    setForm({ ...form, account: { ...a, [k]: v } });
  };
  const byId = new Map(info.strategies.map((s) => [s.id, s]));

  return (
    <form
      className="space-y-4"
      onSubmit={(e) => {
        e.preventDefault();
        try {
          submit(fromForm(form));
        } catch (err) {
          toast.push(err instanceof FormError ? err.message : "Invalid form", "error");
        }
      }}
    >
      <Card title="Limits for the whole account" subtitle="These apply to all robots together." icon="shield">
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 md:grid-cols-3">
          <Field label="Daily loss limit ₹" hint="Reached: everything stops and all trades close. Example: 5000 means you can lose at most about ₹5,000 today.">
            <NumberInput value={a.daily_loss_limit} placeholder="required" onChange={(v) => { setAccount("daily_loss_limit", v); }} />
          </Field>
          <Field label="Profit target ₹" hint="Reached: no new trades today. Open trades keep running.">
            <NumberInput value={a.profit_target} onChange={(v) => { setAccount("profit_target", v); }} />
          </Field>
          <Field label="Most trades today" hint="New trades by all robots together.">
            <NumberInput value={a.max_trades_per_day} step="1" onChange={(v) => { setAccount("max_trades_per_day", v); }} />
          </Field>
          <Field label="Most trades open at once">
            <NumberInput value={a.max_open_positions} step="1" placeholder="required" onChange={(v) => { setAccount("max_open_positions", v); }} />
          </Field>
          <Field label="Most lots in one order">
            <NumberInput value={a.max_lots_per_order} step="1" placeholder="required" onChange={(v) => { setAccount("max_lots_per_order", v); }} />
          </Field>
          <Field label="New trades from" hint="Market opens at 09:15.">
            <input className={inputClass} type="time" value={a.trading_start} onChange={(e) => { setAccount("trading_start", e.target.value); }} />
          </Field>
          <Field label="New trades until" hint="Closing trades is always allowed.">
            <input className={inputClass} type="time" value={a.trading_end} onChange={(e) => { setAccount("trading_end", e.target.value); }} />
          </Field>
        </div>
      </Card>

      <Card title="Robots" subtitle="Tick the robots that may trade today and set their limits. Empty boxes mean no extra limit." icon="robot">
        <div className="space-y-3">
          {Object.entries(form.strategies).map(([id, s]) => (
            <StrategyCard
              key={id}
              id={id}
              info={byId.get(id)}
              value={s}
              onChange={(v) => {
                setForm({ ...form, strategies: { ...form.strategies, [id]: v } });
              }}
            />
          ))}
        </div>
      </Card>

      <div className="sticky bottom-20 z-20 flex flex-wrap items-center gap-3 rounded-2xl border border-slate-200 bg-white/95 p-3 shadow-lg backdrop-blur lg:bottom-3 dark:border-slate-800 dark:bg-slate-900/95">
        <Button type="submit" variant="primary" className="min-h-11 px-6 text-base font-semibold" disabled={pending}>
          {pending ? "Submitting…" : info.submitted ? "Update today's plan" : "Submit today's plan"}
        </Button>
        <span className="text-xs text-slate-500">
          Stricter limits apply at once.{info.market_hours ? " Making limits less strict during market hours needs a typed confirmation." : ""}
          {" "}Closing trades is never blocked.
        </span>
      </div>

      {dialog}
    </form>
  );
}

function ModeSwitch({ advanced, onChange }: { advanced: boolean; onChange: (v: boolean) => void }) {
  return (
    <div className="flex flex-wrap items-center gap-3">
      <div role="tablist" className="inline-flex rounded-xl bg-slate-100 p-1 dark:bg-slate-800">
        {[
          [false, "Simple"],
          [true, "Advanced"],
        ].map(([value, label]) => (
          <button
            key={String(label)}
            type="button"
            role="tab"
            aria-selected={advanced === value}
            onClick={() => { onChange(value as boolean); }}
            className={`rounded-lg px-4 py-2 text-sm font-medium transition ${
              advanced === value ? "bg-white text-slate-900 shadow-sm dark:bg-slate-950 dark:text-white" : "text-slate-600 dark:text-slate-300"
            }`}
          >
            {label}
          </button>
        ))}
      </div>
      <span className="text-sm text-slate-500 dark:text-slate-400">
        {advanced ? "Every limit, for people who know what they mean." : "Just two questions. Everything else keeps its last value."}
      </span>
    </div>
  );
}

function History() {
  // History is a nice-to-have: if the database is down, just hide it.
  const q = useQuery({ queryKey: ["plan-history"], queryFn: api.planHistory, refetchInterval: 30_000, meta: { handlesErrors: true } });
  const rows = q.data ?? [];
  if (rows.length === 0) return null;
  return (
    <Card title="Changes made today" icon="clock">
      <ul className="space-y-2 text-sm">
        {rows.map((r) => (
          <li key={r.ts} className="border-b border-slate-100 pb-2 last:border-0 dark:border-slate-800">
            <span className="mr-2 font-mono text-xs text-slate-500">{istTime(r.ts)}</span>
            {r.loosened.length > 0 && <span className="mr-2 rounded bg-rose-100 px-1.5 text-xs font-semibold text-rose-800 dark:bg-rose-900/50 dark:text-rose-200">LESS STRICT</span>}
            {r.changes.join(" · ") || "no changes"}
          </li>
        ))}
      </ul>
    </Card>
  );
}

export function SettingsPage() {
  const q = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const info = q.data;
  const [advanced, setAdvanced] = useState(false);
  if (!info) return <p className="text-sm text-slate-500">{q.isLoading ? "Loading…" : "Unavailable."}</p>;
  return (
    <div className="space-y-5 pb-24 sm:pb-16">
      <PageTitle
        title="Today's plan"
        subtitle={<>Decide every morning how much risk you accept today. Robots only trade after you submit this <Term k="plan">plan</Term>.</>}
      />
      <Notice tone={info.submitted ? "good" : "warn"}>
        <strong>Plan for {info.trading_date}: </strong>
        {info.submitted
          ? `submitted${info.submitted_at ? ` at ${istDateTime(info.submitted_at)}` : ""}. Changes apply immediately.`
          : "not submitted yet. The boxes are filled in from your last plan. Check them and press Submit; no robot starts until you do."}
      </Notice>
      <ModeSwitch advanced={advanced} onChange={setAdvanced} />
      {/* Re-mount the editors when the saved plan changes (after a submit). */}
      {advanced ? (
        <PlanEditor key={JSON.stringify(info.plan) + String(info.submitted)} info={info} />
      ) : (
        <Card>
          <SimplePlan key={JSON.stringify(info.plan) + String(info.submitted)} info={info} />
        </Card>
      )}
      <History />
    </div>
  );
}
