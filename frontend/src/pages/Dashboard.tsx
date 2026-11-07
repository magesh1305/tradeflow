// Home: is everything ready, how much did we make today, and what are the robots doing.

import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { Position, Status } from "../api/types";
import { useLive } from "../app/live";
import { useToast } from "../app/toasts";
import { Dialog } from "../components/Dialog";
import { Icon } from "../components/Icon";
import { ResolveBlock } from "../components/ResolveBlock";
import { Term } from "../components/Term";
import { Button, Card, Empty, Money, Notice, Pill, RiskBadge } from "../components/ui";
import { istDateTime, istTime, num, todayIst } from "../lib/format";
import { directionWord, lotsText, productWord, robotNames, sideVerb } from "../lib/words";

function Step({ done, title, children, action }: {
  done: boolean;
  title: string;
  children?: React.ReactNode;
  action?: React.ReactNode;
}) {
  return (
    <li className="flex items-start gap-3 py-3">
      <span
        className={`mt-0.5 grid h-7 w-7 shrink-0 place-items-center rounded-full ${
          done ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-500/15 dark:text-emerald-300" : "bg-amber-100 text-amber-800 dark:bg-amber-500/15 dark:text-amber-200"
        }`}
      >
        <Icon name={done ? "check" : "clock"} className="h-4 w-4" />
      </span>
      <div className="min-w-0 flex-1">
        <div className="font-medium text-slate-900 dark:text-white">
          {title} <span className="sr-only">{done ? "(done)" : "(to do)"}</span>
        </div>
        {children && <div className="mt-0.5 text-sm text-slate-500 dark:text-slate-400">{children}</div>}
      </div>
      {action}
    </li>
  );
}

function Checklist({ status }: { status: Status }) {
  const auth = useQuery({ queryKey: ["auth"], queryFn: api.authStatus, refetchInterval: 60_000 });
  const a = auth.data;
  const kiteOk = a ? !a.configured || a.logged_in : false;
  const running = status.strategies.filter((s) => s.state === "RUNNING").length;
  return (
    <Card title="Get ready for today" subtitle="Three steps every morning before 9:15 AM." icon="sun">
      <ol className="divide-y divide-slate-100 dark:divide-slate-800">
        <Step
          done={kiteOk}
          title="1. Connect to Zerodha"
          action={
            a?.configured && !a.logged_in ? (
              <a className="rounded-xl bg-brand-500 px-3 py-2 text-sm font-semibold text-white hover:bg-brand-600" href={api.kiteLoginUrl()}>
                Log in to Kite
              </a>
            ) : undefined
          }
        >
          {!a
            ? "Checking…"
            : !a.configured
              ? "Not needed now: practice prices are simulated."
              : a.logged_in
                ? `Connected as ${a.user_id ?? ""}${a.expires_at ? ` until ${istDateTime(a.expires_at)}` : ""}.`
                : "Zerodha asks you to log in once every morning."}
        </Step>
        <Step
          done={status.plan_submitted}
          title="2. Submit today's plan"
          action={
            !status.plan_submitted ? (
              <Link to="/plan" className="rounded-xl bg-brand-500 px-3 py-2 text-sm font-semibold text-white hover:bg-brand-600">
                Open plan
              </Link>
            ) : undefined
          }
        >
          {status.plan_submitted ? "Your limits for today are set." : "Set how much you may lose and which robots run. Robots wait until you do."}
        </Step>
        <Step done={running > 0} title="3. Robots are working">
          {running > 0
            ? `${running} of ${status.strategies.length} robot${status.strategies.length === 1 ? "" : "s"} running. They trade on their own.`
            : "No robot is running yet. They start when you submit the plan."}
        </Step>
      </ol>
    </Card>
  );
}

function TodayMoney({ status }: { status: Status }) {
  const p = status.pnl;
  return (
    <section className="overflow-hidden rounded-2xl bg-gradient-to-br from-slate-900 to-slate-800 p-5 text-white shadow-lg sm:p-6 dark:from-slate-900 dark:to-slate-950 dark:ring-1 dark:ring-slate-800">
      <div className="flex items-center gap-2 text-sm text-slate-300">
        <Icon name="wallet" className="h-4 w-4" /> Today&apos;s result, after charges
      </div>
      <div className="mt-2 text-4xl font-bold tracking-tight sm:text-5xl">
        <span className={p.net > 0 ? "text-emerald-400" : p.net < 0 ? "text-red-400" : "text-white"}>
          {p.net > 0 ? "+" : p.net < 0 ? "−" : ""}₹{num(Math.abs(p.net), 2)}
        </span>
      </div>
      <div className="mt-1 text-sm font-medium text-slate-300">
        {p.net > 0 ? "You are in profit today." : p.net < 0 ? "You are at a loss today." : "No profit or loss yet today."}
        {status.mode === "paper" && " (Practice money)"}
      </div>
      <dl className="mt-5 grid grid-cols-3 gap-3 border-t border-white/10 pt-4 text-sm">
        <div>
          <dt className="text-slate-400">Booked</dt>
          <dd className="mt-0.5 font-semibold">{signed(p.realized)}</dd>
        </div>
        <div>
          <dt className="text-slate-400">Still open</dt>
          <dd className="mt-0.5 font-semibold">{signed(p.unrealized)}</dd>
        </div>
        <div>
          <dt className="text-slate-400">Charges paid</dt>
          <dd className="mt-0.5 font-semibold">₹{num(p.charges, 2)}</dd>
        </div>
      </dl>
    </section>
  );
}

