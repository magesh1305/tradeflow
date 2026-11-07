import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, SERVER_UNREACHABLE, api, buildUrl, request, wsUrl } from "./client";
import { getToken, setToken } from "./token";

function mockFetch(status: number, body: unknown, ok = status < 400) {
  const fn = vi.fn().mockResolvedValue({
    ok,
    status,
    statusText: "status text",
    json: () => Promise.resolve(body),
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}

afterEach(() => {
  vi.unstubAllGlobals();
  setToken(null);
});

describe("buildUrl", () => {
  it("prefixes the API base and drops empty query values", () => {
    expect(buildUrl("/orders", { from: "2025-11-03", strategy: undefined, mode: "", limit: 10 })).toBe(
      "/api/orders?from=2025-11-03&limit=10",
    );
    expect(buildUrl("/status")).toBe("/api/status");
  });
});

describe("request", () => {
  it("sends the admin token header and parses JSON", async () => {
    setToken("secret-token-123456");
    const fetch = mockFetch(200, { mode: "paper" });
    const res = await request<{ mode: string }>("GET", "/status");
    expect(res.mode).toBe("paper");
    const [url, init] = fetch.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/status");
    expect((init.headers as Record<string, string>)["X-Admin-Token"]).toBe("secret-token-123456");
  });

  it("omits the token for public endpoints", async () => {
    setToken("secret-token-123456");
    const fetch = mockFetch(200, { configured: false, logged_in: false });
    await api.authStatus();
    const init = fetch.mock.calls[0]![1] as RequestInit;
    expect((init.headers as Record<string, string>)["X-Admin-Token"]).toBeUndefined();
  });

  it("serialises JSON bodies for POST/PUT", async () => {
    setToken("t".repeat(20));
    const fetch = mockFetch(200, { tripped: true });
    await api.killSwitch("phone");
    const [url, init] = fetch.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/kill-switch");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ reason: "phone" });
    expect((init.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
  });

  it("builds strategy and filter URLs", async () => {
    const fetch = mockFetch(200, []);
    await api.stopStrategy("ema 1", true);
    await api.fills({ from: "2025-11-01", to: "2025-11-03", strategy: "ema", mode: "live" });
    expect(fetch.mock.calls[0]![0]).toBe("/api/strategies/ema%201/stop?exit_positions=true");
    expect(fetch.mock.calls[1]![0]).toBe(
      "/api/fills?from=2025-11-01&to=2025-11-03&strategy=ema&mode=live",
    );
  });

  it("clears the token on 401", async () => {
    setToken("wrong-token-1234567");
    mockFetch(401, { detail: "invalid admin token" });
    await expect(request("GET", "/status")).rejects.toMatchObject({ status: 401 });
    expect(getToken()).toBeNull();
  });

  it("turns FastAPI error details into readable messages", async () => {
    mockFetch(409, { detail: "stop the strategy before changing its params" });
    await expect(api.setParams("s", {})).rejects.toThrow("stop the strategy before changing its params");
    mockFetch(422, {
      detail: [{ loc: ["body", "params", "fast"], msg: "Input should be a valid integer" }],
    });
    const err = await request("PUT", "/strategies/s/params").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).message).toBe("params.fast: Input should be a valid integer");
    expect((err as ApiError).status).toBe(422);
  });

  it("reports an unreachable backend as status 0", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    await expect(request("GET", "/status")).rejects.toMatchObject({ status: 0, message: SERVER_UNREACHABLE });
  });

  it("explains a 502 from the proxy when the backend is down", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 502, statusText: "Bad Gateway",
      json: () => Promise.reject(new Error("html")) }));
    await expect(request("GET", "/status")).rejects.toMatchObject({ status: 502, message: SERVER_UNREACHABLE });
  });
});

describe("token storage", () => {
  it("uses sessionStorage, never localStorage", () => {
    setToken("abc-token-1234567890");
    expect(sessionStorage.getItem("tradeflow.adminToken")).toBe("abc-token-1234567890");
    expect(localStorage.getItem("tradeflow.adminToken")).toBeNull();
    setToken(null);
    expect(sessionStorage.getItem("tradeflow.adminToken")).toBeNull();
  });
});

describe("wsUrl", () => {
  it("uses the page host and encodes the token", () => {
    expect(wsUrl("a b&c")).toBe(`ws://${window.location.host}/ws?token=a%20b%26c`);
  });
});
