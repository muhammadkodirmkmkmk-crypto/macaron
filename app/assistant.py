"""Ассистент в личке: свободный вопрос -> данные из базы -> ответ на языке вопроса.

Модель не считает ничего сама — она только выбирает, какой срез данных запросить,
и пересказывает полученные цифры. Всё, что попадает в ответ, приходит из БД.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import time

from . import analytics, claude, config
from .db import session

log = logging.getLogger("assistant")

# история переписки по пользователю: [(роль, текст)], живёт в памяти процесса
_HISTORY: dict[int, list[dict]] = {}
_TOUCHED: dict[int, float] = {}
HISTORY_TURNS = 16
HISTORY_TTL = 3 * 3600


# ---------------------------------------------------------------- инструменты

TOOLS = [
    {
        "name": "shop_summary",
        "description": (
            "Сводка по всему цеху за период: сколько партий, среднее и медианное время сушки, "
            "уровень брака, загрузка парка, сколько сушек отчиталось, самая быстрая и самая "
            "медленная сушка, границы нормы. Используй для общих вопросов «как дела в цехе»."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"days": {"type": "integer", "description": "период в днях, 1..365"}},
            "required": ["days"],
        },
    },
    {
        "name": "dryer_report",
        "description": (
            "Полный отчёт по одной сушке (аппарату) за период: количество партий, среднее/мин/макс "
            "время, брак, разбивка ПО ДНЯМ, разбивка по продуктам, список последних партий, "
            "последние показания табло. Это основной инструмент для вопроса «дай отчёт по сушке N»."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dryer": {"type": "integer", "description": "номер сушки"},
                "days": {"type": "integer", "description": "период в днях"},
            },
            "required": ["dryer", "days"],
        },
    },
    {
        "name": "ranking",
        "description": (
            "Все сушки, отсортированные по среднему времени сушки от медленных к быстрым, "
            "с отклонением от среднего по цеху. Для вопросов «кто медленнее всех», «сравни сушки»."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"days": {"type": "integer"}},
            "required": ["days"],
        },
    },
    {
        "name": "product_stats",
        "description": (
            "Статистика по видам продукции (Burama, Pero, Pautinka и т.д.): сколько партий, "
            "среднее/мин/макс время сушки, брак, на скольких сушках выпускали."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"days": {"type": "integer"}},
            "required": ["days"],
        },
    },
    {
        "name": "recent_batches",
        "description": (
            "Список конкретных партий с фильтрами. Для вопросов «покажи последние партии», "
            "«что сушили на 12-й вчера», «все партии Pautinka»."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer"},
                "dryer": {"type": "integer", "description": "номер сушки, необязательно"},
                "product": {"type": "string", "description": "название продукта, необязательно"},
                "limit": {"type": "integer", "description": "сколько строк, по умолчанию 20"},
            },
            "required": ["days"],
        },
    },
    {
        "name": "defects_and_problems",
        "description": (
            "Брак и проблемы за период: партии с браком, сушки с наибольшим числом брака, "
            "записи, которые требуют ручной проверки, и сушки, которые давно не отчитывались."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"days": {"type": "integer"}},
            "required": ["days"],
        },
    },
    {
        "name": "boiler_report",
        "description": (
            "Отчёт по КОТЛАМ (qozon). Сушки разделены на котлы по номерам: "
            "котёл 1 — сушки 1–12, котёл 2 — 13–22, котёл 3 — 23–31. "
            "Возвращает по каждому котлу: сколько партий, среднее/мин/макс время, брак, "
            "сколько сушек сейчас в работе, и список сушек этого котла. "
            "Для вопросов «как работает первый котёл», «сравни котлы», «qozon 2 bo'yicha hisobot»."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer"},
                "boiler": {"type": "integer", "description": "номер котла 1..3, необязательно"},
            },
            "required": ["days"],
        },
    },
    {
        "name": "operators",
        "description": "Кто из операторов сколько отчётов прислал, по скольким сушкам, сколько брака.",
        "input_schema": {
            "type": "object",
            "properties": {"days": {"type": "integer"}},
            "required": ["days"],
        },
    },
]


def _clamp_days(v) -> int:
    try:
        return max(1, min(365, int(v)))
    except (TypeError, ValueError):
        return 7


async def run_tool(name: str, args: dict) -> dict:
    days = _clamp_days(args.get("days", 7))
    async with session() as s:
        if name == "dryer_report":
            n = int(args.get("dryer") or 0)
            if not (1 <= n <= config.DRYER_COUNT):
                return {"error": f"номер сушки должен быть от 1 до {config.DRYER_COUNT}"}
            return await analytics.dryer_report(s, n, days)

        rows = await analytics.load(s, days=days)

        if name == "shop_summary":
            k = analytics.kpi(rows, days)
            running = await analytics.open_loads(s)
            d = [x for x in analytics.dryers(rows) if x["avg"] is not None]
            d.sort(key=lambda x: x["avg"])
            return {
                "days": days, **k,
                "norm_min_minutes": config.NORM_MIN_MINUTES,
                "norm_max_minutes": config.NORM_MAX_MINUTES,
                "fastest": d[0] if d else None,
                "slowest": d[-1] if d else None,
                "dryers_with_data": len(d),
                "boilers": analytics.boilers(analytics.dryers(rows), rows),
                "running_now": [
                    {"dryer": n, "product": v["product"], "minutes_so_far": v["minutes"],
                     "since": v["since"], "boiler": config.boiler_of(n),
                     "norm_minutes": analytics.cycle_norm(rows, v["product"], None),
                     "percent_done": min(100, round(
                         100 * v["minutes"] / analytics.cycle_norm(rows, v["product"], None)))}
                    for n, v in sorted(running.items())
                ],
            }

        if name == "boiler_report":
            ds = analytics.dryers(rows)
            running = await analytics.open_loads(s)
            for x in ds:
                r = running.get(x["number"])
                if r:
                    x["status"] = "in_work"
                    x["running_minutes"] = r["minutes"]
                    x["running_product"] = r["product"]
            bs = analytics.boilers(ds, rows)
            want = args.get("boiler")
            if want:
                bs = [b for b in bs if b["id"] == int(want)]
                if not bs:
                    return {"error": "такого котла нет, котлы 1..%d" % len(config.BOILER_RANGES)}
            return {"days": days, "boilers": [
                {**b, "dryers_list": [
                    {"dryer": x["number"], "batches": x["batches"], "avg_minutes": x["avg"],
                     "status": x["status"], "last_product": x["last_product"],
                     "running_minutes": x.get("running_minutes")}
                    for x in ds if b["from"] <= x["number"] <= b["to"]]}
                for b in bs]}

        if name == "ranking":
            d = [x for x in analytics.dryers(rows) if x["avg"] is not None]
            d.sort(key=lambda x: -x["avg"])
            return {"days": days, "dryers": [
                {"dryer": x["number"], "avg_minutes": x["avg"], "batches": x["batches"],
                 "min_minutes": x["min"], "max_minutes": x["max"],
                 "vs_shop_average_minutes": x["vs_global"], "defects": x["defects"]}
                for x in d]}

        if name == "product_stats":
            return {"days": days, "products": analytics.by_product(rows)}

        if name == "recent_batches":
            dryer = args.get("dryer")
            product = args.get("product")
            limit = max(1, min(80, int(args.get("limit") or 20)))
            sel = rows
            if dryer:
                sel = [b for b in sel if b.dryer_number == int(dryer)]
            if product:
                sel = [b for b in sel if (b.product or "").lower() == str(product).lower()]
            return {"days": days, "count": len(sel),
                    "batches": [analytics.serialize(b) for b in sel[:limit]]}

        if name == "defects_and_problems":
            d = analytics.dryers(rows)
            defects = [b for b in rows if b.quality == "defect"]
            return {
                "days": days,
                "defect_batches": [analytics.serialize(b) for b in defects[:40]],
                "by_dryer": sorted(
                    [{"dryer": x["number"], "defects": x["defects"],
                      "defect_rate_percent": x["defect_rate"]} for x in d if x["defects"]],
                    key=lambda x: -x["defects"]),
                "needs_review": [analytics.serialize(b) for b in rows if b.needs_review][:20],
                "silent_dryers": [x["number"] for x in d if x["status"] in ("stale", "no_data")],
            }

        if name == "operators":
            return {"days": days, "operators": analytics.operators(rows)}

    return {"error": f"неизвестный инструмент {name}"}


# ---------------------------------------------------------------- модель

SYSTEM = """Ты — помощник по сушильному цеху макаронной фабрики «Sana Bogatir» (Узбекистан).
В цехе {dryers} сушильных аппаратов, пронумерованных от 1 до {dryers}.
Сушки разделены на КОТЛЫ (по-узбекски «qozon»): {boilers}.
Операторы присылают в Telegram отчёты: «Start vaqt 23:26 qochqor» — партию заложили,
«Stop vaqt 08:40 qochqor» — выгрузили. Бот сам считает, сколько сушка работала.
Всё копится в базе.
Пока сушка в работе, известно, сколько процентов цикла пройдено: прошедшее время
делится на норму (по продукту, если статистики хватает, иначе общая норма).
Время работы считается ТОЛЬКО по сообщениям Start/Stop. На фото видно номер сушки,
температуру и влажность; верхняя строка табло — оставшееся время программы,
её нельзя выдавать за отработанное.