function signed(v: number): string {
  return `${v > 0 ? "+" : v < 0 ? "−" : ""}₹${num(Math.abs(v), 2)}`;
}

function CloseAll() {
  const [open, setOpen] = useState(false);
  const toast = useToast();
  const m = useMutation({
    mutationFn: api.squareOff,
    onSuccess: () => {
      toast.push("Closing all open trades", "success");
      setOpen(false);
    },
  });
  return (
    <>
      <Button onClick={() => { setOpen(true); }}>Close all trades</Button>
      <Dialog open={open} title="Close all open trades?" onClose={() => { setOpen(false); }}>
        <p className="mb-4 text-sm text-slate-600 dark:text-slate-300">
          Every open trade is closed now at the market price, and waiting orders are cancelled.
          Robots keep running and may open new trades. To stop them too, use Emergency stop.
        </p>
        <div className="flex gap-2">
          <Button className="flex-1" onClick={() => { setOpen(false); }}>Cancel</Button>
          <Button variant="danger" className="flex-1" disabled={m.isPending} onClick={() => m.mutate()}>
            Close all
          </Button>
        </div>
      </Dialog>
    </>
  );
}

function TradeCard({ p, robot }: { p: Position; robot: string }) {
  const long = p.quantity > 0;
  return (
    <div className="rounded-xl border border-slate-200 p-4 dark:border-slate-800">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="truncate font-semibold text-slate-900 dark:text-white">{p.label || p.tradingsymbol}</div>
          <div className="mt-0.5 text-xs text-slate-500 dark:text-slate-400">
            {robot} · {productWord(p.product)}
          </div>
        </div>
        <div className="text-right text-lg font-semibold">
          <Money value={p.pnl.net} />
        </div>
      </div>
      <div className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-sm text-slate-600 dark:text-slate-300">
        <span className="inline-flex items-center gap-1">
          <Icon name={long ? "arrowUp" : "arrowDown"} className={`h-4 w-4 ${long ? "text-emerald-600" : "text-red-600"}`} />
          {directionWord(p.quantity)} {lotsText(p.quantity, p.lot_size)}
        </span>
        <span>Entry ₹{num(p.average_price)}</span>
        <span>Now ₹{num(p.last_price)}</span>
        {p.protected_stop !== null && <span>Stop loss ₹{num(p.protected_stop)}</span>}
      </div>
      <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">
        {long ? "Makes money if the price goes up." : "Makes money if the price goes down."}
      </p>
    </div>
  );
}

function Happening({ status }: { status: Status }) {
  const today = todayIst();
  const fills = useQuery({ queryKey: ["fills", "home", today], queryFn: () => api.fills({ from: today, to: today }), refetchInterval: 5000 });
  const name = robotNames(status.strategies);
  const recent = [...(fills.data ?? [])].sort((a, b) => b.ts.localeCompare(a.ts)).slice(0, 8);
  const waiting = status.open_orders.filter((o) => !o.protective);
  return (
    <Card title="What's happening" subtitle="Everything the robots did today, newest first." icon="activity" actions={<Link to="/activity" className="text-sm font-medium text-brand-600 hover:underline dark:text-brand-200">See all</Link>}>
      {waiting.length > 0 && (
        <ul className="mb-3 space-y-2">
          {waiting.map((o) => (
            <li key={o.client_order_id} className="flex items-center gap-3 rounded-xl bg-amber-50 px-3 py-2 text-sm dark:bg-amber-500/10">
              <Pill value={o.status} />
              <span>
                {name(o.strategy_id)} wants to {o.side === "BUY" ? "buy" : "sell"} {lotsText(o.quantity, o.lot_size)} of {o.label || o.tradingsymbol}
                {o.price !== null && ` at ₹${num(o.price)}`}
              </span>
            </li>
          ))}
        </ul>
      )}
      {recent.length === 0 ? (
        <Empty icon="clock">No trades yet today. Robots trade only when their rules say so; quiet days are normal.</Empty>
      ) : (
        <ol className="relative space-y-3 border-l border-slate-200 pl-5 dark:border-slate-800">
          {recent.map((f) => (
            <li key={f.fill_id} className="text-sm">
              <span className={`absolute -left-1.5 mt-1.5 h-3 w-3 rounded-full ring-4 ring-white dark:ring-slate-900 ${f.side === "BUY" ? "bg-emerald-500" : "bg-red-500"}`} />
              <span className="font-mono text-xs text-slate-500">{istTime(f.ts)}</span>
              <div className="text-slate-800 dark:text-slate-200">
                <strong>{name(f.strategy_id)}</strong> {sideVerb(f.side)} {lotsText(f.quantity, f.lot_size)} of {f.label || f.tradingsymbol} at ₹{num(f.price)}
              </div>
            </li>
          ))}
        </ol>
      )}
    </Card>
  );
}

