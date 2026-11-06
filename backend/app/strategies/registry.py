"""Strategy plugin registry and `strategies.yaml` loader.

    @register
    class MyStrategy(Strategy):
        name = "my_strategy"

`discover()` imports every module under `app.strategies.examples` (and any extra
packages given) so their `@register` decorators run. `strategies.yaml` lists
instances, so one strategy class can run several times with different params:

    strategies:
      - id: ema_nifty_5m
        strategy: ema_crossover
        enabled: true
        params: {fast: 9, slow: 21}
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.strategies.base import Strategy

_REGISTRY: dict[str, type[Strategy]] = {}


class StrategyConfigError(ValueError):
    pass


def register[S: type[Strategy]](cls: S) -> S:
    if not cls.name:
        raise StrategyConfigError(f"{cls.__name__} must set a non-empty `name`")
    existing = _REGISTRY.get(cls.name)
    if existing is not None and existing is not cls:
        raise StrategyConfigError(f"strategy name {cls.name!r} registered twice")
    _REGISTRY[cls.name] = cls
    return cls


def get_strategy_class(name: str) -> type[Strategy]:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise StrategyConfigError(
            f"unknown strategy {name!r}; known: {sorted(_REGISTRY)}"
        ) from None


def registered() -> dict[str, type[Strategy]]:
    return dict(_REGISTRY)


def discover(packages: tuple[str, ...] = ("app.strategies.examples",)) -> None:
    for pkg_name in packages:
        pkg = importlib.import_module(pkg_name)
        for mod in pkgutil.iter_modules(pkg.__path__, pkg_name + "."):
            importlib.import_module(mod.name)


class StrategyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z0-9_\-]{1,64}$")
    strategy: str
    enabled: bool = False
    params: dict[str, Any] = Field(default_factory=dict)


class StrategiesFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategies: list[StrategyConfig] = Field(default_factory=list)


def load_configs(path: Path) -> list[StrategyConfig]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    try:
        configs = StrategiesFile.model_validate(raw).strategies
    except ValidationError as exc:
        raise StrategyConfigError(f"{path}: {exc}") from exc
    ids = [c.id for c in configs]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise StrategyConfigError(f"{path}: duplicate strategy ids {sorted(dupes)}")
    return configs


def build(config: StrategyConfig) -> Strategy:
    cls = get_strategy_class(config.strategy)
    try:
        return cls(config.id, config.params)
    except ValidationError as exc:
        raise StrategyConfigError(f"strategy {config.id!r}: invalid params: {exc}") from exc
