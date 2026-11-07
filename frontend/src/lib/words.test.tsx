import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { Term } from "../components/Term";
import { Money } from "../components/ui";
import { lotsText, orderTypeWord, productWord, robotNames } from "./words";

describe("plain words", () => {
  it("translates broker codes", () => {
    expect(productWord("MIS")).toBe("Intraday");
    expect(productWord("NRML")).toBe("Carry forward");
    expect(orderTypeWord("MARKET", false)).toBe("At market price");
    expect(orderTypeWord("SL", true)).toBe("Stop loss");
  });

  it("counts lots, falling back to units", () => {
    expect(lotsText(75, 75)).toBe("1 lot");
    expect(lotsText(-150, 75)).toBe("2 lots");
    expect(lotsText(10, 75)).toBe("10 units");
    expect(lotsText(1, 1)).toBe("1 unit");
  });

  it("names robots by their title", () => {
    const name = robotNames([{ id: "ema", title: "Trend follower" } as never]);
    expect(name("ema")).toBe("Trend follower");
    expect(name("unknown")).toBe("unknown");
  });
});

describe("Money", () => {
  it("never relies on colour alone", () => {
    const { rerender } = render(<Money value={-1200} word />);
    expect(screen.getByText(/1,200/).textContent).toContain("−");
    expect(screen.getByText("loss")).toBeInTheDocument();
    rerender(<Money value={500} word />);
    expect(screen.getByText(/500/).textContent).toContain("+");
    expect(screen.getByText("profit")).toBeInTheDocument();
  });
});

describe("Term", () => {
  it("explains a word when tapped", async () => {
    const user = userEvent.setup();
    render(<Term k="lot">lot</Term>);
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "lot" }));
    expect(screen.getByRole("tooltip")).toHaveTextContent("minimum quantity");
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  });
});

describe("toasts", () => {
  it("shows the same message only once in a short time", async () => {
    const { ToastProvider, useToast } = await import("../app/toasts");
    function Spam() {
      const t = useToast();
      return (
        <button type="button" onClick={() => { t.push("server down", "error"); }}>
          spam
        </button>
      );
    }
    const user = userEvent.setup();
    render(<ToastProvider><Spam /></ToastProvider>);
    for (let i = 0; i < 5; i++) await user.click(screen.getByRole("button", { name: "spam" }));
    expect(screen.getAllByText("server down")).toHaveLength(1);
  });
});
