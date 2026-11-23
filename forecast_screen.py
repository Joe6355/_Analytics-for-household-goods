from decimal import InvalidOperation
from fractions import Fraction

from PyQt6.QtCore import QLineF, QPointF, Qt
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import QHeaderView

from calculations import calculate_usage, format_number, format_quantity


FORECAST_GRAPH_DAYS = 30


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


class ForecastScreen:
    def setup_forecast_screen(self):
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
        self.connect_screen_action(self.calculateForecastButton.clicked, "Прогноз", self.show_forecast)
        self.frequencyInput.valueChanged.connect(self.clear_forecast)
        self.frequencyPeriod.currentIndexChanged.connect(self.clear_forecast)
        self.clear_forecast()

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
