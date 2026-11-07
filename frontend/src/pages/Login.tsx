import { useState } from "react";
import { ApiError, request } from "../api/client";
import { setToken } from "../api/token";
import type { Status } from "../api/types";
import { Icon } from "../components/Icon";
import { Button, inputClass } from "../components/ui";

/** Admin password login. The token is checked against GET /status before it is kept. */
export function Login() {
  const [value, setValue] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit() {
    setBusy(true);
    setError("");
    setToken(value.trim());
    try {
      await request<Status>("GET", "/status");
    } catch (e) {
      setToken(null);
      setError(e instanceof ApiError && e.status === 401 ? "That password is not correct." :
        e instanceof ApiError && e.status === 503 ? "" : "Can't reach the trading server. Is it running?");
      if (e instanceof ApiError && e.status === 503) setToken(value.trim()); // engine down: still log in
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-gradient-to-br from-slate-50 via-brand-50 to-indigo-100 px-4 dark:from-slate-950 dark:via-slate-900 dark:to-slate-950">
      <div className="w-full max-w-sm">
        <div className="mb-6 text-center">
          <span className="mx-auto grid h-14 w-14 place-items-center rounded-2xl bg-gradient-to-br from-brand-500 to-indigo-600 text-white shadow-lg">
            <Icon name="activity" className="h-7 w-7" />
          </span>
          <h1 className="mt-4 text-2xl font-bold text-slate-900 dark:text-white">tradeflow</h1>
          <p className="mt-1 text-sm text-slate-600 dark:text-slate-400">Trading robots that follow your daily limits.</p>
        </div>
        <form
          className="rounded-2xl border border-slate-200/80 bg-white p-6 shadow-xl dark:border-slate-800 dark:bg-slate-900"
          onSubmit={(e) => {
            e.preventDefault();
            void submit();
          }}
        >
          <label className="mb-1.5 block text-sm font-medium text-slate-800 dark:text-slate-200" htmlFor="token">Admin password</label>
          <input
            id="token"
            type="password"
            autoComplete="current-password"
            className={inputClass}
            value={value}
            onChange={(e) => {
              setValue(e.target.value);
            }}
          />
          <p className="mt-2 text-xs text-slate-500">The ADMIN_TOKEN from your server settings. Forgotten when you close this tab.</p>
          {error && <p role="alert" className="mt-3 text-sm text-red-600">{error}</p>}
          <Button type="submit" variant="primary" className="mt-4 w-full" disabled={!value.trim() || busy}>
            {busy ? "Checking…" : "Log in"}
          </Button>
        </form>
      </div>
    </div>
  );
}
