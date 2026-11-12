import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal


UNITS = ("мл", "г", "шт.")
NUMBER_PATTERN = re.compile(r"(?:[0-9]{1,9}(?:[.,][0-9]{1,3})?|[.,][0-9]{1,3})")


def read_number(text: str, field: str, *, allow_zero: bool = False) -> Decimal:
    text = text.strip()
    if not NUMBER_PATTERN.fullmatch(text):
        raise ValueError(f"{field} — введите число с точностью до трёх знаков после запятой.")
    value = Decimal(text.replace(",", "."))
    if value == 0 and not allow_zero:
        raise ValueError(f"{field} — значение должно быть больше нуля.")
    return value


def format_number(value: Decimal | int, places: int = 3, *, fixed: bool = False) -> str:
    rounded = Decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    whole, _, fraction = f"{rounded:,.{places}f}".partition(".")
    if not fixed:
        fraction = fraction.rstrip("0")
    whole = whole.replace(",", "\u00a0")
    return f"{whole},{fraction}" if fraction else whole


def format_money(value: Decimal) -> str:
    return f"{format_number(value, 2, fixed=True)}\u00a0₽"


@dataclass(frozen=True)
class Usage:
    amount: Decimal
    average: Decimal
    cost_per_use: Decimal
    full_uses: int
    sample_count: int
    unit: str


def calculate_usage(amount: Decimal, price: Decimal, samples: list[Decimal], unit: str) -> Usage:
    if not samples:
        raise ValueError("Добавьте хотя бы один замер расхода.")
    total = sum(samples, Decimal(0))
    count = len(samples)
    return Usage(amount, total / count, price * total / (amount * count),
                 int(amount * count // total), count, unit)
