import csv
import json
import tempfile
from dataclasses import asdict, replace
from datetime import datetime
from decimal import InvalidOperation
from fractions import Fraction
from html import escape
from pathlib import Path

from PyQt6.QtCore import QSignalBlocker, Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QFileDialog, QHeaderView, QInputDialog, QMenu, QMessageBox, QTableWidgetItem

from calculations import format_number, read_number, read_package
from price_loader import PriceLoader
from product_rating import decode_customer_rating
from workspace_state import DEFAULT_COMPARISON_NAME, Comparison, Offer


def card_address(value):
    url = QUrl(value, QUrl.ParsingMode.StrictMode)
    if url.isValid() and url.scheme() in ("http", "https") and url.host() and not url.userInfo():
        return url.toString(QUrl.ComponentFormattingOption.FullyEncoded)
    return ""


def current_price_time():
    return datetime.now().astimezone().isoformat(timespec="seconds")


class PricesScreen:
    def setup_prices_screen(self):
        self.clear_offer_draft()
        self.price_sort_column = 4
        self.price_sort_order = Qt.SortOrder.AscendingOrder
        self.price_loading = False
        self.refreshing_offer = None
        self.pricesProduct.hide()
        self.price_loader = PriceLoader(self)
        self.price_loader.started.connect(lambda: self.set_price_loading(True))
        self.price_loader.loaded.connect(lambda store, product: self.run_screen_action("Цены", self.apply_loaded_product, store, product))
        self.price_loader.failed.connect(lambda message: self.run_screen_action("Цены", self.show_price_error, message))
        self.price_loader.progress.connect(self.statusBar().showMessage)
        self.offerPriceInput.textEdited.connect(self.clear_offer_price_note)
        self.connect_screen_action(self.productLinkInput.returnPressed, "Цены", self.load_price)
        self.bestPriceLabel.setTextFormat(Qt.TextFormat.PlainText)
        comparison_menu = QMenu(self.manageComparisonButton)
        rename_action = comparison_menu.addAction("Переименовать")
        self.delete_comparison_action = comparison_menu.addAction("Удалить список")
        self.connect_screen_action(rename_action.triggered, "Цены", self.rename_comparison)
        self.connect_screen_action(self.delete_comparison_action.triggered, "Цены", self.delete_comparison)
        self.manageComparisonButton.setMenu(comparison_menu)
        self.comparisonSelector.currentIndexChanged.connect(lambda index: self.run_screen_action("Цены", self.switch_comparison, index))
        for button, action in (
            (self.loadPriceButton, self.load_price), (self.loadPriceFileButton, self.load_price_file),
            (self.newComparisonButton, self.create_comparison), (self.addOfferButton, self.add_offer),
            (self.loadCsvButton, self.load_offers_csv), (self.saveCsvButton, self.save_offers_csv),
            (self.useOfferButton, self.use_selected_offer), (self.refreshOfferButton, self.refresh_selected_offer),
            (self.removeOfferButton, self.remove_offer),
        ):
            self.connect_screen_action(button.clicked, "Цены", action)
        self.offersTable.itemSelectionChanged.connect(self.update_offer_buttons)
        self.offersTable.cellDoubleClicked.connect(lambda row, _column: self.run_screen_action("Цены", self.open_offer_page, row))
        self.offerUnit.currentTextChanged.connect(self.set_comparison_unit)
        header = self.offersTable.horizontalHeader()
        header.setSectionsClickable(True)
        header.setSortIndicator(self.price_sort_column, self.price_sort_order)
        header.setSortIndicatorShown(True)
        header.sectionClicked.connect(lambda column: self.run_screen_action("Цены", self.sort_offers, column))
        self.offersTable.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.offersTable.verticalHeader().setMinimumSectionSize(34)
        for column, width in enumerate((108, 74, 70, 96, 124), start=1):
            self.offersTable.setColumnWidth(column, width)
        self.load_comparison_selector()

    def resize_price_table(self, width):
        table = self.offersTable
        other_columns = sum(table.columnWidth(column) for column in range(1, table.columnCount()))
        scroll_margin = self.pricesPageScroll.verticalScrollBar().sizeHint().width()
        table.setColumnWidth(0, max(148, width - other_columns - 2 * scroll_margin - 2))

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
        self.update_offers_table()

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
        if not self.offers:
            self.comparison.unit = unit
            self.update_offers_table()

    def comparison_for_unit(self, unit):
        if not self.offers or self.comparison.unit == unit:
            self.comparison.unit = unit
            return self.workspace_state.active
        base = self.comparison.name.removesuffix(f" · {self.comparison.unit}")
        name = f"{base} · {unit}"
        candidate = name
        suffix = 2
        while (index := self.comparison_index(candidate)) is not None:
            if self.workspace_state.comparisons[index].unit == unit:
                return index
            candidate = f"{name} ({suffix})"
            suffix += 1
        self.workspace_state.comparisons.append(Comparison(candidate, unit))
        return len(self.workspace_state.comparisons) - 1

    def sort_offers(self, column):
        if column not in (2, 3, 4):
            return
        descending = column == self.price_sort_column and self.price_sort_order == Qt.SortOrder.AscendingOrder
        self.price_sort_column = column
        self.price_sort_order = Qt.SortOrder.DescendingOrder if descending else Qt.SortOrder.AscendingOrder
        self.offersTable.horizontalHeader().setSortIndicator(column, self.price_sort_order)
        self.update_offers_table()

    def offer_id_at(self, row):
        item = self.offersTable.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def select_price_offer(self, offer_id):
        for row in range(self.offersTable.rowCount()):
            if self.offer_id_at(row) == offer_id:
                self.offersTable.selectRow(row)
                break

    def open_offer_page(self, row):
        offer_id = self.offer_id_at(row)
        offer = next((offer for offer in self.offers if offer.offer_id == offer_id), None)
        address = card_address(offer.card_url) if offer is not None else ""
        if not address:
            self.statusBar().showMessage("Для этого предложения ссылка не сохранена.", 5000)
        elif not QDesktopServices.openUrl(QUrl(address)):
            self.show_price_error("Не удалось открыть карточку в браузере по умолчанию.")

    def load_price(self):
        self.price_loader.load(self.productLinkInput.text())

    def clear_offer_price_note(self):
        self.offer_price_note = ""
        self.offer_loaded_at = ""

    def load_price_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Сохранённая карточка товара", "", "Страницы HTML (*.html *.htm *.mhtml *.mht)")
        if path:
            self.price_loader.load_file(path)

    def set_price_loading(self, loading):
        self.price_loading = loading
        for field in (self.productLinkInput, self.loadPriceButton, self.loadPriceFileButton, self.addOfferButton, self.loadCsvButton,
                      self.storeInput, self.offerPackageInput, self.offerPriceInput,
                      self.comparisonSelector, self.newComparisonButton, self.manageComparisonButton):
            field.setEnabled(not loading)
        self.offerUnit.setEnabled(not loading)
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

        self.offer_name = product.name
        self.offer_price_note = product.price_note
        self.offer_composition = product.composition
        self.offer_rating = product.rating
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
            composition=product.composition or offer.composition, rating=product.rating or offer.rating,
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
        self.select_price_offer(updated.offer_id)
        old_price = format_number(Fraction(offer.price), 2)
        new_price = format_number(Fraction(updated.price), 2)
        details = f"{updated.store}\nПрежняя цена — {old_price} ₽\nОбновлённая цена — {new_price} ₽"
        if updated.price_note:
            details += f"\nУсловие цены — {updated.price_note}"
        self.account.record("Обновление цены", updated.product, details)
        message = "Цена и состав обновлены." if composition_changed else "Цена обновлена."
        if updated.rating != offer.rating:
            message += " Оценка покупателей обновлена."
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

        offer = Offer(
            store, amount, price, self.offer_name, self.offer_price_note, self.offer_composition,
            self.offer_card_url, self.offer_loaded_at, rating=self.offer_rating,
        )
        selected_before = self.workspace_state.active
        index = self.comparison_for_unit(self.offerUnit.currentText())
        comparison = self.workspace_state.comparisons[index]
        comparison.offers.append(offer)
        comparison.offers.sort(key=lambda entry: entry.price_per_unit)
        if index != selected_before:
            self.workspace_state.active = index
            self.load_comparison_selector()
        else:
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
        message = f"Предложение добавлено в «{comparison.name}»." if index != selected_before else "Предложение добавлено."
        self.statusBar().showMessage(message, 7000)

    def clear_offer_draft(self):
        self.offer_name = ""
        self.offer_price_note = ""
        self.offer_composition = ""
        self.offer_rating = None
        self.offer_card_url = ""
        self.offer_loaded_at = ""
        self.productLinkInput.clear()
        self.storeInput.clear()
        self.offerPackageInput.clear()
        self.offerPriceInput.clear()

    def selected_offer_index(self):
        selected = self.offersTable.selectionModel().selectedRows()
        offer_id = self.offer_id_at(selected[0].row()) if selected else None
        return next((index for index, offer in enumerate(self.offers) if offer.offer_id == offer_id), None)

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

    def load_offers_csv(self):
        path, _ = QFileDialog.getOpenFileName(self, "Загрузить предложения", "", "CSV (*.csv)")
        if not path:
            return
        try:
            offers = []
            unit = self.comparison.unit
            units = {self.offerUnit.itemText(i) for i in range(self.offerUnit.count())}
            with open(path, encoding="utf-8-sig", newline="") as stream:
                reader = csv.reader(stream, delimiter=";", strict=True)
                headers = next(reader, [])
                if (len(headers) not in (5, 6, 9, 10)
                        or headers[:4] != ["Товар", "Магазин", "Упаковка", "Цена, ₽"]
                        or headers[4] not in {f"За {1 if u == 'шт.' else 100} {u}, ₽" for u in units}
                        or (len(headers) >= 6 and headers[5] != "Примечание к цене")
                        or (len(headers) >= 9 and headers[6:9] != ["Состав", "Ссылка", "Дата загрузки"])
                        or (len(headers) == 10 and headers[9] != "Оценка покупателей")):
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
                        if len(row) >= 9 and row[8]:
                            datetime.fromisoformat(row[8])
                        rating = decode_customer_rating(json.loads(row[9])) if len(row) == 10 and row[9] else None
                        offers.append(Offer(
                            row[1].strip(), amount, price,
                            "" if row[0] == "—" else row[0],
                            row[5] if len(row) >= 6 else "",
                            *(row[6:9] if len(row) >= 9 else ("", "", "")),
                            rating=rating,
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
        unit = self.comparison.unit
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
                    "Состав", "Ссылка", "Дата загрузки", "Оценка покупателей",
                ])
                for offer in self.offers:
                    writer.writerow([
                        offer.product, offer.store,
                        f"{format(offer.amount, 'f').replace('.', ',')} {unit}",
                        format(offer.price, "f").replace(".", ","),
                        format_number(offer.price_per_unit * scale, 2),
                        offer.price_note, offer.composition, offer.card_url, offer.loaded_at,
                        json.dumps(asdict(offer.rating), default=str) if offer.rating is not None else "",
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
        unit = self.comparison.unit
        self.offerDifferenceLabel.setText(f"Разница с лучшей ценой за {amount} {unit} — {money}.")
        self.offerDifferenceLabel.show()

    def update_offers_table(self):
        unit = self.comparison.unit
        scale = 1 if unit == "шт." else 100
        quantity = f"{scale} {unit}"
        self.offerUnit.setEnabled(not self.price_loading)

        table = self.offersTable
        selected = table.selectionModel().selectedRows()
        selected_id = self.offer_id_at(selected[0].row()) if selected else None
        key = {
            2: lambda offer: offer.amount,
            3: lambda offer: offer.price,
            4: lambda offer: offer.price_per_unit,
        }[self.price_sort_column]
        ordered = sorted(self.offers, key=key, reverse=self.price_sort_order == Qt.SortOrder.DescendingOrder)
        table.horizontalHeaderItem(4).setText(f"За {quantity}, ₽")
        table.setRowCount(0)
        table.setRowCount(len(ordered))
        for row, offer in enumerate(ordered):
            amount = format(offer.amount, "f").replace(".", ",")
            price = format_number(Fraction(offer.price), 2)
            normalized_price = format_number(offer.price_per_unit * scale, 2)
            loaded_at = datetime.fromisoformat(offer.loaded_at).astimezone().strftime("%d.%m.%y %H:%M") if offer.loaded_at else ""
            card_info = "\n".join(value for value in (loaded_at, offer.price_note) if value) or "—"
            product = offer.product or (self.comparison.name if self.comparison.name != DEFAULT_COMPARISON_NAME else "—")
            for column, text in enumerate((product, offer.store, f"{amount} {unit}", price, normalized_price, card_info)):
                item = QTableWidgetItem(text)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, offer.offer_id)
                if 2 <= column <= 4:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    if column >= 3 and offer.price_note:
                        item.setToolTip(f"<qt>{escape(offer.price_note)}</qt>")
                else:
                    item.setToolTip(f"<qt>{escape(text)}</qt>")
                table.setItem(row, column, item)
        if selected_id is not None:
            self.select_price_offer(selected_id)
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
