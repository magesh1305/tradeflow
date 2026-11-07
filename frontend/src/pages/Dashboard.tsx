import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { Order, Position, StrategySummary } from "../api/types";
import { useLive } from "../app/live";
import { useToast } from "../app/toasts";
import { DataTable } from "../components/DataTable";
import { Dialog } from "../components/Dialog";
import { Button, Card, Pill, Stat } from "../components/ui";
import { inr, istDateTime, istTime, num, pnlClass, qty } from "../lib/format";

function KiteSession() {
  const auth = useQuery({ queryKey: ["auth"], queryFn: api.authStatus, refetchInterval: 60_000 });
  const a = auth.data;
  return (
    <Card title="Kite session">
      {!a ? (
        <p className="text-sm text-slate-500">Checking…</p>
      ) : !a.configured ? (
        <p className="text-sm text-slate-500">Kite is not configured (simulated feed).</p>
      ) : a.logged_in ? (
        <p className="text-sm">
          Logged in as <strong>{a.user_id}</strong>
          {a.expires_at && <> · expires {istDateTime(a.expires_at)}</>}
        </p>
      ) : (
        <div className="flex flex-wrap items-center gap-3">
          <span className="text-sm text-amber-700 dark:text-amber-300">Not logged in today.</span>
          <a className="rounded-md bg-sky-600 px-4 py-2 text-sm font-semibold text-white hover:bg-sky-700" href={api.kiteLoginUrl()}>
            Log in to Kite
          </a>
        </div>
      )}
    </Card>
  );
}

function SquareOff() {
  const [open, setOpen] = useState(false);
  const toast = useToast();
  const m = useMutation({
    mutationFn: api.squareOff,
    onSuccess: () => {
      toast.push("Square-off sent for all positions", "success");
      setOpen(false);
    },
  });
  return (
    <>
      <Button onClick={() => { setOpen(true); }}>Square off all</Button>
      <Dialog open={open} title="Square off all positions?" onClose={() => { setOpen(false); }}>
        <p className="mb-4 text-sm text-slate-600 dark:text-slate-300">
          Cancels open orders and exits every position (MIS and NRML) at market-protected prices.
          Strategies keep running.
        </p>
        <div className="flex gap-2">
          <Button className="flex-1" onClick={() => { setOpen(false); }}>Cancel</Button>
          <Button variant="danger" className="flex-1" disabled={m.isPending} onClick={() => m.mutate()}>
            Square off
          </Button>
        </div>
      </Dialog>
    </>
  );
}

export function Dashboard() {
  const { status } = useLive();
  if (!status) return <p className="text-sm text-slate-500">Waiting for the engine…</p>;
  const p = status.pnl;
  return (
    <div className="space-y-4">
      {status.blocked_reason && (
        <div role="alert" className="rounded-lg border border-red-300 bg-red-50 p-3 text-sm text-red-900 dark:border-red-800 dark:bg-red-950 dark:text-red-100">
          <strong>Engine blocked:</strong> {status.blocked_reason}. Strategies cannot start until this
          is resolved (POST /recovery/resolve).
        </div>
      )}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
        <Stat label="Net P&L" value={p.net} />
        <Stat label="Gross P&L" value={p.gross} />
        <Stat label="Realized" value={p.realized} />
        <Stat label="Unrealized" value={p.unrealized} />
        <Stat label="Charges" value={-p.charges} />
      </div>
      <div className="grid gap-4 md:grid-cols-2">
        <KiteSession />
        <Card title="Engine">
          <div className="flex flex-wrap gap-x-6 gap-y-1 text-sm">
            <span>Feed: {status.feed.source} · {status.feed.connected ? "connected" : status.feed.detail}</span>
            <span>Market time: {istTime(status.time)}</span>
            <span>Leg groups open: {status.open_leg_groups}</span>
            {status.journal.failures > 0 && (
              <span className="text-amber-700 dark:text-amber-300">
                Journal: {status.journal.backlog} buffered, last error {status.journal.last_error}
              </span>
            )}
          </div>
        </Card>
      </div>
      <Card title={`Open positions (${status.positions.length})`} actions={<SquareOff />}>
        <DataTable<Position>
          rows={status.positions}
          rowKey={(r) => `${r.strategy_id}:${r.instrument_token}:${r.product}`}
          empty="Flat."
          columns={[
            { key: "sym", header: "Symbol", render: (r) => r.tradingsymbol },
            { key: "str", header: "Strategy", render: (r) => r.strategy_id },
            { key: "prod", header: "Product", render: (r) => r.product },
            { key: "qty", header: "Qty", align: "right", render: (r) => <span className={pnlClass(r.quantity)}>{qty(r.quantity)}</span> },
            { key: "avg", header: "Avg", align: "right", render: (r) => num(r.average_price) },
            { key: "ltp", header: "LTP", align: "right", render: (r) => num(r.last_price) },
            { key: "stop", header: "Exch. stop", align: "right", render: (r) => num(r.protected_stop) },
            { key: "pnl", header: "Net P&L", align: "right", render: (r) => <span className={pnlClass(r.pnl.net)}>{inr(r.pnl.net)}</span> },
          ]}
        />
      </Card>
      <div className="grid gap-4 lg:grid-cols-2">
        <Card title={`Open orders (${status.open_orders.length})`}>
          <DataTable<Order>
            rows={status.open_orders}
            rowKey={(r) => r.client_order_id}
            empty="No open orders."
            columns={[
              { key: "t", header: "Time", render: (r) => istTime(r.created_at) },
              { key: "sym", header: "Symbol", render: (r) => r.tradingsymbol },
              { key: "side", header: "Side", render: (r) => r.side },
              { key: "q", header: "Qty", align: "right", render: (r) => `${r.filled_quantity}/${r.quantity}` },
              { key: "type", header: "Type", render: (r) => (r.protective ? "SL (stop)" : r.order_type) },
              { key: "px", header: "Price", align: "right", render: (r) => num(r.price ?? r.trigger_price) },
              { key: "st", header: "Status", render: (r) => <Pill value={r.status} /> },
            ]}
          />
        </Card>
        <Card title="Strategies">
          <DataTable<StrategySummary>
            rows={status.strategies}
            rowKey={(r) => r.id}
            columns={[
              { key: "id", header: "Strategy", render: (r) => <Link className="text-sky-700 hover:underline dark:text-sky-400" to={`/strategies/${encodeURIComponent(r.id)}`}>{r.id}</Link> },
              { key: "st", header: "State", render: (r) => <Pill value={r.state} /> },
              { key: "pos", header: "Positions", align: "right", render: (r) => r.open_positions },
              { key: "pnl", header: "Net P&L", align: "right", render: (r) => <span className={pnlClass(r.pnl.net)}>{inr(r.pnl.net)}</span> },
            ]}
          />
        </Card>
      </div>
    </div>
  );
}
