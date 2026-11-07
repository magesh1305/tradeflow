// Robots: what each one does in plain words, start/stop, and its settings.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api/client";
import type { StrategySummary } from "../api/types";
import { useToast } from "../app/toasts";
import { Dialog } from "../components/Dialog";
import { Icon } from "../components/Icon";
import { ParamEditor } from "../components/ParamEditor";
import { Term } from "../components/Term";
import { Button, Card, Empty, Money, Notice, PageTitle, Pill, RiskBadge } from "../components/ui";
import { num } from "../lib/format";
import { directionWord, lotsText, productWord } from "../lib/words";

function RobotCard({ s }: { s: StrategySummary }) {
  return (
    <Link
      to={encodeURIComponent(s.id)}
      className="group flex flex-col rounded-2xl border border-slate-200/80 bg-white p-5 shadow-sm transition hover:-translate-y-0.5 hover:shadow-md dark:border-slate-800 dark:bg-slate-900"
    >
      <div className="flex items-start gap-3">
        <span className="grid h-12 w-12 shrink-0 place-items-center rounded-2xl bg-gradient-to-br from-brand-500 to-indigo-600 text-white shadow">
          <Icon name="robot" className="h-6 w-6" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-lg font-semibold text-slate-900 dark:text-white">{s.title || s.id}</h2>
            <Pill value={s.state} />
          </div>
          <div className="mt-1"><RiskBadge level={s.risk_level} /></div>
        </div>
      </div>
      <p className="mt-3 flex-1 text-sm text-slate-600 dark:text-slate-300">{s.summary || s.strategy}</p>
      {s.error && <p role="alert" className="mt-2 text-sm text-red-600">Problem: {s.error}</p>}
      <div className="mt-4 flex items-center justify-between border-t border-slate-100 pt-3 text-sm dark:border-slate-800">
        <span className="text-slate-500 dark:text-slate-400">
          Today: <Money value={s.pnl.net} className="font-semibold" /> · {s.open_positions} open trade{s.open_positions === 1 ? "" : "s"}
        </span>
        <span className="inline-flex items-center gap-1 font-medium text-brand-600 group-hover:underline dark:text-brand-200">
          Details <Icon name="chevron" className="h-4 w-4" />
        </span>
      </div>
    </Link>
  );
}

export function Strategies() {
  const q = useQuery({ queryKey: ["strategies"], queryFn: api.strategies, refetchInterval: 3000 });
  const rows = q.data ?? [];
  return (
    <div className="space-y-5">
      <PageTitle
        title="Robots"
        subtitle={
          <>
            A <Term k="robot">robot</Term> follows fixed rules to decide when to buy and sell. It never gets tired or emotional,
            and it always stays inside today&apos;s plan.
          </>
        }
      />
      {rows.length === 0 ? (
        <Card><Empty icon="robot">{q.isLoading ? "Loading robots…" : "No robots are set up yet."}</Empty></Card>
      ) : (
        <div className="grid gap-4 md:grid-cols-2">
          {rows.map((s) => <RobotCard key={s.id} s={s} />)}
        </div>
      )}
      <Notice tone="info">
        Robots turn on when you submit <Link to="/plan" className="font-semibold underline">today&apos;s plan</Link>. You can stop any robot
        here at any time.
      </Notice>
    </div>
  );
}

