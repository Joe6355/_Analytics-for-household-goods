import os
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QMessageBox

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

    def test_autosave_records_edits_before_the_window_is_closed(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "workspace.json"
            window = HouseholdGoodsWindow(state_path=path)
            try:
                self.new_comparison(window, "Fairy")
                window.apply_loaded_product("Магазин", ProductPage(Decimal(239), CARD_URL, "Fairy", (Decimal(900), "мл")))
                window.measurementsInput.setPlainText("8,5\n9,5")
                QTest.qWait(window.workspace_save_timer.interval() + 250)

                saved = LocalWorkspace(path).load()
                active = saved.comparisons[saved.active]
                self.assertEqual((active.name, active.offers[0].price), ("Fairy", Decimal(239)))
                self.assertEqual(saved.draft["measurements"], "8,5\n9,5")

                window.measurementsInput.setPlainText("9\n10")
                QTest.qWait(window.workspace_save_timer.interval() + 250)
                self.assertEqual(LocalWorkspace(path).load().draft["measurements"], "9\n10")
            finally:
                window.close()

    def test_list_actions_keep_other_lists_and_respect_cancel(self):
        window = HouseholdGoodsWindow()
        self.addCleanup(window.close)
        self.new_comparison(window, "Fairy")
        window.apply_loaded_product("Первый магазин", ProductPage(Decimal(239), CARD_URL, "Fairy", (Decimal(900), "мл")))
        self.new_comparison(window, "Губки")
        window.apply_loaded_product("Второй магазин", ProductPage(Decimal(80), "", "Губки", (Decimal(4), "шт.")))
        window.comparisonSelector.setCurrentIndex(1)
        window.offersTable.selectRow(0)

        rename = window.manageComparisonButton.menu().actions()[0]
        with patch("main.QInputDialog.getText", return_value=("  Fairy Лимон   ", True)):
            rename.trigger()
        self.assertEqual(window.comparisonSelector.currentText(), "Fairy Лимон")
        self.assertEqual(window.offers[0].store, "Первый магазин")
        self.assertEqual(window.selected_offer_index(), 0)
        with patch("main.QInputDialog.getText", return_value=("губки", True)):
            rename.trigger()
        self.assertEqual(window.comparisonSelector.currentText(), "Fairy Лимон")

        with patch("main.QMessageBox.question", return_value=QMessageBox.StandardButton.Cancel):
            window.delete_comparison_action.trigger()
        self.assertEqual(window.comparisonSelector.count(), 3)
        with patch("main.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            window.delete_comparison_action.trigger()
        self.assertEqual(window.comparisonSelector.count(), 2)
        self.assertEqual(window.comparisonSelector.currentText(), "Губки")
        self.assertEqual((window.offerUnit.currentText(), window.offers[0].store), ("шт.", "Второй магазин"))

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

    def test_user_unit_change_discards_quantities_and_restores_new_values(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "workspace.json"
            window = HouseholdGoodsWindow(state_path=path)
            window.packageInput.setText("500")
            window.measurementsInput.setPlainText("8,5\n9,5")
            window.packageUnit.setCurrentText("г")
            self.assertEqual(window.packageInput.text(), "")
            self.assertEqual(window.measurementsInput.toPlainText(), "")
            window.packageInput.setText("250")
            window.measurementsInput.setPlainText("10")
            window.close()

            restored = HouseholdGoodsWindow(state_path=path)
            self.addCleanup(restored.close)
            self.assertEqual(restored.packageUnit.currentText(), "г")
            self.assertEqual(restored.packageInput.text(), "250")
            self.assertEqual(restored.measurementsInput.toPlainText(), "10")

    def test_offer_transfers_quantity_when_unit_changes(self):
        window = HouseholdGoodsWindow()
        self.addCleanup(window.close)
        window.measurementsInput.setPlainText("9")
        product = ProductPage(Decimal(49), CARD_URL, "Губки", (Decimal(5), "шт."))
        window.apply_loaded_product("Магазин", product)
        window.offersTable.selectRow(0)
        window.useOfferButton.click()
        self.assertEqual(window.packageUnit.currentText(), "шт.")
        self.assertEqual(window.packageInput.text(), "5")
        self.assertEqual(window.measurementsInput.toPlainText(), "")

    def test_corrupt_workspace_is_backed_up_before_saving_new_draft(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "workspace.json"
            original = b'{"version": 1,'
            path.write_bytes(original)
            with patch("main.LOGGER.exception"):
                window = HouseholdGoodsWindow(state_path=path)
            self.assertIsNotNone(window.workspace_notice)
            window.productInput.setText("Новый черновик")
            window.close()
            backups = list(path.parent.glob("workspace.backup-*.json"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), original)
            self.assertEqual(LocalWorkspace(path).load().draft["product"], "Новый черновик")
