import logging
from decimal import InvalidOperation
from fractions import Fraction

from PyQt6.QtWidgets import QMessageBox

from calculations import calculate_usage, format_number, format_quantity, validate_usage
from workspace_state import UsageSamples


LOGGER = logging.getLogger(__name__)


class UsageScreen:
    def setup_usage_screen(self):
        self.connect_screen_action(self.calculateUsageButton.clicked, "Расход", self.check_usage)
        for field in (self.productInput, self.packageInput, self.packagePriceInput, self.measurementsInput):
            field.textChanged.connect(self.clear_usage_result)
            field.textChanged.connect(self.clear_forecast)
        self.packageUnit.currentTextChanged.connect(self.change_usage_unit)
        self.packageUnit.currentTextChanged.connect(self.clear_forecast)
        self.packagePriceInput.textChanged.connect(lambda: self.set_usage_price_note(""))
        self.set_usage_unit(self.packageUnit.currentText())

    def clear_usage_result(self):
        self.usageCostValue.setText("—")
        self.averageUsageValue.setText("—")
        self.fullUsesValue.setText("—")
        self.sampleCountValue.setText("—")
        self.sampleRangeValue.setText("—")
        self.statusBar().clearMessage()

    def set_usage_unit(self, unit):
        self.measurementsLabel.setText(f"<b>Замеры за одно применение, {unit}</b>")
        self.clear_usage_result()

    def change_usage_unit(self, unit):
        had_measurements = bool(self.packageInput.text().strip() or self.measurementsInput.toPlainText().strip())
        if self.usage_context is not None:
            self.detach_offer()
            self.set_usage_samples(UsageSamples())
        self.packageInput.clear()
        self.measurementsInput.clear()
        self.set_usage_price_note("")
        self.set_usage_unit(unit)
        if had_measurements:
            self.statusBar().showMessage("Введите фасовку и замеры в новой единице.", 7000)

    def read_usage_fields(self):
        return validate_usage(
            product=self.productInput.text(),
            amount=self.packageInput.text(),
            price=self.packagePriceInput.text(),
            measurements=self.measurementsInput.toPlainText(),
        )

    def calculation_inputs(self):
        unit = self.packageUnit.currentText()
        inputs = [
            f"Фасовка — {self.packageInput.text()} {unit}",
            f"Цена упаковки — {self.packagePriceInput.text()} ₽",
        ]
        if self.selected_offer_id in self.offer_choices:
            _, offer = self.offer_choices[self.selected_offer_id]
            inputs.insert(0, f"Магазин — {offer.store}")
        if self.usage_price_note:
            inputs.append(f"Условие цены — {self.usage_price_note}")
        inputs.append(f"Замеры ({unit})\n{self.measurementsInput.toPlainText().strip()}")
        return "\n".join(inputs)

    def check_usage(self, *, record=True):
        self.clear_usage_result()
        self.calculateUsageButton.setEnabled(False)
        try:
            amount, price, samples = self.read_usage_fields()
            average, cost_per_use, full_uses = calculate_usage(amount, price, samples)
            average_text = format_quantity(average)
            cost_text = format_number(cost_per_use, 2)
            full_uses_text = str(full_uses)
            low, high = min(samples), max(samples)
            range_text = format_quantity(Fraction(low))
            if low != high:
                range_text += f"–{format_quantity(Fraction(high))}"
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
            self.sampleCountValue.setText(str(len(samples)))
            self.sampleRangeValue.setText(f"{range_text} {self.packageUnit.currentText()}")
            self.statusBar().showMessage("Расход рассчитан.", 3000)
            if record:
                details = (
                    f"Средний расход — {self.averageUsageValue.text()}\n"
                    f"Стоимость применения — {cost_text} ₽\n"
                    f"Полных применений — {full_uses_text}\n\n{self.calculation_inputs()}"
                )
                self.record_calculation("Расчёт расхода", details)
        finally:
            self.calculateUsageButton.setEnabled(True)
