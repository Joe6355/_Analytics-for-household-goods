import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, localcontext


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
    value = Decimal(value)
    with localcontext() as context:
        context.prec = max(context.prec, value.adjusted() + places + 2)
        rounded = value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
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
    sample_total: Decimal
    sample_count: int
    unit: str

    @property
    def average(self) -> Decimal:
        return self.consumption(1)

    @property
    def full_uses(self) -> int:
        return int(self.amount * self.sample_count // self.sample_total)

    def consumption(self, uses: int, periods: int = 1) -> Decimal:
        return self.sample_total * uses / (self.sample_count * periods)

    def cost(self, price: Decimal, uses: int = 1, periods: int = 1) -> Decimal:
        return price * self.sample_total * uses / (self.amount * self.sample_count * periods)


def calculate_usage(amount: Decimal, samples: list[Decimal], unit: str) -> Usage:
    if not samples:
        raise ValueError("Добавьте хотя бы один замер расхода.")
    return Usage(amount, sum(samples, Decimal(0)), len(samples), unit)
