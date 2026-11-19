import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from fractions import Fraction

from PyQt6.QtCore import QIODevice, QLockFile, QSaveFile


UNITS = {"мл", "г", "шт."}


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

    @property
    def price_per_unit(self):
        return Fraction(self.price) / Fraction(self.amount)


@dataclass
class Comparison:
    name: str
    unit: str = "мл"
    offers: list[Offer] = field(default_factory=list)


@dataclass
class WorkspaceState:
    comparisons: list[Comparison] = field(default_factory=lambda: [Comparison("Основное сравнение")])
    active: int = 0
    draft: dict[str, str | int] = field(default_factory=dict)


def decode_state(data):
    try:
        payload = json.loads(data)
        if payload["version"] != 1:
            raise ValueError("Формат сохранённых данных не поддерживается.")
        comparisons = []
        for value in payload["comparisons"]:
            comparison = Comparison(value["name"], value["unit"])
            if not comparison.name.strip() or comparison.unit not in UNITS:
                raise ValueError("Неверное название или единица в списке сравнения.")
            for entry in value["offers"]:
                offer = Offer(**{**entry, "amount": Decimal(entry["amount"]), "price": Decimal(entry["price"])})
                if not offer.amount.is_finite() or offer.amount <= 0 or not offer.price.is_finite() or offer.price < 0:
                    raise ValueError("Неверная цена или фасовка в списке сравнения.")
                if not all(isinstance(text, str) for text in (
                    offer.store, offer.product, offer.price_note, offer.composition, offer.card_url, offer.loaded_at,
                )):
                    raise ValueError("Неверные данные предложения.")
                if offer.loaded_at:
                    datetime.fromisoformat(offer.loaded_at)
                comparison.offers.append(offer)
            comparison.offers.sort(key=lambda offer: offer.price_per_unit)
            comparisons.append(comparison)

        active = payload["active"]
        draft = payload["draft"]
        if type(active) is not int or not 0 <= active < len(comparisons) or not isinstance(draft, dict):
            raise ValueError("Неверные данные рабочего состояния.")
        return WorkspaceState(comparisons, active, draft)
    except (AttributeError, InvalidOperation, KeyError, TypeError, ValueError) as error:
        raise ValueError("Не удалось прочитать сохранённые сравнения и замеры.") from error


class LocalWorkspace:
    def __init__(self, path):
        self.path = path
        self.original = None

    def read(self):
        try:
            return self.path.read_bytes()
        except FileNotFoundError:
            return None

    def load(self):
        self.original = self.read()
        return WorkspaceState() if self.original is None else decode_state(self.original)

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
            file = QSaveFile(str(self.path))
            if not file.open(QIODevice.OpenModeFlag.WriteOnly):
                raise OSError(file.errorString())
            if file.write(data) != len(data):
                file.cancelWriting()
                raise OSError(file.errorString())
            if not file.commit():
                raise OSError(file.errorString())
            self.original = data
        finally:
            lock.unlock()
