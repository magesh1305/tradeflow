// Orders, trades (fills), daily P&L and risk.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import type { DailyPnl, Fill, Mode, Order, ReportFilter } from "../api/types";
import { useLive } from "../app/live";
import { PnlBars } from "../components/Charts";
import { DataTable } from "../components/DataTable";
import { Card, Pill, Stat, inputClass } from "../components/ui";
import { inr, istDateTime, num, pnlClass, todayIst } from "../lib/format";

function Filters({ value, onChange }: { value: ReportFilter; onChange: (f: ReportFilter) => void }) {
  return (
    <div className="mb-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
      <label className="text-xs">From
        <input type="date" className={inputClass} value={value.from ?? ""} onChange={(e) => { onChange({ ...value, from: e.target.value }); }} />
      </label>
      <label className="text-xs">To
        <input type="date" className={inputClass} value={value.to ?? ""} onChange={(e) => { onChange({ ...value, to: e.target.value }); }} />
      </label>
      <label className="text-xs">Strategy
        <input className={inputClass} placeholder="all" value={value.strategy ?? ""} onChange={(e) => { onChange({ ...value, strategy: e.target.value || undefined }); }} />
      </label>
      <label className="text-xs">Mode
        <select className={inputClass} value={value.mode ?? ""} onChange={(e) => { onChange({ ...value, mode: (e.target.value || undefined) as Mode | undefined }); }}>
          <option value="">current</option>
          <option value="paper">paper</option>
          <option value="live">live</option>
        </select>
      </label>
    </div>
  );
}

function useFilter(): [ReportFilter, (f: ReportFilter) => void] {
  const today = todayIst();
  return useState<ReportFilter>({ from: today, to: today });
}

export function OrdersPage() {
  const [f, setF] = useFilter();
  const q = useQuery({ queryKey: ["orders", f], queryFn: () => api.orders(f), refetchInterval: 5000 });
  return (
    <Card title="Orders">
      <Filters value={f} onChange={setF} />
      <DataTable<Order>
        rows={q.data ?? []}
        rowKey={(r) => r.client_order_id}
        csvName={`orders_${f.from ?? ""}_${f.to ?? ""}.csv`}
        empty={q.isLoading ? "Loading…" : "No orders."}
        columns={[
          { key: "t", header: "Time", render: (r) => istDateTime(r.created_at), csv: (r) => r.created_at },
          { key: "s", header: "Strategy", render: (r) => r.strategy_id, csv: (r) => r.strategy_id },
          { key: "sym", header: "Symbol", render: (r) => r.tradingsymbol, csv: (r) => r.tradingsymbol },
          { key: "side", header: "Side", render: (r) => r.side, csv: (r) => r.side },
          { key: "q", header: "Qty", align: "right", render: (r) => `${r.filled_quantity}/${r.quantity}`, csv: (r) => r.quantity },
          { key: "fq", header: "Filled", csvOnly: true, csv: (r) => r.filled_quantity, render: () => null },
          { key: "type", header: "Type", render: (r) => r.order_type, csv: (r) => r.order_type },
          { key: "prod", header: "Product", render: (r) => r.product, csv: (r) => r.product },
          { key: "px", header: "Price", align: "right", render: (r) => num(r.price), csv: (r) => r.price },
          { key: "avg", header: "Avg", align: "right", render: (r) => num(r.average_price), csv: (r) => r.average_price },
          { key: "st", header: "Status", render: (r) => <Pill value={r.status} />, csv: (r) => r.status },
          { key: "msg", header: "Message", render: (r) => <span className="text-xs text-slate-500">{r.status_message || r.reason}</span>, csv: (r) => r.status_message || r.reason },
          { key: "id", header: "Order id", render: (r) => <span className="font-mono text-xs">{r.broker_order_id ?? r.client_order_id}</span>, csv: (r) => r.broker_order_id },
        ]}
      />
    </Card>
  );
}

export function TradesPage() {
  const [f, setF] = useFilter();
  const q = useQuery({ queryKey: ["fills", f], queryFn: () => api.fills(f), refetchInterval: 5000 });
  const rows = q.data ?? [];
  const charges = rows.reduce((s, r) => s + r.charges, 0);
  return (
    <Card title={`Trades · charges ${inr(charges)}`}>
      <Filters value={f} onChange={setF} />
      <DataTable<Fill>
        rows={rows}
        rowKey={(r) => r.fill_id}
        csvName={`trades_${f.from ?? ""}_${f.to ?? ""}.csv`}
        empty={q.isLoading ? "Loading…" : "No trades."}
        columns={[
          { key: "t", header: "Time", render: (r) => istDateTime(r.ts), csv: (r) => r.ts },
          { key: "s", header: "Strategy", render: (r) => r.strategy_id, csv: (r) => r.strategy_id },
          { key: "sym", header: "Symbol", render: (r) => r.tradingsymbol, csv: (r) => r.tradingsymbol },
          { key: "side", header: "Side", render: (r) => r.side, csv: (r) => r.side },
          { key: "q", header: "Qty", align: "right", render: (r) => r.quantity, csv: (r) => r.quantity },
          { key: "px", header: "Price", align: "right", render: (r) => num(r.price), csv: (r) => r.price },
          { key: "prod", header: "Product", render: (r) => r.product, csv: (r) => r.product },
          { key: "ch", header: "Charges", align: "right", render: (r) => num(r.charges), csv: (r) => r.charges },
          { key: "mode", header: "Mode", render: (r) => r.mode, csv: (r) => r.mode },
        ]}
      />
    </Card>
  );
}

