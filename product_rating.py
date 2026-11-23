import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


SCORE_TEXT = r"[0-9]+(?:[.,][0-9]+)?"
COUNT_TEXT = r"(?:[0-9]{1,3}(?: [0-9]{3})+|[0-9]+)"
COUNT_CAPTION = rf"(?P<count>{COUNT_TEXT})\s+(?P<kind>оцен(?:ка|ки|ок)|отзыв(?:а|ов)?)"
PRODUCT_SCORE = re.compile(rf"^Рейтинг товара\s*:\s*(?P<score>{SCORE_TEXT})\s+из\s+5$", re.IGNORECASE)
SCORE_WITH_COUNT = re.compile(rf"^(?P<score>{SCORE_TEXT})(?:\s*[•·—])?\s+{COUNT_CAPTION}$", re.IGNORECASE)
COUNT_LINE = re.compile(rf"^(?:{COUNT_CAPTION})$", re.IGNORECASE)
STAR_COUNTS_HEADING = re.compile(r"^(?:Распределение оценок|Оценки и отзывы|Отзывы и оценки)$", re.IGNORECASE)
STAR_COUNT = re.compile(rf"^([1-5])\s+зв[её]зд[аы]?(?:\s*[:—–·]?\s+({COUNT_TEXT}))?$", re.IGNORECASE)


@dataclass(frozen=True)
class CustomerRating:
    score: Decimal
    rating_count: int | None = None
    review_count: int | None = None
    star_counts: tuple[int, ...] | None = None


def rating_score(value):
    if type(value) not in (str, int, Decimal):
        return None
    try:
        score = Decimal(str(value).strip().replace(",", "."))
    except InvalidOperation:
        return None
    return score if score.is_finite() and 1 <= score <= 5 else None


def rating_count(value):
    if type(value) is int:
        return value if value >= 0 else None
    if isinstance(value, str):
        text = " ".join(value.split())
        if re.fullmatch(COUNT_TEXT, text):
            try:
                return int(text.replace(" ", ""))
            except ValueError:
                pass
    return None


def aggregate_rating(node):
    if not isinstance(node, dict) or rating_score(node.get("bestRating", 5)) != 5:
        return None
    score = rating_score(node.get("ratingValue"))
    if score is None:
        return None
    return CustomerRating(score, rating_count(node.get("ratingCount")), rating_count(node.get("reviewCount")))


def decode_customer_rating(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Неверные данные об оценках покупателей.")
    score = rating_score(value.get("score"))
    counts = (value.get("rating_count"), value.get("review_count"))
    stars = value.get("star_counts")
    if (score is None or any(count is not None and (type(count) is not int or count < 0) for count in counts)
            or stars is not None and (not isinstance(stars, (list, tuple)) or len(stars) != 5
                or any(type(count) is not int or count < 0 for count in stars) or not sum(stars))):
        raise ValueError("Неверные данные об оценках покупателей.")
    return CustomerRating(score, *counts, tuple(stars) if stars is not None else None)


def read_star_counts(lines, expected_count):
    complete = set()
    for index, line in enumerate(lines):
        if not STAR_COUNTS_HEADING.fullmatch(line):
            continue
        counts = {}
        section = lines[index + 1:index + 16]
        for row, following in zip(section, section[1:] + [""]):
            match = STAR_COUNT.fullmatch(row)
            if not match:
                continue
            stars = int(match[1])
            count = rating_count(match[2] or following)
            if stars in counts or count is None:
                break
            counts[stars] = count
            if len(counts) == 5:
                total = sum(counts.values())
                if total and (expected_count is None or expected_count == total):
                    complete.add(tuple(counts[star] for star in range(1, 6)))
                break
    return next(iter(complete)) if len(complete) == 1 else None


def browser_rating(lines):
    if not lines:
        return None

    score_match = PRODUCT_SCORE.fullmatch(lines[0]) or SCORE_WITH_COUNT.fullmatch(lines[0])
    score = rating_score(score_match["score"]) if score_match else rating_score(lines[0])
    if score is None:
        return None

    count, kind = None, ""
    if score_match and "count" in score_match.groupdict():
        count = rating_count(score_match["count"])
        kind = score_match["kind"].casefold()
    else:
        caption = COUNT_LINE.fullmatch(lines[1].strip("()")) if len(lines) > 1 else None
        if caption:
            count = rating_count(caption["count"])
            kind = caption["kind"].casefold()
        elif score_match is None:
            return None

    ratings = count if kind.startswith("оцен") else None
    reviews = count if kind.startswith("отзыв") else None
    return CustomerRating(score, ratings, reviews, read_star_counts(lines, ratings))
