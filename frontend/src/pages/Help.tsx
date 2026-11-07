// How it works, end to end, for someone who has never traded.

import { Link } from "react-router-dom";
import { Icon, type IconName } from "../components/Icon";
import { Card, Notice, PageTitle } from "../components/ui";
import { GLOSSARY } from "../lib/glossary";

const FLOW: { icon: IconName; title: string; text: string }[] = [
  { icon: "link", title: "Prices come in", text: "Every second, live NIFTY prices arrive from Zerodha (or simulated prices in practice)." },
  { icon: "robot", title: "Robots decide", text: "Each robot checks its rules. Most of the time the answer is \"do nothing\"." },
  { icon: "shield", title: "Safety check", text: "Before any order leaves, it is checked against today's plan: loss limit, number of trades, size." },
  { icon: "activity", title: "Order is placed", text: "Practice: the trade is pretend. Real money: the order goes to your Zerodha account." },
  { icon: "wallet", title: "Profit is tracked", text: "Every trade's profit or loss, after charges, is added up live on the Home screen." },
  { icon: "stop", title: "Auto stop", text: "If today's loss reaches your limit, everything stops and all trades are closed automatically." },
];

const ROUTINE: { time: string; title: string; text: string; to?: string }[] = [
  { time: "Before 9:15 AM", title: "Log in to Zerodha", text: "Tap \"Log in to Kite\" on Home. Zerodha requires this once every morning.", to: "/" },
  { time: "Before 9:15 AM", title: "Submit today's plan", text: "Check your loss limit, how many trades, and which robots should run. Then press Submit.", to: "/plan" },
  { time: "9:15 AM to 3:30 PM", title: "Watch, don't touch", text: "Robots trade on their own. Glance at Home now and then. If something looks wrong, press Emergency stop." },
  { time: "3:15 PM", title: "Intraday trades close", text: "All same-day trades are closed automatically before the market closes." },
  { time: "Evening", title: "Review", text: "See how the day went in Profit history and Activity.", to: "/history" },
];

const FAQ: { q: string; a: string }[] = [
  { q: "Do I need to do anything while it trades?", a: "No. After the morning steps, the robots work alone. You only watch, and you can stop everything with one button." },
  { q: "Can I lose more than my daily loss limit?", a: "Normally no: when the limit is reached, everything stops and closes. In a very fast market, prices can jump past the limit before the exit completes, so the actual loss can be a little higher." },
  { q: "Is practice mode really safe?", a: "Yes. In practice mode the app never sends an order to Zerodha. It only pretends, using real or simulated prices." },
  { q: "When should I switch to real money?", a: "Only after several weeks of practice with results you are comfortable with, and with money you can afford to lose. Real money mode needs extra settings on the server, so it can't be switched on by accident." },
  { q: "Why did a robot not trade today?", a: "Robots trade only when their rules match. Quiet days with no trades are normal. Also check that today's plan is submitted and the robot is turned on." },
  { q: "What are charges?", a: "Zerodha brokerage, government taxes (STT, GST, stamp duty) and exchange fees. They are paid on every trade, win or lose, and are always shown." },
];

