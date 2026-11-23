import logging
import sys
from fractions import Fraction
from html import escape
from pathlib import Path

from PyQt6 import uic
from PyQt6.QtCore import QSignalBlocker, QTimer, Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QApplication, QBoxLayout, QLabel, QMainWindow, QMessageBox

from account_client import AccountController
from calculations import format_number
from characteristics_screen import CharacteristicsScreen
from forecast_screen import ForecastScreen
from lr5_Dsv_2_personal_account import PersonalAccountCSV
from prices_screen import PricesScreen
from usage_screen import UsageScreen
from workspace_state import DEFAULT_COMPARISON_NAME, Comparison, LocalWorkspace, UsageSamples, WorkspaceState


PROJECT_DIR = Path(__file__).resolve().parent
LOGGER = logging.getLogger(__name__)
WORKSPACE_SAVE_DELAY_MS = 500
SCREEN_ERRORS = {
    "Цены": "Не удалось обработать предложение. Проверьте ссылку, файл или введённые значения.",
    "Расход": "Не удалось рассчитать расход. Проверьте фасовку, цену и замеры.",
    "Прогноз": "Не удалось построить прогноз. Проверьте замеры и частоту применений.",
    "Характеристики": "Не удалось обработать характеристики. Проверьте выбранное предложение или текст состава.",
    "Личный кабинет": "Не удалось выполнить действие с аккаунтом или историей.",
}


class HouseholdGoodsWindow(QMainWindow, PricesScreen, UsageScreen, ForecastScreen, CharacteristicsScreen):
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
        self.setup_usage_screen()
        self.setup_forecast_screen()
        self.setup_prices_screen()
        self.setup_characteristics_screen()
        for section, selector in zip(("Расход", "Прогноз", "Характеристики"), self.offer_selectors):
            selector.currentIndexChanged.connect(
                lambda _i, combo=selector, name=section: self.run_screen_action(name, self.choose_offer, combo.currentData())
            )

        self.history_actions = {
            "Расчёт расхода": (self.usagePage, lambda: self.check_usage(record=False)),
            "Прогноз": (self.forecastPage, lambda: self.show_forecast(record=False)),
            "Анализ состава": (self.characteristicsPage, lambda: self.analyze_composition(record=False)),
            "Оценка эффективности": (self.characteristicsPage, self.show_archived_effectiveness),
        }
        data_directory = self.local_workspace.path.parent if self.local_workspace else PROJECT_DIR / "data"
        self.account = AccountController(self, data_directory / "accounts.sqlite3")
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

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not hasattr(self, "account"):
            return
        self.sidebarImage.setVisible(event.size().height() >= 555)
        margins = self.workspaceLayout.contentsMargins()
        width = min(
            event.size().width() - self.sidebar.width() - margins.left() - margins.right(),
            self.sectionStack.maximumWidth(),
        )
        self.resize_price_table(width)
        for columns, minimum_width in ((self.usageColumns, 575), (self.accountColumns, 645)):
            direction = QBoxLayout.Direction.TopToBottom if width < minimum_width else QBoxLayout.Direction.LeftToRight
            if columns.direction() != direction:
                columns.setDirection(direction)

    def run_screen_action(self, screen, action, *args):
        try:
            return action(*args)
        except Exception:
            LOGGER.exception("Ошибка вкладки «%s»", screen)
            if screen == "Цены" and self.price_loading:
                self.refreshing_offer = None
                self.set_price_loading(False)
            QMessageBox.warning(self, screen, SCREEN_ERRORS[screen])

    def connect_screen_action(self, signal, screen, action):
        signal.connect(lambda: self.run_screen_action(screen, action))

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
        self.update_customer_rating()

    def choose_offer(self, offer_id):
        self.show_customer_rating()
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
        self.account.finish_pending_jobs()
        super().closeEvent(event)

    def update_page(self):
        page = self.sectionStack.currentWidget()
        if page is self.characteristicsPage:
            self.run_screen_action("Характеристики", self.refresh_characteristics)
        elif page is self.accountPage:
            self.run_screen_action("Личный кабинет", self.account.refresh_history)

    def record_calculation(self, action, details):
        self.account.record(action, self.productInput.text(), details, snapshot=self.draft_values())

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


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = HouseholdGoodsWindow(state_path=PROJECT_DIR / "data" / "workspace.json")
    window.show()
    sys.exit(app.exec())
