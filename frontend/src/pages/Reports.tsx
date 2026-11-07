// Activity (trades and orders), profit history and safety limits.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import type { DailyPnl, Fill, Mode, Order, ReportFilter } from "../api/types";
import { useLive } from "../app/live";
import { PnlBars } from "../components/Charts";
import { DataTable } from "../components/DataTable";
import { Icon } from "../components/Icon";
import { Term } from "../components/Term";
import { Card, Empty, Money, Notice, PageTitle, Pill, Stat, inputClass } from "../components/ui";
import { inr, istDateTime, num, todayIst } from "../lib/format";
import { lotsText, orderTypeWord, productWord, robotNames } from "../lib/words";

function Filters({ value, onChange }: { value: ReportFilter; onChange: (f: ReportFilter) => void }) {
  const { status } = useLive();
  return (
    <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
      <label className="text-sm text-slate-600 dark:text-slate-300">From
        <input type="date" className={inputClass} value={value.from ?? ""} onChange={(e) => { onChange({ ...value, from: e.target.value }); }} />
      </label>
      <label className="text-sm text-slate-600 dark:text-slate-300">To
        <input type="date" className={inputClass} value={value.to ?? ""} onChange={(e) => { onChange({ ...value, to: e.target.value }); }} />
      </label>
      <label className="text-sm text-slate-600 dark:text-slate-300">Robot
        <select className={inputClass} value={value.strategy ?? ""} onChange={(e) => { onChange({ ...value, strategy: e.target.value || undefined }); }}>
          <option value="">All robots</option>
          {(status?.strategies ?? []).map((s) => <option key={s.id} value={s.id}>{s.title || s.id}</option>)}
        </select>
      </label>
      <label className="text-sm text-slate-600 dark:text-slate-300">Money
        <select className={inputClass} value={value.mode ?? ""} onChange={(e) => { onChange({ ...value, mode: (e.target.value || undefined) as Mode | undefined }); }}>
          <option value="">Current mode</option>
          <option value="paper">Practice</option>
          <option value="live">Real money</option>
        </select>
      </label>
    </div>
  );
}

function useFilter(): [ReportFilter, (f: ReportFilter) => void] {
  const today = todayIst();
  return useState<ReportFilter>({ from: today, to: today });
}

function Side({ side }: { side: "BUY" | "SELL" }) {
  return (
    <span className={`inline-flex items-center gap-1 font-medium ${side === "BUY" ? "text-good-text dark:text-emerald-400" : "text-red-600 dark:text-red-400"}`}>
      <Icon name={side === "BUY" ? "arrowUp" : "arrowDown"} className="h-3.5 w-3.5" />
      {side === "BUY" ? "Buy" : "Sell"}
    </span>
  );
}

export function OrdersPage() {
  const [f, setF] = useFilter();
  const { status } = useLive();
  const name = robotNames(status?.strategies);
  const q = useQuery({ queryKey: ["orders", f], queryFn: () => api.orders(f), refetchInterval: 5000 });
  return (
    <>
      <Filters value={f} onChange={setF} />
      <DataTable<Order>
        rows={q.data ?? []}
        rowKey={(r) => r.client_order_id}
        csvName={`orders_${f.from ?? ""}_${f.to ?? ""}.csv`}
        empty={q.isLoading ? "Loading…" : "No orders in this period."}
        columns={[
          { key: "t", header: "When", render: (r) => istDateTime(r.created_at), csv: (r) => r.created_at },
          { key: "s", header: "Robot", render: (r) => name(r.strategy_id), csv: (r) => r.strategy_id },
          { key: "sym", header: "What", render: (r) => r.label || r.tradingsymbol, csv: (r) => r.tradingsymbol },
          { key: "side", header: "Buy / sell", render: (r) => <Side side={r.side} />, csv: (r) => r.side },
          { key: "q", header: "How much", render: (r) => `${lotsText(r.quantity, r.lot_size)}${r.filled_quantity && r.filled_quantity < r.quantity ? ` (${lotsText(r.filled_quantity, r.lot_size)} done)` : ""}`, csv: (r) => r.quantity },
          { key: "fq", header: "Filled", csvOnly: true, csv: (r) => r.filled_quantity, render: () => null },
          { key: "type", header: "Price type", render: (r) => orderTypeWord(r.order_type, r.protective), csv: (r) => r.order_type },
          { key: "prod", header: "Kind", render: (r) => productWord(r.product), csv: (r) => r.product },
          { key: "avg", header: "Got price", align: "right", render: (r) => (r.average_price ? `₹${num(r.average_price)}` : "—"), csv: (r) => r.average_price },
          { key: "px", header: "Price", csvOnly: true, render: () => null, csv: (r) => r.price },
          { key: "st", header: "Result", render: (r) => <Pill value={r.status} />, csv: (r) => r.status },
          { key: "msg", header: "Note", render: (r) => <span className="text-xs text-slate-500">{r.status_message || r.reason}</span>, csv: (r) => r.status_message || r.reason },
          { key: "id", header: "Order id", csvOnly: true, render: () => null, csv: (r) => r.broker_order_id ?? r.client_order_id },
        ]}
      />
    </>
  );
}

