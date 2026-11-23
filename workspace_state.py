import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from uuid import uuid4

from PyQt6.QtCore import QIODevice, QLockFile, QSaveFile

from product_rating import CustomerRating, decode_customer_rating


UNITS = {"мл", "г", "шт."}
DEFAULT_COMPARISON_NAME = "Основное сравнение"


@dataclass(frozen=True)
class Offer:
    store: str
    amount: Decimal
    price: Decimal
    product: str = ""
    price_note: str = ""
    composition: str = ""
    card_url: str = ""
    loaded_at: str = ""
    offer_id: str = field(default_factory=lambda: uuid4().hex)
    rating: CustomerRating | None = None

    @property
    def price_per_unit(self):
        return Fraction(self.price) / Fraction(self.amount)


@dataclass(frozen=True)
class UsageSamples:
    measurements: str = ""
    tests: int = 0
    successes: int = 0


@dataclass
class Comparison:
    name: str
    unit: str = "мл"
    offers: list[Offer] = field(default_factory=list)
    usage_samples: dict[str, UsageSamples] = field(default_factory=dict)


@dataclass
class WorkspaceState:
    comparisons: list[Comparison] = field(default_factory=lambda: [Comparison(DEFAULT_COMPARISON_NAME)])
    active: int = 0
    draft: dict[str, str | int | None] = field(default_factory=dict)


def decode_state(data):
    try:
        payload = json.loads(data)
        if payload["version"] != 1:
            raise ValueError("Формат сохранённых данных не поддерживается.")
        comparisons = []
        offer_ids = set()
        for value in payload["comparisons"]:
            comparison = Comparison(value["name"], value["unit"])
            if not comparison.name.strip() or comparison.unit not in UNITS:
                raise ValueError("Неверное название или единица в списке сравнения.")
            for entry in value["offers"]:
                offer = Offer(**{
                    **entry, "amount": Decimal(entry["amount"]), "price": Decimal(entry["price"]),
                    "rating": decode_customer_rating(entry.get("rating")),
                })
                if not offer.amount.is_finite() or offer.amount <= 0 or not offer.price.is_finite() or offer.price < 0:
                    raise ValueError("Неверная цена или фасовка в списке сравнения.")
                if not all(isinstance(text, str) for text in (
                    offer.store, offer.product, offer.price_note, offer.composition, offer.card_url, offer.loaded_at,
                    offer.offer_id,
                )):
                    raise ValueError("Неверные данные предложения.")
                if not offer.offer_id or offer.offer_id in offer_ids:
                    raise ValueError("Неверный идентификатор предложения.")
                offer_ids.add(offer.offer_id)
                if offer.loaded_at:
                    datetime.fromisoformat(offer.loaded_at)
                comparison.offers.append(offer)
            comparison.offers.sort(key=lambda offer: offer.price_per_unit)
            for unit, fields in value.get("usage_samples", {}).items():
                samples = UsageSamples(**fields)
                if (unit not in UNITS or not isinstance(samples.measurements, str)
                        or type(samples.tests) is not int or type(samples.successes) is not int
                        or samples.tests < 0 or samples.successes < 0):
                    raise ValueError("Неверные замеры или данные испытаний в списке сравнения.")
                comparison.usage_samples[unit] = samples
            comparisons.append(comparison)

        active = payload["active"]
        draft = payload["draft"]
        if type(active) is not int or not 0 <= active < len(comparisons) or not isinstance(draft, dict):
            raise ValueError("Неверные данные рабочего состояния.")
        return WorkspaceState(comparisons, active, draft)
    except (AttributeError, InvalidOperation, KeyError, RecursionError, TypeError, ValueError) as error:
        raise ValueError("Не удалось прочитать сохранённые сравнения и замеры.") from error


class LocalWorkspace:
    def __init__(self, path):
        self.path = path
        self.original = None
        self.recovery_backup = None

    def read(self):
        try:
            return self.path.read_bytes()
        except FileNotFoundError:
            return None

    def load(self):
        self.original = self.read()
        self.recovery_backup = None
        if self.original is None:
            return WorkspaceState()
        try:
            return decode_state(self.original)
        except ValueError:
            self.recovery_backup = self.path.with_name(f"{self.path.stem}.backup-{uuid4().hex}.json")
            raise

    @staticmethod
    def write(path, data):
        file = QSaveFile(str(path))
        if not file.open(QIODevice.OpenModeFlag.WriteOnly):
            raise OSError(file.errorString())
        if file.write(data) != len(data):
            error = file.errorString()
            file.cancelWriting()
            raise OSError(error)
        if not file.commit():
            raise OSError(file.errorString())

    def save(self, state):
        data = json.dumps({"version": 1, **asdict(state)}, ensure_ascii=False, default=str, indent=2).encode("utf-8")
        if data == self.original:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = QLockFile(str(self.path) + ".lock")
        if not lock.tryLock(0):
            raise OSError("Другое окно сейчас сохраняет данные.")
        try:
            if self.read() != self.original:
                raise OSError("Другое окно уже сохранило изменения.")
            if self.recovery_backup is not None:
                self.write(self.recovery_backup, self.original)
            self.write(self.path, data)
            self.original = data
            backup = self.recovery_backup
            self.recovery_backup = None
            return backup
        finally:
            lock.unlock()
