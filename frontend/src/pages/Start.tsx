// "Start here": a step-by-step guide from zero to robots trading, for someone who
// has never traded. Opens automatically on the first visit; reopen it any time.

import { useQuery } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { useLive } from "../app/live";
import { Icon, type IconName } from "../components/Icon";
import { SimplePlan } from "../components/SimplePlan";
import { Button, Card, Notice } from "../components/ui";
import { markStartGuideSeen } from "../lib/onboarding";

const STEPS = ["Welcome", "Practice", "Zerodha", "Your plan", "Done"] as const;

function Point({ icon, title, children }: { icon: IconName; title: string; children: ReactNode }) {
  return (
    <li className="flex gap-4">
      <span className="grid h-11 w-11 shrink-0 place-items-center rounded-xl bg-brand-50 text-brand-600 dark:bg-brand-500/15 dark:text-brand-200">
        <Icon name={icon} />
      </span>
      <div>
        <div className="font-semibold text-slate-900 dark:text-white">{title}</div>
        <p className="text-sm text-slate-600 dark:text-slate-300">{children}</p>
      </div>
    </li>
  );
}

function Welcome() {
  return (
    <>
      <h1 className="text-2xl font-bold text-slate-900 dark:text-white sm:text-3xl">Welcome to tradeflow</h1>
      <p className="mt-2 text-slate-600 dark:text-slate-300">
        In the next 3 minutes you will set up your first trading day. No experience needed.
      </p>
      <ul className="mt-6 space-y-5">
        <Point icon="robot" title="Robots trade for you">
          Small programs called robots buy and sell NIFTY (India&apos;s main stock-market index) on their own, following fixed rules.
        </Point>
        <Point icon="shield" title="You set the limit every morning">
          You decide the most you can lose today. When that amount is reached, everything stops by itself.
        </Point>
        <Point icon="wallet" title="Practice first, with pretend money">
          You start in practice mode. It works exactly like the real thing, but no real money is used.
        </Point>
        <Point icon="stop" title="You are always in control">
          A red Emergency stop button at the top of every screen stops all robots and closes all trades.
        </Point>
      </ul>
    </>
  );
}

function Practice() {
  const { status } = useLive();
  const live = status?.mode === "live";
  return (
    <>
      <h1 className="text-2xl font-bold text-slate-900 dark:text-white">{live ? "You are using real money" : "You are in practice mode"}</h1>
      <p className="mt-2 text-slate-600 dark:text-slate-300">Look at the coloured badge at the top of the screen. It always tells you which mode you are in.</p>
      <div className="mt-6 grid gap-3 sm:grid-cols-2">
        <div className={`rounded-xl border-2 p-4 ${live ? "border-slate-200 opacity-60 dark:border-slate-700" : "border-blue-500 bg-blue-50 dark:bg-blue-500/10"}`}>
          <span className="rounded-full bg-blue-600 px-3 py-1 text-xs font-bold text-white">PRACTICE MODE</span>
          <p className="mt-3 text-sm text-slate-700 dark:text-slate-300">Pretend money. Nothing is sent to Zerodha. Mistakes cost nothing.</p>
          {!live && <p className="mt-2 text-sm font-semibold text-blue-700 dark:text-blue-300">← You are here</p>}
        </div>
        <div className={`rounded-xl border-2 p-4 ${live ? "border-red-500 bg-red-50 dark:bg-red-500/10" : "border-slate-200 dark:border-slate-700"}`}>
          <span className="rounded-full bg-red-600 px-3 py-1 text-xs font-bold text-white">REAL MONEY</span>
          <p className="mt-3 text-sm text-slate-700 dark:text-slate-300">Real orders in your Zerodha account. Profits and losses are real.</p>
          {live && <p className="mt-2 text-sm font-semibold text-red-700 dark:text-red-300">← You are here</p>}
        </div>
      </div>
      <div className="mt-5">
        {live ? (
          <Notice tone="danger">Real money is on. Only continue if you have practised for weeks and can afford to lose the amount you set.</Notice>
        ) : (
          <Notice tone="info">Stay in practice mode for at least a few weeks. Watch how the robots do on good and bad days before thinking about real money.</Notice>
        )}
      </div>
    </>
  );
}

function Zerodha() {
  const auth = useQuery({ queryKey: ["auth"], queryFn: api.authStatus });
  const a = auth.data;
  return (
    <>
      <h1 className="text-2xl font-bold text-slate-900 dark:text-white">Connect to Zerodha</h1>
      <p className="mt-2 text-slate-600 dark:text-slate-300">
        Zerodha is the broker: the company where the trading account lives. The robots get prices from it and, in real money mode, send orders to it.
      </p>
      <div className="mt-6">
        {!a ? (
          <p className="text-sm text-slate-500">Checking…</p>
        ) : !a.configured ? (
          <Notice tone="good">
            <strong>Nothing to do now.</strong> In this practice setup, prices are simulated, so no Zerodha account is needed.
            Later, to practise with real market prices, the person who set up tradeflow connects your Zerodha account once.
          </Notice>
        ) : a.logged_in ? (
          <Notice tone="good"><strong>Connected</strong> as {a.user_id}. Zerodha asks you to log in again every morning.</Notice>
        ) : (
          <div className="space-y-3">
            <Notice tone="warn">Zerodha asks you to log in once every morning. You will come back here after logging in.</Notice>
            <a className="inline-flex rounded-xl bg-brand-500 px-5 py-3 font-semibold text-white hover:bg-brand-600" href={api.kiteLoginUrl()}>
              Log in to Zerodha (Kite)
            </a>
          </div>
        )}
      </div>
    </>
  );
}

