"""List prices for the authorized M2 pilot. Amounts are integer USD micros per 1M tokens."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

# $1 = 1_000_000 micros. Rates are micros per 1,000,000 tokens.
LUNA_INPUT = 100_000  # $0.10
LUNA_CACHED_INPUT = 10_000  # $0.01
LUNA_OUTPUT = 500_000  # $0.50
LUNA_LONG_INPUT = 200_000  # $0.20, prompts at or above 272k
LUNA_LONG_OUTPUT = 750_000  # $0.75
LUNA_LONG_THRESHOLD = 272_000

# DeepSeek V4.1 Flash off-peak, from the official pricing page on 2026-10-17.
FLASH_OFFPEAK_MISS = 150_000  # $0.15
FLASH_OFFPEAK_HIT = 3_000  # $0.003
FLASH_OFFPEAK_OUTPUT = 600_000  # $0.60

PILOT_CAP_MICROS = 2_000_000
MAX_OUTPUT_TOKENS = 2048
INPUT_TOKEN_LIMIT = 24_000
# Byte fallback in the BPE tokenizers used by these APIs is one token per unknown
# byte. Role and tool wrappers are not inside the JSON body, so they are added here.
PROTOCOL_OVERHEAD_TOKENS = 64
PER_ITEM_OVERHEAD_TOKENS = 16
# DeepSeek bills the request it accepts. The margin covers the 45s timeout plus clock drift.
DISPATCH_MARGIN = timedelta(seconds=90)


def tokens_cost(tokens: int, micros_per_million: int) -> int:
    if tokens <= 0:
        return 0
    return (tokens * micros_per_million + 1_000_000 - 1) // 1_000_000


def input_token_bound(payload: str, item_count: int) -> int:
    """Upper bound on billable input tokens for the exact payload about to be sent.

    `len(payload) / 3` can undercount. UTF-8 byte length does not, for a tokenizer
    that falls back to one token per unknown byte. Item overhead covers protocol
    framing that the provider adds around messages and tools.
    """
    items = item_count if item_count > 0 else 1
    body = len(payload.encode("utf-8"))
    framing = PROTOCOL_OVERHEAD_TOKENS + PER_ITEM_OVERHEAD_TOKENS * items
    return body + framing


@dataclass(frozen=True)
class PricedUsage:
    micros: int
    estimated: bool
    detail: dict[str, int]


def luna_reservation(input_tokens: int) -> int:
    output_rate = LUNA_LONG_OUTPUT if input_tokens >= LUNA_LONG_THRESHOLD else LUNA_OUTPUT
    input_rate = LUNA_LONG_INPUT if input_tokens >= LUNA_LONG_THRESHOLD else LUNA_INPUT
    return tokens_cost(input_tokens, input_rate) + tokens_cost(MAX_OUTPUT_TOKENS, output_rate)


def flash_reservation(input_tokens: int) -> int:
    return tokens_cost(input_tokens, FLASH_OFFPEAK_MISS) + tokens_cost(
        MAX_OUTPUT_TOKENS, FLASH_OFFPEAK_OUTPUT
    )


def luna_actual(usage: dict | None) -> PricedUsage | None:
    if not usage or "input_tokens" not in usage or "output_tokens" not in usage:
        return None
    input_tokens = int(usage["input_tokens"])
    output_tokens = int(usage["output_tokens"])
    details = usage.get("input_tokens_details") or {}
    output_details = usage.get("output_tokens_details") or {}
    cached = int(details.get("cached_tokens") or 0)
    reasoning = int(output_details.get("reasoning_tokens") or 0)
    if min(input_tokens, output_tokens, cached, reasoning) < 0 or cached > input_tokens:
        return None
    long_context = input_tokens >= LUNA_LONG_THRESHOLD
    input_rate = LUNA_LONG_INPUT if long_context else LUNA_INPUT
    output_rate = LUNA_LONG_OUTPUT if long_context else LUNA_OUTPUT
    fresh = input_tokens - cached
    # Responses usage counts reasoning tokens inside output_tokens.
    billed_output = output_tokens if reasoning <= output_tokens else output_tokens + reasoning
    micros = (
        tokens_cost(fresh, input_rate)
        + tokens_cost(cached, LUNA_CACHED_INPUT)
        + tokens_cost(billed_output, output_rate)
    )
    known = {
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "input_tokens_details",
        "output_tokens_details",
    }
    return PricedUsage(
        micros=micros,
        estimated=bool(set(usage) - known) or reasoning > output_tokens,
        detail={
            "input_tokens": input_tokens,
            "cached_input_tokens": cached,
            "output_tokens": output_tokens,
            "reasoning_tokens": reasoning,
        },
    )


def flash_actual(usage: dict | None) -> PricedUsage | None:
    if not usage:
        return None
    hit = usage.get("prompt_cache_hit_tokens")
    miss = usage.get("prompt_cache_miss_tokens")
    output_tokens = usage.get("completion_tokens")
    if output_tokens is None:
        return None
    if int(output_tokens) < 0:
        return None
    estimated = False
    if hit is None or miss is None:
        prompt = usage.get("prompt_tokens")
        if prompt is None:
            return None
        hit = 0
        miss = int(prompt)
        estimated = True
    if int(hit) < 0 or int(miss) < 0:
        return None
    completion_details = usage.get("completion_tokens_details") or {}
    reasoning = int(completion_details.get("reasoning_tokens") or 0)
    billed_output = int(output_tokens)
    if reasoning > billed_output:
        billed_output += reasoning
        estimated = True
    micros = tokens_cost(int(miss), FLASH_OFFPEAK_MISS) + tokens_cost(
        int(hit), FLASH_OFFPEAK_HIT
    )
    micros += tokens_cost(billed_output, FLASH_OFFPEAK_OUTPUT)
    return PricedUsage(
        micros=micros,
        estimated=estimated,
        detail={
            "cache_hit_tokens": int(hit),
            "cache_miss_tokens": int(miss),
            "output_tokens": int(output_tokens),
            "reasoning_tokens": reasoning,
        },
    )


def deepseek_offpeak(moment: datetime) -> bool:
    """Official peak is 01:00-04:00 and 06:00-10:00 UTC, weekdays.

    Weekends are off-peak. DeepSeek also treats a weekday Chinese public holiday
    as off-peak. This clock has no holiday calendar, so those weekday hours stay
    paused instead of being billed at the peak rate.
    """
    if moment.tzinfo is None:
        raise ValueError("off-peak checks require a timezone-aware timestamp")
    utc = moment.astimezone(UTC)
    if utc.weekday() >= 5:
        return True
    hour = utc.hour
    minute = utc.minute
    clock = hour * 60 + minute
    peak = (1 * 60 <= clock < 4 * 60) or (6 * 60 <= clock < 10 * 60)
    return not peak


def deepseek_dispatch_allowed(moment: datetime) -> bool:
    """Allow a call only when the timeout window cannot enter peak.

    DeepSeek's published boundary is the time the service accepts the request.
    Checking once before a retry is not enough: a 503 near 01:00 UTC can be
    retried after the peak rate has started. The margin is the 45-second request
    timeout plus allowance for clock drift.
    """
    return deepseek_offpeak(moment) and deepseek_offpeak(moment + DISPATCH_MARGIN)
