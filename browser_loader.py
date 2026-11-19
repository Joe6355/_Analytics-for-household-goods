import sys
from time import monotonic
from urllib.parse import parse_qsl, urlsplit

from PyQt6.QtCore import QThread, pyqtSignal

from store_page import ALIEXPRESS_HOSTS, StorePageUnavailable, parse_browser_product, store_host


def same_product_address(expected, actual):
    try:
        first, second = urlsplit(expected), urlsplit(actual)
        secure_redirect = first.scheme == "http" and second.scheme == "https"
        first_port = first.port or (443 if first.scheme == "https" else 80)
        second_port = second.port or (443 if second.scheme == "https" else 80)
        first_query, second_query = [], []
        for parts, query in ((first, first_query), (second, second_query)):
            for key, value in parse_qsl(parts.query):
                if key.lower() not in ("cpc", "sponsored") and not key.lower().startswith("utm_"):
                    query.append((key, value))
        first_host, second_host = store_host(expected), store_host(actual)
        same_host = first_host == second_host or {first_host, second_host} <= ALIEXPRESS_HOSTS
        return (
            first.scheme in ("http", "https")
            and (first.scheme == second.scheme or secure_redirect)
            and same_host
            and (first_port == second_port or secure_redirect and first_port == 80 and second_port == 443)
            and first.path.rstrip("/") == second.path.rstrip("/")
            and sorted(first_query) == sorted(second_query)
        )
    except ValueError:
        return False


class BrowserCardReader(QThread):
    progress = pyqtSignal(str)
    open_requested = pyqtSignal(str)

    def __init__(self, address, parent=None):
        super().__init__(parent)
        self.address = address
        self.product = None
        self.error = "Карточка ещё не доступна в браузере. Дождитесь её загрузки и повторите добавление."

    def run(self):
        self.progress.emit("Подключаюсь к браузеру…")
        if sys.platform != "win32":
            self.error = "Чтение карточки из браузера поддерживается в Windows."
            return
        try:
            import uiautomation as auto
            from comtypes import COMError
        except ImportError:
            self.error = "Установите зависимости проекта командой python -m pip install -r requirements.txt."
            return

        deadline = monotonic() + 40
        opened = False
        card_error = ""
        self.error = "Откройте карточку в Google Chrome или Microsoft Edge и повторите добавление."
        with auto.UIAutomationInitializerInThread():
            self.progress.emit("Жду карточку товара в Chrome или Edge…")
            while not self.isInterruptionRequested() and monotonic() < deadline:
                try:
                    windows = auto.GetRootControl().GetChildren()
                    for window in windows:
                        if self.isInterruptionRequested():
                            return
                        if window.ClassName != "Chrome_WidgetWin_1" or not window.Name.endswith(
                            (" - Google Chrome", " - Microsoft Edge")
                        ):
                            continue
                        self.error = "Браузер ещё не предоставил страницу товара. Дождитесь её загрузки."
                        document = window.DocumentControl(searchDepth=12, AutomationId="RootWebArea")
                        if not document.Exists(0, 0):
                            continue
                        location = document.GetValuePattern()
                        if location is None:
                            self.error = "Браузер не предоставил адрес карточки."
                            continue
                        if not same_product_address(self.address, location.Value):
                            self.error = "Нужная карточка ещё не открылась в браузере."
                            continue
                        text = document.GetTextPattern()
                        if text is None:
                            self.error = "Браузер не предоставил текст карточки."
                            continue
                        self.progress.emit("Читаю название, цену и фасовку…")
                        try:
                            self.product = parse_browser_product(
                                text.DocumentRange.GetText(200000), self.address, document.Name
                            )
                        except StorePageUnavailable as error:
                            self.error = str(error)
                            return
                        except ValueError as error:
                            card_error = str(error)
                        else:
                            return
                except (COMError, LookupError, OSError) as error:
                    code = getattr(error, "hresult", None)
                    self.error = "Windows не предоставил данные страницы браузера."
                    if code is not None:
                        self.error += f" Код {code & 0xFFFFFFFF:08X}."
                if not opened:
                    opened = True
                    self.open_requested.emit(self.address)
                self.msleep(600)
        if card_error and not self.isInterruptionRequested():
            self.error = card_error
