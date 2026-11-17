import logging
import re
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from unicodedata import normalize

from PyQt6 import uic
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QApplication, QMainWindow, QMessageBox, QTableWidgetItem

from price_loader import PriceLoader


PROJECT_DIR = Path(__file__).resolve().parent
LOGGER = logging.getLogger(__name__)
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


@dataclass(frozen=True)
class Offer:
    store: str
    amount: Decimal
    price: Decimal

    @property
    def price_per_unit(self):
        return Fraction(self.price) / Fraction(self.amount)


class HouseholdGoodsWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        uic.loadUi(str(PROJECT_DIR / "household_goods.ui"), self)
        self.sidebarImage.setPixmap(QPixmap(str(PROJECT_DIR / "household-goods-sidebar.png")))
        self.calculateUsageButton.clicked.connect(self.check_usage)
        for field in (self.productInput, self.packageInput, self.packagePriceInput, self.measurementsInput):
            field.textChanged.connect(self.clear_usage_result)
            field.textChanged.connect(self.clear_forecast)
        self.packageUnit.currentTextChanged.connect(self.set_usage_unit)
        self.packageUnit.currentTextChanged.connect(self.clear_forecast)
        self.set_usage_unit(self.packageUnit.currentText())

        self.forecastProduct.setTextFormat(Qt.TextFormat.PlainText)
        self.calculateForecastButton.clicked.connect(self.show_forecast)
        self.frequencyInput.valueChanged.connect(self.clear_forecast)
        self.frequencyPeriod.currentIndexChanged.connect(self.clear_forecast)
        self.clear_forecast()

        self.offers = []
        self.pricesProduct.hide()
        self.price_loader = PriceLoader(self)
        self.price_loader.started.connect(lambda: self.set_price_loading(True))
        self.price_loader.loaded.connect(self.apply_loaded_price)
        self.price_loader.failed.connect(self.show_price_error)
        self.productLinkInput.returnPressed.connect(self.load_price)
        self.loadPriceButton.clicked.connect(self.load_price)
        self.bestPriceLabel.setTextFormat(Qt.TextFormat.PlainText)
        self.addOfferButton.clicked.connect(self.add_offer)
        self.removeOfferButton.clicked.connect(self.remove_offer)
        self.offersTable.itemSelectionChanged.connect(self.update_remove_button)
        self.offerUnit.currentTextChanged.connect(self.update_offers_table)
        self.update_offers_table()

        self.characteristicsProduct.setTextFormat(Qt.TextFormat.PlainText)
        self.componentCount.setTextFormat(Qt.TextFormat.PlainText)
        self.compositionInput.clear()
        self.componentCount.clear()
        self.compositionInput.textChanged.connect(self.componentCount.clear)
        self.analyzeCompositionButton.clicked.connect(self.analyze_composition)
        self.testCountInput.setValue(0)
        self.successCountInput.setValue(0)
        self.testCountInput.valueChanged.connect(self.update_effectiveness)
        self.successCountInput.valueChanged.connect(self.update_effectiveness)
        self.update_effectiveness()

        self.sectionStack.currentChanged.connect(self.update_page)
        self.update_page()

    def update_page(self):
        page = self.sectionStack.currentWidget()
        self.demoNote.setVisible(page is self.accountPage)
        if page is self.characteristicsPage:
            self.update_characteristics_package()

    def clear_usage_result(self):
        self.usageCostValue.setText("—")
        self.averageUsageValue.setText("—")
        self.fullUsesValue.setText("—")
        self.statusBar().clearMessage()

    def set_usage_unit(self, unit):
        self.measurementsLabel.setText(f"<b>Замеры за одно применение, {unit}</b>")
        self.clear_usage_result()

    def read_usage_fields(self):
        return validate_usage(
            product=self.productInput.text(),
            amount=self.packageInput.text(),
            price=self.packagePriceInput.text(),
            measurements=self.measurementsInput.toPlainText(),
        )

    def check_usage(self):
        self.clear_usage_result()
        self.calculateUsageButton.setEnabled(False)
        try:
            amount, price, samples = self.read_usage_fields()
            average, cost_per_use, full_uses = calculate_usage(amount, price, samples)
            average_text = format_quantity(average)
            cost_text = format_number(cost_per_use, 2)
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

    def set_forecast_rows(self, rows):
        for row, values in enumerate(rows):
            for column, text in enumerate(values, start=1):
                self.forecastTable.item(row, column).setText(text)

    def clear_forecast(self):
        self.forecastProduct.setText("Данные из вкладки «Расход»")
        self.durationValue.setText("—")
        self.set_forecast_rows((("—", "—", "—"), ("—", "—", "—")))

    def show_forecast(self):
        try:
            amount, price, samples = self.read_usage_fields()
        except ValueError as error:
            QMessageBox.warning(self, "Проверьте данные на вкладке «Расход»", str(error))
            return
        except InvalidOperation:
            QMessageBox.warning(self, "Ошибка ввода", "Проверьте числа на вкладке «Расход».")
            return

        average, cost_per_use, full_uses = calculate_usage(amount, price, samples)
        annual_uses = self.frequencyInput.value()
        if self.frequencyPeriod.currentText() == "в месяц":
            annual_uses *= 12
        monthly_uses = Fraction(annual_uses, 12)
        days = Fraction(365 * full_uses, annual_uses)
        days_text = format_quantity(days) if days < 1 else format_number(days, 0)

        unit = self.packageUnit.currentText()
        rows = []
        for uses in (monthly_uses, annual_uses):
            rows.append((
                format_number(uses, 2, trim=True),
                f"{format_quantity(average * uses)} {unit}",
                format_number(cost_per_use * uses, 2),
            ))

        product = self.productInput.text().strip()
        package = format(amount, "f").replace(".", ",")
        self.forecastProduct.setText(f"{product} · {package} {unit}")
        self.durationValue.setText(f"{days_text} дн.")
        self.set_forecast_rows(rows)

    def load_price(self):
        self.price_loader.load(self.productLinkInput.text())

    def set_price_loading(self, loading):
        for field in (self.productLinkInput, self.loadPriceButton, self.addOfferButton,
                      self.storeInput, self.offerPriceInput):
            field.setEnabled(not loading)
        if loading:
            self.statusBar().showMessage("Загружаю цену…")
        else:
            self.statusBar().clearMessage()

    def apply_loaded_price(self, store, price):
        self.set_price_loading(False)
        self.storeInput.setText(store)
        self.offerPriceInput.setText(format(price, "f").replace(".", ","))
        self.statusBar().showMessage("Цена загружена. Проверьте количество в упаковке и нажмите «Добавить».", 8000)
        if self.sectionStack.currentWidget() is self.pricesPage:
            self.offerPackageInput.setFocus()

    def show_price_error(self, message):
        self.set_price_loading(False)
        QMessageBox.warning(self, "Загрузка цены", message)

    def add_offer(self):
        store = self.storeInput.text().strip()
        try:
            if not store:
                raise ValueError("Введите название магазина.")
            amount, price = read_package(self.offerPackageInput.text(), self.offerPriceInput.text())
        except ValueError as error:
            QMessageBox.warning(self, "Проверьте данные", str(error))
            return

        self.offers.append(Offer(store, amount, price))
        self.offers.sort(key=lambda offer: offer.price_per_unit)
        self.update_offers_table()
        self.productLinkInput.clear()
        self.storeInput.clear()
        self.offerPackageInput.clear()
        self.offerPriceInput.clear()
        self.storeInput.setFocus()
        self.statusBar().clearMessage()

    def remove_offer(self):
        selected = self.offersTable.selectionModel().selectedRows()
        if selected:
            del self.offers[selected[0].row()]
            self.update_offers_table()

    def update_remove_button(self):
        self.removeOfferButton.setEnabled(self.offersTable.selectionModel().hasSelection())

    def update_offers_table(self):
        unit = self.offerUnit.currentText()
        scale = 1 if unit == "шт." else 100
        quantity = f"{scale} {unit}"
        self.offerUnit.setEnabled(not self.offers)

        table = self.offersTable
        table.horizontalHeaderItem(3).setText(f"За {quantity}, ₽")
        table.setRowCount(0)
        table.setRowCount(len(self.offers))
        for row, offer in enumerate(self.offers):
            amount = format(offer.amount, "f").replace(".", ",")
            price = format_number(Fraction(offer.price), 2)
            normalized_price = format_number(offer.price_per_unit * scale, 2)
            for column, text in enumerate((offer.store, f"{amount} {unit}", price, normalized_price)):
                item = QTableWidgetItem(text)
                if column > 0:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                table.setItem(row, column, item)
        self.update_remove_button()

        if len(self.offers) < 2:
            self.bestPriceLabel.setText("Добавьте два предложения по одному товару для сравнения.")
            return

        best_price = self.offers[0].price_per_unit
        stores = dict.fromkeys(offer.store for offer in self.offers if offer.price_per_unit == best_price)
        best_amount = format_number(best_price * scale, 2)
        self.bestPriceLabel.setText(f"Лучшая цена — {best_amount} ₽ за {quantity} ({', '.join(stores)}).")

    def update_characteristics_package(self):
        self.characteristicsProduct.setText(self.productInput.text().strip())
        self.labelPackageInput.clear()
        self.packageShareLabel.setText("Укажите количество в упаковке на вкладке «Расход».")
        try:
            amount = read_package_amount(self.packageInput.text())
        except (ValueError, InvalidOperation):
            return

        unit = self.packageUnit.currentText()
        package = format(amount, "f").replace(".", ",")
        self.labelPackageInput.setText(f"{package} {unit}")
        try:
            samples = read_measurements(self.measurementsInput.toPlainText())
        except (ValueError, InvalidOperation):
            self.packageShareLabel.setText("Для расчёта доли упаковки проверьте замеры на вкладке «Расход».")
            return

        share = 100 * average_usage(samples) / Fraction(amount)
        self.packageShareLabel.setText(f"Расход за применение — {format_quantity(share)} % упаковки")

    def analyze_composition(self):
        components = {}
        repeated = {}
        text = self.compositionInput.toPlainText().replace(";", "\n")
        for entry in text.splitlines():
            component = " ".join(normalize("NFC", entry).split())
            if not component:
                continue
            key = component.casefold()
            if key in components:
                repeated[key] = components[key]
            else:
                components[key] = component

        if not components:
            self.componentCount.setText("Введите состав с этикетки.")
            return

        result = f"Уникальных компонентов — {len(components)}"
        if repeated:
            result += f"\nПовторы — {', '.join(repeated.values())}"
        self.componentCount.setText(result)

    def update_effectiveness(self):
        tested = self.testCountInput.value()
        successful = self.successCountInput.value()
        result = "—"
        error = ""
        if successful > tested:
            error = f"Успешных применений может быть от 0 до {tested}."
        elif tested:
            percent = Fraction(100 * successful, tested)
            result = f"{format_quantity(percent)} %"
        self.effectivenessValue.setText(result)
        self.effectivenessError.setText(error)
        self.effectivenessError.setVisible(bool(error))


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = HouseholdGoodsWindow()
    window.show()
    sys.exit(app.exec())
