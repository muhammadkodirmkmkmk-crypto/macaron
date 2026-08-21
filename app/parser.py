"""Разбор сообщений: сначала быстрый regex по тексту, затем Claude Vision по фото.

Формат, который пишут в группе:
    "Burama 9 soat 30 minutda chiqdi"
    "Pero 10 soat 30minutda chiqdi"
    "Pautinka 14 soatda chiqdi trewena yuq"
Номер сушки (1..31) на фото — большая цифра на стене, плюс красное табло контроллера.
"""
from __future__ import annotations

import base64
import datetime as dt
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
# номер ПОСЛЕ слова: «sushka 12», «№12»
DRYER_IN_TEXT_RE = re.compile(
    r"(?:sushka|sushk[ai]|сушк[аи]|№|#|nomer|nomeri|raqam)\s*[-:\s]*(\d{1,2})(?!\s*[:.]\s*\d)",
    re.I | re.U
)
# номер ПЕРЕД словом: «12 sushka», «12-sushka», «7 apparat» — так пишут чаще
DRYER_BEFORE_RE = re.compile(
    r"(?<![\d:.])(\d{1,2})\s*[-–—]?\s*(?:sushka\w*|сушк\w*|apparat\w*|аппарат\w*|kamera\w*|камер\w*)",
    re.I | re.U
)

# --- часы захода и выхода: "00:28 kirgan 10:30 chiqdi burama" ---
CLOCK_TOKEN_RE = re.compile(r"(?<![\d:.])(\d{1,2})\s*[:.\-]\s*(\d{2})(?![\d:.])")
# Слова о НАЧАЛЕ сушки. «yopildi» — буквально «закрыли»: закрыли дверь сушки,
# то есть партию заложили и процесс пошёл. В цехе пишут именно так.
IN_WORDS_RE = re.compile(
    r"(?:kirgan|kirdi|kirib|kirgizildi|kiritildi|solindi|solingan|qo['’]yildi|boshlandi|"
    r"yopildi|yopdi|yopdim|yopilgan|yopib|yopti|start\w*|"
    r"зашл\w*|заложен\w*|заложил\w*|поставил\w*|начал\w*|старт\w*|вход\w*|закрыл\w*)", re.I | re.U)
# Слова о КОНЦЕ сушки. «ochildi» — «открыли» дверь, то есть выгрузили.
OUT_WORDS_RE = re.compile(
    r"(?:chiqdi|chiqti|chiqqan|chiqarildi|olindi|tugadi|"
    r"ochildi|ochdi|ochilgan|ochib|stop\w*|стоп\w*|"
    r"вышл\w*|снял\w*|снят\w*|готов\w*|выгруз\w*|открыл\w*|конец|финиш)", re.I | re.U)

# Новый формат цеха: «Start vaqt 23:26 qochqor» / «Stop vaqt 22:46 burama»
START_STOP_RE = re.compile(r"(?:\bstart\b|\bstop\b|\bстарт\w*|\bстоп\w*)", re.I | re.U)
BARE_NUM_RE = re.compile(r"(?<![\d:.\-])(\d{1,2})(?![\d:.\-])")
UNIT_AFTER_RE = re.compile(r"\s*(?:soat|soa|min|daq|час|ч\b|мин)", re.I | re.U)


def _clock_pairs(text: str):
    """Достаёт (час, минута, позиция) для каждой отметки времени в строке."""
    out = []
    for m in CLOCK_TOKEN_RE.finditer(text):
        h, mi = int(m.group(1)), int(m.group(2))
        if h <= 23 and mi <= 59:
            out.append((h, mi, m.start()))
    return out


