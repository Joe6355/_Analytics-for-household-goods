import json
import re
from collections import defaultdict
from decimal import Decimal
from html.parser import HTMLParser

from calculations import read_number


UNAVAILABLE = {"OutOfStock", "SoldOut", "Discontinued", "PreOrder", "PreSale", "BackOrder"}
CHARSET = re.compile(r"charset\s*=\s*['\"]?([a-zA-Z0-9_-]+)", re.IGNORECASE)


class StoreMarkup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.metadata = defaultdict(set)
        self.scripts = []
        self.script = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "script":
            script_type = (attributes.get("type") or "").partition(";")[0].strip().lower()
            if script_type == "application/ld+json":
                self.script = []

        content = attributes.get("content")
        name = attributes.get("property") or attributes.get("itemprop") or attributes.get("name")
        if content and name:
            self.metadata[name.lower()].add(content.strip())

    def handle_data(self, data):
        if self.script is not None:
            self.script.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.script is not None:
            self.scripts.append("".join(self.script))
            self.script = None


def schema_nodes(data):
    if isinstance(data, list):
        for entry in data:
            yield from schema_nodes(entry)
    elif isinstance(data, dict):
        yield data
        for key in ("@graph", "mainEntity"):
            yield from schema_nodes(data.get(key))


def schema_name(value) -> str:
    return value.rsplit("/", 1)[-1] if isinstance(value, str) else ""


def is_product(node: dict) -> bool:
    types = node.get("@type")
    if not isinstance(types, list):
        types = [types]
    return any(schema_name(value) == "Product" for value in types)


def ruble_price(value, currency) -> Decimal | None:
    if str(currency).strip().upper() not in {"RUB", "RUR", "₽"}:
        return None
    text = "".join(str(value).split()).removesuffix("₽").replace(",", ".")
    whole, dot, fraction = text.partition(".")
    if dot and fraction.isdigit():
        text = f"{whole}.{fraction.rstrip('0')}".rstrip(".")
    try:
        return read_number(text, "Цена", allow_zero=True)
    except ValueError:
        return None


def offer_prices(product: dict, references: dict) -> set[Decimal]:
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
            if values[1] is None and str(offer.get("offerCount")) != "1":
                continue
        prices.update(price for value in values if (price := ruble_price(value, currency)) is not None)

    if offer_count and offer_count == unavailable_count:
        raise ValueError("В карточке нет доступного предложения. Проверьте наличие в магазине.")
    return prices


def metadata_prices(metadata) -> set[Decimal]:
    for amount, currency in (("product:price:amount", "product:price:currency"), ("price", "pricecurrency")):
        currencies = metadata[currency]
        if len(currencies) == 1:
            prices = {price for value in metadata[amount]
                      if (price := ruble_price(value, next(iter(currencies)))) is not None}
            if prices:
                return prices
    return set()


def parse_price(html: bytes, content_type: str = "") -> tuple[Decimal, str]:
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
        raise ValueError("Разметку карточки не удалось прочитать. Введите цену вручную.") from error
    nodes = []
    for script in markup.scripts:
        try:
            nodes.extend(list(schema_nodes(json.loads(script, parse_float=Decimal))))
        except (ValueError, RecursionError):
            continue

    products = [node for node in nodes if is_product(node)]
    title = next(iter(markup.metadata["og:title"]), "")
    prices = set()
    if len(products) == 1:
        product = products[0]
        references = {node["@id"]: node for node in nodes if isinstance(node.get("@id"), str) and len(node) > 1}
        prices = offer_prices(product, references)
        if isinstance(product.get("name"), str):
            title = product["name"]

    if not prices:
        prices = metadata_prices(markup.metadata)
    if len(prices) > 1:
        raise ValueError("В карточке несколько цен. Выберите нужную на сайте и введите вручную.")
    if not prices:
        raise ValueError("Цена в рублях не найдена в HTML. Введите её вручную.")
    return prices.pop(), " ".join(title.split())[:160]
