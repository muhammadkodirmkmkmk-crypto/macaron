"""Разбор сообщений: сначала быстрый regex по тексту, затем Claude Vision по фото.

Формат, который пишут в группе:
    "Burama 9 soat 30 minutda chiqdi"
    "Pero 10 soat 30minutda chiqdi"
    "Pautinka 14 soatda chiqdi trewena yuq"
Номер сушки (1..31) на фото — большая цифра на стене, плюс красное табло контроллера.
"""
from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass, field, asdict

from . import config

log = logging.getLogger("parser")

# ---------------------------------------------------------------- regex

HOUR_RE = re.compile(
    r"(\d{1,2})\s*(?:soat(?:da|ga|lik)?|soa|соат|s\.|час(?:ов|а)?|ч\b)", re.I | re.U
)
MIN_RE = re.compile(
    r"(\d{1,3})\s*(?:minut(?:da|ga)?|min\b|мин(?:ут[аы]?)?|daqiqa|daq\b)", re.I | re.U
)
CLOCK_RE = re.compile(r"\b(\d{1,2})\s*[:.\-]\s*(\d{2})\b")
DRYER_IN_TEXT_RE = re.compile(
    r"(?:sushka|sushk[ai]|сушк[аи]|№|#|nomer|nomeri|raqam)\s*[-:\s]*(\d{1,2})", re.I | re.U
)

CRACK_WORDS = r"(?:trewena|trewna|tresna|treshina|тре[сшщ]ина|трещин\w*|yoriq|yorilgan|yorildi|siniq|singan|brak|брак|деформац\w*)"
NEGATION = r"(?:yuq|yo'q|yoq|yo‘q|нет|net|net\b|emas)"
CRACK_NEG_RE = re.compile(CRACK_WORDS + r"[^\wа-яё]{0,12}" + NEGATION, re.I | re.U)
CRACK_POS_RE = re.compile(CRACK_WORDS + r"(?:[^\wа-яё]{0,12}(?:bor|ko'p|kop|много|есть|bir oz))?", re.I | re.U)
GOOD_WORDS_RE = re.compile(r"\b(?:yaxshi|zo'r|zor|sifatli|нормально|хорошо|ok)\b", re.I | re.U)

DONE_RE = re.compile(r"\b(?:chiqdi|chiqti|tugadi|bo'ldi|boldi|готов\w*|вышл\w*|снял\w*)\b", re.I | re.U)

_ALIASES = {k.lower(): v for k, v in config.PRODUCT_ALIASES.items()}
for _p in config.KNOWN_PRODUCTS:
    _ALIASES.setdefault(_p.lower(), _p)


@dataclass
class Parsed:
    dryer_number: int | None = None
    product: str | None = None
    duration_minutes: int | None = None
    temperature: float | None = None
    humidity: float | None = None
    timer_raw: str | None = None
    display_raw: str | None = None
    quality: str = "unknown"           # ok | defect | unknown
    note: str | None = None
    source: str = "regex"              # regex | vision | mixed | manual
    confidence: float = 0.0
    vision_error: str | None = None
    missing: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _match_product(text: str) -> str | None:
    low = text.lower()
    # Точное вхождение известного слова (по границам)
    best = None
    best_pos = 10**9
    for alias, canon in _ALIASES.items():
        m = re.search(r"(?<![\wа-яё])" + re.escape(alias) + r"(?![\wа-яё])", low, re.U)
        if m and m.start() < best_pos:
            best, best_pos = canon, m.start()
    if best:
        return best
    # Нечёткое: первое слово из букв длиной >=4, если похоже на известное
    m = re.match(r"\s*([A-Za-zА-Яа-яЎўҚқҒғҲҳ'’`]{4,})", text)
    if m:
        w = m.group(1).lower()
        for alias, canon in _ALIASES.items():
            if _fuzzy_close(w, alias):
                return canon
    return None


def _fuzzy_close(a: str, b: str, max_dist: int = 2) -> bool:
    """Простая проверка расстояния Левенштейна для опечаток."""
    if abs(len(a) - len(b)) > max_dist:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1] <= max_dist


