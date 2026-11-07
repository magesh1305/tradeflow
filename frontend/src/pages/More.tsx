// Phones: everything that doesn't fit in the bottom tab bar.

import { Link } from "react-router-dom";
import { setToken } from "../api/token";
import { useTheme } from "../app/theme";
import { Icon, type IconName } from "../components/Icon";
import { PageTitle } from "../components/ui";

const ITEMS: { to: string; icon: IconName; title: string; text: string }[] = [
  { to: "/history", icon: "chart", title: "Profit history", text: "How each day went" },
  { to: "/option-chain", icon: "table", title: "Option prices", text: "Live Call and Put prices" },
  { to: "/backtests", icon: "flask", title: "Test on past data", text: "Try a robot on old prices" },
  { to: "/safety", icon: "shield", title: "Safety limits", text: "Rules every order must pass" },
  { to: "/help", icon: "help", title: "How it works", text: "Guide, words and questions" },
];

export function MorePage() {
  const [theme, toggleTheme] = useTheme();
  return (
    <div className="space-y-5">
      <PageTitle title="More" />
      <div className="grid grid-cols-2 gap-3">
        {ITEMS.map((i) => (
          <Link
            key={i.to}
            to={i.to}
            className="rounded-2xl border border-slate-200/80 bg-white p-4 shadow-sm dark:border-slate-800 dark:bg-slate-900"
          >
            <span className="grid h-10 w-10 place-items-center rounded-xl bg-brand-50 text-brand-600 dark:bg-brand-500/15 dark:text-brand-200">
              <Icon name={i.icon} />
            </span>
            <div className="mt-3 font-semibold text-slate-900 dark:text-white">{i.title}</div>
            <div className="text-xs text-slate-500 dark:text-slate-400">{i.text}</div>
          </Link>
        ))}
      </div>
      <div className="divide-y divide-slate-100 rounded-2xl border border-slate-200/80 bg-white dark:divide-slate-800 dark:border-slate-800 dark:bg-slate-900">
        <button type="button" onClick={toggleTheme} className="flex w-full items-center gap-3 p-4 text-left">
          <Icon name={theme === "dark" ? "sun" : "moon"} /> {theme === "dark" ? "Switch to light look" : "Switch to dark look"}
        </button>
        <button type="button" onClick={() => { setToken(null); }} className="flex w-full items-center gap-3 p-4 text-left text-red-600">
          <Icon name="logout" /> Log out
        </button>
      </div>
    </div>
  );
}
