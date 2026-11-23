from decimal import InvalidOperation
from fractions import Fraction

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QHeaderView, QTableWidgetItem

from calculations import average_usage, calculate_usage, format_number, format_quantity, read_measurements, read_package_amount
from composition_analysis import analyze_components


COMPONENT_MARKS = {
    "base": ("Основа состава", "#e1f1e5", "#1b5437"),
    "functional": ("Функциональная добавка", "#e5eef5", "#254862"),
    "attention": ("Требует внимания", "#fff0c9", "#634409"),
    "label": ("Сверьте предупреждения", "#fbe4e0", "#792720"),
    "unknown": ("Нужны данные", "#edeef0", "#424851"),
}


class CharacteristicsScreen:
    def setup_characteristics_screen(self):
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
        self.connect_screen_action(self.analyzeCompositionButton.clicked, "Характеристики", self.analyze_composition)
        self.testCountInput.setValue(0)
        self.successCountInput.setValue(0)
        self.testCountInput.valueChanged.connect(self.update_effectiveness)
        self.successCountInput.valueChanged.connect(self.update_effectiveness)
        for field in (self.productInput, self.packageInput, self.packagePriceInput, self.measurementsInput):
            field.textChanged.connect(self.update_effectiveness)
        self.update_effectiveness()

    def refresh_characteristics(self):
        self.show_customer_rating()
        self.update_characteristics_package()
        if self.imported_composition and self.compositionTable.rowCount() == 0:
            self.analyze_composition(record=False, show_empty=False)

    def clear_imported_composition(self):
        if self.imported_composition and self.imported_composition == self.compositionInput.toPlainText():
            self.compositionInput.clear()
        self.imported_composition = ""

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
            mark, background, foreground = COMPONENT_MARKS[component.category]
            values = (component.name, component.concentration or "—", component.role, f"{mark}\n{component.note}")
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                if column == 3:
                    item.setBackground(QColor(background))
                    item.setForeground(QColor(foreground))
                self.compositionTable.setItem(row, column, item)

        result = f"Уникальных компонентов — {len(components)}"
        if repeated:
            result += f"\nПовторы — {', '.join(repeated)}"
        self.componentCount.setText(result)
        self.compositionNotice.show()
        if record:
            lines = [f"{item.name} [{item.concentration or 'доля не указана'}] — {item.role}. {item.note}" for item in components]
            self.record_calculation("Анализ состава", "\n".join([result, "", *lines]))

    def update_customer_rating(self):
        choice = self.offer_choices.get(self.selected_offer_id)
        offer = choice[1] if choice is not None else None
        rating = offer.rating if offer is not None else None
        score, count_text, positive = "", "", ""
        source = "Нет данных об оценках"
        if rating is not None:
            score = f"{format_number(Fraction(rating.score), 2, trim=True)} из 5"
            count_labels = []
            for name, count in (("Оценок", rating.rating_count), ("Отзывов", rating.review_count)):
                if count is not None:
                    count_labels.append(f"{name} — {count:,}".replace(",", " "))
            count_text = "\n".join(count_labels)
            if rating.star_counts:
                total = sum(rating.star_counts)
                share = Fraction(100 * sum(rating.star_counts[3:]), total)
                mark = "≈ " if share.denominator != 1 else ""
                percent = format_number(share, 1, trim=True)
                positive = f"{mark}{percent} % оценок — 4 или 5 звёзд"
                if total != rating.rating_count:
                    positive += f"\nОценок в разбивке — {total:,}".replace(",", " ")
            source = f"Источник — {offer.store}" if offer.store else "Данные из карточки магазина"
        for label, value in (
            (self.customerRatingValue, score), (self.customerRatingCount, count_text),
            (self.customerRatingPositive, positive), (self.customerRatingSource, source),
        ):
            label.setText(value)
            label.setVisible(bool(value))

    def show_customer_rating(self):
        self.effectivenessSheet.hide()
        self.customerRatingSheet.show()

    def show_archived_effectiveness(self):
        self.customerRatingSheet.hide()
        self.effectivenessSheet.show()
        self.update_effectiveness()

    def update_effectiveness(self):
        tested = self.testCountInput.value()
        successful = self.successCountInput.value()
        result = "—"
        success_cost = "—"
        error = ""
        if successful > tested:
            error = f"Успешных применений может быть от 0 до {tested}."
        elif not tested:
            error = "В записи нет данных об испытаниях."
        else:
            percent = Fraction(100 * successful, tested)
            result = f"{format_quantity(percent)} %"
            if successful:
                try:
                    amount, price, samples = self.read_usage_fields()
                except (ValueError, InvalidOperation):
                    error = "В записи недостаточно данных для расчёта стоимости."
                else:
                    _, cost_per_use, _ = calculate_usage(amount, price, samples)
                    success_cost = f"{format_number(cost_per_use * Fraction(tested, successful), 2)} ₽"
            else:
                error = "В записи нет успешных применений."
        self.effectivenessValue.setText(result)
        self.successCostValue.setText(success_cost)
        self.effectivenessError.setText(error)
        self.effectivenessError.setVisible(bool(error))