export function HelpPage() {
  return (
    <div className="space-y-5">
      <PageTitle title="How it works" subtitle="Everything you need to know, in plain words. Takes about 5 minutes to read." />

      <Card title="What tradeflow does" icon="info">
        <p className="text-slate-700 dark:text-slate-300">
          tradeflow runs small computer programs, called <strong>robots</strong>, that buy and sell NIFTY contracts on the
          stock market for you. Each robot follows fixed rules, for example &quot;buy when prices start rising&quot;. You decide
          every morning how much you are willing to lose and which robots may trade. The app makes sure no robot
          ever goes beyond those limits.
        </p>
        <ol className="mt-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {FLOW.map((f, i) => (
            <li key={f.title} className="relative rounded-xl bg-slate-50 p-4 dark:bg-slate-800/50">
              <div className="flex items-center gap-2">
                <span className="grid h-8 w-8 place-items-center rounded-lg bg-brand-500 text-white">
                  <Icon name={f.icon} className="h-4 w-4" />
                </span>
                <span className="text-xs font-semibold uppercase tracking-wide text-slate-400">Step {i + 1}</span>
              </div>
              <div className="mt-2 font-semibold text-slate-900 dark:text-white">{f.title}</div>
              <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">{f.text}</p>
            </li>
          ))}
        </ol>
      </Card>

      <Card title="Your daily routine" subtitle="About 5 minutes in the morning." icon="calendar">
        <ol className="space-y-4">
          {ROUTINE.map((r) => (
            <li key={r.title} className="flex gap-4">
              <span className="w-28 shrink-0 pt-0.5 text-xs font-semibold text-brand-600 dark:text-brand-200">{r.time}</span>
              <div>
                <div className="font-medium text-slate-900 dark:text-white">
                  {r.to ? <Link to={r.to} className="hover:underline">{r.title}</Link> : r.title}
                </div>
                <p className="text-sm text-slate-600 dark:text-slate-300">{r.text}</p>
              </div>
            </li>
          ))}
        </ol>
      </Card>

      <div className="grid gap-5 lg:grid-cols-2">
        <Card title="Practice mode vs real money" icon="wallet">
          <div className="space-y-3 text-sm">
            <div className="rounded-xl border border-blue-200 bg-blue-50 p-3 dark:border-blue-500/30 dark:bg-blue-500/10">
              <div className="font-semibold text-blue-800 dark:text-blue-200">PRACTICE MODE (blue badge)</div>
              <p className="text-slate-700 dark:text-slate-300">Pretend money. Nothing reaches Zerodha. Start here, always.</p>
            </div>
            <div className="rounded-xl border border-red-200 bg-red-50 p-3 dark:border-red-500/30 dark:bg-red-500/10">
              <div className="font-semibold text-red-800 dark:text-red-200">REAL MONEY (red, blinking badge)</div>
              <p className="text-slate-700 dark:text-slate-300">Real orders and real profit or loss. Needs several deliberate server settings to turn on.</p>
            </div>
          </div>
        </Card>
        <Card title="Your safety net" icon="shield">
          <ul className="space-y-2 text-sm text-slate-700 dark:text-slate-300">
            {[
              "Daily loss limit: when reached, everything stops and closes by itself.",
              "Profit target: when reached, no new trades are taken.",
              "Stop losses: each trade can have an automatic exit if it goes wrong.",
              "Emergency stop: one red button, always at the top of the screen.",
              "Closing a trade is never blocked, even after a stop.",
            ].map((t) => (
              <li key={t} className="flex gap-2"><Icon name="check" className="mt-0.5 h-4 w-4 shrink-0 text-emerald-600" />{t}</li>
            ))}
          </ul>
        </Card>
      </div>

      <Notice tone="warn">
        <strong>Please read:</strong> Trading futures and options is risky. Most people who trade them lose money. Robots
        follow rules; they cannot predict the future. Results from practice or from past data do not guarantee future
        results. Only use money you can afford to lose.
      </Notice>

      <Card title="Words you will see" subtitle="Tap any word with a dotted underline in the app to see its meaning." icon="help">
        <dl className="grid gap-x-8 gap-y-4 sm:grid-cols-2">
          {Object.values(GLOSSARY).map((g) => (
            <div key={g.term}>
              <dt className="font-semibold text-slate-900 dark:text-white">{g.term}</dt>
              <dd className="text-sm text-slate-600 dark:text-slate-300">{g.short}</dd>
            </div>
          ))}
        </dl>
      </Card>

      <Card title="Common questions" icon="help">
        <div className="divide-y divide-slate-100 dark:divide-slate-800">
          {FAQ.map((f) => (
            <details key={f.q} className="group py-3">
              <summary className="flex cursor-pointer list-none items-center justify-between gap-3 font-medium text-slate-900 dark:text-white">
                {f.q}
                <Icon name="chevron" className="h-4 w-4 shrink-0 transition group-open:rotate-90" />
              </summary>
              <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">{f.a}</p>
            </details>
          ))}
          <details className="group py-3">
            <summary className="flex cursor-pointer list-none items-center justify-between gap-3 font-medium text-slate-900 dark:text-white">
              Home says &quot;Robots can&apos;t start yet&quot;. What do I do?
              <Icon name="chevron" className="h-4 w-4 shrink-0 transition group-open:rotate-90" />
            </summary>
            <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">
              After a restart the app compares its records with Zerodha. If they don&apos;t match (or the database was
              unreachable), it blocks robots so nothing trades on wrong information. Open Kite, check your positions and
              orders, then press &quot;I&apos;ve checked, continue&quot; on Home.
            </p>
          </details>
        </div>
      </Card>
    </div>
  );
}
