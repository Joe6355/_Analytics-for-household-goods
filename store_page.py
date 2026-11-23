import json
import re
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib.parse import urlsplit

from product_rating import CustomerRating, aggregate_rating, browser_rating


UNAVAILABLE = {"OutOfStock", "SoldOut", "Discontinued", "PreOrder", "PreSale", "BackOrder"}
RUBLE_CURRENCIES = {"RUB", "RUR", "₽"}
ALIEXPRESS_HOSTS = {"aliexpress.ru", "aliexpress.com"}
BROWSER_STORE_HOSTS = {
    "ozon.ru", "market.yandex.ru", "wildberries.ru", "5ka.ru", "dixy.ru", "lenta.com",
} | ALIEXPRESS_HOSTS
SUPPORTED_STORE_HOSTS = BROWSER_STORE_HOSTS | {"magnit.ru", "fix-price.com"}
PRICE_TEXT = re.compile(r"(?:[0-9]{1,9}|[0-9]{1,3}(?:[ \u00a0\u202f][0-9]{3}){1,2})(?:[.,][0-9]{1,8})?")
BROWSER_PRICE = re.compile(
    rf"(?<![\w.,+−-])(?P<price>{PRICE_TEXT.pattern})\s*(?:₽|руб(?:\.|ля|лей)?(?!\w))",
    re.IGNORECASE,
)
UNIT_PRICE = re.compile(r"\s*(?:/|за\s+)\s*(?:[0-9]+\s*)?(?:мл|г|кг|л|шт)\b", re.IGNORECASE)
SPLIT_RUBLE_PRICE = re.compile(r"(?<![\d.,])([0-9]{1,3}(?: [0-9]{3}){1,2}|[0-9]{1,9})\s+([0-9]{2})\s*₽")
RECOMMENDATIONS = re.compile(
    r"^(?:Рекомендуем также|Покупают вместе|Подобрали для вас|Похожие товары|С этим товаром покупают|Берут вместе)$",
    re.MULTILINE | re.IGNORECASE,
)
OZON_DESCRIPTION = re.compile(r"^Описание\n(.*?)^Характеристики$", re.MULTILINE | re.DOTALL | re.IGNORECASE)
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
    "объем": "мл", "объем бутылки": "мл", "объем упаковки": "мл", "объем товара": "мл",
    "масса нетто": "г", "вес нетто": "г", "вес": "г",
    "количество в упаковке": "шт.", "количество штук в упаковке": "шт.",
}
PACKAGE_AMOUNT = re.compile(
    rf"(?<![\w.,+−-])(?P<amount>{PRICE_TEXT.pattern})\s*(?P<unit>{'|'.join(PACKAGE_UNITS)})(?!\w)",
    re.IGNORECASE,
)
PACKAGE_LABEL = re.compile(rf"(?P<label>{'|'.join(PACKAGE_LABELS)})[\s:]+(?P<value>[0-9].*)")
PACKAGE_RANGE_OR_SET = re.compile(
    r"\d\s*[-–—/+]\s*\d|(?:[×*]|(?<![а-яёa-z])[xх])\s*\d|\d\s*(?:шт\.?\s*)?по\s+\d",
    re.IGNORECASE,
)
MEASURE_UNITS = "|".join(symbol for symbol, (unit, _) in PACKAGE_UNITS.items() if unit != "шт.")
MEASURE = rf"(?P<amount>{PRICE_TEXT.pattern})\s*(?P<unit>{MEASURE_UNITS})(?!\w)"
MULTIPACK_START = r"(?<![\w.,+−–—/-])"
MULTIPACK_COUNT = r"(?P<count>[0-9]{1,5})"
MULTIPACK_PATTERNS = (
    re.compile(rf"{MULTIPACK_START}{MULTIPACK_COUNT}\s*(?:[×xх*]\s*|шт\.?\s+по\s+){MEASURE}", re.IGNORECASE),
    re.compile(
        rf"{MULTIPACK_START}{MEASURE}\s*[×xх*]\s*{MULTIPACK_COUNT}(?:\s*шт\.?(?!\w)|(?=\s*(?:$|[)])))",
        re.IGNORECASE,
    ),
)
COMPOSITION_LABELS = {"состав", "состав средства", "ингредиенты"}
COMPOSITION_FIELD = re.compile(r"^(?:состав(?: средства)?|ингредиенты)(?:\s*:\s*(.*))?$", re.IGNORECASE)
COMPOSITION_SECTION_END = re.compile(
    r"^(?:описание|характеристики|отзывы|вопросы|доставка|артикул|о товаре|"
    r"способ применения|меры предосторожности|подробнее|показать|перейти|раскрыть|свернуть|"
    r"нет (?:данных|информации)|не указан[оа]?)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ProductPage:
    price: Decimal
    url: str
    name: str
    package: tuple[Decimal, str] | None
    price_note: str = ""
    composition: str = ""
    rating: CustomerRating | None = None


class BrowserPageRequired(ValueError):
    """Цена появится после загрузки карточки в браузере."""


class StorePageUnavailable(ValueError):
    """В браузере показано сообщение об ошибке магазина вместо карточки."""


class ProductUnavailable(StorePageUnavailable):
    """Магазин явно указал отсутствие товара."""


def store_host(address):
    host = (urlsplit(address).hostname or "").removeprefix("www.")
    return {"fix-price.ru": "fix-price.com", "ru.aliexpress.com": "aliexpress.com"}.get(host, host)


def is_store_card(address):
    host = store_host(address)
    path = urlsplit(address).path
    if host == "wildberries.ru":
        return re.fullmatch(r"/catalog/[0-9]+/detail\.aspx/?", path) is not None
    if host in ALIEXPRESS_HOSTS:
        return re.fullmatch(r"/item/(?:0_)?[0-9]+\.html/?", path) is not None
    if host == "market.yandex.ru":
        return path.startswith(("/card/", "/product--"))
    if host == "fix-price.com":
        return path.startswith("/catalog/") and "/p-" in path
    if host in {"ozon.ru", "5ka.ru", "magnit.ru", "dixy.ru", "lenta.com"}:
        return path.startswith("/product/") and bool(path.removeprefix("/product/").strip("/"))
    return False


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
        self.composition_depth = 0
        self.composition_text = None
        self.compositions = set()

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "link" and "canonical" in (attributes.get("rel") or "").lower().split():
            self.canonical_url = attributes.get("href") or ""
        if tag == "form" and CAPTCHA_FORM.search(attributes.get("action") or ""):
            self.needs_verification = True
        if tag in ("h1", "label"):
            self.text_tag = tag
            self.tag_text = []
        if tag == "div":
            if self.composition_depth:
                self.composition_depth += 1
            elif is_composition_label(attributes.get("data-param-name")):
                self.composition_depth = 1
        if (self.composition_depth and tag == "section"
                and attributes.get("data-test-id") == "product-detail-parameters-full-full-context-data"):
            self.composition_text = []
        if self.composition_text is not None and tag in ("p", "li", "br"):
            self.composition_text.append("\n")
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
        if self.composition_text is not None:
            self.composition_text.append(data)
        if self.text_tag and data.strip():
            self.tag_text.append(data)

    def handle_comment(self, data):
        if not self.saved_url and (saved := SAVED_FROM.fullmatch(data.strip())):
            self.saved_url = saved[1]

    def handle_endtag(self, tag):
        if tag == "section" and self.composition_text is not None:
            self.compositions.add(clean_composition("".join(self.composition_text)))
            self.composition_text = None
        if tag == "div" and self.composition_depth:
            self.composition_depth -= 1
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
        raise ProductUnavailable("Товар недоступен. Проверьте наличие на сайте магазина.")
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


def product_properties(product):
    properties = product.get("additionalProperty", [])
    if isinstance(properties, dict):
        properties = [properties]
    if not isinstance(properties, list):
        return
    for prop in properties:
        if isinstance(prop, dict) and isinstance(prop.get("name"), str):
            yield prop


def package_labels(product, labels):
    yield from labels
    for prop in product_properties(product):
        value = prop.get("value")
        if type(value) not in (str, int, Decimal):
            continue
        value = str(value).strip()
        unit = prop.get("unitText")
        if isinstance(unit, str) and PRICE_TEXT.fullmatch(value):
            value = f"{value} {unit}"
        yield f"{prop['name']} {value}"


def is_composition_label(value):
    return isinstance(value, str) and value.strip().removesuffix(":").strip().casefold() in COMPOSITION_LABELS


def clean_composition(value):
    if not isinstance(value, str):
        return ""
    lines = [" ".join(line.split()) for line in value.splitlines()]
    text = "\n".join(line for line in lines if line)
    if len(text) > 4000 or COMPOSITION_SECTION_END.match(text) or not any(letter.isalpha() for letter in text):
        return ""
    return text


def single_composition(values):
    candidates = {value for value in values if value}
    return next(iter(candidates)) if len(candidates) == 1 else ""


def browser_composition(details):
    lines = details.splitlines()
    values = []
    for label, next_line in zip(lines, lines[1:] + [""]):
        match = COMPOSITION_FIELD.fullmatch(label)
        if match:
            candidate = match[1] or next_line
            if not COMPOSITION_FIELD.fullmatch(candidate):
                values.append(clean_composition(candidate))
    return single_composition(values)


def find_multipack(name):
    matches = [match for pattern in MULTIPACK_PATTERNS for match in pattern.finditer(name)]
    if len(matches) != 1:
        return None

    match = matches[0]
    before, after = name[:match.start()], name[match.end():]
    remainder = f"{before} {after}"
    if ("+" in remainder or PACKAGE_RANGE_OR_SET.search(remainder)
            or re.search(r"(?:\d\s*[×xх*/−–—-]|\b(?:от|до))\s*$", before, re.IGNORECASE)
            or re.match(r"\s*[×xх*/−–—-]\s*\d", after, re.IGNORECASE)):
        return None

    count = int(match["count"])
    size = package_amount(match)
    if count <= 0 or not size:
        return None

    amount, unit = size
    total = (amount * count).normalize(), unit
    # В названии иногда дублируют общий объём: «3 × 900 мл (2,7 л)».
    allowed = {total, (Decimal(count), "шт.")}
    if any(package_amount(extra) not in allowed for extra in PACKAGE_AMOUNT.finditer(remainder)):
        return None
    return total


def find_package(name, labels):
    if PACKAGE_RANGE_OR_SET.search(name):
        return find_multipack(name)
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


def truncated_browser_heading(name, lines):
    matches = set()
    for line in lines:
        prefix, ellipsis, suffix = line.partition("…")
        prefix, suffix = prefix.rstrip(), suffix.lstrip()
        if (ellipsis and prefix and suffix and len(prefix) + len(suffix) < len(name)
                and name.startswith(prefix) and name.endswith(suffix)):
            matches.add(line)
    return next(iter(matches)) if len(matches) == 1 else ""


def parse_browser_product(text, address, title):
    host = store_host(address)
    if host not in SUPPORTED_STORE_HOSTS:
        raise ValueError("Для этого магазина пока доступен разбор HTML карточки.")
    if not is_store_card(address):
        raise ValueError("Укажите прямую ссылку на карточку товара.")
    name = re.split(r"(?:\s*[—–:-]\s*|\s+)купить\b", title, maxsplit=1, flags=re.IGNORECASE)[0].strip()
    if host in ALIEXPRESS_HOSTS:
        name = re.sub(r"\s*\|\s*AliExpress\s*$", "", name, flags=re.IGNORECASE)
    name = " ".join(name.split())
    lines = [" ".join(line.replace("\ufffc", "").split()) for line in text.splitlines()]
    lines = [line for line in lines if line]
    if "Не удалось загрузить сайт" in lines:
        raise StorePageUnavailable("Сайт магазина показывает ошибку загрузки. Повторите добавление, когда карточка откроется в браузере.")
    heading = name
    if host == "ozon.ru" and name not in lines:
        # В узком блоке Ozon сокращает середину заголовка, а название вкладки остаётся полным.
        heading = truncated_browser_heading(name, lines)
    if host == "lenta.com" and heading not in lines and "Полное название товара" in lines:
        index = lines.index("Полное название товара")
        if index + 1 < len(lines) and lines[index + 1] in lines[:index]:
            name = heading = lines[index + 1]
    if host == "wildberries.ru":
        article = urlsplit(address).path.split("/")[2]
        title_name = name.removesuffix(f" {article}")
        headings = [
            line for line in lines
            if title_name == line or title_name.startswith(line + " ") and title_name[len(line) + 1:] in lines
        ]
        if headings:
            heading = max(headings, key=len)
            brand = title_name[len(heading):].strip()
            name = f"{brand} / {heading}" if brand else heading
    anchor = name if name in lines else heading
    if not name or anchor not in lines:
        if any("не робот" in line.lower() for line in lines[:20]):
            raise ValueError("Магазин запросил проверку «Вы не робот?». Пройдите её в браузере и повторите добавление.")
        raise ValueError("Дождитесь появления названия и цены товара в браузере.")
    details = "\n".join(lines[lines.index(anchor) + 1:])
    card = RECOMMENDATIONS.split(details, maxsplit=1)[0]
    rating = browser_rating(card.splitlines())
    composition = browser_composition(card)
    if not composition and host == "ozon.ru":
        # Ozon может разместить описание выбранного товара после блока «Покупают вместе».
        composition = single_composition(
            browser_composition(section[1])
            for section in OZON_DESCRIPTION.finditer(details)
            if not RECOMMENDATIONS.search(section[1])
        )
    if re.search(r"^(?:Нет в наличии|Товар закончился|Товар недоступен|Товар распродан)\b", card, re.MULTILINE | re.IGNORECASE):
        raise ProductUnavailable("Товар недоступен. Проверьте наличие на сайте магазина.")
    if host == "lenta.com":
        # В узком окне ценник закреплён внизу, после рекомендаций и категорий.
        _, _, buy_panel = details.partition("\nДругие категории с этим товаром\n")
        if "\nВ корзину" in buy_panel:
            card = buy_panel
        if "С Картой №1" in card:
            card = card.split("С Картой №1", 1)[1]
            price_note = "Цена с Картой №1"
        else:
            price_note = ""
        if "\nВ корзину" not in card:
            raise ValueError("Лента не показывает цену покупки. Проверьте наличие в выбранном магазине.")
    else:
        price_note = ""
    card = re.split(r"^(?:В корзину|Добавить в корзину)$", card, maxsplit=1, flags=re.MULTILINE)[0]
    if host == "ozon.ru":
        card = card.split("Товар проверен", 1)[-1]
    if host == "5ka.ru":
        current_price = re.search(r"^Цена\b[\s:]*", card, re.MULTILINE | re.IGNORECASE)
        if current_price:
            card = card[current_price.end():]
        elif "Старая цена" in card:
            raise ValueError("Дождитесь появления текущей цены товара в Пятёрочке.")
    if host in {"5ka.ru", "lenta.com"}:
        # Рубли и копейки на сайтах могут находиться в разных текстовых узлах.
        card = SPLIT_RUBLE_PRICE.sub(r"\1,\2 ₽", card)
    skip_unit_prices = host == "ozon.ru" or host == "market.yandex.ru" and PACKAGE_RANGE_OR_SET.search(name)
    match = next((match for match in BROWSER_PRICE.finditer(card)
                  if not skip_unit_prices or not UNIT_PRICE.match(card, match.end())), None)
    price = ruble_price(match["price"], "RUB") if match else None
    if price is None:
        raise ValueError("Дождитесь появления цены товара в браузере.")
    before_price = card[max(0, match.start() - 30):match.start()]
    after_price = card[match.end():match.end() + 30]
    if (re.search(r"(?:\d\s*[-–—]|\b(?:от|до|по))\s*$", before_price, re.IGNORECASE)
            or re.match(r"[ \t]*[-–—][ \t]*[0-9]+[.,0-9 ]*\s*₽", after_price)):
        raise ValueError("Выберите вариант товара с полной ценой в браузере и повторите добавление.")
    if host == "ozon.ru" and "С банками" in after_price:
        price_note = "С банками"
    if host == "market.yandex.ru" and "Пэй" in after_price:
        price_note = "Цена с Яндекс Пэй"
    if host in ALIEXPRESS_HOSTS and "В комбо-корзину" in card:
        price_note = "Цена комбо"
    labels = []
    for label, value, following in zip(lines, lines[1:], lines[2:] + [""]):
        key, _, unit = label.lower().replace("ё", "е").partition(",")
        if key in PACKAGE_LABELS:
            # Несколько фасовок подряд относятся к переключателю вариантов товара.
            if PACKAGE_AMOUNT.fullmatch(value) and PACKAGE_AMOUNT.fullmatch(following):
                continue
            if unit.strip() in PACKAGE_UNITS and PRICE_TEXT.fullmatch(value):
                value = f"{value} {unit.strip()}"
            labels.append(f"{key} {value}")
    return ProductPage(price, address, name, find_package(name, labels), price_note, composition, rating)


def parse_product_page(html, content_type=""):
    if not html.strip():
        raise BrowserPageRequired("Магазин вернул пустую страницу. Дождитесь загрузки карточки в браузере.")
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
        raise BrowserPageRequired("В карточке несколько цен. Откройте нужный вариант товара в браузере.")
    if not prices:
        if markup.needs_verification:
            raise BrowserPageRequired(
                "Сайт запросил проверку «Вы не робот?». Пройдите её в браузере и повторите добавление."
            )
        raise BrowserPageRequired("В HTML нет цены в рублях. Дождитесь появления цены в браузере.")
    page_urls = (markup.canonical_url, product.get("url"), *sorted(markup.metadata["og:url"]), markup.saved_url)
    page_url = next((url for url in page_urls if isinstance(url, str) and url.startswith(("https://", "http://"))), "")
    name = product.get("name")
    name = " ".join(name.split()) if isinstance(name, str) else ""
    for names in (markup.headings, markup.metadata["og:title"]):
        if not name and len(names) == 1:
            name = next(iter(names))
    structured_comp = (clean_composition(prop.get("value")) for prop in product_properties(product)
                       if is_composition_label(prop["name"]))
    composition = single_composition([*markup.compositions, *structured_comp])
    return ProductPage(prices.pop(), page_url, name, find_package(name, package_labels(product, markup.labels)),
                       composition=composition, rating=aggregate_rating(product.get("aggregateRating")))
