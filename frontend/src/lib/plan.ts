// Daily-plan form <-> API conversion. Optional limits are empty strings in the form
// and null in the API ("no limit").

import type { AccountPlan, DailyPlan, StrategyPlan } from "../api/types";

export type AccountForm = Record<keyof AccountPlan, string>;
export type StrategyForm = { enabled: boolean } & Record<Exclude<keyof StrategyPlan, "enabled">, string>;
export interface PlanForm {
  account: AccountForm;
  strategies: Record<string, StrategyForm>;
}

const str = (v: number | null): string => (v === null ? "" : String(v));
const hhmm = (t: string): string => t.slice(0, 5);

export function toForm(plan: DailyPlan): PlanForm {
  const a = plan.account;
  const strategies: Record<string, StrategyForm> = {};
  for (const [id, s] of Object.entries(plan.strategies)) {
    strategies[id] = {
      enabled: s.enabled,
      lots: str(s.lots),
      max_trades_per_day: str(s.max_trades_per_day),
      loss_limit: str(s.loss_limit),
      profit_target: str(s.profit_target),
      max_lots_per_order: str(s.max_lots_per_order),
      max_capital: str(s.max_capital),
    };
  }
  return {
    account: {
      daily_loss_limit: str(a.daily_loss_limit),
      profit_target: str(a.profit_target),
      max_trades_per_day: str(a.max_trades_per_day),
      max_open_positions: str(a.max_open_positions),
      max_lots_per_order: str(a.max_lots_per_order),
      trading_start: hhmm(a.trading_start),
      trading_end: hhmm(a.trading_end),
    },
    strategies,
  };
}

export class FormError extends Error {}

function optional(v: string, label: string, integer: boolean): number | null {
  const t = v.trim();
  if (t === "") return null;
  const n = Number(t);
  if (!Number.isFinite(n) || n <= 0 || (integer && !Number.isInteger(n))) {
    throw new FormError(`${label} must be a positive ${integer ? "whole number" : "number"} or empty`);
  }
  return n;
}

function required(v: string, label: string, integer: boolean): number {
  const n = optional(v, label, integer);
  if (n === null) throw new FormError(`${label} is required`);
  return n;
}

export function fromForm(form: PlanForm): DailyPlan {
  const a = form.account;
  if (!/^\d{2}:\d{2}$/.test(a.trading_start) || !/^\d{2}:\d{2}$/.test(a.trading_end)) {
    throw new FormError("trading start and end must be times (HH:MM)");
  }
  if (a.trading_start >= a.trading_end) throw new FormError("trading start must be before trading end");
  const strategies: Record<string, StrategyPlan> = {};
  for (const [id, s] of Object.entries(form.strategies)) {
    strategies[id] = {
      enabled: s.enabled,
      lots: optional(s.lots, `${id} lots`, true),
      max_trades_per_day: optional(s.max_trades_per_day, `${id} max trades/day`, true),
      loss_limit: optional(s.loss_limit, `${id} loss limit`, false),
      profit_target: optional(s.profit_target, `${id} profit target`, false),
      max_lots_per_order: optional(s.max_lots_per_order, `${id} max lots/order`, true),
      max_capital: optional(s.max_capital, `${id} max capital`, false),
    };
  }
  return {
    account: {
      daily_loss_limit: required(a.daily_loss_limit, "Daily loss limit", false),
      profit_target: optional(a.profit_target, "Profit target", false),
      max_trades_per_day: optional(a.max_trades_per_day, "Max trades/day", true),
      max_open_positions: required(a.max_open_positions, "Max open positions", true),
      max_lots_per_order: required(a.max_lots_per_order, "Max lots/order", true),
      trading_start: a.trading_start,
      trading_end: a.trading_end,
    },
    strategies,
  };
}
