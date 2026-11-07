import { useState } from "react";
import { ApiError, request } from "../api/client";
import { setToken } from "../api/token";
import type { Status } from "../api/types";
import { Button, Card, inputClass } from "../components/ui";

/** Admin token login. The token is checked against GET /status before it is kept. */
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
      setError(e instanceof ApiError && e.status === 401 ? "Wrong admin token." :
        e instanceof ApiError && e.status === 503 ? "" : "Backend unreachable.");
      if (e instanceof ApiError && e.status === 503) setToken(value.trim()); // engine down: still log in
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto mt-16 max-w-sm px-3">
      <Card title="tradeflow">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void submit();
          }}
        >
          <label className="mb-1 block text-sm" htmlFor="token">Admin token</label>
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
          <p className="mt-2 text-xs text-slate-500">Kept for this browser tab only (sessionStorage).</p>
          {error && <p role="alert" className="mt-2 text-sm text-rose-600">{error}</p>}
          <Button type="submit" variant="primary" className="mt-3 w-full" disabled={!value.trim() || busy}>
            {busy ? "Checking…" : "Log in"}
          </Button>
        </form>
      </Card>
    </div>
  );
}
