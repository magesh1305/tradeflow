import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ConnectionInfo } from "../api/types";
import { ToastProvider } from "../app/toasts";
import { ConnectionPage } from "./Connection";

const base: ConnectionInfo = {
  mode: "paper",
  feed: "sim",
  storage: "memory",
  env_file: "backend/.env",
  kite: {
    api_key_set: false,
    api_secret_set: false,
    user_id_set: false,
    redirect_url: "http://localhost:5180/api/auth/callback",
    logged_in: false,
    user_id: "",
    expires_at: "",
    session_error: "",
  },
  database: { used: false, ok: null, detail: "not used" },
  live_ready: false,
  live_checks: [
    { label: "MODE=live", ok: false, fix: "Set MODE=live in the server's .env" },
    { label: "Server's static IP whitelisted in the Kite console", ok: null, fix: "Whitelist it" },
  ],
};

function json(status: number, body: unknown) {
  return Promise.resolve({ ok: status < 400, status, statusText: "", json: () => Promise.resolve(body) });
}

function show(info: ConnectionInfo, test?: unknown) {
  vi.stubGlobal("fetch", vi.fn((url: string) =>
    url.endsWith("/connection/kite") ? json(200, test) : json(200, info)));
  render(
    <ToastProvider>
      <QueryClientProvider client={new QueryClient()}>
        <ConnectionPage />
      </QueryClientProvider>
    </ToastProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Connection check", () => {
  it("explains where Kite details go and what live mode still needs", async () => {
    show(base);
    expect(await screen.findByText("Where do I put my Kite Connect details?")).toBeInTheDocument();
    expect(screen.getAllByText(/localhost:5180\/api\/auth\/callback/).length).toBeGreaterThan(0);
    expect(screen.getByText("Set MODE=live in the server's .env")).toBeInTheDocument();
    expect(screen.getAllByText("Check yourself").length).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: "Test connection" })).not.toBeInTheDocument();
  });

  it("tests a logged-in Zerodha session without placing orders", async () => {
    show(
      { ...base, kite: { ...base.kite, api_key_set: true, api_secret_set: true, logged_in: true, user_id: "AB1234",
        expires_at: "2026-11-10T06:00:00+05:30" } },
      { ok: true, detail: "Connected to Zerodha. Market data works; no order was placed.", user_id: "AB1234",
        user_name: "Test User", broker: "ZERODHA" },
    );
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Test connection" }));
    expect(await screen.findByText(/no order was placed/)).toHaveTextContent("Test User (AB1234)");
  });
});
