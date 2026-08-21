"""Konfiguratsiya / Конфигурация — всё через переменные окружения."""
import os
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _csv(name: str) -> list[str]:
    return [x.strip() for x in (os.getenv(name, "") or "").split(",") if x.strip()]


# ---------- Telegram ----------
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()

# Разрешённые группы. Пусто = слушать любую группу, куда добавили бота.
ALLOWED_CHAT_IDS = {int(x) for x in _csv("ALLOWED_CHAT_IDS")}

# Кто может пользоваться админ-командами (/report, /reset). Пусто = все админы группы.
ADMIN_USER_IDS = {int(x) for x in _csv("ADMIN_USER_IDS")}

# Принимать отчёты в личке с ботом (не только в группе)
PRIVATE_ENABLED = os.getenv("PRIVATE_ENABLED", "1") != "0"

# Кто может писать боту в личку. Пусто = любой пользователь.
ALLOWED_USER_IDS = {int(x) for x in _csv("ALLOWED_USER_IDS")}

# ---------- Anthropic (распознавание фото + разбор текста) ----------
ANTHROPIC_API_KEY = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5")
# Фото читает модель посильнее: номер сушки на стене бывает двузначным и с тенью,
# на этом младшая модель ошибалась. Вернуть как было: VISION_MODEL=claude-haiku-4-5
VISION_MODEL = os.getenv("VISION_MODEL", "claude-sonnet-4-5")
VISION_ENABLED = bool(ANTHROPIC_API_KEY) and os.getenv("VISION_ENABLED", "1") != "0"

# Ассистент в личке: свободные вопросы по цеху. Модель посильнее — тут язык и логика,
# а не чтение семисегментных цифр.
ASSISTANT_MODEL = os.getenv("ASSISTANT_MODEL", "claude-haiku-4-5")
# если основная модель перегружена — уходим на эту
ASSISTANT_FALLBACK_MODEL = os.getenv("ASSISTANT_FALLBACK_MODEL", "claude-haiku-4-5")
ASSISTANT_ENABLED = bool(ANTHROPIC_API_KEY) and os.getenv("ASSISTANT_ENABLED", "1") != "0"

# ---------- Производство ----------
DRYER_COUNT = _int("DRYER_COUNT", 31)


def _ranges(name: str, default: str) -> list[tuple[int, int, int]]:
    """'1-12,13-22,23-31' -> [(1,1,12), (2,13,22), (3,23,31)]."""
    out: list[tuple[int, int, int]] = []
    for i, part in enumerate((os.getenv(name, "") or default).split(","), 1):
        part = part.strip()
        if not part:
            continue
        a, _, b = part.partition("-")
        try:
            lo, hi = int(a), int(b or a)
        except ValueError:
            continue
        out.append((i, min(lo, hi), max(lo, hi)))
    return out


# Котлы (qozon): сушки сгруппированы по котлам, к которым подключены.
BOILER_RANGES = _ranges("BOILERS", "1-12,13-22,23-31")


def boiler_of(n: int | None) -> int | None:
    if not n:
        return None
    for i, lo, hi in BOILER_RANGES:
        if lo <= n <= hi:
            return i
    return None

# Известные виды макарон. Парсер сначала ищет их, потом отдаёт текст модели.
KNOWN_PRODUCTS = _csv("PRODUCTS") or [
    # то, что фабрика делает на самом деле — как в системе продаж
    "Quchqor", "Pero", "Kalta Pero", "Speral", "Burama", "Trupka", "Zirak",
    "Rochki", "Rakushka", "Gladkiy", "Manpar", "Vidkiy", "Gildirak",
    "Lapsha", "Pautinka", "Vermishel", "Spagetti", "Chiqindi",
    # встречались в переписке
    "Zvezda", "Bantik", "Nay", "Gulcha", "Yulduzcha", "Qalampir",
]