export function TradesPage() {
  const [f, setF] = useFilter();
  const { status } = useLive();
  const name = robotNames(status?.strategies);
  const q = useQuery({ queryKey: ["fills", f], queryFn: () => api.fills(f), refetchInterval: 5000 });
  const rows = q.data ?? [];
  const charges = rows.reduce((s, r) => s + r.charges, 0);
  return (
    <>
      <Filters value={f} onChange={setF} />
      {rows.length > 0 && (
        <p className="mb-3 text-sm text-slate-600 dark:text-slate-300">
          {rows.length} trade{rows.length === 1 ? "" : "s"} · <Term k="charges">charges</Term> paid {inr(charges)}
        </p>
      )}
      <DataTable<Fill>
        rows={rows}
        rowKey={(r) => r.fill_id}
        csvName={`trades_${f.from ?? ""}_${f.to ?? ""}.csv`}
        empty={q.isLoading ? "Loading…" : "No trades in this period."}
        columns={[
          { key: "t", header: "When", render: (r) => istDateTime(r.ts), csv: (r) => r.ts },
          { key: "s", header: "Robot", render: (r) => name(r.strategy_id), csv: (r) => r.strategy_id },
          { key: "sym", header: "What", render: (r) => r.label || r.tradingsymbol, csv: (r) => r.tradingsymbol },
          { key: "side", header: "Buy / sell", render: (r) => <Side side={r.side} />, csv: (r) => r.side },
          { key: "q", header: "How much", render: (r) => lotsText(r.quantity, r.lot_size), csv: (r) => r.quantity },
          { key: "px", header: "Price", align: "right", render: (r) => `₹${num(r.price)}`, csv: (r) => r.price },
          { key: "prod", header: "Kind", render: (r) => productWord(r.product), csv: (r) => r.product },
          { key: "ch", header: "Charges", align: "right", render: (r) => `₹${num(r.charges)}`, csv: (r) => r.charges },
          { key: "mode", header: "Money", render: (r) => (r.mode === "live" ? "Real" : "Practice"), csv: (r) => r.mode },
        ]}
      />
    </>
  );
}

