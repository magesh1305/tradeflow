import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ToastProvider } from "../app/toasts";
import { toCsv } from "../lib/csv";
import { KillSwitch } from "./KillSwitch";
import { ModeBadge } from "./ModeBadge";
import { coerce } from "./ParamEditor";

function wrap(children: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  return (
    <ToastProvider>
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    </ToastProvider>
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("KillSwitch", () => {
  it("only fires after typing KILL", async () => {
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ tripped: true, reason: "manual", tripped_at: null }),
    });
    vi.stubGlobal("fetch", fetch);
    const user = userEvent.setup();
    render(wrap(<KillSwitch state={{ tripped: false, reason: "", tripped_at: null }} />));
    await user.click(screen.getByRole("button", { name: "EMERGENCY STOP" }));
    const confirm = screen.getByRole("button", { name: "Stop everything" });
    expect(confirm).toBeDisabled();
    await user.type(screen.getByLabelText("Type KILL to confirm"), "kil");
    expect(confirm).toBeDisabled();
    expect(fetch).not.toHaveBeenCalled();
    await user.type(screen.getByLabelText("Type KILL to confirm"), "l"); // auto-uppercased
    expect(confirm).toBeEnabled();
    await user.click(confirm);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect((fetch.mock.calls[0] as [string])[0]).toBe("/api/kill-switch");
  });

  it("shows the active state with a reset button", () => {
    render(wrap(<KillSwitch state={{ tripped: true, reason: "daily loss", tripped_at: null }} />));
    expect(screen.getByText("EMERGENCY STOP ON")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear" })).toBeInTheDocument();
  });
});

describe("ModeBadge", () => {
  it("shows practice mode in blue and real money in red", () => {
    const { rerender } = render(<ModeBadge mode="paper" />);
    expect(screen.getByTestId("mode-badge")).toHaveTextContent("PRACTICE MODE");
    expect(screen.getByTestId("mode-badge").className).toContain("bg-blue-600");
    rerender(<ModeBadge mode="live" />);
    expect(screen.getByTestId("mode-badge")).toHaveTextContent("REAL MONEY");
    expect(screen.getByTestId("mode-badge").className).toContain("bg-red-600");
  });
});

describe("csv", () => {
  it("quotes separators and neutralises formulas", () => {
    const csv = toCsv([{ a: 'x,"y"', b: "=HYPERLINK(1)", c: -5 }], [
      { header: "a", value: (r) => r.a },
      { header: "b", value: (r) => r.b },
      { header: "c", value: (r) => r.c },
    ]);
    expect(csv).toBe('a,b,c\r\n"x,""y""",\'=HYPERLINK(1),-5\r\n');
  });
});

describe("ParamEditor coerce", () => {
  it("converts form strings to schema types", () => {
    const schema = {
      properties: {
        fast: { type: "integer" },
        sl: { type: "number" },
        on: { type: "boolean" },
        entry: { type: "string", format: "time" },
      },
    };
    expect(coerce(schema, { fast: "9", sl: "30.5", on: true, entry: "09:20" })).toEqual({
      fast: 9,
      sl: 30.5,
      on: true,
      entry: "09:20",
    });
  });
});
