import { NavLink, Outlet } from "react-router-dom";
import { setToken } from "../api/token";
import { KillSwitch } from "../components/KillSwitch";
import { ModeBadge } from "../components/ModeBadge";
import { Button } from "../components/ui";
import { useLive } from "./live";
import { useTheme } from "./theme";

const links = [
  ["/", "Dashboard"],
  ["/strategies", "Strategies"],
  ["/option-chain", "Option chain"],
  ["/orders", "Orders"],
  ["/trades", "Trades"],
  ["/pnl", "Daily P&L"],
  ["/backtests", "Backtests"],
  ["/risk", "Risk"],
] as const;

function Connection() {
  const { connected } = useLive();
  return connected ? (
    <span className="flex items-center gap-1.5 text-xs text-emerald-600 dark:text-emerald-400">
      <span className="h-2 w-2 rounded-full bg-emerald-500" /> live
    </span>
  ) : (
    <span role="status" className="flex items-center gap-1.5 rounded bg-amber-100 px-2 py-0.5 text-xs font-semibold text-amber-900 dark:bg-amber-900/60 dark:text-amber-100">
      <span className="h-2 w-2 animate-pulse rounded-full bg-amber-500" /> DISCONNECTED: reconnecting
    </span>
  );
}

export function Layout() {
  const { status } = useLive();
  const [theme, toggleTheme] = useTheme();
  return (
    <div className="min-h-screen pb-20 sm:pb-6">
      <header className="sticky top-0 z-30 border-b border-slate-200 bg-white/95 backdrop-blur dark:border-slate-800 dark:bg-slate-950/95">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-3 px-3 py-2">
          <ModeBadge mode={status?.mode} />
          <span className="font-semibold">tradeflow</span>
          <Connection />
          {status?.blocked_reason && (
            <span className="rounded bg-red-100 px-2 py-0.5 text-xs font-semibold text-red-800 dark:bg-red-900/60 dark:text-red-100" title={status.blocked_reason}>
              ENGINE BLOCKED
            </span>
          )}
          <div className="ml-auto flex items-center gap-2">
            <div className="hidden sm:block">
              <KillSwitch state={status?.kill_switch} />
            </div>
            <Button variant="ghost" onClick={toggleTheme} aria-label="Toggle dark mode">
              {theme === "dark" ? "☀" : "☾"}
            </Button>
            <Button
              variant="ghost"
              onClick={() => {
                setToken(null);
              }}
            >
              Log out
            </Button>
          </div>
        </div>
        <nav className="mx-auto flex max-w-7xl gap-1 overflow-x-auto px-3 pb-2 text-sm">
          {links.map(([to, label]) => (
            <NavLink
              key={to}
              to={to}
              end={to === "/"}
              className={({ isActive }) =>
                `whitespace-nowrap rounded-md px-3 py-1.5 ${isActive ? "bg-sky-100 font-semibold text-sky-900 dark:bg-sky-900/50 dark:text-sky-100" : "text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800"}`
              }
            >
              {label}
            </NavLink>
          ))}
        </nav>
      </header>
      <main className="mx-auto max-w-7xl px-3 py-4">
        <Outlet />
      </main>
      {/* Phone: the kill switch is always one thumb away. */}
      <div className="fixed inset-x-0 bottom-0 z-30 border-t border-slate-200 bg-white p-2 pb-[max(0.5rem,env(safe-area-inset-bottom))] sm:hidden dark:border-slate-800 dark:bg-slate-950">
        <KillSwitch state={status?.kill_switch} compact />
      </div>
    </div>
  );
}
