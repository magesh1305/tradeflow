import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api/client";
import type { Position, StrategySummary } from "../api/types";
import { useToast } from "../app/toasts";
import { DataTable } from "../components/DataTable";
import { Dialog } from "../components/Dialog";
import { ParamEditor } from "../components/ParamEditor";
import { Button, Card, Pill } from "../components/ui";
import { inr, num, pnlClass } from "../lib/format";

export function Strategies() {
  const q = useQuery({ queryKey: ["strategies"], queryFn: api.strategies, refetchInterval: 3000 });
  return (
    <Card title="Strategies">
      <DataTable<StrategySummary>
        rows={q.data ?? []}
        rowKey={(r) => r.id}
        empty={q.isLoading ? "Loading…" : "No strategies configured."}
        columns={[
          { key: "id", header: "Id", render: (r) => <Link className="text-sky-700 hover:underline dark:text-sky-400" to={encodeURIComponent(r.id)}>{r.id}</Link> },
          { key: "s", header: "Strategy", render: (r) => r.strategy },
          { key: "st", header: "State", render: (r) => <Pill value={r.state} /> },
          { key: "err", header: "Error", render: (r) => <span className="text-rose-600">{r.error}</span> },
          { key: "pos", header: "Positions", align: "right", render: (r) => r.open_positions },
          { key: "ord", header: "Orders", align: "right", render: (r) => r.open_orders },
          { key: "pnl", header: "Net P&L", align: "right", render: (r) => <span className={pnlClass(r.pnl.net)}>{inr(r.pnl.net)}</span> },
        ]}
      />
    </Card>
  );
}

export function StrategyPage() {
  const { id = "" } = useParams();
  const qc = useQueryClient();
  const toast = useToast();
  const [askStop, setAskStop] = useState(false);
  const q = useQuery({ queryKey: ["strategy", id], queryFn: () => api.strategy(id), refetchInterval: 3000 });
  const refresh = () => qc.invalidateQueries({ queryKey: ["strategy", id] });
  const start = useMutation({
    mutationFn: () => api.startStrategy(id),
    onSuccess: () => { toast.push(`${id} started`, "success"); void refresh(); },
  });
  const stop = useMutation({
    mutationFn: (exit: boolean) => api.stopStrategy(id, exit),
    onSuccess: (_d, exit) => {
      toast.push(exit ? `${id} stopped and exiting positions` : `${id} stopped (positions kept)`, "success");
      setAskStop(false);
      void refresh();
    },
  });
  const save = useMutation({
    mutationFn: (params: Record<string, unknown>) => api.setParams(id, params),
    onSuccess: () => { toast.push("Params saved (kept across restarts)", "success"); void refresh(); },
  });

  const s = q.data;
  if (!s) return <p className="text-sm text-slate-500">{q.isLoading ? "Loading…" : "Not found."}</p>;
  const running = s.state === "RUNNING";
  return (
    <div className="space-y-4">
      <Card
        title={<span className="flex items-center gap-2 normal-case">{s.id} <Pill value={s.state} /></span>}
        actions={
          running ? (
            <Button variant="danger" onClick={() => { setAskStop(true); }}>Stop</Button>
          ) : (
            <Button variant="primary" disabled={start.isPending} onClick={() => start.mutate()}>Start</Button>
          )
        }
      >
        <div className="flex flex-wrap gap-x-6 gap-y-1 text-sm">
          <span>Type: {s.strategy}</span>
          <span>Net P&L: <span className={pnlClass(s.pnl.net)}>{inr(s.pnl.net)}</span></span>
          <span>Gross: {inr(s.pnl.gross)}</span>
          <span>Charges: {inr(s.pnl.charges)}</span>
          <span>Subscriptions: {s.subscribed_tokens}</span>
        </div>
        {s.error && <p role="alert" className="mt-2 text-sm text-rose-600">Error: {s.error}</p>}
      </Card>
      <Card title="Parameters">
        <ParamEditor
          key={JSON.stringify(s.params)}
          schema={s.params_schema}
          params={s.params}
          disabled={running}
          saving={save.isPending}
          onSave={(p) => save.mutate(p)}
        />
      </Card>
      <Card title="Positions">
        <DataTable<Position>
          rows={s.positions}
          rowKey={(r) => `${r.instrument_token}:${r.product}`}
          empty="No positions today."
          columns={[
            { key: "sym", header: "Symbol", render: (r) => r.tradingsymbol },
            { key: "prod", header: "Product", render: (r) => r.product },
            { key: "q", header: "Qty", align: "right", render: (r) => r.quantity },
            { key: "avg", header: "Avg", align: "right", render: (r) => num(r.average_price) },
            { key: "ltp", header: "LTP", align: "right", render: (r) => num(r.last_price) },
            { key: "pnl", header: "Net", align: "right", render: (r) => <span className={pnlClass(r.pnl.net)}>{inr(r.pnl.net)}</span> },
          ]}
        />
      </Card>
      <Dialog open={askStop} title={`Stop ${s.id}?`} onClose={() => { setAskStop(false); }}>
        <p className="mb-4 text-sm text-slate-600 dark:text-slate-300">
          Open orders are cancelled. Do you also want to exit this strategy&apos;s positions?
          Exchange-side stops stay in place if you keep them.
        </p>
        <div className="flex flex-col gap-2 sm:flex-row">
          <Button className="flex-1" disabled={stop.isPending} onClick={() => stop.mutate(false)}>Stop, keep positions</Button>
          <Button variant="danger" className="flex-1" disabled={stop.isPending} onClick={() => stop.mutate(true)}>Stop and exit positions</Button>
        </div>
      </Dialog>
    </div>
  );
}
