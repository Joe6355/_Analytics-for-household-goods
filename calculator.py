from decimal import Decimal
from pathlib import Path

from PyQt6 import uic
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QWidget

from calculations import UNITS, Usage, calculate_usage, format_money, format_number, read_number


class CalculatorPage(QWidget):
    inputs_changed = pyqtSignal()

    def __init__(self):
        super().__init__()
        uic.loadUi(str(Path(__file__).with_name("ui") / "calculator.ui"), self)
        self.unit_input.addItems(UNITS)
        self.calculate_button.clicked.connect(self.show_usage)
        self.amount_input.returnPressed.connect(self.show_usage)
        self.price_input.returnPressed.connect(self.show_usage)
        self.amount_input.textChanged.connect(self.on_inputs_changed)
        self.price_input.textChanged.connect(self.on_inputs_changed)
        self.samples_input.textChanged.connect(self.on_inputs_changed)
        self.unit_input.currentIndexChanged.connect(self.on_inputs_changed)

    def read_package(self) -> tuple[Decimal, str]:
        amount = read_number(self.amount_input.text(), "Количество в упаковке")
        return amount, self.unit_input.currentText()

    def read_usage(self) -> Usage:
        amount, unit = self.read_package()
        samples = [read_number(line, f"Замер в строке {index}")
                   for index, line in enumerate(self.samples_input.toPlainText().splitlines(), 1)
                   if line.strip()]
        return calculate_usage(amount, samples, unit)

    def read_inputs(self) -> tuple[Usage, Decimal]:
        price = read_number(self.price_input.text(), "Цена упаковки", allow_zero=True)
        return self.read_usage(), price

    def on_inputs_changed(self):
        self.clear_result()
        self.inputs_changed.emit()

    def clear_result(self):
        self.error_label.clear()
        for value in (self.average_value, self.cost_value, self.uses_value, self.samples_value):
            value.setText("—")

    def show_usage(self):
        self.clear_result()
        try:
            usage, price = self.read_inputs()
        except ValueError as error:
            self.error_label.setText(str(error))
            return

        self.average_value.setText(f"{format_number(usage.average)} {usage.unit}")
        self.cost_value.setText(format_money(usage.cost(price)))
        self.uses_value.setText(format_number(usage.full_uses))
        self.samples_value.setText(format_number(usage.sample_count))
