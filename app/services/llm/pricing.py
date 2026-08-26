"""Raw cost estimation — $ actually attributable per call, no markup.
Feeds both internal cost visibility and the wallet-deduction math (the
30% markup is applied at the wallet call site, not here).

Every provider/model this build can route to gets a real entry — there is
no permanent "$0, skip billing" branch anywhere in this module. The only
free usage in the whole system is the one-time signup trial, which is
enforced by a separate message/day counter elsewhere, not by pricing
returning zero.
"""

from __future__ import annotations

from decimal import Decimal
import logging

from app.config import settings

logger = logging.getLogger(__name__)

_warned_missing: set[tuple[str, str]] = set()


def _parse_pricing_table(raw: str) -> dict[tuple[str, str], tuple[Decimal, Decimal]]:
    """"provider:model:input_$_per_1M:output_$_per_1M,..." -> {(provider, model): (input, output)}"""
    table: dict[tuple[str, str], tuple[Decimal, Decimal]] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) != 4:
            logger.warning("Skipping malformed LLM_PRICING_TABLE entry: %r", entry)
            continue
        provider, model, input_price, output_price = parts
        try:
            table[(provider.strip(), model.strip())] = (Decimal(input_price), Decimal(output_price))
        except Exception:
            logger.warning("Skipping LLM_PRICING_TABLE entry with unparseable price: %r", entry)
    return table


_PRICING_TABLE = _parse_pricing_table(settings.LLM_PRICING_TABLE)


def estimate_cost_usd(provider: str, model: str, prompt_tokens: int | None, completion_tokens: int | None) -> Decimal | None:
    """Real $ cost of one call, or None if this (provider, model) has no
    configured price yet — callers must treat None as "cost unknown," never
    silently substitute 0, since 0 would understate real spend. Confirm
    actual per-1M-token prices against each provider's current pricing page
    before relying on this — the numbers in LLM_PRICING_TABLE are config,
    not verified-at-write-time facts."""
    key = (provider, model)
    prices = _PRICING_TABLE.get(key)
    if prices is None:
        if key not in _warned_missing:
            _warned_missing.add(key)
            logger.warning("No LLM_PRICING_TABLE entry for %s/%s — cost will be recorded as unknown", provider, model)
        return None
    if prompt_tokens is None or completion_tokens is None:
        return None
    input_price, output_price = prices
    return (Decimal(prompt_tokens) * input_price + Decimal(completion_tokens) * output_price) / Decimal(1_000_000)
