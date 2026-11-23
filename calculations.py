import re
from decimal import Decimal
from fractions import Fraction


NUMBER_PATTERN = re.compile(r"[+-]?(?:[0-9]+(?:[.,][0-9]+)?|[.,][0-9]+)")


def read_number(text, error_message):
    text = text.strip()
    if not NUMBER_PATTERN.fullmatch(text):
        raise ValueError(error_message)
    return Decimal(text.replace(",", "."))


def read_package_amount(text):
    amount = read_number(text, "Введите количество в упаковке числом.")
    if amount <= 0:
        raise ValueError("Количество в упаковке должно быть больше нуля.")
    return amount


def read_package(amount, price):
    package_amount = read_package_amount(amount)
    price_error = "Введите цену числом от нуля и выше."
    package_price = read_number(price, price_error)
    if package_price < 0:
        raise ValueError(price_error)
    return package_amount, package_price


def read_measurements(measurements):
    samples = []
    for line_number, line in enumerate(measurements.splitlines(), start=1):
        if line.strip():
            sample_error = f"Замер в строке {line_number} должен быть числом больше нуля."
            sample = read_number(line, sample_error)
            if sample <= 0:
                raise ValueError(sample_error)
            samples.append(sample)
    if not samples:
        raise ValueError("Добавьте хотя бы один замер расхода.")
    return samples


def validate_usage(product, amount, price, measurements):
    if not product.strip():
        raise ValueError("Введите название товара.")

    package_amount, package_price = read_package(amount, price)
    return package_amount, package_price, read_measurements(measurements)


def average_usage(samples):
    return sum(Fraction(sample) for sample in samples) / len(samples)


def calculate_usage(package_amount, package_price, samples):
    # Точные дроби сохраняют границу полного применения до округления результата.
    amount = Fraction(package_amount)
    average = average_usage(samples)
    cost_per_use = Fraction(package_price) * average / amount
    full_uses = amount // average
    return average, cost_per_use, full_uses


def format_number(value, places, *, trim=False):
    scale = 10 ** places
    rounded = int(value * scale + Fraction(1, 2))
    whole, remainder = divmod(rounded, scale)
    integer_text = f"{Decimal(whole):f}"
    if places == 0:
        return integer_text
    fraction = f"{remainder:0{places}d}"
    if trim:
        fraction = fraction.rstrip("0")
    return f"{integer_text},{fraction}" if fraction else integer_text


def format_quantity(value):
    text = format_number(value, 3, trim=True)
    return "< 0,001" if text == "0" and value > 0 else text
