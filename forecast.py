from decimal import Decimal
from pathlib import Path

from PyQt6 import uic
from PyQt6.QtWidgets import QWidget

from calculations import format_money, format_number
from calculator import CalculatorPage


class ForecastPage(QWidget):
    def __init__(self, calculator: CalculatorPage):
        super().__init__()
        uic.loadUi(str(Path(__file__).with_name("ui") / "forecast.ui"), self)
        self.calculator = calculator
        self.period_input.addItem("в месяц", 12)
        self.period_input.addItem("в год", 1)
        self.forecast_button.clicked.connect(self.show_forecast)
        self.frequency_input.valueChanged.connect(self.clear_forecast)
        self.period_input.currentIndexChanged.connect(self.clear_forecast)
        self.calculator.inputs_changed.connect(self.clear_forecast)

    def clear_forecast(self):
        self.forecast_error.clear()
        for value in (self.days_value, self.monthly_uses, self.annual_uses, self.monthly_amount,
                      self.annual_amount, self.monthly_cost, self.annual_cost):
            value.setText("—")

    def show_forecast(self):
        self.clear_forecast()
        try:
            usage, price = self.calculator.read_inputs()
        except ValueError as error:
            self.forecast_error.setText(f"Проверьте данные на вкладке «Расход». {error}")
            return

        annual_uses = self.frequency_input.value() * self.period_input.currentData()
        days = Decimal(365 * usage.full_uses) / annual_uses
        self.days_value.setText(f"{format_number(days, 2 if days >= 1 else 3)} дн.")
        self.monthly_uses.setText(format_number(Decimal(annual_uses) / 12, 2))
        self.annual_uses.setText(format_number(annual_uses))
        self.monthly_amount.setText(f"{format_number(usage.consumption(annual_uses, 12), 4)} {usage.unit}")
        self.annual_amount.setText(f"{format_number(usage.consumption(annual_uses), 4)} {usage.unit}")
        self.monthly_cost.setText(format_money(usage.cost(price, annual_uses, 12)))
        self.annual_cost.setText(format_money(usage.cost(price, annual_uses)))
