// Connection check: what is set up, what works, and what real-money mode still needs.
// Read-only: Kite keys and the live switches are set in the server's .env file.

import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { ConnectionCheck, KiteTest } from "../api/types";
import { Icon } from "../components/Icon";
import { Button, Card, Empty, Notice, PageTitle } from "../components/ui";
import { istDateTime } from "../lib/format";

function Mark({ ok }: { ok: boolean | null }) {
  if (ok === null) {
    return (
      <span className="inline-flex items-center gap-1 text-xs font-semibold text-slate-500">
        <Icon name="info" className="h-4 w-4" /> Check yourself
      </span>
    );
  }
  return ok ? (
    <span className="inline-flex items-center gap-1 text-xs font-semibold text-good-text dark:text-emerald-400">
      <Icon name="check" className="h-4 w-4" /> Yes
    </span>
  ) : (
    <span className="inline-flex items-center gap-1 text-xs font-semibold text-red-600 dark:text-red-400">
      <Icon name="x" className="h-4 w-4" /> No
    </span>
  );
}

function Row({ label, ok, children }: { label: string; ok: boolean | null; children?: React.ReactNode }) {
  return (
    <li className="flex items-start justify-between gap-4 py-3">
      <div className="min-w-0">
        <div className="font-medium text-slate-900 dark:text-white">{label}</div>
        {children && <div className="mt-0.5 text-sm text-slate-500 dark:text-slate-400">{children}</div>}
      </div>
      <Mark ok={ok} />
    </li>
  );
}

const ENV_EXAMPLE = `# backend/.env  (or .env next to docker-compose.yml for Docker)
KITE_API_KEY=your_api_key
KITE_API_SECRET=your_api_secret
KITE_USER_ID=AB1234      # your Zerodha client id (recommended)

# Practice with REAL prices (still pretend money):
MODE=paper
FEED=kite`;

export function ConnectionPage() {
  const q = useQuery({ queryKey: ["connection"], queryFn: api.connection, refetchInterval: 15_000 });
  const test = useMutation<KiteTest>({ mutationFn: api.testKite, meta: { handlesErrors: true } });
  const c = q.data;
  if (!c) return <Empty icon="link">{q.isLoading ? "Checking connections…" : "Can't load the connection check."}</Empty>;
  const k = c.kite;
  const keysSet = k.api_key_set && k.api_secret_set;
  const checks: ConnectionCheck[] = c.live_checks;

  return (
    <div className="space-y-5">
      <PageTitle
        title="Connection check"
        subtitle="What is set up, what works, and what real-money mode still needs. Nothing here can place an order."
      />

      <Card title="Right now" icon="info">
        <ul className="divide-y divide-slate-100 dark:divide-slate-800">
          <Row label={c.mode === "live" ? "Mode: REAL MONEY" : "Mode: practice (pretend money)"} ok={null}>
            Set by <code>MODE</code> in the server&apos;s .env file. The coloured badge at the top always shows it.
          </Row>
          <Row label={c.feed === "kite" ? "Prices: real, from Zerodha" : "Prices: simulated"} ok={null}>
            Set by <code>FEED</code>: <code>sim</code> for simulated prices, <code>kite</code> for real Zerodha prices.
          </Row>
          <Row label="Database" ok={c.database.used ? c.database.ok : null}>
            {c.database.used ? c.database.detail : "Not used: practice data is kept in memory and lost when tradeflow stops."}
          </Row>
        </ul>
      </Card>

      <Card
        title="Zerodha (Kite Connect)"
        subtitle="Needed for real prices and for real-money mode. Not needed for practice with simulated prices."
        icon="link"
        actions={
          k.logged_in ? (
            <Button variant="primary" disabled={test.isPending} onClick={() => { test.mutate(); }}>
              {test.isPending ? "Testing…" : "Test connection"}
            </Button>
          ) : keysSet ? (
            <a className="inline-flex min-h-10 items-center rounded-xl bg-brand-500 px-4 text-sm font-semibold text-white hover:bg-brand-600" href={api.kiteLoginUrl()}>
              Log in to Kite
            </a>
          ) : undefined
        }
      >
        <ul className="divide-y divide-slate-100 dark:divide-slate-800">
          <Row label="API key and secret are set" ok={keysSet}>
            {keysSet ? "The values are hidden on purpose." : "Add them to the server's .env file (see below), then restart tradeflow."}
          </Row>
          <Row label="Your Zerodha client id is set (recommended)" ok={k.user_id_set}>
            With <code>KITE_USER_ID</code>, nobody can log in with a different Zerodha account.
          </Row>
          <Row label="Logged in to Zerodha today" ok={keysSet ? k.logged_in : null}>
            {k.session_error ||
              (k.logged_in
                ? `As ${k.user_id}, until ${istDateTime(k.expires_at)}. Zerodha asks for a new login every morning.`
                : "Zerodha requires a login once every morning.")}
          </Row>
        </ul>
        {test.data && (
          <div className="mt-3">
            <Notice tone={test.data.ok ? "good" : "danger"}>
              {test.data.detail}
              {test.data.ok && ` Account: ${test.data.user_name} (${test.data.user_id}).`}
            </Notice>
          </div>
        )}
        {test.error && <div className="mt-3"><Notice tone="danger">{test.error.message}</Notice></div>}
      </Card>

      <Card title="Where do I put my Kite Connect details?" icon="plan">
        <ol className="list-decimal space-y-2 pl-5 text-sm text-slate-700 dark:text-slate-300">
          <li>
            Create an app at <strong>developers.kite.trade</strong> (Kite Connect is a paid Zerodha service). Set its
            redirect URL to exactly: <code className="break-all rounded bg-slate-100 px-1.5 py-0.5 dark:bg-slate-800">{k.redirect_url}</code>
          </li>
          <li>
            Copy the <strong>API key</strong> and <strong>API secret</strong> into the server&apos;s settings file: {c.env_file}.
          </li>
          <li>Restart tradeflow, open Home and press <strong>Log in to Kite</strong>. Then come back here and press Test connection.</li>
        </ol>
        <pre className="mt-4 overflow-x-auto rounded-xl bg-slate-900 p-4 text-xs text-slate-100">{ENV_EXAMPLE}</pre>
        <p className="mt-3 text-sm text-slate-500 dark:text-slate-400">
          Why not type them here? The secret would then be stored where anyone with the dashboard password could read it, and
          a click could switch on real money. Keeping them in the server file is the safe, standard practice.
        </p>
      </Card>

      <Card
        title="Ready for real money?"
        subtitle="Every line must say Yes. Practise for several weeks first."
        icon="shield"
      >
        {c.live_ready && c.mode === "live" ? (
          <Notice tone="danger">Real-money mode is ON.</Notice>
        ) : (
          <ul className="divide-y divide-slate-100 dark:divide-slate-800">
            {checks.map((ch) => (
              <Row key={ch.label} label={ch.label} ok={ch.ok}>
                {ch.ok === true ? undefined : ch.fix}
              </Row>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
