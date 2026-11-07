import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import type { BacktestInput, BacktestRun, MissingData } from "../api/types";
import { useToast } from "../app/toasts";
import { EquityChart } from "../components/Charts";
import { DataTable } from "../components/DataTable";
import { Term } from "../components/Term";
import { Button, Card, Notice, PageTitle, Pill, Stat, inputClass } from "../components/ui";
import { inr, istDateTime, num, pnlClass, todayIst } from "../lib/format";

function RunForm() {
  const today = todayIst();
  const [form, setForm] = useState<BacktestInput>({
    strategy: "ema_crossover",
    params: {},
    start: today,
    end: today,
    interval: "minute",
  });
  const [paramsText, setParamsText] = useState("{}");
  const [paramsError, setParamsError] = useState("");
  const toast = useToast();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const strategies = useQuery({ queryKey: ["strategies"], queryFn: api.strategies });
  const types = [...new Set((strategies.data ?? []).map((s) => s.strategy))];
  const run = useMutation({
    mutationFn: api.runBacktest,
    onSuccess: (r) => {
      toast.push(`Test #${r.id} started`, "success");
      void qc.invalidateQueries({ queryKey: ["backtests"] });
      void navigate(`/backtests/${r.id}`);
    },
  });
  return (
    <Card title="Start a new test" subtitle="Pick a robot and a date range. It replays those days using saved prices." icon="flask">
      <form
        className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5"
        onSubmit={(e) => {
          e.preventDefault();
          try {
            const params = JSON.parse(paramsText) as unknown;
            if (typeof params !== "object" || params === null || Array.isArray(params)) throw new Error();
            setParamsError("");
            run.mutate({ ...form, params: params as Record<string, unknown> });
          } catch {
            setParamsError('Custom settings must look like {"lots": 1}');
          }
        }}
      >
        <label className="text-sm">Robot
          <input list="strategy-types" className={inputClass} value={form.strategy} onChange={(e) => { setForm({ ...form, strategy: e.target.value }); }} />
          <datalist id="strategy-types">{types.map((t) => <option key={t} value={t} />)}</datalist>
        </label>
        <label className="text-sm">From
          <input type="date" className={inputClass} value={form.start} onChange={(e) => { setForm({ ...form, start: e.target.value }); }} />
        </label>
        <label className="text-sm">To
          <input type="date" className={inputClass} value={form.end} onChange={(e) => { setForm({ ...form, end: e.target.value }); }} />
        </label>
        <label className="text-sm">Price step
          <select className={inputClass} value={form.interval} onChange={(e) => { setForm({ ...form, interval: e.target.value as BacktestInput["interval"] }); }}>
            {["minute", "3minute", "5minute", "15minute", "60minute"].map((i) => <option key={i}>{i}</option>)}
          </select>
        </label>
        <div className="flex items-end">
          <Button type="submit" variant="primary" className="w-full" disabled={run.isPending}>Run test</Button>
        </div>
        <label className="text-sm sm:col-span-2 lg:col-span-5">Custom settings (advanced; leave {"{}"} for the defaults)
          <textarea rows={2} className={`${inputClass} font-mono`} value={paramsText} onChange={(e) => { setParamsText(e.target.value); }} />
          {paramsError && <span className="text-rose-600">{paramsError}</span>}
        </label>
      </form>
    </Card>
  );
}

export function BacktestsPage() {
  const q = useQuery({ queryKey: ["backtests"], queryFn: api.backtests, refetchInterval: 5000 });
  return (
    <div className="space-y-5">
      <PageTitle
        title="Test on past data"
        subtitle={<>See how a robot would have done on earlier days (a <Term k="backtest">backtest</Term>). Charges are included.</>}
      />
      <Notice tone="warn">Good past results don&apos;t promise future profit. Use tests to understand a robot, not to predict money.</Notice>
      <RunForm />
      <Card title="Past tests">
        <DataTable<BacktestRun>
          rows={q.data ?? []}
          rowKey={(r) => String(r.id)}
          empty={q.isLoading ? "Loading…" : "No tests yet. Start one above."}
          columns={[
            { key: "id", header: "#", render: (r) => <Link className="text-sky-700 hover:underline dark:text-sky-400" to={String(r.id)}>{r.id}</Link> },
            { key: "s", header: "Robot", render: (r) => r.strategy },
            { key: "range", header: "Days", render: (r) => `${r.start_date} → ${r.end_date}` },
            { key: "st", header: "Status", render: (r) => <Pill value={r.status} /> },
            { key: "net", header: "After charges", align: "right", render: (r) => (r.metrics ? <span className={pnlClass(r.metrics.net_pnl)}>{inr(r.metrics.net_pnl)}</span> : "—") },
            { key: "tr", header: "Trades", align: "right", render: (r) => r.metrics?.trades ?? "—" },
            { key: "at", header: "Started", render: (r) => istDateTime(r.created_at) },
          ]}
        />
      </Card>
    </div>
  );
}