function YourPlan({ onDone }: { onDone: () => void }) {
  const q = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const info = q.data;
  return (
    <>
      <h1 className="text-2xl font-bold text-slate-900 dark:text-white">Your plan for today</h1>
      <p className="mt-2 text-slate-600 dark:text-slate-300">
        Two questions. You answer them every morning; it takes less than a minute.
      </p>
      <div className="mt-6">
        {!info ? (
          <p className="text-sm text-slate-500">{q.isLoading ? "Loading…" : "Can't load the plan right now."}</p>
        ) : (
          <>
            {info.submitted && (
              <div className="mb-4">
                <Notice tone="good">Today&apos;s plan is already submitted. You can change it below, or press Next.</Notice>
              </div>
            )}
            <SimplePlan key={String(info.submitted)} info={info} onDone={onDone} />
          </>
        )}
      </div>
    </>
  );
}

function Done() {
  const { status } = useLive();
  const running = status?.strategies.filter((s) => s.state === "RUNNING").length ?? 0;
  return (
    <>
      <span className="grid h-14 w-14 place-items-center rounded-2xl bg-emerald-100 text-emerald-700 dark:bg-emerald-500/15 dark:text-emerald-300">
        <Icon name="check" className="h-8 w-8" />
      </span>
      <h1 className="mt-4 text-2xl font-bold text-slate-900 dark:text-white">You&apos;re all set</h1>
      <p className="mt-2 text-slate-600 dark:text-slate-300">
        {running > 0
          ? `${running} robot${running === 1 ? " is" : "s are"} now working. You don't need to do anything else today.`
          : "No robot is running yet. Go back one step and submit the plan with at least one robot turned on."}
      </p>
      <h2 className="mt-6 font-semibold text-slate-900 dark:text-white">What happens now</h2>
      <ul className="mt-3 space-y-3 text-sm text-slate-700 dark:text-slate-300">
        {[
          ["activity", "Robots watch prices every second. They trade only when their rules say so. Some days they don't trade at all; that's normal."],
          ["home", "Open Home any time to see today's profit or loss and what the robots did."],
          ["clock", "At 3:15 PM, same-day trades close automatically."],
          ["stop", "If something looks wrong, press the red Emergency stop at the top. It stops everything."],
          ["sun", "Tomorrow morning: open tradeflow, check the checklist on Home, submit the plan. Two minutes."],
        ].map(([icon, text]) => (
          <li key={text} className="flex gap-3">
            <Icon name={icon as IconName} className="mt-0.5 h-5 w-5 shrink-0 text-brand-600 dark:text-brand-200" />
            {text}
          </li>
        ))}
      </ul>
    </>
  );
}

export function StartPage() {
  const [step, setStep] = useState(0);
  const navigate = useNavigate();
  useEffect(() => {
    markStartGuideSeen();
  }, []);
  const last = step === STEPS.length - 1;
  const onPlanStep = STEPS[step] === "Your plan";
  return (
    <div className="mx-auto max-w-3xl space-y-5">
      <ol className="flex items-center gap-2" aria-label="Progress">
        {STEPS.map((s, i) => (
          <li key={s} className="flex flex-1 flex-col gap-1.5">
            <span className={`h-1.5 rounded-full ${i <= step ? "bg-brand-500" : "bg-slate-200 dark:bg-slate-800"}`} />
            <span className={`hidden text-xs sm:block ${i === step ? "font-semibold text-brand-700 dark:text-brand-200" : "text-slate-500"}`}>
              {i + 1}. {s}
            </span>
          </li>
        ))}
      </ol>
      <p className="text-sm text-slate-500 sm:hidden">Step {step + 1} of {STEPS.length}: {STEPS[step]}</p>

      <Card className="sm:p-8">
        {step === 0 && <Welcome />}
        {step === 1 && <Practice />}
        {step === 2 && <Zerodha />}
        {step === 3 && <YourPlan onDone={() => { setStep(4); }} />}
        {step === 4 && <Done />}
      </Card>

      <div className="flex items-center justify-between gap-3">
        {step > 0 ? (
          <Button onClick={() => { setStep(step - 1); }}>
            <Icon name="chevron" className="h-4 w-4 rotate-180" /> Back
          </Button>
        ) : (
          <Link to="/" className="text-sm text-slate-500 hover:underline">Skip, I know how it works</Link>
        )}
        {last ? (
          <div className="flex gap-2">
            <Link to="/help" className="inline-flex min-h-10 items-center rounded-xl border border-slate-200 bg-white px-4 text-sm font-medium dark:border-slate-700 dark:bg-slate-800">
              Read the full guide
            </Link>
            <Button variant="primary" onClick={() => { void navigate("/"); }}>Go to Home</Button>
          </div>
        ) : (
          <Button variant={onPlanStep ? "secondary" : "primary"} onClick={() => { setStep(step + 1); }}>
            {onPlanStep ? "Skip for now" : "Next"} <Icon name="chevron" className="h-4 w-4" />
          </Button>
        )}
      </div>
    </div>
  );
}