ЯЗЫК. Строго отвечай на языке последнего вопроса — он указан ниже в поле «Язык ответа».
Не переключайся на другой язык, даже если вопрос короткий или в нём есть цифры и
названия продуктов. Пиши грамотно и без ошибок, коротко и по делу, как коллега на
производстве, а не как бот. Никаких извинений и вводных фраз.

СЛОВА. Аппарат называй «сушка» / «sushka» — так говорят в цехе. Не пиши «quritgich»,
«аппарат №», «контейнер». Не используй технические названия полей из базы
(needs_review, defect_rate и подобные) — переводи их на человеческий язык.
Котёл по-узбекски «qozon», по-русски «котёл» — не выдумывай других слов.

ПЕРИОД. Почти все данные зависят от периода. Если человек не указал период
(«дай отчёт по 7-й сушке»), СНАЧАЛА спроси, за какой период: за сегодня, за неделю,
за месяц или за другой срок. Не вызывай инструменты и не придумывай период сам.
Исключение: период явно понятен из вопроса («сегодня», «за неделю», «за 3 дня»,
«вчера», «за месяц») или человек уже назвал его в этом же разговоре.

ДАННЫЕ. Все цифры бери ТОЛЬКО из инструментов. Ничего не считай в уме и не выдумывай.
Если инструмент вернул пусто — так и скажи, что отчётов за этот период не было.
Время в базе хранится в МИНУТАХ — переводи в часы и минуты («10 ч 47 мин», «10 soat 47 min»).