def _duration_from_text(text: str) -> int | None:
    h = HOUR_RE.search(text)
    m = MIN_RE.search(text)
    if h or m:
        hours = int(h.group(1)) if h else 0
        mins = int(m.group(1)) if m else 0
        if mins > 59 and h:
            return None
        total = hours * 60 + mins
        return total if 0 < total <= 60 * 48 else None
    # запасной вариант "9:30"
    c = CLOCK_RE.search(text)
    if c:
        hours, mins = int(c.group(1)), int(c.group(2))
        if hours <= 30 and mins < 60:
            total = hours * 60 + mins
            return total or None
    return None


def _quality_from_text(text: str) -> str:
    if CRACK_NEG_RE.search(text):
        return "ok"
    if CRACK_POS_RE.search(text):
        return "defect"
    if GOOD_WORDS_RE.search(text):
        return "ok"
    return "unknown"


def parse_text(text: str) -> Parsed:
    """Быстрый разбор без обращения к модели."""
    p = Parsed(source="regex")
    if not text:
        return p
    clean = text.strip()

    p.product = _match_product(clean)
    p.duration_minutes = _duration_from_text(clean)
    p.quality = _quality_from_text(clean)

    d = DRYER_IN_TEXT_RE.search(clean)
    if d:
        n = int(d.group(1))
        if 1 <= n <= config.DRYER_COUNT:
            p.dryer_number = n

    # Заметка = остаток текста после вырезания понятых кусков
    note = clean
    for rx in (HOUR_RE, MIN_RE, DONE_RE, DRYER_IN_TEXT_RE):
        note = rx.sub(" ", note)
    if p.product:
        note = re.sub(re.escape(p.product), " ", note, flags=re.I)
        for alias, canon in _ALIASES.items():
            if canon == p.product:
                note = re.sub(r"(?<![\wа-яё])" + re.escape(alias) + r"(?![\wа-яё])", " ", note, flags=re.I | re.U)
    note = re.sub(r"\s+", " ", note).strip(" .,-—")
    p.note = note or None

    score = 0.0
    score += 0.45 if p.duration_minutes else 0.0
    score += 0.30 if p.product else 0.0
    score += 0.25 if p.dryer_number else 0.0
    p.confidence = score
    p.missing = [k for k, v in (
        ("dryer_number", p.dryer_number),
        ("product", p.product),
        ("duration_minutes", p.duration_minutes),
    ) if not v]
    return p


def is_report_message(text: str, has_photo: bool) -> bool:
    """Похоже ли сообщение на отчёт о выгрузке партии."""
    if not text and has_photo:
        return True  # фото без подписи — попробуем распознать
    t = text or ""
    if DONE_RE.search(t):
        return True
    if HOUR_RE.search(t) and (_match_product(t) or has_photo):
        return True
    return False


# ---------------------------------------------------------------- vision

VISION_SYSTEM = """Ты — оператор-аналитик на макаронной фабрике в Узбекистане.
Тебе дают фото сушильной камеры и подпись к нему на узбекской латинице.

На фото обычно видно:
1. Крупную золотистую/накладную цифру на стене над щитком — это НОМЕР СУШКИ
   (от 1 до {max_dryer}). Цифра одна или две, часто с тенью, шрифт декоративный.
2. Красное светодиодное табло контроллера FUBA МПР-49/МПР-51 с тремя строками:
   строка 1 = ВРЕМЯ, вид «07.22» или «0722» (часы.минуты);
   строка 2 = ТЕМПЕРАТУРА в °C, всегда с десятичной точкой, например «82.4», «59.3»;
   строка 3 = ВЛАЖНОСТЬ в %, целое число, например «08», «33», «18».
   Читай цифры буквально, семисегментным шрифтом; не додумывай пропущенные разряды.

Подпись обычно вида "<продукт> <N> soat <M> minutda chiqdi" —
"chiqdi" = вышло/готово, "soat" = час, "minut/daqiqa" = минута.
"trewena/tresna yuq" = трещин нет (качество ок); "trewena bor" = есть трещины (брак).

Верни СТРОГО один JSON-объект, без пояснений и без markdown:
{{
  "dryer_number": <int 1..{max_dryer} или null>,
  "display_line1": "<строка 1 табло как видишь, или null>",
  "display_line2": "<строка 2 или null>",
  "display_line3": "<строка 3 или null>",
  "product": "<название продукта латиницей с большой буквы, или null>",
  "hours": <int или null>,
  "minutes": <int или null>,
  "quality": "ok" | "defect" | "unknown",
  "note": "<короткая заметка оператора или null>",
  "dryer_number_confidence": <0.0..1.0>
}}
Если цифру сушки не видно — ставь null, НЕ ВЫДУМЫВАЙ."""


