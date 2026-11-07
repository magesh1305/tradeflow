import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { DailyPlan, SettingsInfo } from "../api/types";
import { ToastProvider } from "../app/toasts";
import { FormError, fromForm, toForm } from "../lib/plan";
import { SettingsPage } from "./Settings";

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
    ema: { enabled: true, lots: 1, max_trades_per_day: null, loss_limit: 2000, profit_target: null,
      max_lots_per_order: null, max_capital: null },
  },
};

const info: SettingsInfo = {
  trading_date: "2025-11-03",
  submitted: true,
  submitted_at: "2025-11-03T09:01:00+05:30",
  market_hours: true,
  loosen_confirmation: "LOOSEN",
  plan,
  strategies: [{ id: "ema", strategy: "ema_crossover", state: "RUNNING", has_lots: true }],
};

function json(status: number, body: unknown) {
  return Promise.resolve({ ok: status < 400, status, statusText: "", json: () => Promise.resolve(body) });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("plan form conversion", () => {
  it("round-trips and turns empty optional fields into null", () => {
    const form = toForm(plan);
    expect(form.account.trading_start).toBe("09:15");
    expect(form.account.profit_target).toBe("");
    form.strategies.ema!.loss_limit = "";
    form.account.max_trades_per_day = "";
    const back = fromForm(form);
    expect(back.account.max_trades_per_day).toBeNull();
    expect(back.strategies.ema!.loss_limit).toBeNull();
    expect(back.account.daily_loss_limit).toBe(10000);
    expect(back.account.trading_end).toBe("15:10");
  });

  it("rejects invalid input before it reaches the backend", () => {
    const form = toForm(plan);
    form.account.daily_loss_limit = "";
    expect(() => fromForm(form)).toThrow(FormError);
    const f2 = toForm(plan);
    f2.strategies.ema!.lots = "1.5";
    expect(() => fromForm(f2)).toThrow("whole number");
    const f3 = toForm(plan);
    f3.account.trading_start = "16:00";
    expect(() => fromForm(f3)).toThrow("before trading end");
    const f4 = toForm(plan);
    f4.strategies.ema!.loss_limit = "-5";
    expect(() => fromForm(f4)).toThrow(FormError);
  });
});

describe("Settings page", () => {
  it("asks for LOOSEN when the backend says limits would loosen, then resubmits", async () => {
    const posted: { plan: DailyPlan; confirm?: string }[] = [];
    const fetch = vi.fn((url: string, init?: RequestInit) => {
      if (url.endsWith("/settings/plan")) {
        const body = JSON.parse(init!.body as string) as { plan: DailyPlan; confirm?: string };
        posted.push(body);
        if (!body.confirm) {
          return json(409, { detail: { message: "needs confirmation",
            loosened: ["account daily loss limit: 10000 -> 20000"] } });
        }
        return json(200, { plan: body.plan, changes: ["x"], loosened: ["y"], started: [], stopped: [],
          warnings: [] });
      }
      if (url.endsWith("/settings/history")) return json(200, []);
      return json(200, info);
    });
    vi.stubGlobal("fetch", fetch);
    const user = userEvent.setup();
    render(
      <ToastProvider>
        <QueryClientProvider client={new QueryClient()}>
          <SettingsPage />
        </QueryClientProvider>
      </ToastProvider>,
    );
    const loss = await screen.findByLabelText(/Daily loss limit/);
    await user.clear(loss);
    await user.type(loss, "20000");
    await user.click(screen.getByRole("button", { name: "Update today's plan" }));

    expect(await screen.findByText("account daily loss limit: 10000 -> 20000")).toBeInTheDocument();
    const confirm = screen.getByRole("button", { name: "Loosen limits" });
    expect(confirm).toBeDisabled();
    await user.type(screen.getByLabelText("Type LOOSEN to confirm"), "loosen");
    expect(confirm).toBeEnabled();
    await user.click(confirm);
    await waitFor(() => {
      expect(posted).toHaveLength(2);
    });
    expect(posted[0]!.plan.account.daily_loss_limit).toBe(20000);
    expect(posted[0]!.confirm).toBeUndefined();
    expect(posted[1]!.confirm).toBe("LOOSEN");
  });

  it("warns when today's plan is not submitted", async () => {
    vi.stubGlobal("fetch", vi.fn((url: string) =>
      url.endsWith("/settings/history") ? json(200, []) : json(200, { ...info, submitted: false, submitted_at: null })));
    render(
      <ToastProvider>
        <QueryClientProvider client={new QueryClient()}>
          <SettingsPage />
        </QueryClientProvider>
      </ToastProvider>,
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("not submitted yet");
    expect(screen.getByRole("button", { name: "Submit today's plan" })).toBeInTheDocument();
  });
});