def parse_clock_range(text: str) -> tuple[tuple[int, int], tuple[int, int], int] | None:
    """'00:28 kirgan 10:30 chiqdi' -> ((0,28), (10,30), 602 минуты).

    Слова «зашла» и «вышла» могут стоять и до времени, и после, поэтому каждую
    отметку привязываем к ближайшему ключевому слову.
    """
    if not text:
        return None
    times = _clock_pairs(text)
    if len(times) < 2:
        return None

    ins = [m.start() for m in IN_WORDS_RE.finditer(text)]
    outs = [m.start() for m in OUT_WORDS_RE.finditer(text)]

    def nearest(anchors, used=()):
        if not anchors:
            return None
        best, best_d = None, 10 ** 9
        for h, mi, pos in times:
            if (h, mi, pos) in used:
                continue
            d = min(abs(pos - a) for a in anchors)
            if d < best_d:
                best, best_d = (h, mi, pos), d
        return best

    start = nearest(ins)
    end = nearest(outs, used=(start,) if start else ())

    # запасной вариант: два времени и слово «вышла» — значит первое вход, второе выход
    if (start is None or end is None) and len(times) == 2 and outs:
        start, end = times[0], times[1]
    if start is None or end is None or start[:2] == end[:2]:
        return None

    a = start[0] * 60 + start[1]
    b = end[0] * 60 + end[1]
    dur = (b - a) % (24 * 60)          # переход через полночь
    if not (30 <= dur <= 24 * 60 - 30):  # меньше получаса или почти сутки — не верим
        return None
    return (start[0], start[1]), (end[0], end[1]), dur


def parse_load_only(text: str) -> tuple[int, int] | None:
    """«00:28 kirdi burama» — партию только заложили, выхода ещё нет.

    Возвращает час:мин загрузки. Если в сообщении есть слово о выходе
    («chiqdi»), это готовая партия, а не загрузка — вернём None.
    Если слово о загрузке есть, а времени нет, вернём (-1, -1):
    значит, засекать надо от момента сообщения.
    """
    if not text:
        return None
    if OUT_WORDS_RE.search(text):
        return None
    if not IN_WORDS_RE.search(text):
        return None
    times = _clock_pairs(text)
    if len(times) > 1:
        return None
    if not times:
        return (-1, -1)
    return times[0][0], times[0][1]


def is_load_message(text: str) -> bool:
    return parse_load_only(text or "") is not None


def parse_stop_only(text: str) -> tuple[int, int] | None:
    """«Stop vaqt 22:46 burama» — партию выгрузили, а заход был отдельным сообщением.

    Возвращает час:мин выгрузки, (-1,-1) если времени нет (считаем от момента
    сообщения) и None, если это не «голая» выгрузка: есть слово о заходе,
    написана длительность («9 soat 30 minutda chiqdi») или отметок времени больше одной.
    """
    if not text:
        return None
    if not OUT_WORDS_RE.search(text):
        return None
    if IN_WORDS_RE.search(text):
        return None
    if HOUR_RE.search(text) or MIN_RE.search(text):
        return None            # длительность написана прямо — это самодостаточный отчёт
    times = _clock_pairs(text)
    if len(times) > 1:
        return None
    if not times:
        return (-1, -1)
    return times[0][0], times[0][1]


def dryer_hint(text: str) -> int | None:
    """Номер сушки из текста: «12-sushka», «№12», а в формате Start/Stop —
    любое отдельно стоящее число (часы туда не попадают, единицы времени отсекаем)."""
    if not text:
        return None
    m = DRYER_BEFORE_RE.search(text)          # «12 sushka» — номер стоит первым
    if m:
        n = int(m.group(1))
        if 1 <= n <= config.DRYER_COUNT:
            return n
    m = DRYER_IN_TEXT_RE.search(text)          # «sushka 12», «№12»
    if m:
        n = int(m.group(1))
        if 1 <= n <= config.DRYER_COUNT:
            return n
    if not START_STOP_RE.search(text):
        return None
    for m in BARE_NUM_RE.finditer(text):
        if UNIT_AFTER_RE.match(text[m.end():m.end() + 8]):
            continue
        n = int(m.group(1))
        if 1 <= n <= config.DRYER_COUNT:
            return n
    return None


