import logging
import re
import sys
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path

from PyQt6 import uic
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QApplication, QMainWindow, QMessageBox


PROJECT_DIR = Path(__file__).resolve().parent
LOGGER = logging.getLogger(__name__)
NUMBER_PATTERN = re.compile(r"[+-]?(?:[0-9]+(?:[.,][0-9]+)?|[.,][0-9]+)")


def read_number(text, error_message):
    text = text.strip()
    if not NUMBER_PATTERN.fullmatch(text):
        raise ValueError(error_message)
    return Decimal(text.replace(",", "."))


def validate_usage(product, amount, price, measurements):
    if not product.strip():
        raise ValueError("Введите название товара.")

    package_amount = read_number(amount, "Введите количество в упаковке числом.")
    if package_amount <= 0:
        raise ValueError("Количество в упаковке должно быть больше нуля.")

    price_error = "Введите цену числом от нуля и выше."
    package_price = read_number(price, price_error)
    if package_price < 0:
        raise ValueError(price_error)

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
    return package_amount, package_price, samples


def calculate_usage(package_amount, package_price, samples):
    # Точные дроби сохраняют границу полного применения до округления результата.
    amount = Fraction(package_amount)
    average = sum(Fraction(sample) for sample in samples) / len(samples)
    cost_per_use = Fraction(package_price) * average / amount
    full_uses = amount // average
    return average, cost_per_use, full_uses


def format_usage_number(value, places):
    scale = 10 ** places
    rounded = int(value * scale + Fraction(1, 2))
    whole, remainder = divmod(rounded, scale)
    return f"{whole},{remainder:0{places}d}"


class HouseholdGoodsWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        uic.loadUi(str(PROJECT_DIR / "household_goods.ui"), self)
        self.sidebarImage.setPixmap(QPixmap(str(PROJECT_DIR / "household-goods-sidebar.png")))
        self.calculateUsageButton.clicked.connect(self.check_usage)
        for field in (self.productInput, self.packageInput, self.packagePriceInput, self.measurementsInput):
            field.textChanged.connect(self.clear_usage_result)
        self.packageUnit.currentTextChanged.connect(self.set_usage_unit)
        self.set_usage_unit(self.packageUnit.currentText())

    def clear_usage_result(self):
        self.usageCostValue.setText("—")
        self.averageUsageValue.setText("—")
        self.fullUsesValue.setText("—")
        self.statusBar().clearMessage()

    def set_usage_unit(self, unit):
        self.measurementsLabel.setText(f"<b>Замеры за одно применение, {unit}</b>")
        self.clear_usage_result()

    def check_usage(self):
        self.clear_usage_result()
        self.calculateUsageButton.setEnabled(False)
        try:
            amount, price, samples = validate_usage(
                product=self.productInput.text(),
                amount=self.packageInput.text(),
                price=self.packagePriceInput.text(),
                measurements=self.measurementsInput.toPlainText(),
            )
            average, cost_per_use, full_uses = calculate_usage(amount, price, samples)
            average_text = format_usage_number(average, 3).rstrip("0").rstrip(",")
            if average_text == "0":
                average_text = "< 0,001"
            cost_text = format_usage_number(cost_per_use, 2)
            full_uses_text = str(full_uses)
        except ValueError as error:
            QMessageBox.warning(self, "Проверьте данные", str(error))
        except InvalidOperation:
            QMessageBox.warning(self, "Ошибка ввода", "Невозможно преобразовать значение в число.")
        except Exception:
            LOGGER.exception("Непредвиденная ошибка при расчёте расхода")
            QMessageBox.critical(self, "Ошибка расчёта", "Не удалось рассчитать расход.")
        else:
            self.averageUsageValue.setText(f"{average_text} {self.packageUnit.currentText()}")
            self.usageCostValue.setText(f"{cost_text} ₽")
            self.fullUsesValue.setText(full_uses_text)
            self.statusBar().showMessage("Расход рассчитан.", 3000)
        finally:
            self.calculateUsageButton.setEnabled(True)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = HouseholdGoodsWindow()
    window.show()
    sys.exit(app.exec())
