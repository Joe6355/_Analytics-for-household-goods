import sys
from time import monotonic
from urllib.parse import parse_qsl, urlencode, urlsplit

from PyQt6.QtCore import QThread, pyqtSignal

from store_page import ALIEXPRESS_HOSTS, StorePageUnavailable, parse_browser_product, store_host


BROWSER_ADDED_QUERY = {"market.yandex.ru": "do-waremd5", "ozon.ru": "sh"}


def resolve_product_address(expected, actual):
    try:
        first, second = urlsplit(expected), urlsplit(actual)
        secure_redirect = first.scheme == "http" and second.scheme == "https"
        first_port = first.port or (443 if first.scheme == "https" else 80)
        second_port = second.port or (443 if second.scheme == "https" else 80)
        first_query, second_query = [], []
        for parts, query in ((first, first_query), (second, second_query)):
            for key, value in parse_qsl(parts.query, keep_blank_values=True):
                if key.lower() not in ("cpc", "sponsored") and not key.lower().startswith("utm_"):
                    query.append((key, value))
        first_host, second_host = store_host(expected), store_host(actual)
        same_host = first_host == second_host or {first_host, second_host} <= ALIEXPRESS_HOSTS
        same_card = (
            first.scheme in ("http", "https")
            and (first.scheme == second.scheme or secure_redirect)
            and same_host
            and first.username is None and second.username is None
            and (first_port == second_port or secure_redirect and first_port == 80 and second_port == 443)
            and first.path.rstrip("/") == second.path.rstrip("/")
        )
        if not same_card:
            return None
        if sorted(first_query) == sorted(second_query):
            return expected
        added_key = BROWSER_ADDED_QUERY.get(first_host)
        if added_key and not any(key == added_key for key, _ in first_query):
            selected = [(key, value) for key, value in second_query if key == added_key]
            other = [(key, value) for key, value in second_query if key != added_key]
            if len(selected) == 1 and selected[0][1] and sorted(first_query) == sorted(other):
                # Маркет подставляет продавца, если ссылка вела на общую карточку.
                if first_host == "market.yandex.ru":
                    return first._replace(query=urlencode([*first_query, *selected])).geturl()
                return expected
    except ValueError:
        return None
    return None


def product_redirect_message(expected, actual):
    if not actual:
        return ""
    try:
        actual_parts = urlsplit(actual)
        expected_host, actual_host = store_host(expected), store_host(actual)
    except ValueError:
        return ""
    expected_path = urlsplit(expected).path
    actual_path = actual_parts.path.rstrip("/")
    if expected_host == actual_host == "ozon.ru" and actual_path == "/search":
        article = expected_path.rstrip("/").rsplit("/", 1)[-1].rsplit("-", 1)[-1]
        if article.isdigit() and ("product_id", article) in parse_qsl(actual_parts.query):
            return "Ozon перенаправил карточку в поиск. Откройте другое предложение этого товара и вставьте его ссылку."
    if {expected_host, actual_host} <= ALIEXPRESS_HOSTS:
        suffix = "/_____tmd_____/punish"
        if actual_path.endswith(suffix) and actual_path.removesuffix(suffix).lstrip("/") == expected_path.strip("/"):
            return "AliExpress запросил проверку в браузере. Завершите её и повторите добавление."
    return ""


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
                        browser_address = location.Value
                        address = resolve_product_address(self.address, browser_address)
                        if address is None:
                            redirect_message = product_redirect_message(self.address, browser_address)
                            if redirect_message:
                                card_error = redirect_message
                            self.error = "Нужная карточка ещё не открылась в браузере."
                            continue
                        text = document.GetTextPattern()
                        if text is None:
                            self.error = "Браузер не предоставил текст карточки."
                            continue
                        self.progress.emit("Читаю название, цену и фасовку…")
                        try:
                            self.product = parse_browser_product(
                                text.DocumentRange.GetText(200000), address, document.Name
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
