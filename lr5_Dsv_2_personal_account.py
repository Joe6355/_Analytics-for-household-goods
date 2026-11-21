
import csv
import io
from pathlib import Path

from PyQt6.QtCore import QIODevice, QSaveFile, Qt
from PyQt6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)


HEADERS = ["Дата", "Товар", "Действие"]
EXPORT_HEADERS = HEADERS + ["Подробности"]


class PersonalAccountCSV:
    def __init__(self, window):
        self.window = window
        self.table = window.historyTable

        # кнопки под историей
        self.load_button = QPushButton("Загрузить CSV", window.accountPage)
        self.load_button.setObjectName("loadHistoryCsvButton")

        self.save_button = QPushButton("Сохранить CSV", window.accountPage)
        self.save_button.setObjectName("saveHistoryCsvButton")
        self.account_button = QPushButton("История аккаунта", window.accountPage)
        self.account_button.setObjectName("showAccountHistoryButton")

        buttons = QHBoxLayout()
        buttons.addWidget(self.load_button)
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.account_button)

        history_layout = window.findChild(QVBoxLayout, "historyLayout")
        history_layout.insertLayout(
            history_layout.indexOf(self.table) + 1, buttons
        )

        self.load_button.clicked.connect(self.load_csv)
        self.save_button.clicked.connect(self.save_csv)
        self.account_button.clicked.connect(window.account.restore_history)

    def load_csv(self):
        path, _ = QFileDialog.getOpenFileName(
            self.window, "Загрузить историю", "", "CSV-файлы (*.csv)"
        )
        if not path:
            return

        # читаем csv и проверяем строки
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as file:
                reader = csv.DictReader(file, delimiter=";", strict=True)

                if reader.fieldnames not in (HEADERS, EXPORT_HEADERS):
                    raise ValueError("Нужны столбцы: Дата, Товар, Действие и необязательный столбец Подробности.")

                rows = []

                for number, record in enumerate(reader, start=2):
                    if None in record or any(
                        record[name] is None for name in reader.fieldnames
                    ):
                        raise ValueError(f"Ошибка в строке {number}")

                    row = [record[name].strip() for name in HEADERS]

                    if not all(row):
                        raise ValueError(f"Пустые данные в строке {number}")

                    rows.append((*row, record.get("Подробности", "")))

        except (OSError, UnicodeError, csv.Error, ValueError) as error:
            QMessageBox.warning(
                self.window, "Ошибка загрузки CSV", str(error)
            )
            return

        self.window.account.show_csv_history(rows)

    def save_csv(self):
        path, _ = QFileDialog.getSaveFileName(
            self.window,
            "Сохранить историю",
            "history.csv",
            "CSV-файлы (*.csv)"
        )
        if not path:
            return

        if Path(path).suffix.lower() != ".csv":
            path += ".csv"

        # QSaveFile заменяет файл только после успешной записи всех строк.
        try:
            buffer = io.StringIO(newline="")
            writer = csv.writer(buffer, delimiter=";")
            writer.writerow(EXPORT_HEADERS)
            for row in range(self.table.rowCount()):
                values = []
                for column in range(len(HEADERS)):
                    item = self.table.item(row, column)
                    values.append(item.text() if item else "")
                action = self.table.item(row, 2)
                values.append((action.data(Qt.ItemDataRole.UserRole) if action else "") or "")
                writer.writerow(values)
            data = buffer.getvalue().encode("utf-8-sig")
            file = QSaveFile(path)
            if not file.open(QIODevice.OpenModeFlag.WriteOnly):
                raise OSError(file.errorString())
            if file.write(data) != len(data):
                file.cancelWriting()
                raise OSError(file.errorString())
            if not file.commit():
                raise OSError(file.errorString())

        except (OSError, UnicodeError, csv.Error) as error:
            QMessageBox.warning(
                self.window, "Ошибка сохранения CSV", str(error)
            )
            return

        QMessageBox.information(
            self.window,
            "Готово",
            f"Сохранено записей: {self.table.rowCount()}"
        )