# Синонимы и опечатки -> канонический продукт
PRODUCT_ALIASES = {
    # как это пишут в группе: латиница, кириллица, описки
    "quchqor": "Quchqor", "qochqor": "Quchqor", "kochkor": "Quchqor",
    "qo'chqor": "Quchqor", "qochkor": "Quchqor", "кочкор": "Quchqor",
    "качкор": "Quchqor", "quchqar": "Quchqor", "qo'chqar": "Quchqor",
    "кучкар": "Quchqor", "кучкор": "Quchqor", "кўчқор": "Quchqor",
    "kalta pero": "Kalta Pero", "kaltapero": "Kalta Pero",
    "калта перо": "Kalta Pero", "калтаперо": "Kalta Pero",
    "kalta": "Kalta Pero",
    "speral": "Speral", "spiral": "Speral", "спираль": "Speral",
    "спирал": "Speral", "sperale": "Speral", "sprial": "Speral",
    "trupka": "Trupka", "trubka": "Trupka", "трупка": "Trupka",
    "трубка": "Trupka", "trupca": "Trupka",
    "rochki": "Rochki", "рочки": "Rochki", "rojki": "Rochki",
    "rojok": "Rochki", "рожки": "Rochki", "рожок": "Rochki",
    "gladkiy": "Gladkiy", "gladki": "Gladkiy", "гладкий": "Gladkiy",
    "гладки": "Gladkiy", "glatkiy": "Gladkiy",
    "manpar": "Manpar", "манпар": "Manpar", "manpr": "Manpar",
    "vidkiy": "Vidkiy", "vitkiy": "Vidkiy", "видкий": "Vidkiy",
    "витким": "Vidkiy", "видки": "Vidkiy",
    "gildirak": "Gildirak", "гилдирак": "Gildirak", "ғилдирак": "Gildirak",
    "gildrak": "Gildirak", "gildarak": "Gildirak", "гильдирак": "Gildirak",
    "zrak": "Zirak", "зрак": "Zirak", "zirak": "Zirak", "зирак": "Zirak",
    "spagetti": "Spagetti", "спагетти": "Spagetti", "spagetdi": "Spagetti",
    "спагети": "Spagetti", "spageti": "Spagetti",
    "chiqindi": "Chiqindi", "чикинди": "Chiqindi", "чиқинди": "Chiqindi",
    "chikindi": "Chiqindi", "otxod": "Chiqindi", "отход": "Chiqindi",
    "burama": "Burama", "борама": "Burama", "burma": "Burama",
    "zirak": "Zirak", "зирак": "Zirak", "zirek": "Zirak", "ziyrak": "Zirak",
    "qochqor": "Qochqor", "kochkor": "Qochqor", "qo'chqor": "Qochqor",
    "qochkor": "Qochqor", "кочкор": "Qochqor", "качкор": "Qochqor",
    "quchqar": "Qochqor", "quchqor": "Qochqor", "qo'chqar": "Qochqor",
    "кучкар": "Qochqor", "кучкор": "Qochqor",
    "pero": "Pero", "перо": "Pero", "pyero": "Pero",
    "pautinka": "Pautinka", "паутинка": "Pautinka", "poutinka": "Pautinka",
    "pautunka": "Pautinka", "pavtinka": "Pautinka",
    "spiral": "Spiral", "спираль": "Spiral", "spral": "Spiral",
    "rojok": "Rojok", "рожок": "Rojok", "rojki": "Rojok",
    "rakushka": "Rakushka", "ракушка": "Rakushka",
    "zvezda": "Zvezda", "звезда": "Zvezda", "yulduzcha": "Yulduzcha",
    "vermishel": "Vermishel", "вермишель": "Vermishel", "vermisel": "Vermishel",
    "lapsha": "Lapsha", "лапша": "Lapsha",
    "bantik": "Bantik", "бантик": "Bantik",
    "nay": "Nay", "най": "Nay",
    "trubka": "Trubka", "трубка": "Trubka",
}

# Нормальные диапазоны времени сушки (минуты) для подсветки отклонений.
NORM_MIN_MINUTES = _int("NORM_MIN_MINUTES", 6 * 60)
NORM_MAX_MINUTES = _int("NORM_MAX_MINUTES", 16 * 60)

# Норма одного цикла: от неё считаем «сделано столько-то процентов», пока сушка
# в работе. Если по продукту уже накопилась статистика, берём её среднее.
NORM_CYCLE_MINUTES = _int("NORM_CYCLE_MINUTES", 10 * 60)

# Напоминание в группу: заложили партию и через столько часов молчат
LOAD_REMINDER_HOURS = _int("LOAD_REMINDER_HOURS", 10)
LOAD_REMINDER_ENABLED = os.getenv("LOAD_REMINDER_ENABLED", "1") != "0"

# Через сколько часов без сообщения считаем сушку «молчащей»
STALE_HOURS = _int("STALE_HOURS", 24)

# Одинаковый отчёт (та же сушка, продукт, время) в пределах этого окна — дубль.
# Защита от двойного учёта, когда оператор пишет и в группу, и боту в личку.
DUP_WINDOW_MINUTES = _int("DUP_WINDOW_MINUTES", 20)

# ---------- База данных ----------
_raw_db = os.getenv("DATABASE_URL", "").strip()
if _raw_db.startswith("postgres://"):
    _raw_db = _raw_db.replace("postgres://", "postgresql+asyncpg://", 1)
elif _raw_db.startswith("postgresql://"):
    _raw_db = _raw_db.replace("postgresql://", "postgresql+asyncpg://", 1)
DATABASE_URL = _raw_db or f"sqlite+aiosqlite:///{DATA_DIR / 'makaron.db'}"

# ---------- Веб ----------
PORT = _int("PORT", 8080)
DASHBOARD_PASSWORD = (os.getenv("DASHBOARD_PASSWORD") or "").strip()
PUBLIC_URL = (os.getenv("PUBLIC_URL") or "").strip()

# ---------- Время / отчёты ----------
TIMEZONE = os.getenv("TZ", "Asia/Tashkent")
TZ = ZoneInfo(TIMEZONE)
DAILY_REPORT_HOUR = _int("DAILY_REPORT_HOUR", 8)  # локальное время; -1 = выключить