export function Dashboard() {
  const { status } = useLive();
  if (!status) return <Empty icon="clock">Connecting to the trading engine…</Empty>;
  const name = robotNames(status.strategies);
  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-2xl font-bold tracking-tight text-slate-900 dark:text-white">Home</h1>
        <p className="mt-1 text-slate-600 dark:text-slate-400">
          Your <Term k="robot">robots</Term> trade <Term k="nifty">NIFTY</Term> automatically within the limits you set.
          You watch, and you can stop everything any time.
        </p>
      </div>

      {status.blocked_reason && (
        <Notice tone="danger">
          <strong>Robots can&apos;t start yet.</strong> After a restart, the records didn&apos;t match what the broker reports: {status.blocked_reason}.
          Open Zerodha Kite, check your positions and orders, then continue.
          <div className="mt-3"><ResolveBlock /></div>
        </Notice>
      )}
      {status.kill_switch.tripped && (
        <Notice tone="danger">
          <strong>Emergency stop is on</strong> ({status.kill_switch.reason}). All robots are stopped. Clear it at the top of the
          screen, then start robots again from the Robots page.
        </Notice>
      )}
      {status.mode === "paper" && (
        <Notice tone="info">
          <strong>Practice mode.</strong> No real money is used. Run it like this for a few weeks and see how the robots do before going live.
        </Notice>
      )}

      <div className="grid gap-5 lg:grid-cols-5">
        <div className="lg:col-span-3">
          <TodayMoney status={status} />
        </div>
        <div className="lg:col-span-2">
          <Checklist status={status} />
        </div>
      </div>

      <Card
        title={`Open trades (${status.positions.length})`}
        subtitle="Trades the robots are holding right now. The amount updates with every price change."
        icon="wallet"
        actions={status.positions.length > 0 ? <CloseAll /> : undefined}
      >
        {status.positions.length === 0 ? (
          <Empty icon="check">No open trades. Nothing is at risk right now.</Empty>
        ) : (
          <div className="grid gap-3 md:grid-cols-2">
            {status.positions.map((p) => (
              <TradeCard key={`${p.strategy_id}:${p.instrument_token}:${p.product}`} p={p} robot={name(p.strategy_id)} />
            ))}
          </div>
        )}
      </Card>

      <div className="grid gap-5 lg:grid-cols-2">
        <Happening status={status} />
        <Card title="Your robots" subtitle="Tap a robot to see what it does." icon="robot" actions={<Link to="/robots" className="text-sm font-medium text-brand-600 hover:underline dark:text-brand-200">Manage</Link>}>
          <ul className="space-y-2">
            {status.strategies.map((s) => (
              <li key={s.id}>
                <Link
                  to={`/robots/${encodeURIComponent(s.id)}`}
                  className="flex items-center gap-3 rounded-xl border border-slate-200 p-3 transition hover:border-brand-200 hover:bg-brand-50/40 dark:border-slate-800 dark:hover:bg-slate-800/40"
                >
                  <span className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300">
                    <Icon name="robot" />
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2 font-medium text-slate-900 dark:text-white">
                      {s.title || s.id} <Pill value={s.state} />
                    </div>
                    <div className="mt-0.5"><RiskBadge level={s.risk_level} /></div>
                  </div>
                  <div className="text-right font-semibold">
                    <Money value={s.pnl.net} />
                  </div>
                  <Icon name="chevron" className="h-4 w-4 text-slate-400" />
                </Link>
              </li>
            ))}
          </ul>
        </Card>
      </div>

      <Link
        to="/help"
        className="flex items-center gap-4 rounded-2xl border border-brand-200 bg-brand-50 p-4 text-brand-700 hover:bg-brand-100 dark:border-brand-500/30 dark:bg-brand-500/10 dark:text-brand-200"
      >
        <Icon name="help" className="h-8 w-8 shrink-0" />
        <div className="flex-1">
          <div className="font-semibold">New here? Learn how it works in 5 minutes</div>
          <div className="text-sm opacity-80">What the robots do, what every word means, and what to do each morning.</div>
        </div>
        <Icon name="chevron" />
      </Link>
    </div>
  );
}
