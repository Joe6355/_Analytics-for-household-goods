import logging
import re
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path

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


def read_package(amount, price):
    package_amount = read_number(amount, "Введите количество в упаковке числом.")
    if package_amount <= 0:
        raise ValueError("Количество в упаковке должно быть больше нуля.")

    price_error = "Введите цену числом от нуля и выше."
    package_price = read_number(price, price_error)
    if package_price < 0:
        raise ValueError(price_error)
    return package_amount, package_price


def validate_usage(product, amount, price, measurements):
    if not product.strip():
        raise ValueError("Введите название товара.")

    package_amount, package_price = read_package(amount, price)
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


def format_number(value, places):
    scale = 10 ** places
    rounded = int(value * scale + Fraction(1, 2))
    whole, remainder = divmod(rounded, scale)
    return f"{Decimal(whole):f},{remainder:0{places}d}"


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
        self.packageUnit.currentTextChanged.connect(self.set_usage_unit)
        self.set_usage_unit(self.packageUnit.currentText())

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

        self.sectionStack.currentChanged.connect(self.update_demo_note)
        self.update_demo_note()

    def update_demo_note(self):
        page = self.sectionStack.currentWidget()
        self.demoNote.setVisible(page not in (self.usagePage, self.pricesPage))

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
            average_text = format_number(average, 3).rstrip("0").rstrip(",")
            if average_text == "0":
                average_text = "< 0,001"
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


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = HouseholdGoodsWindow()
    window.show()
    sys.exit(app.exec())
