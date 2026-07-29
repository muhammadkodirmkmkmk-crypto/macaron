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

# ---------- Anthropic (распознавание фото + разбор текста) ----------
ANTHROPIC_API_KEY = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")
VISION_ENABLED = bool(ANTHROPIC_API_KEY) and os.getenv("VISION_ENABLED", "1") != "0"

# ---------- Производство ----------
DRYER_COUNT = _int("DRYER_COUNT", 31)

# Известные виды макарон. Парсер сначала ищет их, потом отдаёт текст модели.
KNOWN_PRODUCTS = _csv("PRODUCTS") or [
    "Burama", "Pero", "Pautinka", "Spiral", "Rojok", "Rakushka",
    "Zvezda", "Vermishel", "Lapsha", "Bantik", "Nay", "Gulcha",
    "Yulduzcha", "Qalampir", "Cho'p", "Uzun", "Qisqa", "Trubka",
]

# Синонимы и опечатки -> канонический продукт
PRODUCT_ALIASES = {
    "burama": "Burama", "борама": "Burama", "burma": "Burama",
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

# Через сколько часов без сообщения считаем сушку «молчащей»
STALE_HOURS = _int("STALE_HOURS", 24)

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
