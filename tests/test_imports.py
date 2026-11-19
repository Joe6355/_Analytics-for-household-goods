import json
import os
import unittest
from decimal import Decimal
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QUrl
from PyQt6.QtNetwork import QNetworkReply
from PyQt6.QtWidgets import QApplication

from browser_loader import same_product_address
from main import HouseholdGoodsWindow
from price_loader import PriceLoader
from store_page import (
    BrowserPageRequired, ProductPage, ProductUnavailable, StorePageUnavailable, find_package,
    is_store_card, parse_browser_product, parse_product_page,
)


WB_URL = "https://www.wildberries.ru/catalog/1052203330/detail.aspx"
ALI_URL = "https://aliexpress.ru/item/1005012259382305.html?sku_id=12000057880117745"
ALI_NAME = "Набор губок-щеток для мытья посуды 6 шт."


class CardParsingTests(unittest.TestCase):
    def test_ozon_skips_cost_per_100_ml_in_volume_options(self):
        name = "Средство для мытья посуды SYNERGETIC 1 л"
        text = f"{name}\nОбъем, мл:\n500 36 ₽/100 мл\n1000 23 ₽/100 мл\n226 ₽\nС банками\n250 ₽\nВ корзину"
        page = parse_browser_product(text, "https://www.ozon.ru/product/synergetic-540246714/", f"{name} купить на OZON")
        self.assertEqual((page.price, page.package, page.price_note), (Decimal(226), (Decimal(1000), "мл"), "С банками"))

    def test_wildberries_volume_selector_does_not_replace_selected_package(self):
        heading = "Средство для мытья посуды Лимон, 900 мл"
        page = parse_browser_product(
            f"Объем товара\n450 мл\n900 мл\n1350 мл\nFairy\n{heading}\n265 ₽\n274 ₽\nВ корзину",
            WB_URL, f"{heading} Fairy 1052203330 купить за 274 ₽ в интернет-магазине Wildberries",
        )
        self.assertEqual((page.name, page.price, page.package),
                         (f"Fairy / {heading}", Decimal(265), (Decimal(900), "мл")))

    def test_aliexpress_combo_is_a_pack_not_stock_count(self):
        page = parse_browser_product(
            f"{ALI_NAME}\n291 ₽\nВ комбо-корзину\n{ALI_NAME}\nОсталось 12 шт.\nПохожие товары\n99 ₽",
            ALI_URL, f"{ALI_NAME} | AliExpress",
        )
        self.assertEqual((page.name, page.price, page.package, page.price_note),
                         (ALI_NAME, Decimal(291), (Decimal(6), "шт."), "Цена комбо"))

    def test_yandex_volume_is_read_from_characteristics(self):
        name = "Средство для мытья посуды FAIRY Зеленое яблоко"
        page = parse_browser_product(
            f"{name}\n244 ₽\nс Пэй\nОбъем\n0.9 л\nВ корзину",
            "https://market.yandex.ru/card/fairy/4850591942", f"{name} — купить в Яндекс Маркете",
        )
        self.assertEqual(page.package, (Decimal(900), "мл"))
        self.assertEqual(page.price_note, "Цена с Яндекс Пэй")

    def test_5ka_uses_current_price_with_split_kopecks(self):
        name = "Средство для мытья посуды Synergetic 1л"
        page = parse_browser_product(
            f"{name}\n1 л\nСтарая цена\n219\n99 ₽\nЦена\n204\n99 ₽\nВ корзину",
            "https://5ka.ru/product/synergetic--3963807/", f"{name}: купить с доставкой",
        )
        self.assertEqual(page.price, Decimal("204.99"))

    def test_lenta_mobile_uses_product_price_after_recommendations(self):
        name = "Средство для посуды ЛЕНТА Бамбук, 900мл"
        text = (f"{name}\nПолное название товара\n{name}\nПохожие товары\nС Картой №1\n50 ₽\nВ корзину\n"
                "Другие категории с этим товаром\nС Картой №1\n144\n99\n₽\n/ шт\n173\n69\n-16%\nВ корзину")
        page = parse_browser_product(text, "https://lenta.com/product/bambuk-524523/", "Лента – сеть магазинов")
        self.assertEqual((page.name, page.price, page.package, page.price_note),
                         (name, Decimal("144.99"), (Decimal(900), "мл"), "Цена с Картой №1"))

    def test_browser_reads_composition_from_the_product_section(self):
        name = "Гель для посуды 900 мл"
        ingredients = "Вода, анионные ПАВ (5,5%), отдушка"
        details = (f"{name}\nЦена\n199,99 ₽\nВ корзину\nОписание\nГель для кухни\n"
                   f"Состав\n{ingredients}\nХарактеристики\nПохожие товары\nСостав: Чужой товар")
        page = parse_browser_product(details, "https://5ka.ru/product/gel--123/", f"{name}: купить с доставкой")
        self.assertEqual(page.composition, ingredients)

    def test_html_reads_magnit_composition_block_and_ignores_description(self):
        schema = {
            "@type": "Product", "name": "Гель 900 мл", "description": "Гель для мытья посуды",
            "offers": {"@type": "Offer", "price": "239.99", "priceCurrency": "RUB"},
        }
        head = f'<script type="application/ld+json">{json.dumps(schema)}</script>'
        block = ('<div data-param-name="Состав"><div>Состав</div><div>'
                 '<section data-test-id="product-detail-parameters-full-full-context-data">'
                 '<p>Вода, &lt;5% неионогенные ПАВ</p></section></div></div>')
        self.assertEqual(parse_product_page(head.encode()).composition, "")
        self.assertEqual(parse_product_page((head + block).encode()).composition, "Вода, <5% неионогенные ПАВ")

    def test_dixy_loads_product_price_in_rubles_and_reports_site_error(self):
        name = "Чистящее средство 500г"
        url = "https://dixy.ru/product/chistyashchee-sredstvo-2000623173/"
        page = parse_browser_product(f"{name}\n329.90 руб.\n499.90 руб.\nВ корзину", url, name)
        self.assertEqual((page.price, page.package), (Decimal("329.90"), (Decimal(500), "г")))
        with self.assertRaises(StorePageUnavailable):
            parse_browser_product("Не удалось загрузить сайт", url, "Не удалось загрузить сайт")

    def test_unavailable_item_does_not_take_recommendation_price(self):
        name = "Средство FAIRY 900мл"
        for stock in ("Нет в наличии", "Товар закончился"):
            with self.subTest(stock=stock), self.assertRaises(ProductUnavailable):
                parse_browser_product(
                    f"{name}\n{stock}\nПохожие товары\n99 ₽",
                    "https://lenta.com/product/fairy-123/", f"{name} купить в Ленте",
                )

    def test_aliexpress_foreign_currency_and_price_range_are_rejected(self):
        for price in ("$12.99", "от 99 ₽", "4 платежа по 99 ₽", "99 ₽ – 150 ₽"):
            with self.subTest(price=price), self.assertRaises(ValueError):
                parse_browser_product(f"{ALI_NAME}\n{price}", ALI_URL, f"{ALI_NAME} | AliExpress")

    def test_html_stock_status_takes_priority_over_metadata_price(self):
        schema = {"@type": "Product", "name": "Гель 900 мл", "offers": {
            "@type": "Offer", "price": "279.00", "priceCurrency": "RUB",
            "availability": "https://schema.org/OutOfStock",
        }}
        html = (f'<meta itemprop="price" content="279"><meta itemprop="priceCurrency" content="RUB">'
                f'<script type="application/ld+json">{json.dumps(schema)}</script>').encode()
        with self.assertRaises(ProductUnavailable):
            parse_product_page(html)

    def test_missing_html_price_requests_browser(self):
        with self.assertRaises(BrowserPageRequired):
            parse_product_page(b"<html><h1>Product</h1></html>")

    def test_multipack_uses_total_volume_or_weight(self):
        cases = (
            ("Гель 2 шт по 900 мл", Decimal(1800), "мл"),
            ("Fairy Pure&Clean 3 х 900 мл. (2,7л)", Decimal(2700), "мл"),
            ("Порошок 1,25 кг × 2 шт.", Decimal(2500), "г"),
        )
        for name, total, unit in cases:
            with self.subTest(name=name):
                self.assertEqual(find_package(name, []), (total, unit))

    def test_ambiguous_pack_size_is_left_unset(self):
        cases = ("Гель 450–900 мл", "Гель 500+250 мл", "Гель 2 × 3 × 900 мл", "Гель 2 × 900 мл (1,5 л)",
                 "Гель 900 мл × 2 кг", "Гель от 2 × 900 мл", "Гель 2 × 900 мл + губка")
        for name in cases:
            with self.subTest(name=name):
                self.assertIsNone(find_package(name, []))

    def test_yandex_multipack_uses_price_of_whole_set(self):
        name = "Средство для мытья посуды Fairy Pure&Clean 3 х 900 мл. (2,7л)"
        page = parse_browser_product(
            f"{name}\n226 ₽/шт\n678 ₽\nПэй\nОбъем\n900 мл\nВ корзину",
            "https://market.yandex.ru/card/fairy/4770204134", f"{name} — купить в Яндекс Маркете",
        )
        self.assertEqual((page.price, page.package, page.price_note),
                         (Decimal(678), (Decimal(2700), "мл"), "Цена с Яндекс Пэй"))

    def test_aliexpress_item_prefix_is_supported_but_catalog_is_not(self):
        self.assertTrue(is_store_card("https://aliexpress.ru/item/0_1005012259382305.html"))
        self.assertFalse(is_store_card("https://aliexpress.ru/wholesale?SearchText=губки"))

    def test_browser_address_must_keep_selected_sku_and_store(self):
        self.assertTrue(same_product_address(ALI_URL, ALI_URL + "&utm_source=test"))
        self.assertFalse(same_product_address(ALI_URL, ALI_URL.replace("117745", "117746")))
        self.assertFalse(same_product_address(ALI_URL, ALI_URL.replace("aliexpress.ru", "example.com")))
        self.assertFalse(same_product_address(ALI_URL, ALI_URL.replace("https:", "http:")))


class ImportFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_loaded_card_immediately_adds_complete_offer(self):
        window = HouseholdGoodsWindow()
        self.addCleanup(window.close)
        product = ProductPage(Decimal(291), ALI_URL, ALI_NAME, (Decimal(6), "шт."), "Цена комбо")
        window.apply_loaded_product("aliexpress.ru", product)
        self.assertEqual(window.offersTable.rowCount(), 1)
        self.assertEqual(window.offersTable.item(0, 0).text(), ALI_NAME)
        self.assertEqual(window.offersTable.item(0, 2).text(), "6 шт.")
        self.assertEqual(window.offersTable.item(0, 3).text(), "291,00")
        self.assertEqual(window.offersTable.item(0, 4).text(), "48,50")
        self.assertIn("Цена комбо", window.offersTable.item(0, 3).toolTip())

    def test_selected_product_composition_is_transferred_and_analyzed(self):
        window = HouseholdGoodsWindow()
        self.addCleanup(window.close)
        name = "Гель 900 мл"
        ingredients = "Вода, ПАВ (0,5%, неионогенные), Консервант 0,5%; вода"
        page = ProductPage(Decimal(199), "", name, (Decimal(900), "мл"), composition=ingredients)
        window.apply_loaded_product("Магазин", page)
        window.offersTable.selectRow(0)
        window.useOfferButton.click()
        window.sectionList.setCurrentRow(window.sectionStack.indexOf(window.characteristicsPage))
        self.assertEqual(window.characteristicsProduct.text(), name)
        self.assertEqual(window.compositionInput.toPlainText(), ingredients)
        window.analyzeCompositionButton.click()
        self.assertEqual(window.componentCount.text(), "Уникальных компонентов — 3\nПовторы — Вода")

        other = ProductPage(Decimal(99), "", "Другой гель 900 мл", (Decimal(900), "мл"))
        window.apply_loaded_product("Другой магазин", other)
        window.offersTable.selectRow(0)
        window.useOfferButton.click()
        self.assertEqual(window.productInput.text(), other.name)
        self.assertEqual(window.compositionInput.toPlainText(), "")
        self.assertEqual(window.componentCount.text(), "")

    def test_http_access_error_automatically_uses_browser_without_failure(self):
        for status in (401, 403, 429):
            with self.subTest(status=status):
                loader = PriceLoader()
                loader.request_url = QUrl("https://fix-price.com/catalog/household/p-123-gel")
                reply = Mock()
                reply.error.return_value = QNetworkReply.NetworkError.ContentAccessDenied
                reply.attribute.return_value = status
                loader.reply = reply
                failed = Mock()
                loader.failed.connect(failed)
                with patch.object(loader, "load_browser") as browser:
                    loader.finish()
                    browser.assert_called_once_with(loader.request_url, announce=False)
                failed.assert_not_called()
                self.assertIsNone(loader.reply)
                reply.deleteLater.assert_called_once()

    def test_timeout_automatically_uses_browser(self):
        loader = PriceLoader()
        loader.request_url = QUrl("https://magnit.ru/product/123-gel")
        loader.reply = Mock()
        loader.download_error = "Магазин не ответил вовремя."
        with patch.object(loader, "load_browser") as browser:
            loader.finish()
            browser.assert_called_once_with(loader.request_url, announce=False)


if __name__ == "__main__":
    unittest.main()
