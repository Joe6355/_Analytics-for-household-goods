from decimal import Decimal
from pathlib import Path
from unicodedata import normalize

from PyQt6 import uic
from PyQt6.QtWidgets import QWidget

from calculations import format_number
from calculator import CalculatorPage


def read_components(text: str) -> dict[str, str]:
    components = {}
    for raw_line in text.splitlines():
        line = " ".join(normalize("NFC", raw_line).split())
        if line:
            components.setdefault(line.casefold(), line)
    return components


class CharacteristicsPage(QWidget):
    def __init__(self, calculator: CalculatorPage):
        super().__init__()
        uic.loadUi(str(Path(__file__).with_name("ui") / "characteristics.ui"), self)
        self.calculator = calculator
        self.composition_button.clicked.connect(self.show_composition)
        self.composition_input.textChanged.connect(self.composition_result.clear)
        self.reference_input.textChanged.connect(self.composition_result.clear)
        self.calculator.inputs_changed.connect(self.show_packaging)
        self.tested_input.valueChanged.connect(self.show_effectiveness)
        self.success_input.valueChanged.connect(self.show_effectiveness)
        self.show_packaging()

    def show_composition(self):
        first = read_components(self.composition_input.toPlainText())
        second = read_components(self.reference_input.toPlainText())
        if not first:
            self.composition_result.setPlainText("Укажите состав первого товара.")
            return
        if not second:
            self.composition_result.setPlainText(f"Компонентов — {len(first)}.\n\n" + "\n".join(first.values()))
            return

        common = ", ".join(name for key, name in first.items() if key in second) or "нет"
        only_first = ", ".join(name for key, name in first.items() if key not in second) or "нет"
        only_second = ", ".join(name for key, name in second.items() if key not in first) or "нет"
        self.composition_result.setPlainText(
            f"Компонентов в первом товаре — {len(first)}, во втором — {len(second)}.\n\n"
            f"В обоих составах — {common}.\n"
            f"Только в первом — {only_first}.\n"
            f"Только во втором — {only_second}."
        )

    def show_packaging(self):
        self.package_amount.setText("—")
        self.package_share.setText("—")
        try:
            amount, unit = self.calculator.read_package()
        except ValueError:
            self.package_hint.setText("Укажите количество и замеры на вкладке «Расход».")
            return

        self.package_amount.setText(f"{format_number(amount)} {unit}")
        try:
            usage = self.calculator.read_usage()
        except ValueError:
            self.package_hint.setText("Добавьте замеры на вкладке «Расход».")
            return

        share = usage.sample_total * 100 / (usage.sample_count * usage.amount)
        self.package_share.setText(f"{format_number(share)} %" if share >= Decimal("0.001") else "< 0,001 %")
        self.package_hint.clear()

    def show_effectiveness(self):
        tested = self.tested_input.value()
        successful = self.success_input.value()
        self.effectiveness_error.clear()
        self.effectiveness_value.setText("—")
        if successful > tested:
            self.effectiveness_error.setText("Успешных применений не может быть больше, чем испытаний.")
        elif tested:
            percent = Decimal(successful * 100) / tested
            self.effectiveness_value.setText(f"{format_number(percent)} %")
