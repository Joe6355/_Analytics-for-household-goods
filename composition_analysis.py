import re
from dataclasses import dataclass
from unicodedata import normalize


QUOTES = {'"': '"', '«': '»', '“': '”'}
SYMBOLS = str.maketrans({"˂": "<", "˃": ">"})
NUMBER = r"\d+(?:[.,]\d+)?"
CONCENTRATION = re.compile(
    rf"(?<!\w)((?:[<>≤≥]|(?:не\s+)?(?:менее|более|до|от))?\s*{NUMBER}"
    rf"(?:\s*[-–—]\s*{NUMBER})?\s*%(?:\s+и\s+(?:более|менее))?)$",
    re.IGNORECASE,
)
FRAGRANCE_BLEND = re.compile(r"\bпарфюмер\w+\s+композици\w*|\bсмесь\s+эфирных\s+масел")
COMPONENT_PROFILES = (
    (r"\b(?:отдуш\w*|парфюмер\w*|ароматизатор\w*|эфирн\w*\s+масл\w*|parfum|fragrance)\b",
     "Отдушка", "У чувствительных людей возможна реакция на душистые вещества. Сверьте маркировку аллергенов."),
    (r"\b(?:пав|тензид\w*|сурфактант\w*|лаурилсульфат\w*|лауретсульфат\w*)\b",
     "Моющее вещество (ПАВ)", "Возможное раздражение кожи и глаз зависит от конкретного ПАВ и его концентрации."),
    (r"\b(?:фермент\w*|(?:био)?энзим\w*|протеаз\w*|амилаз\w*|липаз\w*)\b",
     "Фермент", "Помогает расщеплять загрязнения. Учитывайте предупреждения производителя."),
    (r"\b(?:бензоат\w*|сорбат\w*|консервант\w*)\b",
     "Консервант", "Подавляет рост микроорганизмов в средстве. Для оценки нужна точная концентрация."),
    (r"\b(?:лимонн\w*\s+кислот\w*|цитрат\w*)\b",
     "Регулятор кислотности", "Возможное раздражение зависит от кислотности готового средства."),
    (r"\b(?:полимер\w*|загустител\w*|соль пищев\w*|хлорид натрия)\b",
     "Регулятор вязкости", "Для оценки важны вид вещества и его доля в составе."),
    (r"\b(?:глицерин\w*|glycerin)\b",
     "Удерживает влагу", "Компонент рецептуры. Для оценки важна концентрация в готовом средстве."),
    (r"\b(?:экстракт\w*|aloe)\b",
     "Растительный экстракт", "Возможна индивидуальная чувствительность."),
    (r"^(?:(?:подготовленная|очищенная|деминерализованная)\s+)?вода$|^aqua$",
     "Основа, растворитель", "На основе воды смешивают остальные компоненты."),
)


@dataclass(frozen=True)
class Component:
    name: str
    concentration: str
    role: str
    note: str


def split_top_level(text, separators):
    start = depth = 0
    quote = None
    for index, char in enumerate(text):
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in QUOTES:
            quote = QUOTES[char]
        elif char in "([":
            depth += 1
        elif char in ")]":
            depth = max(0, depth - 1)
        elif depth == 0 and char in separators:
            if char == "," and 0 < index < len(text) - 1 and text[index - 1].isdigit() and text[index + 1].isdigit():
                continue
            yield text[start:index]
            start = index + 1
    yield text[start:]


def composition_entries(text):
    for block in split_top_level(text, ";\n"):
        parts = list(split_top_level(block, ","))
        # Запятые в перечне масел внутри одной парфюмерной композиции описывают смесь.
        if FRAGRANCE_BLEND.search(parts[0].casefold()) and sum("%" in part for part in parts) <= 1:
            yield block
        else:
            yield from parts


def analyze_components(text):
    components = {}
    repeated = {}
    for entry in composition_entries(text):
        name = " ".join(normalize("NFC", entry).split()).strip(" .")
        if not name:
            continue
        strength = ""
        match = CONCENTRATION.search(name.translate(SYMBOLS))
        if match and name[:match.start()].strip(" :"):
            strength = match[1].strip()
            name = name[:match.start()].strip(" :")
        key = name.casefold().replace("ё", "е")
        if key in components:
            repeated[key] = components[key].name
            continue
        role, note = next(
            ((role, note) for pattern, role, note in COMPONENT_PROFILES if re.search(pattern, key)),
            ("Не определено", "Для оценки нужно точное название вещества или данные производителя."),
        )
        components[key] = Component(name, strength, role, note)
    return list(components.values()), list(repeated.values())
