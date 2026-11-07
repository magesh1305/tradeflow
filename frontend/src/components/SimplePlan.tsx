// Today's plan in two questions: how much may I lose, and which robots may trade.
// Everything else keeps the values from the last plan (see "Advanced" on the Plan page).

import { useState } from "react";
import type { DailyPlan, PlanResult, SettingsInfo } from "../api/types";
import { useLive } from "../app/live";
import { inr } from "../lib/format";
import { Icon } from "./Icon";
import { usePlanSubmit } from "./PlanSubmit";
import { Button, Notice, RiskBadge, inputClass } from "./ui";

const QUICK = [500, 1000, 2000, 5000];

export function SimplePlan({ info, onDone }: { info: SettingsInfo; onDone?: (r: PlanResult) => void }) {
  const { status } = useLive();
  const [loss, setLoss] = useState(String(info.plan.account.daily_loss_limit));
  const [enabled, setEnabled] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(Object.entries(info.plan.strategies).map(([id, s]) => [id, s.enabled])),
  );
  const [error, setError] = useState("");
  const { submit, pending, dialog } = usePlanSubmit(info, onDone);
  const lossValue = Number(loss);
  const lossOk = loss.trim() !== "" && Number.isFinite(lossValue) && lossValue > 0;
  const chosen = Object.values(enabled).filter(Boolean).length;
  const meta = (id: string) => {
    const s = info.strategies.find((x) => x.id === id);
    const live = status?.strategies.find((x) => x.id === id);
    return { title: s?.title || live?.title || id, risk: s?.risk_level ?? live?.risk_level, summary: live?.summary ?? "" };
  };

  return (
    <form
      className="space-y-6"
      onSubmit={(e) => {
        e.preventDefault();
        if (!lossOk) {
          setError("Enter the most you can afford to lose today, in rupees (more than 0).");
          return;
        }
        setError("");
        const plan: DailyPlan = {
          ...info.plan,
          account: { ...info.plan.account, daily_loss_limit: lossValue },
          strategies: Object.fromEntries(
            Object.entries(info.plan.strategies).map(([id, s]) => [id, { ...s, enabled: enabled[id] ?? false }]),
          ),
        };
        submit(plan);
      }}
    >
      <section>
        <label htmlFor="simple-loss" className="block text-base font-semibold text-slate-900 dark:text-white">
          1. Daily loss limit ₹ (the most you can afford to lose today)
        </label>
        <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
          Pick an amount you would be OK losing. In practice mode this is pretend money, so it is safe to try.
        </p>
        <div className="mt-3 flex flex-wrap gap-2">
          {QUICK.map((v) => (
            <button
              key={v}
              type="button"
              onClick={() => { setLoss(String(v)); }}
              className={`rounded-xl border px-4 py-2 text-sm font-medium transition ${
                lossValue === v
                  ? "border-brand-500 bg-brand-500 text-white"
                  : "border-slate-200 bg-white text-slate-700 hover:border-brand-200 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-200"
              }`}
            >
              {inr(v).replace(".00", "")}
            </button>
          ))}
        </div>
        <input
          id="simple-loss"
          className={`${inputClass} mt-3 max-w-xs text-lg`}
          type="number"
          inputMode="numeric"
          min={1}
          value={loss}
          onChange={(e) => { setLoss(e.target.value); }}
        />
        {lossOk && (
          <p className="mt-2 text-sm text-slate-700 dark:text-slate-300">
            If today&apos;s loss reaches <strong>{inr(lossValue)}</strong>, every robot stops and all trades close automatically.
          </p>
        )}
      </section>

      <section>
        <div className="text-base font-semibold text-slate-900 dark:text-white">2. Which robots may trade today?</div>
        <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">Tap to turn a robot on or off. New here? Start with one.</p>
        <div className="mt-3 grid gap-3 sm:grid-cols-2">
          {Object.keys(info.plan.strategies).map((id) => {
            const m = meta(id);
            const on = enabled[id] ?? false;
            return (
              <label
                key={id}
                className={`flex cursor-pointer gap-3 rounded-xl border-2 p-4 transition ${
                  on ? "border-brand-500 bg-brand-50/50 dark:bg-brand-500/10" : "border-slate-200 dark:border-slate-700"
                }`}
              >
                <input
                  type="checkbox"
                  className="mt-1 h-5 w-5 accent-brand-500"
                  checked={on}
                  onChange={(e) => { setEnabled({ ...enabled, [id]: e.target.checked }); }}
                />
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2 font-semibold text-slate-900 dark:text-white">
                    <Icon name="robot" className="h-4 w-4" /> {m.title}
                  </div>
                  {m.risk && <div className="mt-1"><RiskBadge level={m.risk} /></div>}
                  {m.summary && <p className="mt-1.5 text-sm text-slate-600 dark:text-slate-300">{m.summary}</p>}
                  <p className="mt-1 text-xs font-medium text-slate-500">{on ? "Will trade today" : "Off today"}</p>
                </div>
              </label>
            );
          })}
        </div>
        {chosen === 0 && (
          <p className="mt-2 text-sm text-amber-700 dark:text-amber-300">No robot is on, so nothing will trade today.</p>
        )}
      </section>

      {error && <Notice tone="danger">{error}</Notice>}

      <div className="flex flex-wrap items-center gap-3">
        <Button type="submit" variant="primary" className="min-h-12 px-6 text-base font-semibold" disabled={pending}>
          {pending ? "Submitting…" : info.submitted ? "Update today's plan" : "Submit today's plan"}
        </Button>
        <span className="text-sm text-slate-500 dark:text-slate-400">Robots that are on start right away.</span>
      </div>
      {dialog}
    </form>
  );
}
