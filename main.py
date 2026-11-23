import csv
import logging
import re
import sys
import tempfile
from dataclasses import replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from html import escape
from pathlib import Path

from PyQt6 import uic
from PyQt6.QtCore import QLineF, QPointF, QSignalBlocker, QTimer, Qt, QUrl
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import QApplication, QFileDialog, QHeaderView, QInputDialog, QLabel, QMainWindow, QMenu, QMessageBox, QTableWidgetItem

from account_client import AccountController
from composition_analysis import analyze_components
from lr5_Dsv_2_personal_account import PersonalAccountCSV
from price_loader import PriceLoader
from workspace_state import DEFAULT_COMPARISON_NAME, Comparison, LocalWorkspace, Offer, UsageSamples, WorkspaceState


PROJECT_DIR = Path(__file__).resolve().parent
LOGGER = logging.getLogger(__name__)
NUMBER_PATTERN = re.compile(r"[+-]?(?:[0-9]+(?:[.,][0-9]+)?|[.,][0-9]+)")
FORECAST_GRAPH_DAYS = 30
WORKSPACE_SAVE_DELAY_MS = 500


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


def draw_remaining_graph(remaining):
    preview = QPixmap(236, 86)
    preview.fill(Qt.GlobalColor.transparent)
    left, right, top, bottom = 8, 228, 6, 64
    days = len(remaining) - 1
    points = [
        QPointF(left + (right - left) * day / days, bottom - (bottom - top) * float(amount / remaining[0]))
        for day, amount in enumerate(remaining)
    ]
    line = QPainterPath(points[0])
    for previous, point in zip(points, points[1:]):
        line.lineTo(point.x(), previous.y())
        line.lineTo(point)
    area = QPainterPath(line)
    area.lineTo(right, bottom)
    area.lineTo(left, bottom)
    area.closeSubpath()

    with QPainter(preview) as painter:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillPath(area, QColor("#e1eeeb"))
        painter.setPen(QColor("#cedbd6"))
        for y in (top, (top + bottom) / 2, bottom):
            painter.drawLine(QLineF(left, y, right, y))
        pen = QPen(QColor("#397b76"), 2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawPath(line)

        painter.setPen(QColor("#53646b"))
        metrics = painter.fontMetrics()
        midpoint, endpoint = str(days // 2), f"{days} дн."
        painter.drawText(left, 81, "0")
        painter.drawText((left + right - metrics.horizontalAdvance(midpoint)) // 2, 81, midpoint)
        painter.drawText(right - metrics.horizontalAdvance(endpoint), 81, endpoint)
    return preview


def card_address(value):
    url = QUrl(value, QUrl.ParsingMode.StrictMode)
    if url.isValid() and url.scheme() in ("http", "https") and url.host() and not url.userInfo():
        return url.toString(QUrl.ComponentFormattingOption.FullyEncoded)
    return ""


def current_price_time():
    return datetime.now().astimezone().isoformat(timespec="seconds")


class HouseholdGoodsWindow(QMainWindow):
    def __init__(self, state_path=None):
        super().__init__()
        uic.loadUi(str(PROJECT_DIR / "household_goods.ui"), self)
        self.local_workspace = LocalWorkspace(Path(state_path)) if state_path is not None else None
        self.workspace_state = WorkspaceState()
        self.workspace_save_timer = None
        self.workspace_notice = None
        self.usage_context: tuple[Comparison, str] | None = None
        self.selected_offer_id = None
        self.offer_choices = {}
        self.offer_selectors = (self.usageOfferSelector, self.forecastOfferSelector, self.characteristicsOfferSelector)
        self.set_usage_price_note("")
        load_warning = ""
        if self.local_workspace is not None:
            try:
                self.workspace_state = self.local_workspace.load()
            except ValueError:
                LOGGER.exception("Не удалось восстановить локальное рабочее состояние")
                load_warning = "Черновик повреждён. При сохранении будет создана резервная копия."
            except OSError:
                LOGGER.exception("Не удалось прочитать файл рабочего состояния")
                load_warning = "Черновик недоступен. Проверьте доступ к файлу данных."
        self.sidebarImage.setPixmap(QPixmap(str(PROJECT_DIR / "household-goods-sidebar.png")))
        self.calculateUsageButton.clicked.connect(self.check_usage)
        for field in (self.productInput, self.packageInput, self.packagePriceInput, self.measurementsInput):
            field.textChanged.connect(self.clear_usage_result)
            field.textChanged.connect(self.clear_forecast)
        self.packageUnit.currentTextChanged.connect(self.change_usage_unit)
        self.packageUnit.currentTextChanged.connect(self.clear_forecast)
        self.packagePriceInput.textChanged.connect(lambda: self.set_usage_price_note(""))
        self.set_usage_unit(self.packageUnit.currentText())

        self.forecastProduct.setTextFormat(Qt.TextFormat.PlainText)
        forecast_header = self.forecastTable.horizontalHeader()
        forecast_header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.forecastTable.ensurePolished()
        forecast_header.ensurePolished()
        self.forecastTable.setFixedHeight(
            forecast_header.sizeHint().height()
            + sum(self.forecastTable.rowHeight(row) for row in range(self.forecastTable.rowCount()))
            + 2 * self.forecastTable.frameWidth()
        )
        self.calculateForecastButton.clicked.connect(self.show_forecast)
        self.frequencyInput.valueChanged.connect(self.clear_forecast)
        self.frequencyPeriod.currentIndexChanged.connect(self.clear_forecast)
        self.clear_forecast()

        self.clear_offer_draft()
        self.price_loading = False
        self.refreshing_offer = None
        self.pricesProduct.hide()
        self.price_loader = PriceLoader(self)
        self.price_loader.started.connect(lambda: self.set_price_loading(True))
        self.price_loader.loaded.connect(self.apply_loaded_product)
        self.price_loader.failed.connect(self.show_price_error)
        self.price_loader.progress.connect(self.statusBar().showMessage)
        self.offerPriceInput.textEdited.connect(self.clear_offer_price_note)
        self.productLinkInput.returnPressed.connect(self.load_price)
        self.loadPriceButton.clicked.connect(self.load_price)
        self.loadPriceFileButton.clicked.connect(self.load_price_file)
        self.bestPriceLabel.setTextFormat(Qt.TextFormat.PlainText)
        self.newComparisonButton.clicked.connect(self.create_comparison)
        comparison_menu = QMenu(self.manageComparisonButton)
        comparison_menu.addAction("Переименовать", self.rename_comparison)
        self.delete_comparison_action = comparison_menu.addAction("Удалить список", self.delete_comparison)
        self.manageComparisonButton.setMenu(comparison_menu)
        self.comparisonSelector.currentIndexChanged.connect(self.switch_comparison)
        self.addOfferButton.clicked.connect(self.add_offer)
        self.loadCsvButton.clicked.connect(self.load_offers_csv)
        self.saveCsvButton.clicked.connect(self.save_offers_csv)
        self.useOfferButton.clicked.connect(self.use_selected_offer)
        self.refreshOfferButton.clicked.connect(self.refresh_selected_offer)
        self.removeOfferButton.clicked.connect(self.remove_offer)
        self.offersTable.itemSelectionChanged.connect(self.update_offer_buttons)
        self.offerUnit.currentTextChanged.connect(self.set_comparison_unit)
        self.offersTable.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.offersTable.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.offersTable.verticalHeader().setMinimumSectionSize(34)
        for column, width in enumerate((98, 74, 70, 96, 124), start=1):
            self.offersTable.setColumnWidth(column, width)
        for selector in self.offer_selectors:
            selector.currentIndexChanged.connect(lambda _i, combo=selector: self.choose_offer(combo.currentData()))
        self.load_comparison_selector()

        self.characteristicsProduct.setTextFormat(Qt.TextFormat.PlainText)
        self.componentCount.setTextFormat(Qt.TextFormat.PlainText)
        self.imported_composition = ""
        self.compositionInput.clear()
        self.compositionTable.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.compositionTable.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.compositionTable.setColumnWidth(1, 78)
        self.compositionTable.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.clear_composition_result()
        self.compositionInput.textChanged.connect(self.clear_composition_result)
        self.productInput.textEdited.connect(self.clear_imported_composition)
        self.analyzeCompositionButton.clicked.connect(self.analyze_composition)
        self.saveEffectivenessButton.clicked.connect(self.save_effectiveness)
        self.testCountInput.setValue(0)
        self.successCountInput.setValue(0)
        self.testCountInput.valueChanged.connect(self.update_effectiveness)
        self.successCountInput.valueChanged.connect(self.update_effectiveness)
        for field in (self.productInput, self.packageInput, self.packagePriceInput, self.measurementsInput):
            field.textChanged.connect(self.update_effectiveness)
        self.update_effectiveness()

        self.history_actions = {
            "Расчёт расхода": (self.usagePage, lambda: self.check_usage(record=False)),
            "Прогноз": (self.forecastPage, lambda: self.show_forecast(record=False)),
            "Анализ состава": (self.characteristicsPage, lambda: self.analyze_composition(record=False)),
            "Оценка эффективности": (self.characteristicsPage, self.update_effectiveness),
        }
        self.account = AccountController(self)
        self.personal_account_csv = PersonalAccountCSV(self)
        self.demoNote.hide()
        self.sectionStack.currentChanged.connect(self.update_page)
        self.restore_draft()
        self.update_offer_selector_selection()
        if self.local_workspace is not None:
            self.workspace_save_timer = QTimer(self)
            self.workspace_save_timer.setSingleShot(True)
            self.workspace_save_timer.setInterval(WORKSPACE_SAVE_DELAY_MS)
            self.workspace_save_timer.timeout.connect(self.autosave_workspace)
            for _, _, _, changed in self.draft_bindings():
                changed.connect(self.schedule_workspace_save)
        self.update_page()
        if load_warning:
            self.workspace_notice = QLabel(load_warning)
            self.statusBar().addPermanentWidget(self.workspace_notice)

    @property
    def comparison(self):
        return self.workspace_state.comparisons[self.workspace_state.active]

    @property
    def offers(self):
        return self.comparison.offers

    def load_comparison_selector(self):
        with QSignalBlocker(self.comparisonSelector):
            self.comparisonSelector.clear()
            self.comparisonSelector.addItems([comparison.name for comparison in self.workspace_state.comparisons])
            self.comparisonSelector.setCurrentIndex(self.workspace_state.active)
        self.delete_comparison_action.setEnabled(len(self.workspace_state.comparisons) > 1)
        self.switch_comparison(self.workspace_state.active)

    def comparison_index(self, name):
        return next((index for index, comparison in enumerate(self.workspace_state.comparisons)
                     if comparison.name.casefold() == name.casefold()), None)

    def create_comparison(self):
        name, accepted = QInputDialog.getText(self, "Новый список сравнения", "Название товара")
        if not accepted or not (name := " ".join(name.split())):
            return
        existing = self.comparison_index(name)
        if existing is not None:
            self.comparisonSelector.setCurrentIndex(existing)
            return
        self.workspace_state.comparisons.append(Comparison(name))
        self.workspace_state.active = len(self.workspace_state.comparisons) - 1
        self.load_comparison_selector()

    def rename_comparison(self):
        if self.price_loading:
            return
        name, accepted = QInputDialog.getText(self, "Переименовать список", "Название товара", text=self.comparison.name)
        if not accepted or not (name := " ".join(name.split())) or name == self.comparison.name:
            return
        existing = self.comparison_index(name)
        if existing is not None and existing != self.workspace_state.active:
            self.statusBar().showMessage("Список с таким названием уже есть.", 5000)
            return
        self.comparison.name = name
        self.comparisonSelector.setItemText(self.workspace_state.active, name)
        selected = self.selected_offer_index()
        self.update_offers_table()
        if selected is not None:
            self.offersTable.selectRow(selected)

    def delete_comparison(self):
        comparisons = self.workspace_state.comparisons
        if self.price_loading or len(comparisons) <= 1:
            return
        if self.offers or self.comparison.usage_samples:
            answer = QMessageBox.question(
                self, "Удаление списка", "Удалить выбранный список, его предложения и сохранённые замеры?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        if self.usage_context is not None and self.usage_context[0] is self.comparison:
            self.usage_context = None
        del comparisons[self.workspace_state.active]
        self.workspace_state.active = min(self.workspace_state.active, len(comparisons) - 1)
        self.load_comparison_selector()

    def switch_comparison(self, index):
        if index < 0:
            return
        self.workspace_state.active = index
        self.clear_offer_draft()
        with QSignalBlocker(self.offerUnit):
            self.offerUnit.setCurrentText(self.comparison.unit)
        self.update_offers_table()

    def set_comparison_unit(self, unit):
        self.comparison.unit = unit
        self.update_offers_table()

    def load_offer_selectors(self):
        self.offer_choices = {
            offer.offer_id: (comparison, offer)
            for comparison in self.workspace_state.comparisons
            for offer in comparison.offers
        }
        if self.selected_offer_id not in self.offer_choices:
            self.selected_offer_id = None

        for selector in self.offer_selectors:
            with QSignalBlocker(selector):
                selector.clear()
                manual = "Текущие данные" if selector is self.usageOfferSelector else "Текущие данные из «Расхода»"
                selector.addItem(manual if self.offer_choices else "Добавьте предложение в «Ценах»")
                for offer_id, (comparison, offer) in self.offer_choices.items():
                    amount = format(offer.amount, "f").replace(".", ",")
                    price = format_number(Fraction(offer.price), 2)
                    label = f"{comparison.name} · {offer.store} · {amount} {comparison.unit} · {price} ₽"
                    if offer.product and offer.product.casefold() != comparison.name.casefold():
                        label += f" — {offer.product}"
                    selector.addItem(label, offer_id)
                    selector.setItemData(selector.count() - 1, f"<qt>{escape(label)}</qt>", Qt.ItemDataRole.ToolTipRole)
                selector.setEnabled(bool(self.offer_choices))
        self.update_offer_selector_selection()

    def update_offer_selector_selection(self):
        for selector in self.offer_selectors:
            index = selector.findData(self.selected_offer_id) if self.selected_offer_id else 0
            with QSignalBlocker(selector):
                selector.setCurrentIndex(max(0, index))
            selector.setToolTip(selector.currentData(Qt.ItemDataRole.ToolTipRole) or selector.currentText())

    def choose_offer(self, offer_id):
        if offer_id in self.offer_choices:
            self.apply_offer(*self.offer_choices[offer_id])
            if self.sectionStack.currentWidget() is self.usagePage and not self.measurementsInput.toPlainText().strip():
                self.measurementsInput.setFocus()
        else:
            self.detach_offer()

    def detach_offer(self):
        self.remember_usage_samples()
        self.usage_context = None
        self.selected_offer_id = None
        self.update_offer_selector_selection()
        self.schedule_workspace_save()

    def draft_bindings(self):
        return (
            ("product", self.productInput.text, self.productInput.setText, self.productInput.textChanged),
            ("unit", self.packageUnit.currentText, self.packageUnit.setCurrentText, self.packageUnit.currentTextChanged),
            ("package", self.packageInput.text, self.packageInput.setText, self.packageInput.textChanged),
            ("price", self.packagePriceInput.text, self.packagePriceInput.setText, self.packagePriceInput.textChanged),
            ("measurements", self.measurementsInput.toPlainText, self.measurementsInput.setPlainText, self.measurementsInput.textChanged),
            ("frequency", self.frequencyInput.value, self.frequencyInput.setValue, self.frequencyInput.valueChanged),
            ("period", self.frequencyPeriod.currentText, self.frequencyPeriod.setCurrentText, self.frequencyPeriod.currentTextChanged),
            ("composition", self.compositionInput.toPlainText, self.compositionInput.setPlainText, self.compositionInput.textChanged),
            ("tests", self.testCountInput.value, self.testCountInput.setValue, self.testCountInput.valueChanged),
            ("successes", self.successCountInput.value, self.successCountInput.setValue, self.successCountInput.valueChanged),
        )

    def draft_values(self):
        values = {key: read() for key, read, _, _ in self.draft_bindings()}
        values.update(imported_composition=self.imported_composition, price_note=self.usage_price_note)
        return values

    def apply_draft_values(self, saved):
        for key, read, write, _ in self.draft_bindings():
            value = saved.get(key)
            if type(value) is type(read()):
                write(value)
        composition = self.compositionInput.toPlainText()
        self.imported_composition = composition if saved.get("imported_composition") == composition else ""
        price_note = saved.get("price_note")
        self.set_usage_price_note(price_note if isinstance(price_note, str) else "")

    def can_open_history_record(self, action, snapshot):
        spinboxes = (("frequency", self.frequencyInput), ("tests", self.testCountInput),
                     ("successes", self.successCountInput))
        return (
            action in self.history_actions and isinstance(snapshot, dict)
            and all(type(snapshot.get(key)) is type(read()) for key, read, _, _ in self.draft_bindings())
            and self.packageUnit.findText(snapshot["unit"]) >= 0
            and self.frequencyPeriod.findText(snapshot["period"]) >= 0
            and all(widget.minimum() <= snapshot[key] <= widget.maximum() for key, widget in spinboxes)
        )

    def open_history_record(self, action, snapshot):
        if not self.can_open_history_record(action, snapshot):
            return
        self.detach_offer()
        self.apply_draft_values(snapshot)
        self.clear_usage_result()
        self.clear_forecast()
        self.update_characteristics_package()
        self.analyze_composition(record=False, show_empty=False)
        page, calculate = self.history_actions[action]
        self.sectionList.setCurrentRow(self.sectionStack.indexOf(page))
        calculate()
        self.schedule_workspace_save()

    def restore_draft(self):
        saved = self.workspace_state.draft
        self.apply_draft_values(saved)
        offer_id = saved.get("offer_id")
        if isinstance(offer_id, str) and offer_id in self.offer_choices:
            self.selected_offer_id = offer_id

        comparisons = self.workspace_state.comparisons
        unit = self.packageUnit.currentText()
        index = saved.get("comparison")
        if type(index) is int and 0 <= index < len(comparisons):
            self.usage_context = (comparisons[index], unit)
        elif "comparison" not in saved and (name := str(saved.get("product", "")).strip().casefold()):
            # Старый черновик связываем со списком, только когда совпадение однозначно.
            matches = [
                comparison for comparison in comparisons
                if comparison.unit == unit and any(offer.product.casefold() == name for offer in comparison.offers)
            ]
            if len(matches) == 1:
                self.usage_context = (matches[0], unit)

    def remember_usage_samples(self):
        if self.usage_context is None:
            return
        comparison, unit = self.usage_context
        comparison.usage_samples[unit] = UsageSamples(
            self.measurementsInput.toPlainText(), self.testCountInput.value(), self.successCountInput.value(),
        )

    def set_usage_samples(self, samples):
        self.measurementsInput.setPlainText(samples.measurements)
        self.testCountInput.setValue(samples.tests)
        self.successCountInput.setValue(samples.successes)

    def set_usage_price_note(self, note):
        self.usage_price_note = note
        self.usagePriceNote.setText(f"Условие цены — {note}" if note else "")
        self.usagePriceNote.setVisible(bool(note))
        self.update_forecast_product()

    def schedule_workspace_save(self):
        if self.workspace_save_timer is not None:
            self.workspace_save_timer.start()

    def save_workspace(self):
        self.remember_usage_samples()
        self.workspace_state.draft = self.draft_values()
        comparison = self.usage_context[0] if self.usage_context is not None else None
        self.workspace_state.draft.update(
            comparison=next((i for i, item in enumerate(self.workspace_state.comparisons) if item is comparison), None),
            offer_id=self.selected_offer_id,
        )
        backup = self.local_workspace.save(self.workspace_state)
        if self.workspace_notice is not None:
            self.statusBar().removeWidget(self.workspace_notice)
            self.workspace_notice.deleteLater()
            self.workspace_notice = None
        if backup:
            self.statusBar().showMessage(f"Черновик сохранён. Резервная копия: {backup.name}", 12000)

    def autosave_workspace(self):
        try:
            self.save_workspace()
        except OSError as error:
            self.statusBar().showMessage(f"Не удалось сохранить сравнения и замеры. {error}", 9000)

    def closeEvent(self, event):
        if self.workspace_save_timer is not None:
            self.workspace_save_timer.stop()
            try:
                self.save_workspace()
            except OSError as error:
                reply = QMessageBox.question(
                    self, "Сохранение данных",
                    f"Не удалось сохранить сравнения и замеры. {error}\nЗакрыть приложение без сохранения?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel,
                )
                if reply != QMessageBox.StandardButton.Yes:
                    event.ignore()
                    return
        super().closeEvent(event)

    def update_page(self):
        page = self.sectionStack.currentWidget()
        if page is self.characteristicsPage:
            self.update_characteristics_package()
            if self.imported_composition and self.compositionTable.rowCount() == 0:
                self.analyze_composition(record=False, show_empty=False)
        elif page is self.accountPage:
            self.account.refresh_history()

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

    def record_calculation(self, action, details, *, success_message=""):
        self.account.record(
            action, self.productInput.text(), details, snapshot=self.draft_values(), success_message=success_message,
        )

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

    def set_forecast_rows(self, rows):
        for row, values in enumerate(rows):
            for column, text in enumerate(values, start=1):
                self.forecastTable.item(row, column).setText(text)

    def update_forecast_product(self):
        name = self.productInput.text().strip()
        amount = self.packageInput.text().strip()
        text = f"{name} · {amount} {self.packageUnit.currentText()}" if name and amount else name
        if self.usage_price_note:
            text += f"\nУсловие цены — {self.usage_price_note}"
        self.forecastProduct.setText(text.strip() or "Данные из вкладки «Расход»")

    def clear_forecast(self):
        self.update_forecast_product()
        self.forecastHint.hide()
        self.durationValue.setText("—")
        self.durationValue.show()
        self.forecastDurationCaption.hide()
        self.forecastGraphPreview.setText("График появится после расчёта")
        self.forecastGraphPreview.setAccessibleDescription("")
        self.forecastGraphValue.hide()
        self.set_forecast_rows((("—", "—", "—"),) * self.forecastTable.rowCount())

    def show_forecast(self, *, record=True):
        try:
            amount, price, samples = self.read_usage_fields()
        except ValueError as error:
            self.forecastHint.setText(f"На вкладке «Расход»: {error}")
            self.durationValue.hide()
            self.forecastHint.show()
            return
        except InvalidOperation:
            self.forecastHint.setText("Проверьте числа на вкладке «Расход».")
            self.durationValue.hide()
            self.forecastHint.show()
            return

        average, cost_per_use, full_uses = calculate_usage(amount, price, samples)
        annual_uses = self.frequencyInput.value()
        if self.frequencyPeriod.currentText() == "в месяц":
            annual_uses *= 12
        days = Fraction(365 * full_uses, annual_uses)
        days_text = format_quantity(days) if days < 1 else format_number(days, 0)

        unit = self.packageUnit.currentText()
        rows = []
        for uses in (Fraction(annual_uses, 12), annual_uses):
            rows.append((
                format_number(uses, 2, trim=True),
                f"{format_quantity(average * uses)} {unit}",
                format_number(cost_per_use * uses, 2),
            ))

        self.update_forecast_product()
        self.forecastHint.hide()
        self.durationValue.show()
        self.forecastDurationCaption.show()
        monthly_row, yearly_row = rows
        self.durationValue.setText(f"≈ {days_text} дн.")
        self.set_forecast_rows(rows)
        self.show_remaining_graph(Fraction(amount), average, full_uses, annual_uses, unit)
        if record:
            details = (
                f"Упаковки хватит примерно на {days_text} дн.\n"
                f"За месяц — {monthly_row[0]} применений, {monthly_row[1]}, {monthly_row[2]} ₽\n"
                f"За год — {yearly_row[0]} применений, {yearly_row[1]}, {yearly_row[2]} ₽\n\n"
                f"Частота — {self.frequencyInput.value()} {self.frequencyPeriod.currentText()}\n"
                f"{self.calculation_inputs()}"
            )
            self.record_calculation("Прогноз", details)

    def show_remaining_graph(self, amount, average, full_uses, annual_uses, unit):
        remaining = [
            amount - average * min(day * annual_uses // 365, full_uses)
            for day in range(FORECAST_GRAPH_DAYS + 1)
        ]
        text = f"Через {FORECAST_GRAPH_DAYS} дней — {format_quantity(remaining[-1])} {unit}"
        self.forecastGraphPreview.setPixmap(draw_remaining_graph(remaining))
        self.forecastGraphPreview.setAccessibleDescription(text)
        self.forecastGraphValue.setText(text)
        self.forecastGraphValue.show()

    def load_price(self):
        self.price_loader.load(self.productLinkInput.text())

    def clear_offer_price_note(self):
        self.offer_price_note = ""
        self.offer_loaded_at = ""

    def load_price_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Сохранённая карточка товара", "", "Страницы HTML (*.html *.htm)")
        if path:
            self.price_loader.load_file(path)

    def set_price_loading(self, loading):
        self.price_loading = loading
        for field in (self.productLinkInput, self.loadPriceButton, self.loadPriceFileButton, self.addOfferButton, self.loadCsvButton,
                      self.storeInput, self.offerPackageInput, self.offerPriceInput,
                      self.comparisonSelector, self.newComparisonButton, self.manageComparisonButton):
            field.setEnabled(not loading)
        self.offerUnit.setEnabled(not loading and not self.offers)
        self.update_offer_buttons()
        if loading:
            self.statusBar().showMessage("Загружаю карточку…")
        else:
            self.statusBar().clearMessage()

    def apply_loaded_product(self, store, product):
        if self.refreshing_offer is not None:
            self.apply_refreshed_price(product)
            return
        self.set_price_loading(False)
        amount, unit = product.package or (None, "")
        current_unit = self.offerUnit.currentText()
        if unit and self.offers and unit != current_unit:
            self.show_price_error(
                f"В карточке указаны {unit}, а в таблице сравниваются {current_unit}. "
                "Удалите текущие предложения, чтобы сменить единицу сравнения."
            )
            return

        self.offer_name = product.name
        self.offer_price_note = product.price_note
        self.offer_composition = product.composition
        self.offer_card_url = card_address(product.url)
        self.offer_loaded_at = current_price_time()
        amount_text = format(amount, "f").replace(".", ",") if amount is not None else ""
        price_text = format(product.price, "f").replace(".", ",")
        self.storeInput.setText(store)
        self.offerPackageInput.setText(amount_text)
        self.offerPriceInput.setText(price_text)
        if unit:
            self.offerUnit.setCurrentText(unit)
        package_text = f"{amount_text} {unit}" if unit else "упаковку"
        details = f"{price_text} ₽ за {package_text}."
        if product.price_note:
            details += f" {product.price_note}."
        if self.offer_card_url:
            details += f"\nКарточка — {self.offer_card_url}"
        self.account.record("Загрузка цены", product.name, f"{store} — {details}" if store else details)

        missing = [field for value, field in ((store, "магазин"), (unit, "фасовку")) if not value]
        if missing:
            self.statusBar().showMessage(f"Укажите {' и '.join(missing)} и нажмите «Добавить».", 10000)
            focus = self.offerPackageInput if store else self.storeInput
        else:
            self.add_offer()
            self.statusBar().showMessage("Предложение добавлено.", 6000)
            focus = self.productLinkInput
        if self.sectionStack.currentWidget() is self.pricesPage:
            focus.setFocus()

    def show_price_error(self, message):
        self.refreshing_offer = None
        self.set_price_loading(False)
        QMessageBox.warning(self, "Загрузка карточки", message)

    def refresh_selected_offer(self):
        row = self.selected_offer_index()
        if self.price_loading or row is None:
            return
        offer = self.offers[row]
        if address := card_address(offer.card_url):
            self.refreshing_offer = offer
            self.price_loader.load(address)

    def apply_refreshed_price(self, product):
        offer = self.refreshing_offer
        same_name = " ".join(product.name.split()).casefold() == " ".join(offer.product.split()).casefold()
        if not product.name or not same_name or product.package != (offer.amount, self.comparison.unit):
            self.show_price_error("Название или фасовка карточки изменились либо недоступны. Добавьте её как новое предложение.")
            return
        index = next((row for row, item in enumerate(self.offers) if item is offer), None)
        if index is None:
            self.show_price_error("Предложение для обновления отсутствует.")
            return

        updated = replace(
            offer, price=product.price, price_note=product.price_note, loaded_at=current_price_time(),
            composition=product.composition or offer.composition,
        )
        self.offers[index] = updated
        self.offers.sort(key=lambda item: item.price_per_unit)
        self.refreshing_offer = None
        self.set_price_loading(False)
        self.update_offers_table()
        composition_changed = updated.composition != offer.composition
        if self.selected_offer_id == offer.offer_id:
            if self.usage_price_note == offer.price_note:
                try:
                    current_price = read_number(self.packagePriceInput.text(), "")
                except (ValueError, InvalidOperation):
                    current_price = None
                if current_price == offer.price:
                    self.packagePriceInput.setText(format(updated.price, "f").replace(".", ","))
                    self.set_usage_price_note(updated.price_note)
            current_composition = self.compositionInput.toPlainText()
            if composition_changed and (not current_composition.strip() or current_composition == self.imported_composition):
                self.compositionInput.setPlainText(updated.composition)
                self.imported_composition = updated.composition
                self.analyze_composition(record=False)
        self.offersTable.selectRow(next(row for row, item in enumerate(self.offers) if item is updated))
        old_price = format_number(Fraction(offer.price), 2)
        new_price = format_number(Fraction(updated.price), 2)
        details = f"{updated.store}\nПрежняя цена — {old_price} ₽\nОбновлённая цена — {new_price} ₽"
        if updated.price_note:
            details += f"\nУсловие цены — {updated.price_note}"
        self.account.record("Обновление цены", updated.product, details)
        message = "Цена и состав обновлены." if composition_changed else "Цена обновлена."
        self.statusBar().showMessage(message, 6000)

    def add_offer(self):
        store = self.storeInput.text().strip()
        try:
            if not store:
                raise ValueError("Введите название магазина.")
            amount, price = read_package(self.offerPackageInput.text(), self.offerPriceInput.text())
        except ValueError as error:
            QMessageBox.warning(self, "Проверьте данные", str(error))
            return

        self.offers.append(Offer(
            store, amount, price, self.offer_name, self.offer_price_note, self.offer_composition,
            self.offer_card_url, self.offer_loaded_at,
        ))
        self.offers.sort(key=lambda offer: offer.price_per_unit)
        self.update_offers_table()
        if len(self.offers) >= 2 and self.account.token:
            details = [self.bestPriceLabel.text(), ""]
            heading = self.offersTable.horizontalHeaderItem(4).text().removesuffix(", ₽").casefold()
            for row in range(self.offersTable.rowCount()):
                _, store, package, price, normalized_price, info = (
                    self.offersTable.item(row, column).text() for column in range(self.offersTable.columnCount())
                )
                details.append(f"{store} · {package} · {price} ₽ ({normalized_price} ₽ {heading})")
                if info != "—":
                    details.append(info)
            self.account.record("Сравнение цен", self.comparison.name, "\n".join(details))
        self.clear_offer_draft()
        self.storeInput.setFocus()
        self.statusBar().clearMessage()

    def clear_offer_draft(self):
        self.offer_name = ""
        self.offer_price_note = ""
        self.offer_composition = ""
        self.offer_card_url = ""
        self.offer_loaded_at = ""
        self.productLinkInput.clear()
        self.storeInput.clear()
        self.offerPackageInput.clear()
        self.offerPriceInput.clear()

    def selected_offer_index(self):
        selected = self.offersTable.selectionModel().selectedRows()
        row = selected[0].row() if selected else -1
        return row if 0 <= row < len(self.offers) else None

    def use_selected_offer(self):
        row = self.selected_offer_index()
        if row is None:
            return

        self.apply_offer(self.comparison, self.offers[row])
        self.sectionList.setCurrentRow(self.sectionStack.indexOf(self.usagePage))
        if not self.productInput.text().strip():
            self.productInput.setFocus()
            message = "Укажите название товара, чтобы рассчитать расход."
        elif not self.measurementsInput.toPlainText().strip():
            self.measurementsInput.setFocus()
            message = "Добавьте замеры для выбранного товара."
        else:
            self.calculateUsageButton.setFocus()
            message = "Предложение перенесено в расчёт расхода."
        if self.imported_composition:
            message += " Состав доступен во вкладке «Характеристики»."
        self.statusBar().showMessage(message, 7000)

    def apply_offer(self, comparison, offer):
        unit = comparison.unit
        same_list = self.usage_context is not None and self.usage_context[0] is comparison
        current_product = self.productInput.text().strip()
        product = offer.product or (current_product if same_list else "")
        if not product and comparison.name != DEFAULT_COMPARISON_NAME:
            product = comparison.name
        same_title = product.casefold() == current_product.casefold()
        self.remember_usage_samples()
        samples = comparison.usage_samples.get(unit)
        same_context = same_list and self.usage_context[1] == unit
        keep_manual_samples = (
            self.usage_context is None and bool(product) and same_title
            and unit == self.packageUnit.currentText() and samples is None
        )
        if not (same_context or keep_manual_samples):
            self.set_usage_samples(samples or UsageSamples())

        current_composition = self.compositionInput.toPlainText()
        can_replace_composition = not current_composition.strip() or current_composition == self.imported_composition
        if not same_title or not same_list or can_replace_composition:
            self.compositionInput.setPlainText(offer.composition)
            self.imported_composition = offer.composition
        self.compositionInput.setPlaceholderText("В карточке состава нет. Вставьте его с этикетки.")

        self.productInput.setText(product)
        with QSignalBlocker(self.packageUnit):
            self.packageUnit.setCurrentText(unit)
        self.packageInput.setText(format(offer.amount, "f").replace(".", ","))
        self.packagePriceInput.setText(format(offer.price, "f").replace(".", ","))
        self.set_usage_price_note(offer.price_note)
        self.set_usage_unit(unit)
        self.clear_forecast()
        self.usage_context = (comparison, unit)
        self.selected_offer_id = offer.offer_id
        self.update_offer_selector_selection()
        self.update_characteristics_package()
        self.analyze_composition(record=False, show_empty=False)
        self.schedule_workspace_save()

    def clear_imported_composition(self):
        if self.imported_composition and self.imported_composition == self.compositionInput.toPlainText():
            self.compositionInput.clear()
        self.imported_composition = ""

    def load_offers_csv(self):
        path, _ = QFileDialog.getOpenFileName(self, "Загрузить предложения", "", "CSV (*.csv)")
        if not path:
            return
        try:
            offers = []
            unit = self.offerUnit.currentText()
            units = {self.offerUnit.itemText(i) for i in range(self.offerUnit.count())}
            with open(path, encoding="utf-8-sig", newline="") as stream:
                reader = csv.reader(stream, delimiter=";", strict=True)
                headers = next(reader, [])
                if (len(headers) not in (5, 6, 9)
                        or headers[:4] != ["Товар", "Магазин", "Упаковка", "Цена, ₽"]
                        or headers[4] not in {f"За {1 if u == 'шт.' else 100} {u}, ₽" for u in units}
                        or (len(headers) >= 6 and headers[5] != "Примечание к цене")
                        or (len(headers) == 9 and headers[6:] != ["Состав", "Ссылка", "Дата загрузки"])):
                    raise ValueError("Неверные заголовки CSV. Используйте файл, сохранённый приложением.")
                for row in reader:
                    if not row or all(not cell.strip() for cell in row):
                        continue
                    try:
                        if len(row) != len(headers):
                            raise ValueError("Количество столбцов не совпадает с заголовком.")
                        amount_text, row_unit = row[2].rsplit(maxsplit=1)
                        if row_unit not in units:
                            raise ValueError("Неизвестная единица измерения.")
                        if offers and row_unit != unit:
                            raise ValueError("Все предложения должны иметь одинаковую единицу измерения.")
                        if not row[1].strip():
                            raise ValueError("Не указан магазин.")
                        amount, price = read_package(amount_text, row[3])
                        if len(row) == 9 and row[8]:
                            datetime.fromisoformat(row[8])
                        offers.append(Offer(
                            row[1].strip(), amount, price,
                            "" if row[0] == "—" else row[0],
                            row[5] if len(row) >= 6 else "",
                            *(row[6:] if len(row) == 9 else ("", "", "")),
                        ))
                        unit = row_unit
                    except (ValueError, InvalidOperation) as error:
                        raise ValueError(f"Строка {reader.line_num}: {error}") from error
            offers.sort(key=lambda offer: offer.price_per_unit)
        except (OSError, UnicodeError, ValueError, csv.Error) as error:
            QMessageBox.warning(self, "Ошибка загрузки CSV", str(error))
            return

        self.comparison.offers = offers
        self.comparison.unit = unit
        self.clear_offer_draft()
        with QSignalBlocker(self.offerUnit):
            self.offerUnit.setCurrentText(unit)
        self.update_offers_table()
        self.statusBar().showMessage(f"Загружено предложений: {len(offers)}", 5000)

    def save_offers_csv(self):
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить предложения", "offers.csv", "CSV (*.csv)")
        if not path:
            return
        if not path.lower().endswith(".csv"):
            path += ".csv"
        unit = self.offerUnit.currentText()
        scale = 1 if unit == "шт." else 100
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8-sig", newline="", delete=False,
                dir=Path(path).parent, suffix=".tmp",
            ) as stream:
                temporary_path = Path(stream.name)
                writer = csv.writer(stream, delimiter=";")
                writer.writerow([
                    "Товар", "Магазин", "Упаковка", "Цена, ₽",
                    f"За {scale} {unit}, ₽", "Примечание к цене",
                    "Состав", "Ссылка", "Дата загрузки",
                ])
                for offer in self.offers:
                    writer.writerow([
                        offer.product, offer.store,
                        f"{format(offer.amount, 'f').replace('.', ',')} {unit}",
                        format(offer.price, "f").replace(".", ","),
                        format_number(offer.price_per_unit * scale, 2),
                        offer.price_note, offer.composition, offer.card_url, offer.loaded_at,
                    ])
            temporary_path.replace(path)
        except (OSError, UnicodeError, csv.Error) as error:
            QMessageBox.warning(self, "Ошибка сохранения CSV", str(error))
            return
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        self.statusBar().showMessage("Предложения сохранены в CSV.", 5000)

    def remove_offer(self):
        row = self.selected_offer_index()
        if row is not None and not self.price_loading:
            del self.offers[row]
            self.update_offers_table()

    def update_offer_buttons(self):
        row = self.selected_offer_index()
        offer = self.offers[row] if row is not None else None
        self.useOfferButton.setEnabled(offer is not None)
        self.removeOfferButton.setEnabled(offer is not None and not self.price_loading)
        self.refreshOfferButton.setEnabled(offer is not None and not self.price_loading and bool(card_address(offer.card_url)))
        self.update_offer_difference(offer)

    def update_offer_difference(self, offer):
        if offer is None or len(self.offers) < 2:
            self.offerDifferenceLabel.hide()
            return

        difference = (offer.price_per_unit - self.offers[0].price_per_unit) * Fraction(offer.amount)
        if 0 < difference < Fraction(1, 100):
            money = "меньше 0,01 ₽"
        else:
            money = f"{format_number(difference, 2)} ₽"
        amount = format(offer.amount, "f").replace(".", ",")
        unit = self.offerUnit.currentText()
        self.offerDifferenceLabel.setText(f"Разница с лучшей ценой за {amount} {unit} — {money}.")
        self.offerDifferenceLabel.show()

    def update_offers_table(self):
        unit = self.offerUnit.currentText()
        scale = 1 if unit == "шт." else 100
        quantity = f"{scale} {unit}"
        self.offerUnit.setEnabled(not self.price_loading and not self.offers)

        table = self.offersTable
        table.horizontalHeaderItem(4).setText(f"За {quantity}, ₽")
        table.setRowCount(0)
        table.setRowCount(len(self.offers))
        for row, offer in enumerate(self.offers):
            amount = format(offer.amount, "f").replace(".", ",")
            price = format_number(Fraction(offer.price), 2)
            normalized_price = format_number(offer.price_per_unit * scale, 2)
            loaded_at = datetime.fromisoformat(offer.loaded_at).astimezone().strftime("%d.%m.%y %H:%M") if offer.loaded_at else ""
            card_info = "\n".join(value for value in (loaded_at, offer.price_note) if value) or "—"
            product = offer.product or (self.comparison.name if self.comparison.name != DEFAULT_COMPARISON_NAME else "—")
            for column, text in enumerate((product, offer.store, f"{amount} {unit}", price, normalized_price, card_info)):
                item = QTableWidgetItem(text)
                if 2 <= column <= 4:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    if column >= 3 and offer.price_note:
                        item.setToolTip(f"<qt>{escape(offer.price_note)}</qt>")
                else:
                    item.setToolTip(f"<qt>{escape(text)}</qt>")
                table.setItem(row, column, item)
        self.update_offer_buttons()
        self.load_offer_selectors()
        self.schedule_workspace_save()

        if len(self.offers) < 2:
            self.bestPriceLabel.setText("Добавьте два предложения по одному товару для сравнения.")
            return

        best_price = self.offers[0].price_per_unit
        best_offers = dict.fromkeys(
            " · ".join(filter(None, (offer.store, offer.price_note)))
            for offer in self.offers if offer.price_per_unit == best_price
        )
        best_amount = format_number(best_price * scale, 2)
        self.bestPriceLabel.setText(f"Лучшая из указанных цен — {best_amount} ₽ за {quantity} ({', '.join(best_offers)}).")

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

    def clear_composition_result(self):
        self.compositionTable.setRowCount(0)
        self.componentCount.clear()
        self.compositionNotice.hide()

    def analyze_composition(self, *, record=True, show_empty=True):
        components, repeated = analyze_components(self.compositionInput.toPlainText())
        self.clear_composition_result()
        if not components:
            if show_empty:
                self.componentCount.setText("Введите состав с этикетки.")
            return

        self.compositionTable.setRowCount(len(components))
        for row, component in enumerate(components):
            for column, text in enumerate((component.name, component.concentration or "—", component.role, component.note)):
                self.compositionTable.setItem(row, column, QTableWidgetItem(text))

        result = f"Уникальных компонентов — {len(components)}"
        if repeated:
            result += f"\nПовторы — {', '.join(repeated)}"
        self.componentCount.setText(result)
        self.compositionNotice.show()
        if record:
            lines = [f"{item.name} [{item.concentration or 'доля не указана'}] — {item.role}. {item.note}" for item in components]
            self.record_calculation("Анализ состава", "\n".join([result, "", *lines]))

    def update_effectiveness(self):
        tested = self.testCountInput.value()
        successful = self.successCountInput.value()
        result = "—"
        success_cost = "—"
        error = ""
        if successful > tested:
            error = f"Успешных применений может быть от 0 до {tested}."
        elif not tested:
            error = "Введите число испытаний и сколько из них было успешными."
        else:
            percent = Fraction(100 * successful, tested)
            result = f"{format_quantity(percent)} %"
            if successful:
                try:
                    amount, price, samples = self.read_usage_fields()
                except (ValueError, InvalidOperation):
                    error = "Для расчёта стоимости добавьте фасовку, цену и замеры в «Расходе»."
                else:
                    _, cost_per_use, _ = calculate_usage(amount, price, samples)
                    success_cost = f"{format_number(cost_per_use * Fraction(tested, successful), 2)} ₽"
            else:
                error = "Стоимость успешного применения появится, когда будет хотя бы один успех."
        self.effectivenessValue.setText(result)
        self.successCostValue.setText(success_cost)
        self.effectivenessError.setText(error)
        self.effectivenessError.setVisible(bool(error))
        self.saveEffectivenessButton.setEnabled(0 < tested and successful <= tested)

    def save_effectiveness(self):
        if not self.account.token:
            self.statusBar().showMessage("Войдите в личный кабинет, чтобы сохранить оценку в истории.", 8000)
            return
        details = (
            f"Испытаний — {self.testCountInput.value()}\n"
            f"Успешных — {self.successCountInput.value()}\n"
            f"Доля успешных — {self.effectivenessValue.text()}\n"
            f"Стоимость успешного применения — {self.successCostValue.text()}"
        )
        self.record_calculation("Оценка эффективности", details, success_message="Оценка сохранена в истории.")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = HouseholdGoodsWindow(state_path=PROJECT_DIR / "data" / "workspace.json")
    window.show()
    sys.exit(app.exec())
