// Mirrors backend/app/api/schemas.py. Keep the two in sync.

export type Mode = "paper" | "live";
export type StrategyState = "STOPPED" | "RUNNING" | "ERROR";

export interface Pnl {
  gross: number;
  net: number;
  realized: number;
  unrealized: number;
  charges: number;
}

export interface Position {
  strategy_id: string;
  exchange: string;
  tradingsymbol: string;
  instrument_token: number;
  product: string;
  quantity: number;
  average_price: number;
  last_price: number | null;
  pnl: Pnl;
  protected_stop: number | null;
}

export interface Order {
  client_order_id: string;
  broker_order_id: string | null;
  mode: string;
  strategy_id: string;
  exchange: string;
  tradingsymbol: string;
  side: "BUY" | "SELL";
  quantity: number;
  order_type: string;
  product: string;
  price: number | null;
  trigger_price: number | null;
  status: string;
  filled_quantity: number;
  average_price: number;
  status_message: string;
  reason: string;
  leg_group_id: string | null;
  protective: boolean;
  created_at: string;
  updated_at: string;
}

export interface Fill {
  fill_id: string;
  client_order_id: string;
  broker_order_id: string | null;
  mode: string;
  strategy_id: string;
  exchange: string;
  tradingsymbol: string;
  side: "BUY" | "SELL";
  quantity: number;
  price: number;
  product: string;
  charges: number;
  ts: string;
  trading_date: string;
}

export interface StrategySummary {
  id: string;
  strategy: string;
  state: StrategyState;
  error: string;
  pnl: Pnl;
  open_positions: number;
  open_orders: number;
}

export interface JsonSchemaProperty {
  type?: string;
  format?: string;
  title?: string;
  default?: unknown;
  minimum?: number;
  maximum?: number;
  exclusiveMinimum?: number;
  exclusiveMaximum?: number;
  description?: string;
}

export interface JsonSchema {
  properties?: Record<string, JsonSchemaProperty>;
  required?: string[];
}

export interface StrategyDetail extends StrategySummary {
  params: Record<string, unknown>;
  params_schema: JsonSchema;
  positions: Position[];
  subscribed_tokens: number;
}

export interface KillSwitch {
  tripped: boolean;
  reason: string;
  tripped_at: string | null;
}

export interface Status {
  time: string;
  mode: Mode;
  engine_running: boolean;
  blocked_reason: string;
  feed: { source: string; connected: boolean; detail: string };
  kill_switch: KillSwitch;
  pnl: Pnl;
  positions: Position[];
  open_orders: Order[];
  strategies: StrategySummary[];
  open_leg_groups: number;
  journal: { backlog: number; failures: number; last_error: string };
}

export interface DailyPnl {
  trading_date: string;
  mode: string;
  strategy_id: string;
  gross_pnl: number;
  charges: number;
  net_pnl: number;
  trades: number;
  live: boolean;
}

export interface Risk {
  limits: Record<string, unknown>;
  usage: {
    orders_last_minute: number;
    open_positions: number;
    net_pnl: number;
    daily_loss_used: number;
    daily_loss_used_pct: number;
    rejections_today: number;
    open_orders: number;
  };
  kill_switch: KillSwitch;
  margin: Record<string, unknown> | null;
}

export interface Greeks {
  iv: number;
  delta: number;
  gamma: number;
  theta: number;
  vega: number;
}

export interface OptionQuote {
  tradingsymbol: string;
  instrument_token: number;
  ltp: number | null;
  bid: number | null;
  ask: number | null;
  oi: number;
  volume: number;
  greeks: Greeks | null;
}

export interface ChainRow {
  strike: number;
  is_atm: boolean;
  call: OptionQuote | null;
  put: OptionQuote | null;
}

export interface OptionChain {
  underlying: string;
  expiry: string | null;
  expiries: string[];
  spot: number | null;
  rows: ChainRow[];
  note: string;
}

export interface AuthStatus {
  configured: boolean;
  logged_in: boolean;
  user_id?: string;
  login_time?: string;
  expires_at?: string;
}

export interface BacktestMetrics {
  gross_pnl: number;
  charges: number;
  net_pnl: number;
  trades: number;
  wins: number;
  losses: number;
  win_rate: number | null;
  avg_win: number | null;
  avg_loss: number | null;
  max_drawdown: number;
  max_drawdown_pct: number;
  sharpe: number | null;
  days: number;
  fills: number;
  notes?: string[];
  non_trading_days?: string[];
  trades_list?: Record<string, unknown>[];
}

export interface BacktestRun {
  id: number;
  status: "running" | "done" | "failed";
  strategy: string;
  params: Record<string, unknown>;
  start_date: string;
  end_date: string;
  created_at: string;
  finished_at: string | null;
  metrics: BacktestMetrics | null;
  error: string | null;
}

export interface EquityPoint {
  date: string;
  net: number;
  gross: number;
  charges: number;
  open_positions: number;
}

export interface MissingData {
  date: string;
  token: number;
  symbol: string;
  expected_candles: number;
  found_candles: number;
}

export interface BacktestDetail extends BacktestRun {
  equity_curve: EquityPoint[] | null;
  missing_data: MissingData[] | null;
}

export interface BacktestInput {
  strategy: string;
  params: Record<string, unknown>;
  start: string;
  end: string;
  interval: "minute" | "3minute" | "5minute" | "10minute" | "15minute" | "30minute" | "60minute";
}

export interface ReportFilter {
  from?: string;
  to?: string;
  strategy?: string;
  mode?: Mode;
  limit?: number;
}
