// The daily trading plan. Submit it every morning: no strategy starts until you do.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError, api } from "../api/client";
import type { DailyPlan, SettingsInfo } from "../api/types";
import { useToast } from "../app/toasts";
import { Dialog } from "../components/Dialog";
import { Button, Card, Pill, inputClass } from "../components/ui";
import { istDateTime, istTime } from "../lib/format";
import { FormError, fromForm, toForm, type PlanForm, type StrategyForm } from "../lib/plan";

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="text-sm">
      <span className="mb-1 block text-slate-600 dark:text-slate-300">{label}</span>
      {children}
      {hint && <span className="mt-0.5 block text-xs text-slate-500">{hint}</span>}
    </label>
  );
}

function NumberInput({ value, onChange, placeholder = "no limit", step = "any" }: {
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
    <div className="rounded-lg border border-slate-200 p-3 dark:border-slate-800">
      <div className="mb-3 flex flex-wrap items-center gap-3">
        <label className="flex items-center gap-2 font-semibold">
          <input
            type="checkbox"
            className="h-5 w-5"
            checked={value.enabled}
            onChange={(e) => {
              set("enabled", e.target.checked);
            }}
          />
          {id}
        </label>
        {info && <span className="text-xs text-slate-500">{info.strategy}</span>}
        {info && <Pill value={info.state} />}
        <span className="text-xs text-slate-500">{value.enabled ? "trades today" : "off today"}</span>
      </div>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
        <Field label="Lots per trade">
          <NumberInput
            value={value.lots}
            step="1"
            placeholder={info?.has_lots === false ? "n/a" : "unchanged"}
            onChange={(v) => {
              set("lots", v);
            }}
          />
        </Field>
        <Field label="Max trades / day">
          <NumberInput value={value.max_trades_per_day} step="1" onChange={(v) => { set("max_trades_per_day", v); }} />
        </Field>
        <Field label="Loss limit ₹" hint="hit: stop + exit">
          <NumberInput value={value.loss_limit} onChange={(v) => { set("loss_limit", v); }} />
        </Field>
        <Field label="Profit target ₹" hint="hit: no new trades">
          <NumberInput value={value.profit_target} onChange={(v) => { set("profit_target", v); }} />
        </Field>
        <Field label="Max lots / order">
          <NumberInput value={value.max_lots_per_order} step="1" onChange={(v) => { set("max_lots_per_order", v); }} />
        </Field>
        <Field label="Max capital ₹" hint="margin estimate">
          <NumberInput value={value.max_capital} onChange={(v) => { set("max_capital", v); }} />
        </Field>
      </div>
    </div>
  );
}