export function DailyPnlPage() {
  const q = useQuery({ queryKey: ["pnl"], queryFn: () => api.dailyPnl(), refetchInterval: 30_000 });
  const account = (q.data ?? []).filter((r) => r.strategy_id === "__account__");
  const total = account.reduce((s, r) => s + r.net_pnl, 0);
  const wins = account.filter((r) => r.net_pnl > 0).length;
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Net (period)" value={total} />
        <Stat label="Charges" value={-account.reduce((s, r) => s + r.charges, 0)} />
        <Stat label="Days" value={account.length} money={false} tone={false} />
        <Stat label="Green days" value={wins} money={false} tone={false} />
      </div>
      <Card title="Daily net P&L">
        {account.length > 0 ? (
          <PnlBars points={account.map((r) => ({ time: r.trading_date, value: r.net_pnl }))} />
        ) : (
          <p className="text-sm text-slate-500">No days recorded yet.</p>
        )}
      </Card>
      <Card title="By day">
        <DataTable<DailyPnl>
          rows={[...account].reverse()}
          rowKey={(r) => r.trading_date}
          csvName="daily_pnl.csv"
          columns={[
            { key: "d", header: "Date", render: (r) => <>{r.trading_date}{r.live && <span className="ml-1 text-xs text-sky-600">(live)</span>}</>, csv: (r) => r.trading_date },
            { key: "g", header: "Gross", align: "right", render: (r) => inr(r.gross_pnl), csv: (r) => r.gross_pnl },
            { key: "c", header: "Charges", align: "right", render: (r) => inr(r.charges), csv: (r) => r.charges },
            { key: "n", header: "Net", align: "right", render: (r) => <span className={pnlClass(r.net_pnl)}>{inr(r.net_pnl)}</span>, csv: (r) => r.net_pnl },
            { key: "t", header: "Fills", align: "right", render: (r) => r.trades, csv: (r) => r.trades },
          ]}
        />
      </Card>
    </div>
  );
}

function show(v: unknown): string {
  if (v === null || v === undefined) return "�";
  return typeof v === "string" || typeof v === "number" || typeof v === "boolean"
    ? String(v)
    : JSON.stringify(v);
}

function Meter({ label, used, max, unit = "" }: { label: string; used: number; max: number; unit?: string }) {
  const pct = max > 0 ? Math.min((used / max) * 100, 100) : 0;
  const colour = pct >= 90 ? "bg-rose-500" : pct >= 70 ? "bg-amber-500" : "bg-emerald-500";
  return (
    <div>
      <div className="flex justify-between text-sm"><span>{label}</span><span>{num(used)}{unit} / {num(max)}{unit}</span></div>
      <div className="mt-1 h-2 rounded bg-slate-200 dark:bg-slate-800">
        <div className={`h-2 rounded ${colour}`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

export function RiskPage() {
  const q = useQuery({ queryKey: ["risk"], queryFn: api.risk, refetchInterval: 3000 });
  const { status } = useLive();
  const r = q.data;
  if (!r) return <p className="text-sm text-slate-500">{q.isLoading ? "Loading…" : "Unavailable."}</p>;
  const lim = r.limits;
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Card title="Today's usage">
        <div className="space-y-3">
          <Meter label="Daily loss used (INR)" used={r.usage.daily_loss_used} max={Number(lim.daily_loss_limit)} />
          <Meter label="Open positions" used={r.usage.open_positions} max={Number(lim.max_open_positions)} />
          <Meter label="Orders in the last minute" used={r.usage.orders_last_minute} max={Number(lim.max_orders_per_minute)} />
          <div className="text-sm">Risk rejections today: {r.usage.rejections_today}</div>
          <div className="text-sm">Kill switch: {r.kill_switch.tripped ? `TRIPPED (${r.kill_switch.reason})` : "armed"}</div>
          {status?.mode && <div className="text-sm">Mode: {status.mode}</div>}
        </div>
      </Card>
      <Card title="Margin">
        {r.margin ? (
          <dl className="grid grid-cols-2 gap-1 text-sm">
            {Object.entries(r.margin).map(([k, v]) => (
              <div key={k} className="contents"><dt className="text-slate-500">{k}</dt><dd className="text-right">{typeof v === "number" ? inr(v) : show(v)}</dd></div>
            ))}
          </dl>
        ) : (
          <p className="text-sm text-slate-500">Margin checks disabled.</p>
        )}
      </Card>
      <Card title="Limits" className="lg:col-span-2">
        <dl className="grid gap-x-6 gap-y-1 text-sm sm:grid-cols-2">
          {Object.entries(lim).map(([k, v]) => (
            <div key={k} className="flex justify-between border-b border-slate-100 py-1 dark:border-slate-800">
              <dt className="text-slate-500">{k}</dt>
              <dd>{show(v)}</dd>
            </div>
          ))}
        </dl>
      </Card>
    </div>
  );
}