def _to_float(s: str | None) -> float | None:
    if not s:
        return None
    s = str(s).strip().replace(",", ".")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def map_display(l1: str | None, l2: str | None, l3: str | None) -> tuple[str | None, float | None, float | None]:
    """Строки табло -> (таймер, температура, влажность). Эвристика: число с точкой = температура."""
    lines = [x for x in (l1, l2, l3) if x]
    timer = None
    nums: list[tuple[str, float]] = []
    for raw in lines:
        s = str(raw).strip()
        if re.fullmatch(r"\d{1,2}\s*[:.]\s*\d{2}", s) and ":" in s:
            timer = s.replace(" ", "")
            continue
        v = _to_float(s)
        if v is not None:
            nums.append((s, v))
    if timer is None and nums:
        # первая строка часто таймер вида 0722
        s, v = nums[0]
        if re.fullmatch(r"\d{4}", s.replace(":", "")):
            timer = f"{s[:2]}:{s[2:]}"
            nums = nums[1:]
    temp = hum = None
    for s, v in nums:
        if ("." in s or "," in s) and temp is None and 20 <= v <= 120:
            temp = v
        elif hum is None and 0 <= v <= 100:
            hum = v
    if temp is None and hum is not None and hum > 40 and len(nums) == 1:
        temp, hum = hum, None
    return timer, temp, hum


async def parse_with_vision(image_bytes: bytes, caption: str, media_type: str = "image/jpeg") -> Parsed:
    """Полный разбор: текст + фото через Claude."""
    base = parse_text(caption or "")
    if not config.VISION_ENABLED or not image_bytes:
        return base

    try:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
        resp = await client.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=600,
            temperature=0,
            system=VISION_SYSTEM.format(max_dryer=config.DRYER_COUNT),
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": base64.standard_b64encode(image_bytes).decode(),
                    }},
                    {"type": "text", "text": f"Подпись к фото: {caption or '(нет подписи)'}"},
                ],
            }],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        m = re.search(r"\{.*\}", text, re.S)
        data = json.loads(m.group(0) if m else text)
    except Exception as exc:  # noqa: BLE001
        log.warning("vision failed: %s", exc)
        base.vision_error = str(exc)[:300]
        return base

    return _merge(base, data)


def _merge(base: Parsed, data: dict) -> Parsed:
    out = Parsed(**{k: v for k, v in base.as_dict().items() if k not in {"source", "confidence", "missing"}})
    out.source = "mixed"

    dn = data.get("dryer_number")
    dn_conf = float(data.get("dryer_number_confidence") or 0)
    if out.dryer_number is None and isinstance(dn, int) and 1 <= dn <= config.DRYER_COUNT and dn_conf >= 0.5:
        out.dryer_number = dn

    if not out.product and data.get("product"):
        out.product = _match_product(str(data["product"])) or str(data["product"]).strip().title()

    if out.duration_minutes is None:
        h, mn = data.get("hours"), data.get("minutes")
        if isinstance(h, int) or isinstance(mn, int):
            total = (h or 0) * 60 + (mn or 0)
            if 0 < total <= 60 * 48:
                out.duration_minutes = total

    l1, l2, l3 = data.get("display_line1"), data.get("display_line2"), data.get("display_line3")
    timer, temp, hum = map_display(l1, l2, l3)
    out.timer_raw = out.timer_raw or timer
    out.temperature = out.temperature if out.temperature is not None else temp
    out.humidity = out.humidity if out.humidity is not None else hum
    raw = "|".join(str(x) if x else "" for x in (l1, l2, l3))
    out.display_raw = raw if raw.strip("|") else None

    if out.quality == "unknown" and data.get("quality") in {"ok", "defect"}:
        out.quality = data["quality"]
    if not out.note and data.get("note"):
        out.note = str(data["note"])[:500]

    score = 0.0
    score += 0.40 if out.duration_minutes else 0.0
    score += 0.25 if out.product else 0.0
    score += 0.25 if out.dryer_number else 0.0
    score += 0.10 if (out.temperature is not None or out.humidity is not None) else 0.0
    out.confidence = round(score, 2)
    out.missing = [k for k, v in (
        ("dryer_number", out.dryer_number),
        ("product", out.product),
        ("duration_minutes", out.duration_minutes),
    ) if not v]
    return out
