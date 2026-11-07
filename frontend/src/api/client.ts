// The single typed API client. Every backend call goes through `request`.

import { getToken, setToken } from "./token";
import type {
  AuthStatus,
  BacktestDetail,
  BacktestInput,
  BacktestRun,
  DailyPnl,
  Fill,
  KillSwitch,
  DailyPlan,
  OptionChain,
  Order,
  PlanEvent,
  PlanResult,
  ReportFilter,
  Risk,
  SettingsInfo,
  Status,
  StrategyDetail,
  StrategySummary,
} from "./types";

export const API_BASE: string = (import.meta.env.VITE_API_BASE as string | undefined) ?? "/api";

export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(status: number, message: string, detail?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

type Query = Record<string, string | number | boolean | undefined | null>;

export function buildUrl(path: string, query?: Query): string {
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(query ?? {})) {
    if (v !== undefined && v !== null && v !== "") params.set(k, String(v));
  }
  const qs = params.toString();
  return `${API_BASE}${path}${qs ? `?${qs}` : ""}`;
}

/** Shown when the backend can't be reached (not running, restarting, or the proxy can't connect). */
export const SERVER_UNREACHABLE =
  "Can't reach the trading server. It may be stopped or restarting. If you use one-click practice, double-click Start tradeflow.";

function describe(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    // FastAPI / pydantic validation errors: [{loc: [...], msg: "..."}]
    return detail
      .map((d: { loc?: unknown[]; msg?: string }) =>
        [d.loc?.slice(1).join("."), d.msg].filter(Boolean).join(": "),
      )
      .join("; ");
  }
  return "request failed";
}

export async function request<T>(
  method: "GET" | "POST" | "PUT",
  path: string,
  opts: { query?: Query; body?: unknown; auth?: boolean; signal?: AbortSignal } = {},
): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (opts.auth !== false) {
    const token = getToken();
    if (token) headers["X-Admin-Token"] = token;
  }
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  let res: Response;
  try {
    res = await fetch(buildUrl(path, opts.query), {
      method,
      headers,
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
      signal: opts.signal,
    });
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") throw e;
    throw new ApiError(0, SERVER_UNREACHABLE);
  }
  if (res.status === 401) {
    setToken(null); // wrong or rotated token: back to the login screen
    throw new ApiError(401, "invalid admin token");
  }
  if (res.status === 502 || res.status === 504) {
    // The dev server / nginx answers these when the backend process is down.
    throw new ApiError(res.status, SERVER_UNREACHABLE);
  }
  if (!res.ok) {
    let detail: unknown = res.statusText;
    try {
      detail = ((await res.json()) as { detail?: unknown }).detail ?? detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, describe(detail), detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

const filterQuery = (f: ReportFilter = {}): Query => ({ ...f });

export const api = {
  health: () => request<{ status: string; mode: string }>("GET", "/health", { auth: false }),
  authStatus: () => request<AuthStatus>("GET", "/auth/status", { auth: false }),
  kiteLoginUrl: (): string => `${API_BASE}/auth/login`,

  status: () => request<Status>("GET", "/status"),

  strategies: () => request<StrategySummary[]>("GET", "/strategies"),
  strategy: (id: string) => request<StrategyDetail>("GET", `/strategies/${encodeURIComponent(id)}`),
  startStrategy: (id: string) =>
    request<StrategyDetail>("POST", `/strategies/${encodeURIComponent(id)}/start`),
  stopStrategy: (id: string, exitPositions: boolean) =>
    request<StrategyDetail>("POST", `/strategies/${encodeURIComponent(id)}/stop`, {
      query: { exit_positions: exitPositions },
    }),
  setParams: (id: string, params: Record<string, unknown>) =>
    request<StrategyDetail>("PUT", `/strategies/${encodeURIComponent(id)}/params`, {
      body: { params },
    }),

  orders: (f?: ReportFilter) => request<Order[]>("GET", "/orders", { query: filterQuery(f) }),
  fills: (f?: ReportFilter) => request<Fill[]>("GET", "/fills", { query: filterQuery(f) }),
  dailyPnl: (f?: ReportFilter) =>
    request<DailyPnl[]>("GET", "/pnl/daily", { query: filterQuery(f) }),
  risk: () => request<Risk>("GET", "/risk"),
  optionChain: (underlying: string, expiry?: string, width?: number) =>
    request<OptionChain>("GET", `/option-chain/${encodeURIComponent(underlying)}`, {
      query: { expiry, width },
    }),

  squareOff: () => request<Status>("POST", "/square-off"),
  killSwitch: (reason: string) => request<KillSwitch>("POST", "/kill-switch", { body: { reason } }),
  resetKillSwitch: () => request<KillSwitch>("POST", "/kill-switch/reset"),
  resolveRecovery: (adoptBrokerPositions: boolean) =>
    request<Record<string, unknown>>("POST", "/recovery/resolve", {
      body: { adopt_broker_positions: adoptBrokerPositions, confirm: "RESOLVE" },
    }),

  settings: () => request<SettingsInfo>("GET", "/settings"),
  submitPlan: (plan: DailyPlan, confirm?: string) =>
    request<PlanResult>("POST", "/settings/plan", { body: { plan, confirm } }),
  planHistory: () => request<PlanEvent[]>("GET", "/settings/history"),

  backtests: () => request<BacktestRun[]>("GET", "/backtests"),
  backtest: (id: number) => request<BacktestDetail>("GET", `/backtests/${id}`),
  runBacktest: (input: BacktestInput) => request<BacktestRun>("POST", "/backtests", { body: input }),
};

export function wsUrl(token: string): string {
  const explicit = import.meta.env.VITE_WS_URL as string | undefined;
  const base =
    explicit ?? `${window.location.protocol === "https:" ? "wss" : "ws"}://${window.location.host}/ws`;
  return `${base}?token=${encodeURIComponent(token)}`;
}
