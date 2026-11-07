import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { DailyPlan, SettingsInfo } from "../api/types";
import { ToastProvider } from "../app/toasts";
import { startGuideSeen } from "../lib/onboarding";
import { SettingsPage } from "./Settings";
import { StartPage } from "./Start";

const plan: DailyPlan = {
  account: {
    daily_loss_limit: 10000,
    profit_target: null,
    max_trades_per_day: 5,
    max_open_positions: 6,
    max_lots_per_order: 10,
    trading_start: "09:15:00",
    trading_end: "15:10:00",
  },
  strategies: {
    ema: { enabled: false, lots: 1, max_trades_per_day: null, loss_limit: null, profit_target: null,
      max_lots_per_order: null, max_capital: null },
    straddle: { enabled: true, lots: 1, max_trades_per_day: null, loss_limit: null, profit_target: null,
      max_lots_per_order: null, max_capital: null },
  },
};

const info: SettingsInfo = {
  trading_date: "2026-11-09",
  submitted: false,
  submitted_at: null,
  market_hours: false,
  loosen_confirmation: "LOOSEN",
  plan,
  strategies: [
    { id: "ema", strategy: "ema_crossover", state: "STOPPED", has_lots: true, title: "Trend follower", risk_level: "medium" },
    { id: "straddle", strategy: "short_straddle", state: "STOPPED", has_lots: true, title: "Quiet-day option seller", risk_level: "high" },
  ],
};

function json(status: number, body: unknown) {
  return Promise.resolve({ ok: status < 400, status, statusText: "", json: () => Promise.resolve(body) });
}

function stubApi(posted: DailyPlan[]) {
  vi.stubGlobal("fetch", vi.fn((url: string, init?: RequestInit) => {
    if (url.endsWith("/settings/plan")) {
      const body = JSON.parse(init!.body as string) as { plan: DailyPlan };
      posted.push(body.plan);
      return json(200, { plan: body.plan, changes: [], loosened: [], started: ["ema"], stopped: ["straddle"], warnings: [] });
    }
    if (url.endsWith("/settings/history")) return json(200, []);
    if (url.endsWith("/auth/status")) return json(200, { configured: false, logged_in: false });
    return json(200, info);
  }));
}

function wrap(children: React.ReactNode) {
  return (
    <ToastProvider>
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>{children}</MemoryRouter>
      </QueryClientProvider>
    </ToastProvider>
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe("Start here guide", () => {
  it("walks a beginner to a submitted plan", async () => {
    const posted: DailyPlan[] = [];
    stubApi(posted);
    const user = userEvent.setup();
    render(wrap(<StartPage />));
    expect(startGuideSeen()).toBe(true);
    expect(screen.getByText("Welcome to tradeflow")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Next/ }));
    expect(screen.getByText("You are in practice mode")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Next/ }));
    expect(await screen.findByText(/Nothing to do now/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Next/ }));

    await user.click(await screen.findByRole("button", { name: "₹1,000" }));
    await user.click(screen.getByRole("checkbox", { name: /Trend follower/ }));
    await user.click(screen.getByRole("checkbox", { name: /Quiet-day option seller/ }));
    await user.click(screen.getByRole("button", { name: "Submit today's plan" }));

    expect(await screen.findByText("You're all set")).toBeInTheDocument();
    expect(posted).toHaveLength(1);
    expect(posted[0]!.account.daily_loss_limit).toBe(1000);
    expect(posted[0]!.account.max_trades_per_day).toBe(5); // untouched
    expect(posted[0]!.strategies.ema!.enabled).toBe(true);
    expect(posted[0]!.strategies.straddle!.enabled).toBe(false);
  });
});

describe("Plan page", () => {
  it("starts simple and keeps every limit under Advanced", async () => {
    stubApi([]);
    const user = userEvent.setup();
    render(wrap(<SettingsPage />));
    expect(await screen.findByLabelText(/Daily loss limit/)).toBeInTheDocument();
    expect(screen.queryByText("Most trades open at once")).not.toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Advanced" }));
    expect(screen.getByText("Most trades open at once")).toBeInTheDocument();
  });

  it("rejects an empty loss limit before calling the server", async () => {
    const posted: DailyPlan[] = [];
    stubApi(posted);
    const user = userEvent.setup();
    render(wrap(<SettingsPage />));
    await user.clear(await screen.findByLabelText(/Daily loss limit/));
    await user.click(screen.getByRole("button", { name: "Submit today's plan" }));
    expect(screen.getByText(/Enter the most you can afford to lose/)).toBeInTheDocument();
    await waitFor(() => { expect(posted).toHaveLength(0); });
  });
});
