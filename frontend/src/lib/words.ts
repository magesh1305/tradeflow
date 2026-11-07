// Turns broker codes into plain English for the screens.

import type { StrategySummary } from "../api/types";

export function productWord(product: string): string {
  if (product === "MIS") return "Intraday";
  if (product === "NRML") return "Carry forward";
  return product;
}

/** "1 lot", "3 lots", or "75 units" when the quantity is not whole lots. */
export function lotsText(quantity: number, lotSize: number): string {
  const q = Math.abs(quantity);
  if (lotSize > 1 && q % lotSize === 0) {
    const n = q / lotSize;
    return `${n} lot${n === 1 ? "" : "s"}`;
  }
  return `${q} unit${q === 1 ? "" : "s"}`;
}

/** Past-tense verb for a fill: "bought" / "sold". */
export function sideVerb(side: "BUY" | "SELL"): string {
  return side === "BUY" ? "bought" : "sold";
}

/** A position's direction in words. */
export function directionWord(quantity: number): string {
  return quantity > 0 ? "Bought" : "Sold";
}

export function robotNames(strategies: StrategySummary[] | undefined): (id: string) => string {
  const map = new Map((strategies ?? []).map((s) => [s.id, s.title || s.id]));
  return (id) => map.get(id) ?? id;
}

export function orderTypeWord(type: string, protective: boolean): string {
  if (protective) return "Stop loss";
  return { MARKET: "At market price", LIMIT: "At a set price", SL: "Stop loss", "SL-M": "Stop loss (market)" }[type] ?? type;
}
