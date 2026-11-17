from pathlib import Path

from PyQt6.QtCore import QObject, QTimer, QUrl, QUrlQuery, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PyQt6.QtWidgets import QApplication

from browser_loader import BrowserCardReader
from store_page import parse_product_page


MAX_HTML_BYTES = 8 * 1024 * 1024
DOWNLOAD_TIMEOUT_MS = 15000
PAGE_TOO_LARGE = "Карточка слишком большая. Введите цену вручную."
MARKET_TRACKING = {"cpc", "sponsored"}


def clean_market_url(url):
    if url.host().removeprefix("www.") != "market.yandex.ru" or not url.path().startswith("/card/"):
        return
    query = QUrlQuery(url)
    for key, _ in query.queryItems():
        if key.lower() in MARKET_TRACKING or key.lower().startswith("utm_"):
            query.removeAllQueryItems(key)
    url.setQuery(query)


class PriceLoader(QObject):
    started = pyqtSignal()
    loaded = pyqtSignal(str, object)
    failed = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.network = QNetworkAccessManager(self)
        self.network.setRedirectPolicy(QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.download_timed_out)
        self.reply = None
        self.browser_reader = None
        self.download_error = ""
        QApplication.instance().aboutToQuit.connect(self.stop_browser)

    def load(self, address):
        if self.reply is not None or self.browser_reader is not None:
            return
        address = address.strip()
        if "://" not in address:
            address = f"https://{address}"
        url = QUrl(address, QUrl.ParsingMode.StrictMode)
        if not url.isValid() or url.scheme() not in ("http", "https") or not url.host() or url.userInfo():
            self.failed.emit("Введите ссылку на карточку товара в магазине.")
            return

        clean_market_url(url)
        if url.host().removeprefix("www.") in ("ozon.ru", "market.yandex.ru"):
            self.load_browser(url)
            return
        request = QNetworkRequest(url)
        request.setRawHeader(b"Accept", b"text/html, application/xhtml+xml")
        request.setRawHeader(b"User-Agent", b"HouseholdGoodsAnalytics/1.0")
        self.download_error = ""
        self.reply = self.network.get(request)
        self.reply.setReadBufferSize(MAX_HTML_BYTES + 1)
        self.reply.readyRead.connect(self.check_size)
        self.reply.finished.connect(self.finish)
        self.timer.start(DOWNLOAD_TIMEOUT_MS)
        self.started.emit()

    def load_file(self, path):
        if self.reply is not None or self.browser_reader is not None:
            return
        try:
            with Path(path).open("rb") as file:
                html = file.read(MAX_HTML_BYTES + 1)
            if len(html) > MAX_HTML_BYTES:
                raise ValueError(PAGE_TOO_LARGE)
            product = parse_product_page(html)
        except OSError:
            self.failed.emit("Не удалось открыть HTML-файл карточки.")
        except ValueError as error:
            self.failed.emit(str(error))
        else:
            url = QUrl(product.url)
            store = url.host().removeprefix("www.") if url.scheme() in ("http", "https") else ""
            self.loaded.emit(store, product)

    def load_browser(self, url):
        self.browser_reader = BrowserCardReader(url.toString(), self)
        self.browser_reader.progress.connect(self.progress)
        self.browser_reader.open_requested.connect(self.open_browser)
        self.browser_reader.finished.connect(self.finish_browser)
        self.browser_reader.start()
        self.started.emit()

    def open_browser(self, address):
        if not QDesktopServices.openUrl(QUrl(address)):
            self.browser_reader.error = "Не удалось открыть карточку в браузере."
            self.browser_reader.requestInterruption()

    def finish_browser(self):
        reader = self.browser_reader
        self.browser_reader = None
        if reader.product is None:
            self.failed.emit(reader.error)
        else:
            store = QUrl(reader.address).host().removeprefix("www.")
            self.loaded.emit(store, reader.product)
        reader.deleteLater()

    def stop_browser(self):
        if self.browser_reader is not None:
            self.browser_reader.requestInterruption()
            self.browser_reader.wait()

    def check_size(self):
        if self.reply is not None and self.reply.bytesAvailable() > MAX_HTML_BYTES:
            self.download_error = PAGE_TOO_LARGE
            self.reply.abort()

    def download_timed_out(self):
        if self.reply is not None:
            self.download_error = "Магазин не ответил вовремя. Попробуйте ещё раз или введите цену вручную."
            self.reply.abort()

    def finish(self):
        reply = self.reply
        self.reply = None
        self.timer.stop()
        try:
            if self.download_error:
                raise ValueError(self.download_error)
            if reply.error() != QNetworkReply.NetworkError.NoError:
                status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
                code = f" (HTTP {status})" if status and status >= 400 else ""
                if status in (403, 429):
                    raise ValueError(
                        f"Магазин ограничил доступ{code}. Откройте карточку в браузере и загрузите сохранённую страницу через «Из HTML»."
                    )
                raise ValueError(f"Не удалось открыть карточку{code}. Проверьте ссылку или введите цену вручную.")

            html = bytes(reply.read(MAX_HTML_BYTES + 1) or b"")
            if len(html) > MAX_HTML_BYTES:
                raise ValueError(PAGE_TOO_LARGE)
            content_type = bytes(reply.rawHeader(b"Content-Type")).decode("latin-1")
            product = parse_product_page(html, content_type)
            store = reply.url().host().removeprefix("www.")
        except ValueError as error:
            self.failed.emit(str(error))
        else:
            self.loaded.emit(store, product)
        finally:
            reply.deleteLater()
