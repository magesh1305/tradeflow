import { NavLink, Outlet } from "react-router-dom";
import { setToken } from "../api/token";
import { Icon, type IconName } from "../components/Icon";
import { KillSwitch } from "../components/KillSwitch";
import { ModeBadge } from "../components/ModeBadge";
import { useLive } from "./live";
import { useTheme } from "./theme";

interface NavItem {
  to: string;
  label: string;
  icon: IconName;
}

// Every screen, in the order a new user meets them.
const NAV: NavItem[] = [
  { to: "/start", label: "Start here", icon: "play" },
  { to: "/", label: "Home", icon: "home" },
  { to: "/plan", label: "Today's plan", icon: "plan" },
  { to: "/robots", label: "Robots", icon: "robot" },
  { to: "/activity", label: "Activity", icon: "activity" },
  { to: "/history", label: "Profit history", icon: "chart" },
  { to: "/option-chain", label: "Option prices", icon: "table" },
  { to: "/backtests", label: "Test on past data", icon: "flask" },
  { to: "/safety", label: "Safety limits", icon: "shield" },
  { to: "/help", label: "How it works", icon: "help" },
];

// Phones: five thumb-sized tabs; the rest live under "More".
const TABS: NavItem[] = [
  { to: "/", label: "Home", icon: "home" },
  { to: "/plan", label: "Plan", icon: "plan" },
  { to: "/robots", label: "Robots", icon: "robot" },
  { to: "/activity", label: "Activity", icon: "activity" },
  { to: "/more", label: "More", icon: "more" },
];

function Connection() {
  const { connected } = useLive();
  return connected ? (
    <span className="hidden items-center gap-1.5 rounded-full bg-emerald-50 px-2.5 py-1 text-xs font-medium text-emerald-800 sm:inline-flex dark:bg-emerald-500/10 dark:text-emerald-300">
      <span className="h-2 w-2 rounded-full bg-emerald-500" /> Live
    </span>
  ) : (
    <span
      role="status"
      className="inline-flex items-center gap-1.5 rounded-full bg-amber-100 px-2.5 py-1 text-xs font-semibold text-amber-900 dark:bg-amber-500/15 dark:text-amber-200"
    >
      <span className="h-2 w-2 animate-pulse rounded-full bg-amber-500" /> DISCONNECTED: reconnecting
    </span>
  );
}

function Logo() {
  return (
    <div className="flex items-center gap-2.5">
      <span className="grid h-9 w-9 place-items-center rounded-xl bg-gradient-to-br from-brand-500 to-indigo-600 text-white shadow">
        <Icon name="activity" className="h-5 w-5" />
      </span>
      <div className="leading-tight">
        <div className="font-bold text-slate-900 dark:text-white">tradeflow</div>
        <div className="text-xs text-slate-500 dark:text-slate-400">Trading robots</div>
      </div>
    </div>
  );
}

export function Layout() {
  const { status } = useLive();
  const [theme, toggleTheme] = useTheme();
  const planMissing = status !== null && !status.plan_submitted;
  return (
    <div className="min-h-screen lg:pl-64">
      {/* Desktop sidebar */}
      <aside className="fixed inset-y-0 left-0 z-30 hidden w-64 flex-col border-r border-slate-200 bg-white px-3 py-5 lg:flex dark:border-slate-800 dark:bg-slate-950">
        <div className="px-2">
          <Logo />
        </div>
        <nav className="mt-8 flex-1 space-y-1" aria-label="Main">
          {NAV.map((n) => (
            <NavLink
              key={n.to}
              to={n.to}
              end={n.to === "/"}
              className={({ isActive }) =>
                `flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium transition ${
                  isActive
                    ? "bg-brand-50 text-brand-700 dark:bg-brand-500/15 dark:text-brand-200"
                    : "text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-900"
                }`
              }
            >
              <Icon name={n.icon} />
              <span className="flex-1">{n.label}</span>
              {n.to === "/plan" && planMissing && <span className="h-2 w-2 rounded-full bg-amber-500" aria-label="needs attention" />}
            </NavLink>
          ))}
        </nav>
        <div className="space-y-1 border-t border-slate-200 pt-3 dark:border-slate-800">
          <button
            type="button"
            onClick={toggleTheme}
            className="flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-sm text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-900"
          >
            <Icon name={theme === "dark" ? "sun" : "moon"} /> {theme === "dark" ? "Light look" : "Dark look"}
          </button>
          <button
            type="button"
            onClick={() => {
              setToken(null);
            }}
            className="flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-sm text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-900"
          >
            <Icon name="logout" /> Log out
          </button>
        </div>
      </aside>

      {/* Top bar: mode, connection and the emergency stop are always visible */}
      <header className="sticky top-0 z-20 border-b border-slate-200/80 bg-white/90 backdrop-blur dark:border-slate-800 dark:bg-slate-950/90">
        <div className="mx-auto flex max-w-6xl items-center gap-2 px-4 py-2.5">
          <div className="lg:hidden">
            <Logo />
          </div>
          <div className="ml-auto flex flex-wrap items-center justify-end gap-2 lg:ml-0">
            <ModeBadge mode={status?.mode} />
            <Connection />
            {planMissing && (
              <NavLink
                to="/plan"
                className="hidden items-center gap-1.5 rounded-full bg-amber-100 px-2.5 py-1 text-xs font-semibold text-amber-900 sm:inline-flex dark:bg-amber-500/15 dark:text-amber-200"
              >
                <Icon name="plan" className="h-3.5 w-3.5" /> Submit today&apos;s plan
              </NavLink>
            )}
            {status?.blocked_reason && (
              <span
                className="inline-flex items-center gap-1.5 rounded-full bg-red-100 px-2.5 py-1 text-xs font-semibold text-red-800 dark:bg-red-500/15 dark:text-red-200"
                title={status.blocked_reason}
              >
                <Icon name="alert" className="h-3.5 w-3.5" /> Needs a check
              </span>
            )}
          </div>
          <div className="ml-auto hidden sm:block">
            <KillSwitch state={status?.kill_switch} />
          </div>
          <div className="sm:hidden">
            <KillSwitch state={status?.kill_switch} compact />
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-6xl px-4 pb-28 pt-5 lg:pb-10">
        <Outlet />
      </main>

      {/* Phone tab bar */}
      <nav
        aria-label="Tabs"
        className="fixed inset-x-0 bottom-0 z-30 grid grid-cols-5 border-t border-slate-200 bg-white/95 pb-[env(safe-area-inset-bottom)] backdrop-blur lg:hidden dark:border-slate-800 dark:bg-slate-950/95"
      >
        {TABS.map((t) => (
          <NavLink
            key={t.to}
            to={t.to}
            end={t.to === "/"}
            className={({ isActive }) =>
              `relative flex flex-col items-center gap-0.5 py-2 text-[11px] font-medium ${
                isActive ? "text-brand-600 dark:text-brand-200" : "text-slate-500 dark:text-slate-400"
              }`
            }
          >
            <Icon name={t.icon} className="h-6 w-6" />
            {t.label}
            {t.to === "/plan" && planMissing && <span className="absolute right-[30%] top-1.5 h-2 w-2 rounded-full bg-amber-500" />}
          </NavLink>
        ))}
      </nav>
    </div>
  );
}