def resolve_single(sent_local: dt.datetime, hm) -> dt.datetime:
    """Один час:мин -> дата. Отчёт пишут после события, поэтому в будущее не уходим."""
    if not hm or hm == (-1, -1):
        return sent_local
    t = sent_local.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
    if t > sent_local + dt.timedelta(hours=2):
        t -= dt.timedelta(days=1)
    return t


def resolve_times(sent_local: dt.datetime, start_hm, end_hm, duration_min: int):
    """Часы с табло -> реальные даты. Отчёт присылают вскоре после выгрузки,
    поэтому выход ищем не в будущем относительно момента сообщения."""
    finished = sent_local.replace(hour=end_hm[0], minute=end_hm[1], second=0, microsecond=0)
    if finished > sent_local + dt.timedelta(hours=2):
        finished -= dt.timedelta(days=1)
    return finished - dt.timedelta(minutes=duration_min), finished


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
    started_hm: tuple[int, int] | None = None   # час:мин захода, если написали
    finished_hm: tuple[int, int] | None = None  # час:мин выхода
    quality: str = "unknown"           # ok | defect | unknown
    note: str | None = None
    source: str = "regex"              # regex | vision | mixed | manual
    confidence: float = 0.0
    vision_error: str | None = None
    missing: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


# Служебные слова формата — за название продукта их принимать нельзя
NOT_PRODUCT = {
    "start", "stop", "старт", "стоп", "vaqt", "vaqti", "vakt", "время",
    "soat", "soatda", "soatga", "minut", "minutda", "daqiqa", "daqiqada",
    "sushka", "sushkasi", "сушка", "сушки", "камера", "kamera", "apparat",
    "kirdi", "kirgan", "kirib", "chiqdi", "chiqti", "chiqqan", "tugadi",
    "yopildi", "yopilgan", "ochildi", "ochilgan", "boshlandi", "solindi",
    "trewena", "trewna", "tresna", "treshina", "yoriq", "brak", "брак",
    "bordi", "yaxshi", "zo'r", "salom", "rahmat", "bo'ldi", "boldi",
    # слова про моторы: их нельзя принимать за название продукта
    "mator", "matori", "motor", "motori", "matorlar", "матор", "мотор",
    "матори", "мотори", "chap", "chapdagi", "tarafdagi", "tomon", "tomondagi",
    "чапдаги", "тарафдаги", "томон", "o'ng", "ong", "ung", "ўнг", "унг",
    "buzildi", "buzuq", "ishlamayapti", "ishlamadi", "sindi", "kuydi",
    "бузилди", "ишламаяпти", "синди", "куйди", "шовкин", "шовқин",
}
WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁёЎўҚқҒғҲҳ'’`]{4,}", re.U)


