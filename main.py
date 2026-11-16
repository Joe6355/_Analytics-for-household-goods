import logging
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from PyQt6 import uic
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QApplication, QMainWindow, QMessageBox


PROJECT_DIR = Path(__file__).resolve().parent
LOGGER = logging.getLogger(__name__)
NUMBER_PATTERN = re.compile(r"[+-]?(?:[0-9]+(?:[.,][0-9]+)?|[.,][0-9]+)")


def validate_usage(product, amount, price, measurements):
    if not product.strip():
        raise ValueError("Введите название товара.")

    amount = amount.strip()
    if not NUMBER_PATTERN.fullmatch(amount):
        raise ValueError("Введите количество в упаковке числом.")
    if Decimal(amount.replace(",", ".")) <= 0:
        raise ValueError("Количество в упаковке должно быть больше нуля.")

    price = price.strip()
    if not NUMBER_PATTERN.fullmatch(price) or Decimal(price.replace(",", ".")) < 0:
        raise ValueError("Введите цену числом от нуля и выше.")

    lines = measurements.splitlines()
    if not any(line.strip() for line in lines):
        raise ValueError("Добавьте хотя бы один замер расхода.")

    for line_number, line in enumerate(lines, start=1):
        sample = line.strip()
        if not sample:
            continue
        if not NUMBER_PATTERN.fullmatch(sample) or Decimal(sample.replace(",", ".")) <= 0:
            raise ValueError(f"Замер в строке {line_number} должен быть числом больше нуля.")


class HouseholdGoodsWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        uic.loadUi(str(PROJECT_DIR / "household_goods.ui"), self)
        self.sidebarImage.setPixmap(QPixmap(str(PROJECT_DIR / "household-goods-sidebar.png")))
        self.calculateUsageButton.clicked.connect(self.check_usage)

    def check_usage(self):
        self.statusBar().clearMessage()
        self.calculateUsageButton.setEnabled(False)
        try:
            validate_usage(
                product=self.productInput.text(),
                amount=self.packageInput.text(),
                price=self.packagePriceInput.text(),
                measurements=self.measurementsInput.toPlainText(),
            )
        except ValueError as error:
            QMessageBox.warning(self, "Проверьте данные", str(error))
        except InvalidOperation:
            QMessageBox.warning(self, "Ошибка ввода", "Невозможно преобразовать значение в число.")
        except Exception:
            LOGGER.exception("Непредвиденная ошибка при проверке расхода")
            QMessageBox.critical(self, "Ошибка проверки", "Не удалось завершить проверку данных.")
        else:
            self.statusBar().showMessage("Все данные успешно проверены.", 3000)
        finally:
            self.calculateUsageButton.setEnabled(True)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = HouseholdGoodsWindow()
    window.show()
    sys.exit(app.exec())
