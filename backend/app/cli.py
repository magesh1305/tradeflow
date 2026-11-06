"""Command line: history download, daily recording, backtests.

  python -m app.cli download-history --underlying NIFTY --from 2026-09-01 --to 2026-10-31
  python -m app.cli download-history --symbol NFO:NIFTY26NOVFUT --interval 5minute --from ...
  python -m app.cli backtest --strategy ema_crossover --params '{"fast": 9, "slow": 21}' \\
      --from 2026-09-01 --to 2026-10-31

Download needs a Kite session (log in through the dashboard first). Only contracts
in today's instrument master can be downloaded; expired options are not available
from the historical API, which is why the daily recording job exists.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from datetime import date, datetime, time
from typing import Any

from app import factory
from app.backtest.runner import BacktestRequest
from app.backtest.store import BacktestStore, SqlBacktestStore, execute
from app.config import Settings
from app.core.clock import IST, now_ist
from app.core.logging import configure_logging
from app.data.historical import (
    INTERVAL_SECONDS,
    HistoryDownloader,
    HistoryStore,
    record_underlying,
)
from app.data.instruments import InstrumentStore
from app.storage.db import make_engine, make_sessionmaker


def _date(s: str) -> date:
    return date.fromisoformat(s)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.cli")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("download-history", help="fetch candles from Kite into the candles table")
    d.add_argument(
        "--underlying",
        action="append",
        default=[],
        help="spot, nearest future and strikes around ATM (repeatable)",
    )
    d.add_argument(
        "--symbol", action="append", default=[], help="exact key, e.g. NFO:NIFTY26NOVFUT"
    )
    d.add_argument("--from", dest="start", type=_date, required=True)
    d.add_argument("--to", dest="end", type=_date, required=True)
    d.add_argument("--interval", default="minute", choices=sorted(INTERVAL_SECONDS))
    d.add_argument("--width", type=int, default=10, help="strikes either side of ATM")
    d.add_argument("--expiries", type=int, default=2)

    b = sub.add_parser("backtest", help="run a strategy over stored candles")
    b.add_argument("--strategy", required=True)
    b.add_argument("--params", default="{}", help="JSON object of strategy params")
    b.add_argument("--from", dest="start", type=_date, required=True)
    b.add_argument("--to", dest="end", type=_date, required=True)
    b.add_argument("--interval", default="minute", choices=sorted(INTERVAL_SECONDS))
    return p


async def cmd_backtest(
    args: argparse.Namespace, settings: Settings, history: HistoryStore, runs: BacktestStore
) -> dict[str, Any]:
    params = json.loads(args.params)
    if not isinstance(params, dict):
        raise SystemExit("--params must be a JSON object")
    req = BacktestRequest(
        args.strategy, params, args.start, args.end, INTERVAL_SECONDS[args.interval]
    )
    run_id, result = await execute(settings, req, history, runs)
    assert result is not None  # noqa: S101 - execute raises on failure
    out = {"run_id": run_id, **result.summary()}
    print(json.dumps(out, indent=2, default=str))
    if result.missing_data:
        print(f"\nMISSING DATA ({len(result.missing_data)} instrument-days):", file=sys.stderr)
        for m in result.missing_data[:50]:
            print(
                f"  {m['date']} {m['symbol']}: {m['found_candles']}/{m['expected_candles']}",
                file=sys.stderr,
            )
    return out


async def cmd_download(
    args: argparse.Namespace, settings: Settings, history: HistoryStore
) -> dict[str, Any]:
    store = factory.build_token_store(settings)
    auth = factory.build_auth(settings, store)
    session = await auth.current_session()
    if session is None:
        raise SystemExit("no Kite session: log in through the dashboard (/auth/login) first")
    broker = factory.build_broker(settings, session)
    today = now_ist().date()
    master = InstrumentStore()
    rows: list[dict[str, Any]] = []
    for exch in ("NSE", "NFO"):
        rows.extend(await broker.instruments(exch))
    master.load_kite_rows(rows, today)
    downloader = HistoryDownloader(broker, history, settings.history_chunk_days)
    start = datetime.combine(args.start, time(9, 15), IST)
    end = datetime.combine(args.end, time(15, 30), IST)

    results = [
        await downloader.download(master.get(k), args.interval, start, end) for k in args.symbol
    ]
    for und in args.underlying:
        results += await record_underlying(
            downloader,
            history,
            master,
            und,
            start=start,
            end=end,
            interval=args.interval,
            width=args.width,
            expiries=args.expiries,
        )
    for res in results:
        print(f"{res.instrument}: {res.candles} candles in {res.chunks} chunks")
    summary: dict[str, Any] = {
        "instruments": len(results),
        "candles": sum(r.candles for r in results),
        "failed": [f"{r.instrument} {f}" for r in results for f in r.failed_chunks],
    }
    print(json.dumps(summary, indent=2))
    return summary


async def main_async(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    settings = Settings()
    configure_logging(settings.log_level, json_output=False)
    engine = make_engine(settings.database_url)
    try:
        history = factory.build_history_store(settings)
        if args.command == "backtest":
            await cmd_backtest(args, settings, history, SqlBacktestStore(make_sessionmaker(engine)))
        else:
            await cmd_download(args, settings, history)
    finally:
        await engine.dispose()
    return 0


def main() -> None:
    sys.exit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