function PlanEditor({ info }: { info: SettingsInfo }) {
  const [form, setForm] = useState<PlanForm>(() => toForm(info.plan));
  const [loosen, setLoosen] = useState<{ plan: DailyPlan; items: string[] } | null>(null);
  const [typed, setTyped] = useState("");
  const toast = useToast();
  const qc = useQueryClient();
  const submit = useMutation({
    mutationFn: ({ plan, confirm }: { plan: DailyPlan; confirm?: string }) => api.submitPlan(plan, confirm),
    meta: { handlesErrors: true },
    onSuccess: (r) => {
      setLoosen(null);
      setTyped("");
      toast.push(
        [info.submitted ? "Plan updated" : "Today's plan submitted",
          r.started.length ? `started: ${r.started.join(", ")}` : "",
          r.stopped.length ? `stopped: ${r.stopped.join(", ")}` : ""].filter(Boolean).join(" · "),
        "success",
      );
      for (const w of r.warnings) toast.push(w, "error");
      void qc.invalidateQueries();
    },
    onError: (e, vars) => {
      if (e instanceof ApiError && e.status === 409 && typeof e.detail === "object" && e.detail !== null
        && "loosened" in e.detail) {
        setLoosen({ plan: vars.plan, items: (e.detail as { loosened: string[] }).loosened });
        return;
      }
      toast.push(e.message, "error");
    },
  });

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
          submit.mutate({ plan: fromForm(form) });
        } catch (err) {
          toast.push(err instanceof FormError ? err.message : "Invalid form", "error");
        }
      }}
    >
      <Card title="Account limits">
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <Field label="Daily loss limit ₹" hint="hit: kill switch">
            <NumberInput value={a.daily_loss_limit} placeholder="required" onChange={(v) => { setAccount("daily_loss_limit", v); }} />
          </Field>
          <Field label="Profit target ₹" hint="hit: no new trades">
            <NumberInput value={a.profit_target} onChange={(v) => { setAccount("profit_target", v); }} />
          </Field>
          <Field label="Max trades / day" hint="new entries, all strategies">
            <NumberInput value={a.max_trades_per_day} step="1" onChange={(v) => { setAccount("max_trades_per_day", v); }} />
          </Field>
          <Field label="Max open positions">
            <NumberInput value={a.max_open_positions} step="1" placeholder="required" onChange={(v) => { setAccount("max_open_positions", v); }} />
          </Field>
          <Field label="Max lots / order">
            <NumberInput value={a.max_lots_per_order} step="1" placeholder="required" onChange={(v) => { setAccount("max_lots_per_order", v); }} />
          </Field>
          <Field label="New trades from">
            <input className={inputClass} type="time" value={a.trading_start} onChange={(e) => { setAccount("trading_start", e.target.value); }} />
          </Field>
          <Field label="New trades until">
            <input className={inputClass} type="time" value={a.trading_end} onChange={(e) => { setAccount("trading_end", e.target.value); }} />
          </Field>
        </div>
      </Card>

      <Card title="Strategies">
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

      <div className="sticky bottom-16 z-20 flex flex-wrap items-center gap-3 rounded-lg border border-slate-200 bg-white/95 p-3 shadow sm:bottom-2 dark:border-slate-800 dark:bg-slate-900/95">
        <Button type="submit" variant="primary" className="min-h-11 px-6 text-base font-semibold" disabled={submit.isPending}>
          {submit.isPending ? "Submitting…" : info.submitted ? "Update today's plan" : "Submit today's plan"}
        </Button>
        <span className="text-xs text-slate-500">
          Tightening applies instantly.{info.market_hours ? " Loosening during market hours needs confirmation." : ""}
          {" "}Exits and stop losses are never blocked.
        </span>
      </div>

      <Dialog open={loosen !== null} title="Loosen limits during market hours?" onClose={() => { setLoosen(null); setTyped(""); }}>
        <p className="mb-2 text-sm text-slate-600 dark:text-slate-300">These limits become less strict:</p>
        <ul className="mb-3 list-disc pl-5 text-sm">
          {loosen?.items.map((i) => <li key={i}>{i}</li>)}
        </ul>
        <p className="mb-2 text-sm">
          This is logged and sent as an alert. Type <strong>{info.loosen_confirmation}</strong> to confirm.
        </p>
        <input
          aria-label="Type LOOSEN to confirm"
          className={`${inputClass} mb-3 text-center font-bold tracking-widest`}
          value={typed}
          autoComplete="off"
          onChange={(e) => {
            setTyped(e.target.value.toUpperCase());
          }}
        />
        <div className="flex gap-2">
          <Button className="flex-1" onClick={() => { setLoosen(null); setTyped(""); }}>Cancel</Button>
          <Button
            variant="danger"
            className="flex-1"
            disabled={typed !== info.loosen_confirmation || submit.isPending}
            onClick={() => {
              if (loosen) submit.mutate({ plan: loosen.plan, confirm: info.loosen_confirmation });
            }}
          >
            Loosen limits
          </Button>
        </div>
      </Dialog>
    </form>
  );
}

function History() {
  const q = useQuery({ queryKey: ["plan-history"], queryFn: api.planHistory, refetchInterval: 30_000 });
  const rows = q.data ?? [];
  if (rows.length === 0) return null;
  return (
    <Card title="Today's changes">
      <ul className="space-y-2 text-sm">
        {rows.map((r) => (
          <li key={r.ts} className="border-b border-slate-100 pb-2 last:border-0 dark:border-slate-800">
            <span className="mr-2 font-mono text-xs text-slate-500">{istTime(r.ts)}</span>
            {r.loosened.length > 0 && <span className="mr-2 rounded bg-rose-100 px-1.5 text-xs font-semibold text-rose-800 dark:bg-rose-900/50 dark:text-rose-200">LOOSENED</span>}
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
  if (!info) return <p className="text-sm text-slate-500">{q.isLoading ? "Loading…" : "Unavailable."}</p>;
  return (
    <div className="space-y-4 pb-24 sm:pb-16">
      <div
        role={info.submitted ? "status" : "alert"}
        className={`rounded-lg border p-3 text-sm ${info.submitted
          ? "border-emerald-300 bg-emerald-50 text-emerald-900 dark:border-emerald-800 dark:bg-emerald-950 dark:text-emerald-100"
          : "border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-100"}`}
      >
        <strong>Plan for {info.trading_date}: </strong>
        {info.submitted
          ? `submitted${info.submitted_at ? ` at ${istDateTime(info.submitted_at)}` : ""}. Changes apply immediately.`
          : "not submitted yet. Review the values (pre-filled from your last plan) and submit; no strategy starts until you do."}
      </div>
      {/* Re-mount the editor when the saved plan changes (after a submit). */}
      <PlanEditor key={JSON.stringify(info.plan) + String(info.submitted)} info={info} />
      <History />
    </div>
  );
}
