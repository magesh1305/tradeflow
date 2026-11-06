from __future__ import annotations

from pathlib import Path

import pytest

from app.strategies.base import Strategy
from app.strategies.registry import (
    StrategyConfig,
    StrategyConfigError,
    build,
    discover,
    load_configs,
    register,
    registered,
)

BACKEND = Path(__file__).resolve().parents[1]


def test_discover_finds_examples() -> None:
    discover()
    assert {"ema_crossover", "short_straddle"} <= set(registered())


def test_repo_strategies_yaml_is_valid() -> None:
    discover()
    configs = load_configs(BACKEND / "strategies.yaml")
    by_id = {c.id: c for c in configs}
    assert by_id["ema_nifty_5m"].enabled is True
    assert by_id["straddle_nifty"].enabled is False  # short options off by default
    for c in configs:
        build(c)


def test_same_strategy_twice_with_different_params(tmp_path: Path) -> None:
    discover()
    f = tmp_path / "s.yaml"
    f.write_text(
        "strategies:\n"
        "  - {id: a, strategy: ema_crossover, params: {fast: 5, slow: 10}}\n"
        "  - {id: b, strategy: ema_crossover, params: {fast: 9, slow: 21, lots: 2}}\n"
    )
    a, b = (build(c) for c in load_configs(f))
    assert (a.params.fast, b.params.fast, b.params.lots) == (5, 9, 2)


def test_duplicate_ids_rejected(tmp_path: Path) -> None:
    f = tmp_path / "s.yaml"
    f.write_text("strategies:\n  - {id: a, strategy: x}\n  - {id: a, strategy: y}\n")
    with pytest.raises(StrategyConfigError, match="duplicate"):
        load_configs(f)


def test_unknown_strategy_and_bad_params() -> None:
    discover()
    with pytest.raises(StrategyConfigError, match="unknown"):
        build(StrategyConfig(id="x", strategy="nope"))
    with pytest.raises(StrategyConfigError, match="fast must be smaller"):
        build(StrategyConfig(id="x", strategy="ema_crossover", params={"fast": 30, "slow": 10}))
    with pytest.raises(StrategyConfigError, match="invalid params"):
        build(StrategyConfig(id="x", strategy="ema_crossover", params={"typo": 1}))


def test_register_requires_unique_name() -> None:
    class NoName(Strategy):
        pass

    with pytest.raises(StrategyConfigError):
        register(NoName)

    class Clash(Strategy):
        name = "ema_crossover"

    discover()
    with pytest.raises(StrategyConfigError, match="twice"):
        register(Clash)
