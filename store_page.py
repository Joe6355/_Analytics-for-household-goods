import json
import re
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib.parse import urlsplit


UNAVAILABLE = {"OutOfStock", "SoldOut", "Discontinued", "PreOrder", "PreSale", "BackOrder"}
RUBLE_CURRENCIES = {"RUB", "RUR", "₽"}
PRICE_TEXT = re.compile(r"(?:[0-9]{1,9}|[0-9]{1,3}(?:[ \u00a0\u202f][0-9]{3}){1,2})(?:[.,][0-9]{1,8})?")
CHARSET = re.compile(r"charset\s*=\s*['\"]?([a-zA-Z0-9_-]+)", re.IGNORECASE)
CAPTCHA_FORM = re.compile(r"(?:^|/)(?:checkcaptcha|showcaptcha)(?:[/?#]|$)", re.IGNORECASE)
SAVED_FROM = re.compile(r"saved from url=\([0-9]+\)(https?://\S+)", re.IGNORECASE)
PACKAGE_UNITS = {
    "мл": ("мл", 1), "ml": ("мл", 1), "л": ("мл", 1000), "l": ("мл", 1000),
    "г": ("г", 1), "g": ("г", 1), "кг": ("г", 1000), "kg": ("г", 1000),
    "шт": ("шт.", 1), "штука": ("шт.", 1), "штуки": ("шт.", 1), "штук": ("шт.", 1),
    "pcs": ("шт.", 1),
}
PACKAGE_LABELS = {
    "объем": "мл", "объем бутылки": "мл", "объем упаковки": "мл",
    "масса нетто": "г", "вес нетто": "г", "вес": "г",
    "количество в упаковке": "шт.", "количество штук в упаковке": "шт.",
}
PACKAGE_AMOUNT = re.compile(
    rf"(?<![\w.,+−-])(?P<amount>{PRICE_TEXT.pattern})\s*(?P<unit>{'|'.join(PACKAGE_UNITS)})(?!\w)",
    re.IGNORECASE,
)
PACKAGE_LABEL = re.compile(rf"(?P<label>{'|'.join(PACKAGE_LABELS)})[\s:]+(?P<value>[0-9].*)")
PACKAGE_RANGE_OR_SET = re.compile(
    r"\d\s*[-–—/]\s*\d|(?:[×*]|(?<![а-яёa-z])[xх])\s*\d|\d\s*(?:шт\.?\s*)?по\s+\d",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ProductPage:
    price: Decimal
    url: str
    name: str
    package: tuple[Decimal, str] | None
    price_note: str = ""


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
        self.saved_url = ""
        self.needs_verification = False
        self.headings = set()
        self.labels = set()
        self.text_tag = ""
        self.tag_text = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "link" and "canonical" in (attributes.get("rel") or "").lower().split():
            self.canonical_url = attributes.get("href") or ""
        if tag == "form" and CAPTCHA_FORM.search(attributes.get("action") or ""):
            self.needs_verification = True
        if tag in ("h1", "label"):
            self.text_tag = tag
            self.tag_text = []
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
        if self.text_tag and data.strip():
            self.tag_text.append(data)

    def handle_comment(self, data):
        if not self.saved_url and (saved := SAVED_FROM.fullmatch(data.strip())):
            self.saved_url = saved[1]

    def handle_endtag(self, tag):
        if tag == self.text_tag:
            text = " ".join(" ".join(self.tag_text).split())
            if text:
                (self.headings if tag == "h1" else self.labels).add(text)
            self.text_tag = ""
            self.tag_text = []
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


def package_amount(match):
    amount = Decimal("".join(match["amount"].split()).replace(",", "."))
    unit, scale = PACKAGE_UNITS[match["unit"].lower()]
    if amount > 0 and (unit != "шт." or amount == amount.to_integral_value()):
        return (amount * scale).normalize(), unit
    return None


def package_labels(product, labels):
    yield from labels
    properties = product.get("additionalProperty", [])
    if isinstance(properties, dict):
        properties = [properties]
    if not isinstance(properties, list):
        return
    for prop in properties:
        if not isinstance(prop, dict) or not isinstance(prop.get("name"), str):
            continue
        value = prop.get("value")
        if type(value) not in (str, int, Decimal):
            continue
        value = str(value).strip()
        unit = prop.get("unitText")
        if isinstance(unit, str) and PRICE_TEXT.fullmatch(value):
            value = f"{value} {unit}"
        yield f"{prop['name']} {value}"


def find_package(name, labels):
    if PACKAGE_RANGE_OR_SET.search(name):
        return None
    packages = {amount for match in PACKAGE_AMOUNT.finditer(name) if (amount := package_amount(match)) is not None}
    if len(packages - {(Decimal(1), "шт.")}) > 1:
        return None
    for label in labels:
        match = PACKAGE_LABEL.fullmatch(label.lower().replace("ё", "е"))
        if not match:
            continue
        unit = PACKAGE_LABELS[match["label"]]
        value = match["value"].strip().removesuffix(".")
        if unit == "шт." and re.fullmatch(r"[0-9]{1,9}", value):
            value += " шт"
        quantity = PACKAGE_AMOUNT.fullmatch(value)
        if quantity and (amount := package_amount(quantity)) is not None and amount[1] == unit:
            packages.add(amount)

    if any(unit == "шт." and amount > 1 for amount, unit in packages) and any(unit != "шт." for _, unit in packages):
        return None
    for unit in ("мл", "г", "шт."):
        candidates = {package for package in packages if package[1] == unit}
        if candidates:
            return candidates.pop() if len(candidates) == 1 else None
    return None


def parse_price(html, content_type=""):
    return parse_product_page(html, content_type).price


def parse_browser_product(text, address, title):
    host = (urlsplit(address).hostname or "").removeprefix("www.")
    if host not in ("ozon.ru", "market.yandex.ru"):
        raise ValueError("Для этого магазина пока доступен разбор HTML карточки.")
    name = re.split(r"\s+(?:—\s*)?купить\b", title, maxsplit=1, flags=re.IGNORECASE)[0].strip()
    lines = [" ".join(line.replace("\ufffc", "").split()) for line in text.splitlines()]
    lines = [line for line in lines if line]
    if not name or name not in lines:
        raise ValueError("Дождитесь появления названия и цены товара в браузере.")
    card = "\n".join(lines[lines.index(name) + 1:])
    for marker in ("В корзину", "Рекомендуем также", "Покупают вместе", "Подобрали для вас", "Нет в наличии"):
        card = card.split(marker, 1)[0]
    if host == "ozon.ru":
        card = card.split("Товар проверен", 1)[-1]
    match = re.search(rf"(?P<price>{PRICE_TEXT.pattern})\s*₽", card)
    if not match:
        raise ValueError("Дождитесь появления цены товара в браузере.")
    price = ruble_price(match["price"], "RUB")
    note = "С банками" if "С банками" in card[match.end():match.end() + 30] else ""
    if host == "market.yandex.ru" and "Пэй" in card[match.end():match.end() + 30]:
        note = "Цена с Яндекс Пэй"
    labels = []
    for label, value in zip(lines, lines[1:]):
        key, _, unit = label.lower().replace("ё", "е").partition(",")
        if key in PACKAGE_LABELS:
            if unit.strip() in PACKAGE_UNITS and PRICE_TEXT.fullmatch(value):
                value = f"{value} {unit.strip()}"
            labels.append(f"{key} {value}")
    return ProductPage(price, address, name, find_package(name, labels), note)


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
    product = products[0] if len(products) == 1 else {}
    prices = set()
    if product:
        references = {node["@id"]: node for node in nodes if isinstance(node.get("@id"), str) and len(node) > 1}
        prices = offer_prices(product, references)

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
    page_urls = (markup.canonical_url, product.get("url"), *sorted(markup.metadata["og:url"]), markup.saved_url)
    page_url = next((url for url in page_urls if isinstance(url, str) and url.startswith(("https://", "http://"))), "")
    name = product.get("name")
    name = " ".join(name.split()) if isinstance(name, str) else ""
    for names in (markup.headings, markup.metadata["og:title"]):
        if not name and len(names) == 1:
            name = next(iter(names))
    return ProductPage(prices.pop(), page_url, name, find_package(name, package_labels(product, markup.labels)))