def _levenshtein(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _match_product(text: str) -> str | None:
    low = text.lower()
    # 1) Точное вхождение известного слова (по границам)
    best = None
    best_pos = 10**9
    for alias, canon in _ALIASES.items():
        m = re.search(r"(?<![\wа-яё])" + re.escape(alias) + r"(?![\wа-яё])", low, re.U)
        if m and m.start() < best_pos:
            best, best_pos = canon, m.start()
    if best:
        return best

    # 2) Нечёткое — по ЛЮБОМУ слову сообщения, а не только по первому:
    # в «Stop vaqt 10:07 quchqar» продукт стоит последним, и написан с опиской.
    best, best_d = None, 99
    for m in WORD_RE.finditer(low):
        w = m.group(0)
        if w in NOT_PRODUCT:
            continue
        for alias, canon in _ALIASES.items():
            if len(alias) < 4 or abs(len(w) - len(alias)) > 2:
                continue
            d = _levenshtein(w, alias)
            if d <= 2 and d < best_d:
                best, best_d = canon, d
                if d == 1:
                    break
    return best


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
    p.quality = _quality_from_text(clean)

    # сначала пробуем «зашла в HH:MM, вышла в HH:MM» — это точнее длительности
    rng = parse_clock_range(clean)
    if rng:
        p.started_hm, p.finished_hm, p.duration_minutes = rng
    else:
        p.duration_minutes = _duration_from_text(clean)

    p.dryer_number = dryer_hint(clean)

    # Заметка = остаток текста после вырезания понятых кусков
    note = clean
    for rx in (HOUR_RE, MIN_RE, DONE_RE, DRYER_BEFORE_RE, DRYER_IN_TEXT_RE):
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
    if not text:
        # Голое фото ничего не говорит о времени работы: на табло только
        # оставшееся время. Номер сушки с него читаем, отчётом не считаем.
        return False
    t = text or ""
    if DONE_RE.search(t):
        return True
    if START_STOP_RE.search(t) and _clock_pairs(t):
        return True
    if HOUR_RE.search(t) and (_match_product(t) or has_photo):
        return True
    return False


# ---------------------------------------------------------------- vision

VISION_SYSTEM = """Ты — оператор-аналитик на макаронной фабрике в Узбекистане.
Тебе дают фото сушильной камеры и подпись к нему на узбекской латинице.

На фото обычно видно:
1. Крупную золотистую/накладную цифру на стене над щитком — это НОМЕР СУШКИ
   (от 1 до {max_dryer}). Шрифт декоративный, объёмный, с тенью.
   ВАЖНО — здесь ошибаются чаще всего:
   • номер часто ДВУЗНАЧНЫЙ (10, 16, 23, 26, 31) — прочитай ВСЕ цифры таблички;
   • две цифры рядом на стене — это ОДНО число: «3» и «1» = 31, а не 3 и не 1;
   • не путай его с цифрами на красном табло и с наклейками на щитке:
     номер сушки крупный, на стене, НЕ светится;
   • если табличка обрезана кадром, закрыта рукой или не читается — ставь null
     и низкую уверенность. Пустой ответ лучше выдуманного номера.
2. Красное светодиодное табло контроллера FUBA МПР-49/МПР-51 с тремя строками:
   строка 1 = ОСТАВШЕЕСЯ ВРЕМЯ программы, вид «07.22» или «0722» (часы.минуты);
   строка 2 = ТЕМПЕРАТУРА в °C, всегда с десятичной точкой, например «82.4», «59.3»;
   строка 3 = ВЛАЖНОСТЬ в %, целое число, например «08», «33», «18».
   Читай цифры буквально, семисегментным шрифтом; не додумывай пропущенные разряды.

Подпись обычно вида "<продукт> <N> soat <M> minutda chiqdi" —
"chiqdi" = вышло/готово, "soat" = час, "minut/daqiqa" = минута.
"trewena/tresna yuq" = трещин нет (качество ок); "trewena bor" = есть трещины (брак).

Верни СТРОГО один JSON-объект, без пояснений и без markdown:
{{
  "dryer_number": <int 1..{max_dryer} или null>,
  "dryer_number_text": "<цифры таблички ровно как видишь, например «31», или null>",
  "display_line1": "<строка 1 табло как видишь, или null>",
  "display_line2": "<строка 2 или null>",
  "display_line3": "<строка 3 или null>",
  "product": "<название продукта латиницей с большой буквы, или null>",
  "quality": "ok" | "defect" | "unknown",
  "note": "<короткая заметка оператора или null>",
  "dryer_number_confidence": <0.0..1.0>
}}
Если цифру сушки не видно — ставь null, НЕ ВЫДУМЫВАЙ.
Сколько сушка отработала — по фото НЕ определяй: строка 1 это остаток программы,
а не отработанное время. Про длительность в ответе ничего не пиши."""


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


def _digits(raw) -> str:
    return re.sub(r"\D", "", str(raw or ""))


def _as_timer(raw) -> str | None:
    """Строка 1 табло — ВРЕМЯ вида 07.22 / 0722 -> '07:22'."""
    d = _digits(raw)
    if len(d) == 4 and int(d[:2]) <= 47 and int(d[2:]) <= 59:
        return f"{d[:2]}:{d[2:]}"
    if len(d) == 3 and int(d[1:]) <= 59:
        return f"0{d[0]}:{d[1:]}"
    return None


def _as_temp(raw) -> float | None:
    """Строка 2 — ТЕМПЕРАТУРА, всегда XX.X. Модели теряют десятичную точку
    ('8.19', '564' вместо 81.9 и 56.4), поэтому считаем по цифрам."""
    d = _digits(raw)
    if len(d) in (3, 4):
        v = int(d) / 10
        if 20 <= v <= 130:
            return round(v, 1)
    return None


def _as_hum(raw) -> int | None:
    """Строка 3 — ВЛАЖНОСТЬ, целое 0..100."""
    d = _digits(raw)
    if 1 <= len(d) <= 3 and "." not in str(raw) and "," not in str(raw):
        v = int(d)
        if 0 <= v <= 100:
            return v
    return None


def map_display(l1, l2, l3) -> tuple[str | None, float | None, int | None]:
    """Строки табло МПР-49 -> (таймер, температура, влажность).

    Порядок строк на контроллере фиксированный (ВРЕМЯ / ТЕМПЕР / ВЛАЖН),
    поэтому доверяем позиции, а эвристику включаем только для пустых слотов.
    """
    timer = _as_timer(l1)
    temp = _as_temp(l2)
    hum = _as_hum(l3)

    used = {0: timer is not None, 1: temp is not None, 2: hum is not None}
    lines = [l1, l2, l3]

    # если строка не распозналась — ищем подходящее значение в оставшихся
    if temp is None:
        for i, raw in enumerate(lines):
            if not used.get(i) and raw and _as_temp(raw) is not None:
                temp = _as_temp(raw)
                used[i] = True
                break
    if hum is None:
        for i, raw in enumerate(lines):
            if not used.get(i) and raw and _as_hum(raw) is not None:
                hum = _as_hum(raw)
                used[i] = True
                break
    if timer is None:
        for i, raw in enumerate(lines):
            if not used.get(i) and raw and _as_timer(raw) is not None:
                timer = _as_timer(raw)
                break
    return timer, temp, hum


async def parse_with_vision(image_bytes: bytes, caption: str, media_type: str = "image/jpeg") -> Parsed:
    """Полный разбор: текст + фото через Claude."""
    base = parse_text(caption or "")
    if not config.VISION_ENABLED or not image_bytes:
        return base

    try:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
        kwargs = dict(
            model=config.ANTHROPIC_MODEL,
            max_tokens=600,
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
        from . import claude
        resp = await claude.create(client, **kwargs)
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
    if not isinstance(dn, int):                     # «31» строкой тоже принимаем
        d = _digits(dn)
        dn = int(d) if d and len(d) <= 2 else None
    # номер, списанный с таблички, важнее — модель иногда теряет первую цифру
    seen = _digits(data.get("dryer_number_text"))
    if seen and len(seen) <= 2 and 1 <= int(seen) <= config.DRYER_COUNT:
        if dn is None or (int(seen) != dn and len(seen) == 2):
            dn = int(seen)
    if out.dryer_number is None and isinstance(dn, int) and 1 <= dn <= config.DRYER_COUNT and dn_conf >= 0.5:
        out.dryer_number = dn

    if not out.product and data.get("product"):
        out.product = _match_product(str(data["product"])) or str(data["product"]).strip().title()

    # Время работы берём ТОЛЬКО из сообщений (Start/Stop или «9 soat 30 minutda»).
    # На табло верхняя строка — ОСТАВШЕЕСЯ время программы, а не отработанное:
    # если считать её длительностью, партия получается выдуманной.

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
