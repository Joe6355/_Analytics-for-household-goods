from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtSignal
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from store_page import parse_price


MAX_HTML_BYTES = 8 * 1024 * 1024
DOWNLOAD_TIMEOUT_MS = 15000
PAGE_TOO_LARGE = "Карточка слишком большая. Введите цену вручную."


class PriceLoader(QObject):
    started = pyqtSignal()
    loaded = pyqtSignal(str, object)
    failed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.network = QNetworkAccessManager(self)
        self.network.setRedirectPolicy(QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.download_timed_out)
        self.reply = None
        self.download_error = ""

    def load(self, address):
        if self.reply is not None:
            return
        address = address.strip()
        if "://" not in address:
            address = f"https://{address}"
        url = QUrl(address, QUrl.ParsingMode.StrictMode)
        if not url.isValid() or url.scheme() not in ("http", "https") or not url.host() or url.userInfo():
            self.failed.emit("Введите ссылку на карточку товара в магазине.")
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
                raise ValueError(f"Не удалось открыть карточку{code}. Проверьте ссылку или введите цену вручную.")

            html = bytes(reply.read(MAX_HTML_BYTES + 1))
            if len(html) > MAX_HTML_BYTES:
                raise ValueError(PAGE_TOO_LARGE)
            content_type = bytes(reply.rawHeader(b"Content-Type")).decode("latin-1")
            price = parse_price(html, content_type)
            store = reply.url().host().removeprefix("www.")
        except ValueError as error:
            self.failed.emit(str(error))
        else:
            self.loaded.emit(store, price)
        finally:
            reply.deleteLater()