ФОРМАТ. Это Telegram. Используй только теги <b>, <i>, <code> — никакого markdown,
никаких ** и ##. Списки — простыми строками с «•» или «—». Держи ответ компактным:
для отчёта по сушке — итог, потом разбивка по дням, потом заметное отклонение, если есть.
Не вываливай сырые списки, если не просили.

КОНТЕКСТ. Норма времени сушки в цехе — от {norm_min} до {norm_max} часов.
Сегодня {today}, часовой пояс {tz}.
Язык ответа: {lang}.
Если вопрос не про цех, коротко скажи, что отвечаешь только по сушке и производству."""


CYRILLIC = set("абвгдеёжзийклмнопрстуфхцчшщъыьэюя")


def detect_lang(text: str) -> str:
    """Русский пишут кириллицей, узбекский — латиницей. Для этого цеха этого хватает."""
    low = (text or "").lower()
    if any(ch in CYRILLIC for ch in low):
        return "русский"
    if any("a" <= ch <= "z" for ch in low):
        return "узбекский (латиница)"
    return "русский"


def _system(lang: str = "русский") -> str:
    return SYSTEM.format(
        lang=lang,
        dryers=config.DRYER_COUNT,
        boilers=", ".join(f"котёл {i} — сушки {lo}–{hi}" for i, lo, hi in config.BOILER_RANGES),
        norm_min=round(config.NORM_MIN_MINUTES / 60, 1),
        norm_max=round(config.NORM_MAX_MINUTES / 60, 1),
        today=dt.datetime.now(config.TZ).strftime("%d.%m.%Y, %H:%M"),
        tz=config.TIMEZONE,
    )


def _history(uid: int) -> list[dict]:
    if time.time() - _TOUCHED.get(uid, 0) > HISTORY_TTL:
        _HISTORY.pop(uid, None)
    return _HISTORY.setdefault(uid, [])


def reset(uid: int) -> None:
    _HISTORY.pop(uid, None)


async def answer(uid: int, question: str) -> str:
    """Задаёт вопрос модели, даёт ей сходить в базу и возвращает готовый текст."""
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    hist = _history(uid)
    messages = hist + [{"role": "user", "content": question}]

    lang = detect_lang(question)
    used: list[str] = []
    for _ in range(6):
        resp = await claude.create(
            client,
            model=config.ASSISTANT_MODEL,
            fallback_model=config.ASSISTANT_FALLBACK_MODEL,
            max_tokens=2000,
            system=_system(lang),
            tools=TOOLS,
            messages=messages,
        )

        if resp.stop_reason != "tool_use":
            text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
            hist.append({"role": "user", "content": question})
            hist.append({"role": "assistant", "content": text})
            del hist[:-HISTORY_TURNS]
            _TOUCHED[uid] = time.time()
            if used:
                log.info("assistant: инструменты %s", ", ".join(used))
            return text or "Не смог сформулировать ответ, повторите вопрос."

        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for block in resp.content:
            if getattr(block, "type", "") == "tool_use":
                used.append(block.name)
                try:
                    data = await run_tool(block.name, block.input or {})
                except Exception as exc:  # noqa: BLE001
                    log.exception("инструмент %s упал", block.name)
                    data = {"error": str(exc)[:200]}
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(data, ensure_ascii=False, default=str)[:60000],
                })
        messages.append({"role": "user", "content": results})

    return "Запрос оказался слишком сложным. Переформулируйте, пожалуйста, короче."
