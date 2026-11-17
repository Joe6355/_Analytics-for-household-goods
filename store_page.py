import json
import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser


UNAVAILABLE = {"OutOfStock", "SoldOut", "Discontinued", "PreOrder", "PreSale", "BackOrder"}
RUBLE_CURRENCIES = {"RUB", "RUR", "₽"}
PRICE_TEXT = re.compile(r"(?:[0-9]{1,9}|[0-9]{1,3}(?:[ \u00a0\u202f][0-9]{3}){1,2})(?:[.,][0-9]{1,8})?")
CHARSET = re.compile(r"charset\s*=\s*['\"]?([a-zA-Z0-9_-]+)", re.IGNORECASE)
CAPTCHA_FORM = re.compile(r"(?:^|/)(?:checkcaptcha|showcaptcha)(?:[/?#]|$)", re.IGNORECASE)


def ruble_price(value, currency):
    if not isinstance(currency, str) or currency.strip().upper() not in RUBLE_CURRENCIES:
        return None
    if type(value) not in (str, int, Decimal):
        return None
    text = str(value).strip().removesuffix("₽").rstrip()
    if not PRICE_TEXT.fullmatch(text):
        return None
    return Decimal("".join(text.split()).replace(",", "."))


class StoreMarkup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.metadata = defaultdict(set)
        self.scripts = []
        self.script = None
        self.woocommerce_product = False
        self.in_woocommerce_price = False
        self.in_old_price = False
        self.amount_text = None
        self.woocommerce_prices = set()
        self.canonical_url = ""
        self.needs_verification = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "link" and "canonical" in (attributes.get("rel") or "").lower().split():
            self.canonical_url = attributes.get("href") or ""
        if tag == "form" and CAPTCHA_FORM.search(attributes.get("action") or ""):
            self.needs_verification = True
        if tag == "script":
            script_type = (attributes.get("type") or "").partition(";")[0].strip().lower()
            if script_type == "application/ld+json":
                self.script = []

        content = attributes.get("content")
        if tag == "data" and content is None:
            content = attributes.get("value")
        name = attributes.get("property") or attributes.get("itemprop") or attributes.get("name")
        if content and name:
            self.metadata[name.lower()].add(content.strip())

        if tag == "body":
            classes = set((attributes.get("class") or "").split())
            self.woocommerce_product = {"woocommerce", "single-product"} <= classes
        elif self.woocommerce_product and tag == "p":
            self.in_woocommerce_price = "price" in (attributes.get("class") or "").split()
            self.in_old_price = False
            self.amount_text = None
        elif self.in_woocommerce_price and tag == "del":
            self.in_old_price = True
        elif self.in_woocommerce_price and tag == "bdi" and not self.in_old_price:
            self.amount_text = []

    def handle_data(self, data):
        if self.script is not None:
            self.script.append(data)
        if self.amount_text is not None:
            self.amount_text.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.script is not None:
            self.scripts.append("".join(self.script))
            self.script = None

        if not self.in_woocommerce_price:
            return
        if tag == "bdi" and self.amount_text is not None:
            amount = "".join(self.amount_text).strip()
            if amount.endswith("₽") and (price := ruble_price(amount, "₽")) is not None:
                self.woocommerce_prices.add(price)
            self.amount_text = None
        elif tag == "del":
            self.in_old_price = False
        elif tag == "p":
            self.in_woocommerce_price = False
            self.in_old_price = False
            self.amount_text = None


def schema_nodes(data):
    if isinstance(data, list):
        for entry in data:
            yield from schema_nodes(entry)
    elif isinstance(data, dict):
        yield data
        for key in ("@graph", "mainEntity"):
            yield from schema_nodes(data.get(key))


def schema_name(value):
    return value.rsplit("/", 1)[-1].rsplit(":", 1)[-1] if isinstance(value, str) else ""


def is_product(node):
    types = node.get("@type")
    if not isinstance(types, list):
        types = [types]
    return any(schema_name(value) == "Product" for value in types)


def offer_prices(product, references):
    offers = product.get("offers", [])
    if isinstance(offers, dict):
        offers = [offers]
    if not isinstance(offers, list):
        return set()

    prices = set()
    offer_count = 0
    unavailable_count = 0
    for offer in offers:
        if not isinstance(offer, dict):
            continue
        reference = offer.get("@id")
        if isinstance(reference, str):
            offer = references.get(reference, {}) | offer
        offer_count += 1
        if schema_name(offer.get("availability")) in UNAVAILABLE:
            unavailable_count += 1
            continue
        currency = offer.get("priceCurrency")
        if "price" in offer:
            values = (offer["price"],)
        else:
            values = (offer.get("lowPrice"), offer.get("highPrice"))
            if values[1] is None and offer.get("offerCount") not in (1, "1"):
                continue
        prices.update(price for value in values if (price := ruble_price(value, currency)) is not None)

    if offer_count and offer_count == unavailable_count:
        raise ValueError("Товар недоступен. Проверьте наличие на сайте магазина.")
    return prices


def metadata_prices(metadata):
    for amount, currency in (("product:price:amount", "product:price:currency"), ("price", "pricecurrency")):
        currencies = metadata[currency]
        if len(currencies) == 1:
            code = next(iter(currencies))
            prices = {price for value in metadata[amount] if (price := ruble_price(value, code)) is not None}
            if prices:
                return prices
    return set()


def parse_price(html, content_type=""):
    return parse_product_page(html, content_type)[0]


def parse_product_page(html, content_type=""):
    if not html.strip():
        raise ValueError("Магазин вернул пустую страницу. Введите цену вручную или загрузите сохранённый HTML.")
    charset = CHARSET.search(content_type) or CHARSET.search(html[:2048].decode("ascii", errors="ignore"))
    try:
        text = html.decode(charset[1] if charset else "utf-8-sig", errors="replace")
    except (LookupError, UnicodeError):
        text = html.decode("utf-8-sig", errors="replace")

    markup = StoreMarkup()
    try:
        markup.feed(text)
        markup.close()
    except AssertionError as error:
        raise ValueError("Карточку товара не удалось прочитать. Введите цену вручную.") from error

    nodes = []
    for script in markup.scripts:
        try:
            nodes.extend(list(schema_nodes(json.loads(script, parse_float=Decimal))))
        except (ValueError, InvalidOperation, RecursionError):
            continue

    products = [node for node in nodes if is_product(node)]
    prices = set()
    product_url = None
    if len(products) == 1:
        references = {node["@id"]: node for node in nodes if isinstance(node.get("@id"), str) and len(node) > 1}
        prices = offer_prices(products[0], references)
        product_url = products[0].get("url")

    prices = prices or metadata_prices(markup.metadata) or markup.woocommerce_prices
    if len(prices) > 1:
        raise ValueError("В карточке несколько цен. Выберите нужную на сайте и введите её вручную.")
    if not prices:
        if markup.needs_verification:
            raise ValueError(
                "Сайт запросил проверку «Вы не робот?». Откройте карточку в браузере, "
                "сохраните её через Ctrl+S и выберите «Из HTML»."
            )
        raise ValueError("В HTML нет цены в рублях. Загрузите сохранённую карточку через «Из HTML» или введите цену вручную.")
    page_urls = (markup.canonical_url, product_url, *sorted(markup.metadata["og:url"]))
    page_url = next((url for url in page_urls if isinstance(url, str) and url.startswith(("https://", "http://"))), "")
    return prices.pop(), page_url
