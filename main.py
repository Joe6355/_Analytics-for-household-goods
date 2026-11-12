import sys
from pathlib import Path

from PyQt6 import uic
from PyQt6.QtCore import QLocale
from PyQt6.QtWidgets import QApplication, QMainWindow

from calculator import CalculatorPage


UI_DIR = Path(__file__).with_name("ui")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        uic.loadUi(str(UI_DIR / "main.ui"), self)
        self.calculator = CalculatorPage()
        self.tabs.addTab(self.calculator, "Расход")


def main():
    QLocale.setDefault(QLocale("ru_RU"))
    app = QApplication(sys.argv)
    app.setApplicationName("Аналитика товаров для дома")
    app.setStyle("Fusion")
    app.setStyleSheet((UI_DIR / "app.qss").read_text(encoding="utf-8"))
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
