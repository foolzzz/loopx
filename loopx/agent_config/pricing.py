"""Optional per-provider token prices for estimated Turn cost (fork gap G9).

A provider that reports no USD (for example Codex through CPA) can declare a
small price table in ``providers.yaml``. Prices are USD per 1M tokens::

    cpa:
      kind: codex-cpa
      auth: {type: api_key}
      pricing:
        input: 1.25            # uncached input
        cached_input: 0.125    # cache reads
        output: 10             # output, reasoning included
        models:                # optional per-model overrides (exact model id)
          gpt-5.6-sol: {input: 2.5, cached_input: 0.25, output: 20}

``cache_creation_input`` is optional and defaults to the ``input`` price.
A cost computed from this table is always marked ``estimated``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

PRICING_RATE_FIELDS = ("input", "cached_input", "cache_creation_input", "output")
_REQUIRED_RATE_FIELDS = ("input", "cached_input", "output")
_MAX_RATE = 10_000.0
_MAX_PRICED_MODELS = 64


@dataclass(frozen=True)
class TokenRates:
    """USD per 1M tokens."""

    input: float
    cached_input: float
    output: float
    cache_creation_input: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "input": self.input,
            "cached_input": self.cached_input,
            "cache_creation_input": self.cache_creation_input,
            "output": self.output,
        }


@dataclass(frozen=True)
class ProviderPricing:
    default: TokenRates | None = None
    models: dict[str, TokenRates] = field(default_factory=dict)

    def rates_for(self, model: str | None) -> TokenRates | None:
        if model and model in self.models:
            return self.models[model]
        return self.default

    def to_dict(self) -> dict[str, Any]:
        return {
            "default": self.default.to_dict() if self.default else None,
            "models": {name: rates.to_dict() for name, rates in sorted(self.models.items())},
        }


def _rate(raw: Mapping[str, Any], key: str, where: str, issues: list[str]) -> float | None:
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        issues.append(f"{where}: pricing.{key} must be a number (USD per 1M tokens)")
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0 or number > _MAX_RATE:
        issues.append(f"{where}: pricing.{key} must be between 0 and {_MAX_RATE:g}")
        return None
    return number


def _parse_rates(raw: Mapping[str, Any], where: str, issues: list[str]) -> TokenRates | None:
    present = [key for key in PRICING_RATE_FIELDS if raw.get(key) is not None]
    if not present:
        return None
    missing = [key for key in _REQUIRED_RATE_FIELDS if raw.get(key) is None]
    if missing:
        issues.append(f"{where}: pricing needs {', '.join(_REQUIRED_RATE_FIELDS)} (missing {', '.join(missing)})")
        return None
    before = len(issues)
    values = {key: _rate(raw, key, where, issues) for key in PRICING_RATE_FIELDS}
    if len(issues) > before:
        return None
    return TokenRates(
        input=values["input"] or 0.0,
        cached_input=values["cached_input"] or 0.0,
        output=values["output"] or 0.0,
        cache_creation_input=values["cache_creation_input"],
    )


def parse_provider_pricing(raw: Any, *, where: str, issues: list[str]) -> ProviderPricing | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        issues.append(f"{where}: pricing must be a mapping of USD per 1M tokens")
        return None
    unknown = sorted(set(map(str, raw)) - {*PRICING_RATE_FIELDS, "models"})
    if unknown:
        issues.append(f"{where}: unsupported pricing fields: {', '.join(unknown)}")
    default = _parse_rates(raw, where, issues)
    models: dict[str, TokenRates] = {}
    raw_models = raw.get("models") or {}
    if not isinstance(raw_models, Mapping):
        issues.append(f"{where}: pricing.models must map a model id to its prices")
        raw_models = {}
    if len(raw_models) > _MAX_PRICED_MODELS:
        issues.append(f"{where}: pricing.models lists more than {_MAX_PRICED_MODELS} models")
    for name, entry in list(raw_models.items())[:_MAX_PRICED_MODELS]:
        model_where = f"{where} pricing.models.{name}"
        if not isinstance(name, str) or not name.strip() or not isinstance(entry, Mapping):
            issues.append(f"{model_where}: expected a model id mapping to prices")
            continue
        unknown_rates = sorted(set(map(str, entry)) - set(PRICING_RATE_FIELDS))
        if unknown_rates:
            issues.append(f"{model_where}: unsupported pricing fields: {', '.join(unknown_rates)}")
        rates = _parse_rates(entry, model_where, issues)
        if rates is not None:
            models[name.strip()] = rates
    if default is None and not models:
        issues.append(f"{where}: pricing declares no prices")
        return None
    return ProviderPricing(default=default, models=models)


def estimate_cost_usd(tokens: Mapping[str, Any] | None, rates: TokenRates | None) -> float | None:
    """Estimated USD for normalized ``turn_usage`` tokens, or ``None`` if unpriceable."""

    if rates is None or not isinstance(tokens, Mapping):
        return None

    def count(key: str) -> int:
        value = tokens.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0

    cache_creation_rate = (
        rates.cache_creation_input if rates.cache_creation_input is not None else rates.input
    )
    total = (
        count("input") * rates.input
        + count("cached_input") * rates.cached_input
        + count("cache_creation_input") * cache_creation_rate
        + count("output") * rates.output
    ) / 1_000_000
    return round(total, 8)