export function StrategyPage() {
  const { id = "" } = useParams();
  const qc = useQueryClient();
  const toast = useToast();
  const [askStop, setAskStop] = useState(false);
  const q = useQuery({ queryKey: ["strategy", id], queryFn: () => api.strategy(id), refetchInterval: 3000 });
  const refresh = () => qc.invalidateQueries({ queryKey: ["strategy", id] });
  const s = q.data;
  const name = s?.title || id;
  const start = useMutation({
    mutationFn: () => api.startStrategy(id),
    onSuccess: () => { toast.push(`${name} started`, "success"); void refresh(); },
  });
  const stop = useMutation({
    mutationFn: (exit: boolean) => api.stopStrategy(id, exit),
    onSuccess: (_d, exit) => {
      toast.push(exit ? `${name} stopped and is closing its trades` : `${name} stopped (its trades stay open)`, "success");
      setAskStop(false);
      void refresh();
    },
  });
  const save = useMutation({
    mutationFn: (params: Record<string, unknown>) => api.setParams(id, params),
    onSuccess: () => { toast.push("Settings saved. They are kept for the next days too.", "success"); void refresh(); },
  });

  if (!s) return <Empty icon="robot">{q.isLoading ? "Loading…" : "This robot doesn't exist."}</Empty>;
  const running = s.state === "RUNNING";
  return (
    <div className="space-y-5">
      <Link to="/robots" className="inline-flex items-center gap-1 text-sm text-slate-500 hover:text-slate-800 dark:hover:text-slate-200">
        <Icon name="chevron" className="h-4 w-4 rotate-180" /> All robots
      </Link>

      <Card>
        <div className="flex flex-wrap items-start gap-4">
          <span className="grid h-14 w-14 shrink-0 place-items-center rounded-2xl bg-gradient-to-br from-brand-500 to-indigo-600 text-white shadow">
            <Icon name="robot" className="h-7 w-7" />
          </span>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="text-2xl font-bold text-slate-900 dark:text-white">{name}</h1>
              <Pill value={s.state} />
              <RiskBadge level={s.risk_level} />
            </div>
            <p className="mt-1 text-slate-600 dark:text-slate-300">{s.summary}</p>
          </div>
          {running ? (
            <Button variant="danger" onClick={() => { setAskStop(true); }}>
              <Icon name="pause" className="h-4 w-4" /> Stop robot
            </Button>
          ) : (
            <Button variant="success" disabled={start.isPending} onClick={() => start.mutate()}>
              <Icon name="play" className="h-4 w-4" /> Start robot
            </Button>
          )}
        </div>
        {s.error && <div className="mt-4"><Notice tone="danger">Problem: {s.error}</Notice></div>}
        <dl className="mt-5 grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Fact label="Today after charges"><Money value={s.pnl.net} /></Fact>
          <Fact label="Before charges"><Money value={s.pnl.gross} /></Fact>
          <Fact label="Charges paid">₹{num(s.pnl.charges)}</Fact>
          <Fact label="Open trades">{s.open_positions}</Fact>
        </dl>
      </Card>

      <div className="grid gap-5 lg:grid-cols-2">
        <Card title="How it works" icon="info">
          {s.how_it_works.length > 0 ? (
            <ol className="space-y-3">
              {s.how_it_works.map((step, i) => (
                <li key={step} className="flex gap-3 text-sm text-slate-700 dark:text-slate-300">
                  <span className="grid h-6 w-6 shrink-0 place-items-center rounded-full bg-brand-50 text-xs font-bold text-brand-700 dark:bg-brand-500/15 dark:text-brand-200">
                    {i + 1}
                  </span>
                  {step}
                </li>
              ))}
            </ol>
          ) : (
            <p className="text-sm text-slate-500">{s.strategy}</p>
          )}
        </Card>
        <Card title="What can go wrong" icon="alert">
          <p className="text-sm text-slate-700 dark:text-slate-300">{s.risk_note || "Every trading robot can lose money."}</p>
          <p className="mt-3 text-sm text-slate-500 dark:text-slate-400">
            Your <Term k="loss_limit">daily loss limit</Term> and <Term k="stop_loss">stop losses</Term> limit how much it can lose in one day.
          </p>
        </Card>
      </div>

      <Card title="Settings" subtitle={running ? "Stop the robot to change these." : "Changes are kept for the next days too."} icon="plan">
        <ParamEditor
          key={JSON.stringify(s.params)}
          schema={s.params_schema}
          params={s.params}
          disabled={running}
          saving={save.isPending}
          onSave={(p) => save.mutate(p)}
        />
      </Card>

      <Card title="Its open trades" icon="wallet">
        {s.positions.length === 0 ? (
          <Empty icon="check">No trades today.</Empty>
        ) : (
          <ul className="divide-y divide-slate-100 dark:divide-slate-800">
            {s.positions.map((p) => (
              <li key={`${p.instrument_token}:${p.product}`} className="flex flex-wrap items-center justify-between gap-2 py-3 text-sm">
                <div>
                  <div className="font-medium text-slate-900 dark:text-white">{p.label || p.tradingsymbol}</div>
                  <div className="text-xs text-slate-500">
                    {p.quantity === 0 ? "Closed" : `${directionWord(p.quantity)} ${lotsText(p.quantity, p.lot_size)}`} · {productWord(p.product)} · entry ₹{num(p.average_price)} · now ₹{num(p.last_price)}
                  </div>
                </div>
                <Money value={p.pnl.net} className="font-semibold" />
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Dialog open={askStop} title={`Stop ${name}?`} onClose={() => { setAskStop(false); }}>
        <p className="mb-4 text-sm text-slate-600 dark:text-slate-300">
          The robot stops and its waiting orders are cancelled. Should it also close the trades it is holding?
          If you keep them, their stop losses stay in place.
        </p>
        <div className="flex flex-col gap-2 sm:flex-row">
          <Button className="flex-1" disabled={stop.isPending} onClick={() => stop.mutate(false)}>Stop, keep trades</Button>
          <Button variant="danger" className="flex-1" disabled={stop.isPending} onClick={() => stop.mutate(true)}>Stop and close trades</Button>
        </div>
      </Dialog>
    </div>
  );
}

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="rounded-xl bg-slate-50 p-3 dark:bg-slate-800/50">
      <dt className="text-xs text-slate-500 dark:text-slate-400">{label}</dt>
      <dd className="mt-0.5 text-lg font-semibold text-slate-900 dark:text-white">{children}</dd>
    </div>
  );
}