export function ActivityPage() {
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") === "orders" ? "orders" : "trades";
  const tabs = [
    { id: "trades", label: "Trades done", hint: "Every buy and sell that actually happened." },
    { id: "orders", label: "All orders", hint: "Every request sent, including ones still waiting, cancelled or rejected." },
  ] as const;
  return (
    <div className="space-y-5">
      <PageTitle title="Activity" subtitle="A full record of what the robots did. Download it as a spreadsheet (CSV) any time." />
      <Card>
        <div role="tablist" className="mb-4 inline-flex rounded-xl bg-slate-100 p-1 dark:bg-slate-800">
          {tabs.map((t) => (
            <button
              key={t.id}
              type="button"
              role="tab"
              aria-selected={tab === t.id}
              onClick={() => { setParams(t.id === "trades" ? {} : { tab: t.id }); }}
              className={`rounded-lg px-4 py-2 text-sm font-medium transition ${
                tab === t.id ? "bg-white text-slate-900 shadow-sm dark:bg-slate-950 dark:text-white" : "text-slate-600 dark:text-slate-300"
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
        <p className="mb-4 text-sm text-slate-500 dark:text-slate-400">{tabs.find((t) => t.id === tab)?.hint}</p>
        {tab === "trades" ? <TradesPage /> : <OrdersPage />}
      </Card>
    </div>
  );
}

export function DailyPnlPage() {
  const q = useQuery({ queryKey: ["pnl"], queryFn: () => api.dailyPnl(), refetchInterval: 30_000 });
  const account = (q.data ?? []).filter((r) => r.strategy_id === "__account__");
  const total = account.reduce((s, r) => s + r.net_pnl, 0);
  const wins = account.filter((r) => r.net_pnl > 0).length;
  return (
    <div className="space-y-5">
      <PageTitle title="Profit history" subtitle="How each day went, after all charges." />
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Total, after charges" value={total} />
        <Stat label="Charges paid" value={-account.reduce((s, r) => s + r.charges, 0)} />
        <Stat label="Days traded" value={account.length} money={false} />
        <Stat label="Profitable days" value={wins} money={false} hint={account.length ? `${num((wins / account.length) * 100, 0)}% of days` : undefined} />
      </div>
      <Card title="Day by day" subtitle="Bars above the line are profit days; below the line are loss days." icon="chart">
        {account.length > 0 ? (
          <PnlBars points={account.map((r) => ({ time: r.trading_date, value: r.net_pnl }))} />
        ) : (
          <Empty icon="chart">No days recorded yet. Come back after the first trading day.</Empty>
        )}
      </Card>
      <Card title="Table">
        <DataTable<DailyPnl>
          rows={[...account].reverse()}
          rowKey={(r) => r.trading_date}
          csvName="daily_pnl.csv"
          empty="No days recorded yet."
          columns={[
            { key: "d", header: "Date", render: (r) => <>{r.trading_date}{r.live && <span className="ml-1 text-xs text-brand-600">(today, live)</span>}</>, csv: (r) => r.trading_date },
            { key: "g", header: "Before charges", align: "right", render: (r) => inr(r.gross_pnl), csv: (r) => r.gross_pnl },
            { key: "c", header: "Charges", align: "right", render: (r) => inr(r.charges), csv: (r) => r.charges },
            { key: "n", header: "After charges", align: "right", render: (r) => <Money value={r.net_pnl} word />, csv: (r) => r.net_pnl },
            { key: "t", header: "Trades", align: "right", render: (r) => r.trades, csv: (r) => r.trades },
          ]}
        />
      </Card>
    </div>
  );
}

function show(v: unknown): string {
  if (v === null || v === undefined) return "—";
  return typeof v === "string" || typeof v === "number" || typeof v === "boolean" ? String(v) : JSON.stringify(v);
}

function Meter({ label, used, max, hint, money = false }: { label: string; used: number; max: number; hint?: string; money?: boolean }) {
  const pct = max > 0 ? Math.min((used / max) * 100, 100) : 0;
  const level = pct >= 90 ? "critical" : pct >= 70 ? "warn" : "ok";
  const colour = { critical: "bg-red-500", warn: "bg-amber-500", ok: "bg-emerald-500" }[level];
  const fmt = (v: number) => (money ? inr(v) : num(v, 0));
  return (
    <div>
      <div className="flex justify-between gap-3 text-sm">
        <span className="font-medium text-slate-800 dark:text-slate-200">{label}</span>
        <span className="text-slate-600 dark:text-slate-300">
          {fmt(used)} of {fmt(max)}
          {level !== "ok" && <span className="ml-1 font-semibold">{level === "critical" ? "(almost reached)" : "(getting close)"}</span>}
        </span>
      </div>
      <div className="mt-1.5 h-2.5 rounded-full bg-slate-100 dark:bg-slate-800">
        <div className={`h-2.5 rounded-full ${colour}`} style={{ width: `${pct}%` }} />
      </div>
      {hint && <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">{hint}</p>}
    </div>
  );
}

const LIMIT_LABELS: Record<string, string> = {
  max_lots_per_order: "Most lots in one order",
  max_open_positions: "Most trades open at once",
  max_orders_per_minute: "Most orders per minute",
  daily_loss_limit: "Daily loss limit (₹)",
  duplicate_window_sec: "Block repeat orders within (seconds)",
  trading_start: "New trades from",
  trading_end: "New trades until",
  square_off_time: "Intraday trades closed at",
  expiry_no_entry_after: "No new trades on expiry day after",
  option_max_spread_pct: "Skip options with buy/sell gap above (%)",
  option_min_volume: "Skip options traded less than (units today)",
  freeze_limits: "Exchange size limits (orders above are split)",
};

const MARGIN_LABELS: Record<string, string> = {
  source: "Where this comes from",
  capital: "Your capital",
  used: "Blocked for open trades",
  available: "Free to use",
  error: "Problem",
};

export function RiskPage() {
  const q = useQuery({ queryKey: ["risk"], queryFn: api.risk, refetchInterval: 3000 });
  const r = q.data;
  if (!r) return <Empty icon="shield">{q.isLoading ? "Loading…" : "Not available right now."}</Empty>;
  const lim = r.limits;
  return (
    <div className="space-y-5">
      <PageTitle
        title="Safety limits"
        subtitle="Every order is checked against these rules before it is sent. Orders that close a trade are always allowed."
      />
      {r.kill_switch.tripped ? (
        <Notice tone="danger"><strong>Emergency stop is on:</strong> {r.kill_switch.reason}.</Notice>
      ) : (
        <Notice tone="good">Emergency stop is ready. It fires by itself if the daily loss limit is reached.</Notice>
      )}
      <div className="grid gap-5 lg:grid-cols-2">
        <Card title="Today so far" icon="shield">
          <div className="space-y-5">
            <Meter label="Loss used today" used={r.usage.daily_loss_used} max={Number(lim.daily_loss_limit)} money hint="When this bar is full, everything stops and all trades are closed." />
            <Meter label="Trades open now" used={r.usage.open_positions} max={Number(lim.max_open_positions)} />
            <Meter label="Orders in the last minute" used={r.usage.orders_last_minute} max={Number(lim.max_orders_per_minute)} hint="Keeps robots from flooding the broker." />
            <p className="text-sm text-slate-600 dark:text-slate-300">
              Orders blocked by these rules today: <strong>{r.usage.rejections_today}</strong>
            </p>
          </div>
        </Card>
        <Card title={<Term k="margin">Money in your account</Term>} icon="wallet">
          {r.margin ? (
            <dl className="divide-y divide-slate-100 text-sm dark:divide-slate-800">
              {Object.entries(r.margin).map(([k, v]) => (
                <div key={k} className="flex justify-between py-2">
                  <dt className="text-slate-500 dark:text-slate-400">{MARGIN_LABELS[k] ?? k}</dt>
                  <dd className="font-medium">{typeof v === "number" ? inr(v) : show(v)}</dd>
                </div>
              ))}
            </dl>
          ) : (
            <Empty>Margin checks are turned off.</Empty>
          )}
        </Card>
      </div>
      <Card title="All rules" subtitle="Change the daily ones on the Today's plan page." icon="plan">
        <dl className="grid gap-x-8 sm:grid-cols-2">
          {Object.entries(lim).map(([k, v]) => (
            <div key={k} className="flex justify-between gap-4 border-b border-slate-100 py-2 text-sm dark:border-slate-800">
              <dt className="text-slate-500 dark:text-slate-400">{LIMIT_LABELS[k] ?? k}</dt>
              <dd className="text-right font-medium">
                {k === "freeze_limits" && typeof v === "object" && v !== null && Object.keys(v).length === 0 ? "—" : show(v)}
              </dd>
            </div>
          ))}
        </dl>
      </Card>
    </div>
  );
}
