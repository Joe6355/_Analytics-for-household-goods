from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from PyQt6 import uic
from PyQt6.QtCore import QTimer, QUrl
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PyQt6.QtWidgets import QHeaderView, QTableWidgetItem, QWidget

from calculations import UNITS, format_money, format_number, read_number
from store_page import parse_price


MAX_HTML_BYTES = 8 * 1024 * 1024
DOWNLOAD_TIMEOUT_MS = 15000


@dataclass(frozen=True)
class Offer:
    store: str
    amount: Decimal
    price: Decimal

    @property
    def unit_price(self) -> Decimal:
        return self.price / self.amount


class PricesPage(QWidget):
    def __init__(self):
        super().__init__()
        uic.loadUi(str(Path(__file__).with_name("ui") / "prices.ui"), self)
        self.offers: list[Offer] = []
        self.offer_unit_input.addItems(UNITS)
        self.offer_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.add_offer_button.clicked.connect(self.add_offer)
        self.remove_offer_button.clicked.connect(self.remove_offer)
        self.offer_table.itemSelectionChanged.connect(self.update_remove_button)
        self.offer_unit_input.currentTextChanged.connect(self.update_table)

        self.network = QNetworkAccessManager(self)
        self.network.setRedirectPolicy(QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy)
        self.download_timer = QTimer(self)
        self.download_timer.setSingleShot(True)
        self.download_timer.timeout.connect(self.download_timed_out)
        self.download_reply = None
        self.download_error = ""
        self.download_button.clicked.connect(self.download_price)
        self.url_input.returnPressed.connect(self.download_price)
        self.update_table()

    def download_price(self):
        if self.download_reply is not None:
            return
        text = self.url_input.text().strip()
        url = QUrl(text if "://" in text else f"https://{text}", QUrl.ParsingMode.StrictMode)
        if not url.isValid() or url.scheme() not in ("http", "https") or not url.host() or url.userInfo():
            self.download_status.setText("Введите ссылку на карточку товара в магазине.")
            return

        request = QNetworkRequest(url)
        request.setRawHeader(b"Accept", b"text/html, application/xhtml+xml")
        request.setRawHeader(b"User-Agent", b"HouseholdGoodsAnalytics/1.0")
        self.download_error = ""
        self.download_reply = self.network.get(request)
        self.download_reply.setReadBufferSize(MAX_HTML_BYTES + 1)
        self.download_reply.readyRead.connect(self.check_download_size)
        self.download_reply.finished.connect(self.finish_download)
        self.download_timer.start(DOWNLOAD_TIMEOUT_MS)
        self.download_button.setEnabled(False)
        self.url_input.setEnabled(False)
        self.add_offer_button.setEnabled(False)
        self.store_input.setEnabled(False)
        self.offer_price_input.setEnabled(False)
        self.download_status.setText("Загружаю карточку товара…")

    def check_download_size(self):
        if self.download_reply.bytesAvailable() > MAX_HTML_BYTES:
            self.download_error = "Карточка слишком большая. Введите цену вручную."
            self.download_reply.abort()

    def download_timed_out(self):
        self.download_error = "Магазин не ответил вовремя. Попробуйте ещё раз или введите цену вручную."
        self.download_reply.abort()

    def finish_download(self):
        reply = self.download_reply
        self.download_reply = None
        self.download_timer.stop()
        self.download_button.setEnabled(True)
        self.url_input.setEnabled(True)
        self.add_offer_button.setEnabled(True)
        self.store_input.setEnabled(True)
        self.offer_price_input.setEnabled(True)
        try:
            if self.download_error:
                raise ValueError(self.download_error)
            if reply.error() != QNetworkReply.NetworkError.NoError:
                status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
                code = f" (HTTP {status})" if status and status >= 400 else ""
                raise ValueError(f"Не удалось открыть карточку{code}. Проверьте ссылку или введите цену вручную.")

            content_type = bytes(reply.rawHeader(b"Content-Type")).decode("latin-1")
            price, title = parse_price(bytes(reply.readAll()), content_type)
            self.offer_price_input.setText(format(price, "f"))
            self.store_input.setText(reply.url().host().removeprefix("www."))
            name = f"Товар «{title}». " if title else ""
            self.download_status.setText(f"{name}Цена загружена. Укажите количество в упаковке.")
        except ValueError as error:
            self.download_status.setText(str(error))
        finally:
            reply.deleteLater()

    def add_offer(self):
        self.offer_error.clear()
        store = self.store_input.text().strip()
        try:
            if not store:
                raise ValueError("Введите название магазина.")
            amount = read_number(self.offer_amount_input.text(), "Количество в упаковке")
            price = read_number(self.offer_price_input.text(), "Цена упаковки", allow_zero=True)
        except ValueError as error:
            self.offer_error.setText(str(error))
            return

        self.offers.append(Offer(store, amount, price))
        self.offers.sort(key=lambda offer: offer.unit_price)
        for field in (self.url_input, self.store_input, self.offer_amount_input, self.offer_price_input):
            field.clear()
        self.download_status.clear()
        self.update_table()

    def update_remove_button(self):
        self.remove_offer_button.setEnabled(bool(self.offer_table.selectedItems()))

    def remove_offer(self):
        selected = self.offer_table.selectionModel().selectedRows()
        if selected:
            del self.offers[selected[0].row()]
            self.update_table()

    def update_table(self):
        unit = self.offer_unit_input.currentText()
        scale = 1 if unit == "шт." else 100
        self.offer_table.horizontalHeaderItem(3).setText(f"За {scale} {unit}")
        self.offer_unit_input.setEnabled(not self.offers)
        self.offer_table.clearContents()
        self.offer_table.setRowCount(len(self.offers))
        for row, offer in enumerate(self.offers):
            values = (offer.store, f"{format_number(offer.amount)} {unit}",
                      format_money(offer.price), format_money(offer.unit_price * scale))
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.offer_table.setItem(row, column, item)
        self.offer_table.clearSelection()
        self.update_remove_button()

        if len(self.offers) < 2:
            self.best_offer_label.setText("Добавьте два предложения для сравнения.")
        else:
            best = self.offers[0].unit_price
            stores = dict.fromkeys(f"«{offer.store}»" for offer in self.offers if offer.unit_price == best)
            self.best_offer_label.setText(f"Лучшая цена за {scale} {unit} в {', '.join(stores)} — "
                                          f"{format_money(best * scale)}.")
