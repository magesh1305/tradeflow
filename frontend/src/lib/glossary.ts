// Plain-English explanations of every trading word the app shows. Used by <Term>
// (tap a dotted word to read it) and listed in full on the Help page.

export interface GlossaryEntry {
  term: string;
  short: string; // one line
  long?: string;
}

export const GLOSSARY = {
  paper: {
    term: "Practice mode (paper trading)",
    short: "Trades use pretend money. Prices can be real, but no order ever reaches Zerodha.",
    long: "Use practice mode for weeks before risking real money. Everything works the same way, so you can see how the robots would have done.",
  },
  live: {
    term: "Real money mode (live)",
    short: "Robots send real orders to your Zerodha account. Profits and losses are real.",
  },
  robot: {
    term: "Robot (strategy)",
    short: "A set of rules that decides when to buy and sell, and does it automatically.",
  },
  nifty: {
    term: "NIFTY",
    short: "India's main stock-market index: one number (around 24,000) that follows the 50 biggest companies.",
  },
  future: {
    term: "Future",
    short: "A contract that moves rupee-for-rupee with NIFTY. If NIFTY rises 100 points, 1 lot gains 100 × lot size.",
  },
  option: {
    term: "Option (Call / Put)",
    short: "A contract whose price rises when NIFTY moves one way. A Call gains when NIFTY goes up; a Put gains when it goes down.",
  },
  call: { term: "Call option", short: "Gains value when NIFTY goes up." },
  put: { term: "Put option", short: "Gains value when NIFTY goes down." },
  lot: {
    term: "Lot",
    short: "The minimum quantity you can trade. For NIFTY, 1 lot is a fixed number of units (e.g. 75).",
  },
  buy: {
    term: "Buy (long)",
    short: "You profit if the price goes up after you buy.",
  },
  sell: {
    term: "Sell (short)",
    short: "Selling first: you profit if the price goes down, and buy it back later.",
  },
  booked: {
    term: "Booked profit / loss",
    short: "Money already made or lost on trades that are closed. It won't change any more.",
  },
  open_pnl: {
    term: "Open profit / loss",
    short: "Profit or loss on trades still running. It changes with the price until they close.",
  },
  charges: {
    term: "Charges",
    short: "Brokerage, taxes (STT, GST) and exchange fees paid on every trade. Always subtracted.",
  },
  net: {
    term: "After charges (net)",
    short: "Profit or loss after all charges. This is what really reaches your pocket.",
  },
  stop_loss: {
    term: "Stop loss",
    short: "An automatic exit that closes a trade when the loss reaches a set level, so it can't grow further.",
  },
  emergency_stop: {
    term: "Emergency stop (kill switch)",
    short: "Stops every robot, cancels waiting orders and closes all trades at once.",
  },
  plan: {
    term: "Today's plan",
    short: "The limits you set every morning: how much you may lose, how many trades, which robots run.",
  },
  loss_limit: {
    term: "Daily loss limit",
    short: "If today's loss reaches this amount, everything stops automatically.",
  },
  profit_target: {
    term: "Profit target",
    short: "If today's profit reaches this amount, no new trades are taken. Open trades keep running.",
  },
  intraday: {
    term: "Intraday (MIS)",
    short: "Trades closed the same day. Robots close all intraday trades by 3:15 PM.",
  },
  carry: {
    term: "Carry forward (NRML)",
    short: "Trades that can stay open overnight.",
  },
  margin: {
    term: "Margin",
    short: "Money Zerodha blocks from your account while a trade is open, as a safety deposit.",
  },
  square_off: {
    term: "Close all trades (square off)",
    short: "Exit every open trade now.",
  },
  backtest: {
    term: "Test on past data (backtest)",
    short: "Runs a robot on old prices to see how it would have done. Past results don't guarantee future ones.",
  },
  option_chain: {
    term: "Option prices (option chain)",
    short: "A table of all Call and Put prices for different NIFTY levels.",
  },
  atm: {
    term: "At the money (ATM)",
    short: "The option level closest to where NIFTY is right now.",
  },
  greeks: {
    term: "Delta, IV and other Greeks",
    short: "Numbers describing how an option's price reacts. Delta: how much it moves when NIFTY moves 1 point (0.5 = half).",
  },
  drawdown: {
    term: "Worst dip (max drawdown)",
    short: "The biggest fall from a high point to a low point. Shows how bad a losing streak got.",
  },
  win_rate: {
    term: "Win rate",
    short: "Out of 100 trades, how many made money.",
  },
} satisfies Record<string, GlossaryEntry>;

export type GlossaryKey = keyof typeof GLOSSARY;
