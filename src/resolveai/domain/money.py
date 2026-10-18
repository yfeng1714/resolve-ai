from decimal import Decimal


def format_usd(amount_cents: int) -> str:
    if amount_cents < 0:
        raise ValueError("amount_cents must be nonnegative")
    return f"${amount_cents // 100}.{amount_cents % 100:02d}"


def parse_usd_to_cents(amount: Decimal) -> int:
    if amount != amount.to_integral_value():
        raise ValueError("USD amounts must be integer cents")
    cents = int(amount)
    if cents < 0:
        raise ValueError("amount must be nonnegative")
    return cents