export function BacktestPage() {
  const { id = "" } = useParams();
  const q = useQuery({
    queryKey: ["backtest", id],
    queryFn: () => api.backtest(Number(id)),
    refetchInterval: (query) => (query.state.data?.status === "running" ? 2000 : false),
  });
  const r = q.data;
  if (!r) return <p className="text-sm text-slate-500">{q.isLoading ? "Loading…" : "Not found."}</p>;
  const m = r.metrics;
  return (
    <div className="space-y-5">
      <Card title={<span className="flex items-center gap-2 normal-case">Test #{r.id} · {r.strategy} <Pill value={r.status} /></span>}>
        <p className="text-sm text-slate-600 dark:text-slate-300">
          {r.start_date} → {r.end_date} · settings <code className="text-xs">{JSON.stringify(r.params)}</code>
        </p>
        {r.error && <p role="alert" className="mt-2 text-sm text-rose-600">{r.error}</p>}
        {r.status === "running" && <p className="mt-2 text-sm text-slate-500">Running… this page updates by itself.</p>}
      </Card>
      {m && (
        <>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4 lg:grid-cols-6">
            <Stat label="Result after charges" value={m.net_pnl} />
            <Stat label="Before charges" value={m.gross_pnl} />
            <Stat label="Charges" value={-m.charges} />
            <Stat label={<Term k="drawdown">Worst dip</Term>} value={-m.max_drawdown} />
            <Stat label="Trades" value={m.trades} money={false} />
            <Stat label="Days" value={m.days} money={false} />
          </div>
          <Card title="Details">
            <dl className="grid grid-cols-2 gap-x-6 gap-y-1 text-sm md:grid-cols-4">
              <dt className="text-slate-500"><Term k="win_rate">Win rate</Term></dt><dd>{m.win_rate === null ? "—" : `${num(m.win_rate)}%`}</dd>
              <dt className="text-slate-500">Average win</dt><dd>{inr(m.avg_win)}</dd>
              <dt className="text-slate-500">Average loss</dt><dd>{inr(m.avg_loss)}</dd>
              <dt className="text-slate-500">Steadiness (Sharpe)</dt><dd>{num(m.sharpe)}</dd>
              <dt className="text-slate-500">Worst dip %</dt><dd>{num(m.max_drawdown_pct)}%</dd>
              <dt className="text-slate-500">Orders filled</dt><dd>{m.fills}</dd>
            </dl>
            {(m.notes ?? []).length > 0 && (
              <ul className="mt-3 list-disc pl-5 text-xs text-slate-500">{m.notes?.map((n) => <li key={n}>{n}</li>)}</ul>
            )}
          </Card>
          <Card title="Money over time" subtitle="Running total after charges, day by day.">
            {(r.equity_curve ?? []).length > 0 ? (
              <EquityChart points={(r.equity_curve ?? []).map((p) => ({ time: p.date, value: p.net }))} />
            ) : (
              <p className="text-sm text-slate-500">No trading days in range.</p>
            )}
          </Card>
        </>
      )}
      {(r.missing_data ?? []).length > 0 && (
        <Card title={`Missing data (${r.missing_data?.length ?? 0})`}>
          <p className="mb-2 text-sm text-amber-700 dark:text-amber-300">
            These instrument-days had fewer candles than expected. Results that depend on them are less reliable.
          </p>
          <DataTable<MissingData>
            rows={r.missing_data ?? []}
            rowKey={(x) => `${x.date}:${x.token}`}
            csvName={`backtest_${r.id}_missing.csv`}
            columns={[
              { key: "d", header: "Date", render: (x) => x.date, csv: (x) => x.date },
              { key: "s", header: "Symbol", render: (x) => x.symbol, csv: (x) => x.symbol },
              { key: "f", header: "Found", align: "right", render: (x) => x.found_candles, csv: (x) => x.found_candles },
              { key: "e", header: "Expected", align: "right", render: (x) => x.expected_candles, csv: (x) => x.expected_candles },
            ]}
          />
        </Card>
      )}
    </div>
  );
}
