import os
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from main import HouseholdGoodsWindow
from store_page import ProductPage
from workspace_state import LocalWorkspace


CARD_URL = "https://market.yandex.ru/card/gel/123456?do-waremd5=abc123"


class WorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def new_comparison(self, window, name):
        with patch("main.QInputDialog.getText", return_value=(name, True)):
            window.newComparisonButton.click()

    def test_separate_comparisons_and_draft_survive_reopening(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "workspace.json"
            window = HouseholdGoodsWindow(state_path=path)
            self.new_comparison(window, "Гель для посуды")
            gel = ProductPage(Decimal("199.99"), CARD_URL, "Гель", (Decimal(500), "мл"), "Цена с Яндекс Пэй")
            window.apply_loaded_product("Маркет", gel)
            self.new_comparison(window, "Губки")
            sponges = ProductPage(Decimal(70), "", "Губки", (Decimal(5), "шт."))
            window.apply_loaded_product("Супермаркет", sponges)
            self.assertEqual(window.offersTable.rowCount(), 1)
            self.assertEqual(window.offerUnit.currentText(), "шт.")

            window.comparisonSelector.setCurrentIndex(1)
            self.assertEqual(window.offersTable.rowCount(), 1)
            self.assertEqual(window.offerUnit.currentText(), "мл")
            self.assertIn("Цена с Яндекс Пэй", window.offersTable.item(0, 5).text())
            window.productInput.setText("Гель")
            window.packageInput.setText("500")
            window.packagePriceInput.setText("199,99")
            window.measurementsInput.setPlainText("8,5\n9,5")
            window.frequencyPeriod.setCurrentText("в год")
            window.frequencyInput.setValue(180)
            window.close()

            restored = HouseholdGoodsWindow(state_path=path)
            try:
                self.assertEqual(restored.comparisonSelector.currentText(), "Гель для посуды")
                self.assertEqual(restored.offers[0].card_url, CARD_URL)
                self.assertEqual(restored.offers[0].price, Decimal("199.99"))
                self.assertEqual(restored.measurementsInput.toPlainText(), "8,5\n9,5")
                self.assertEqual((restored.frequencyPeriod.currentText(), restored.frequencyInput.value()), ("в год", 180))
                restored.comparisonSelector.setCurrentIndex(2)
                self.assertEqual((restored.offerUnit.currentText(), restored.offers[0].product), ("шт.", "Губки"))
                self.assertEqual(restored.offersTable.rowCount(), 1)
            finally:
                restored.close()

    def test_refresh_replaces_selected_price_and_refuses_changed_package(self):
        window = HouseholdGoodsWindow()
        self.addCleanup(window.close)
        window.apply_loaded_product("Маркет", ProductPage(Decimal(260), CARD_URL, "Гель", (Decimal(500), "мл")))
        window.apply_loaded_product("Супермаркет", ProductPage(Decimal(230), "", "Гель", (Decimal(500), "мл")))
        window.offersTable.selectRow(0)
        self.assertFalse(window.refreshOfferButton.isEnabled())
        window.offersTable.selectRow(1)
        with patch.object(window.price_loader, "load") as load:
            window.refreshOfferButton.click()
            load.assert_called_once_with(CARD_URL)
        window.price_loader.started.emit()
        self.assertFalse(window.comparisonSelector.isEnabled())
        window.price_loader.loaded.emit(
            "Маркет", ProductPage(Decimal(199), CARD_URL, "Гель", (Decimal(500), "мл"), "Цена с Яндекс Пэй"),
        )
        self.assertEqual(window.offersTable.rowCount(), 2)
        self.assertEqual(window.offersTable.item(0, 3).text(), "199,00")
        self.assertIn("Цена с Яндекс Пэй", window.offersTable.item(0, 5).text())
        self.assertEqual(window.selected_offer_index(), 0)

        with patch.object(window.price_loader, "load"), patch("main.QMessageBox.warning") as warning:
            window.refreshOfferButton.click()
            window.price_loader.loaded.emit("Маркет", ProductPage(Decimal(80), CARD_URL, "Гель", (Decimal(250), "мл")))
            warning.assert_called_once()
        self.assertEqual(window.offers[0].price, Decimal(199))
        self.assertTrue(window.refreshOfferButton.isEnabled())

    def test_success_cost_uses_total_spent_on_trials(self):
        window = HouseholdGoodsWindow()
        self.addCleanup(window.close)
        window.packageInput.setText("400")
        window.packagePriceInput.setText("80")
        window.measurementsInput.setPlainText("8\n12")
        window.testCountInput.setValue(10)
        window.successCountInput.setValue(8)
        self.assertEqual(window.effectivenessValue.text(), "80 %")
        self.assertEqual(window.successCostValue.text(), "2,50 ₽")
        window.successCountInput.setValue(0)
        self.assertEqual(window.successCostValue.text(), "—")

    def test_old_window_cannot_overwrite_newer_local_state(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "workspace.json"
            first, second = LocalWorkspace(path), LocalWorkspace(path)
            old, new = first.load(), second.load()
            new.draft["measurements"] = "9,5"
            second.save(new)
            old.draft["measurements"] = "5"
            with self.assertRaises(OSError):
                first.save(old)
            self.assertEqual(LocalWorkspace(path).load().draft["measurements"], "9,5")
