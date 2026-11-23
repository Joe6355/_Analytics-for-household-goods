import csv
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from main import HouseholdGoodsWindow


class AccountCSVTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.window = HouseholdGoodsWindow(state_path=self.path / "workspace.json")
        self.addCleanup(self.window.deleteLater)
        self.account = self.window.account
        self.csv = self.window.personal_account_csv

    def load(self, rows):
        path = self.path / "history.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as file:
            csv.writer(file, delimiter=";").writerows(rows)
        with patch("lr5_Dsv_2_personal_account.QFileDialog.getOpenFileName", return_value=(str(path), "")):
            self.csv.load_button.click()

    def test_pending_server_reply_does_not_replace_csv_and_return_refreshes(self):
        self.account.token = "test-token"
        with patch.object(self.account, "send") as send:
            self.account.refresh_history()
            callback = send.call_args.args[1]
            self.load([["Дата", "Товар", "Действие"], ["20.11.2026", "CSV", "Расчёт"]])
            callback(200, {"entries": [{"product": "Сервер", "action": "Расчёт", "details": "6,28 ₽"}]}, "")
            self.assertEqual(self.window.historyTable.item(0, 1).text(), "CSV")
            self.assertIn("CSV", self.window.historyHint.text())
            self.account.refresh_history()
            self.assertEqual(send.call_count, 1)
            self.csv.account_button.click()
            self.assertEqual(self.window.historyTable.item(0, 1).text(), "Сервер")
            self.assertEqual(send.call_count, 2)

    def test_export_import_preserves_details_with_quotes_and_newlines(self):
        details = 'Цена; "349 ₽"\nРасход 9 мл < 10 мл'
        self.account.show_history([{"created_at": "2026-11-20T10:00:00Z", "product": "Гель",
                                    "action": "Расчёт", "details": details}])
        path = self.path / "export.csv"
        with patch("lr5_Dsv_2_personal_account.QFileDialog.getSaveFileName", return_value=(str(path), "")), \
                patch("lr5_Dsv_2_personal_account.QMessageBox.information"):
            self.csv.save_button.click()
        with path.open(encoding="utf-8-sig", newline="") as file:
            rows = list(csv.reader(file, delimiter=";"))
        self.assertEqual(rows[0], ["Дата", "Товар", "Действие", "Подробности"])
        self.assertEqual(rows[1][3], details)
        self.load(rows)
        item = self.window.historyTable.item(0, 2)
        self.assertEqual(item.data(Qt.ItemDataRole.UserRole), details)
        self.assertIn("&lt;", item.toolTip())

    def test_invalid_import_preserves_current_rows(self):
        self.account.show_history([{"product": "Гель"}])
        with patch("lr5_Dsv_2_personal_account.QMessageBox.warning") as warning:
            self.load([["Дата", "Товар", "Действие"], ["20.11.2026", "CSV"]])
        warning.assert_called_once()
        self.assertEqual(self.window.historyTable.item(0, 1).text(), "Гель")
        self.assertIsNone(self.account.csv_rows)

    def test_unclosed_quote_does_not_replace_imported_history(self):
        self.load([["Дата", "Товар", "Действие"], ["20.11.2026", "Гель", "Расчёт"]])
        broken = self.path / "broken.csv"
        broken.write_text('Дата;Товар;Действие\n21.11.2026;Другой товар;"Расчёт', encoding="utf-8")

        with patch("lr5_Dsv_2_personal_account.QFileDialog.getOpenFileName", return_value=(str(broken), "")), \
                patch("lr5_Dsv_2_personal_account.QMessageBox.warning") as warning:
            self.csv.load_button.click()
        warning.assert_called_once()
        self.assertEqual(self.window.historyTable.rowCount(), 1)
        self.assertEqual(self.window.historyTable.item(0, 1).text(), "Гель")

    def test_session_change_clears_import(self):
        self.load([["Дата", "Товар", "Действие"], ["20.11.2026", "CSV", "Расчёт"]])
        self.account.show_session()
        self.assertIsNone(self.account.csv_rows)
        self.assertEqual(self.window.historyTable.rowCount(), 0)

    def test_history_shows_full_details_and_reopens_original_inputs(self):
        self.account.token = "test-token"
        title = ("Гель для мытья посуды с длинным названием " * 5).strip()
        samples = "\n".join(["8", "10"] * 175)
        self.window.productInput.setText(title)
        self.window.packageInput.setText("900")
        self.window.packagePriceInput.setText("270")
        self.window.measurementsInput.setPlainText(samples)
        with patch.object(self.account, "send") as send:
            self.window.calculateUsageButton.click()
            payload = send.call_args.kwargs["data"]
            self.assertEqual(payload["product"], title)
            self.assertGreater(len(payload["details"]), 600)
            self.account.show_history([
                {"id": 3, **payload},
                {"id": 2, "product": "Губки", "action": "Расчёт расхода", "details": "Старая запись"},
            ])
            self.window.historySearchInput.setText("Полных применений")
            self.assertFalse(self.window.historyTable.isRowHidden(0))
            self.assertTrue(self.window.historyTable.isRowHidden(1))
            self.window.historyTable.selectRow(0)
            self.assertTrue(self.window.historyDetails.toPlainText().endswith(samples))
            self.assertTrue(self.window.openHistoryButton.isEnabled())
            self.window.packagePriceInput.setText("999")
            self.window.openHistoryButton.click()
            self.assertIs(self.window.sectionStack.currentWidget(), self.window.usagePage)
            self.assertEqual(self.window.packagePriceInput.text(), "270")
            self.assertEqual(self.window.usageCostValue.text(), "2,70 ₽")
            self.assertEqual(send.call_count, 1)

        self.window.historySearchInput.clear()
        self.window.historyTable.selectRow(1)
        self.assertIn("Старая запись", self.window.historyDetails.toPlainText())
        self.assertFalse(self.window.openHistoryButton.isEnabled())
