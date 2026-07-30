"""Сквозная проверка нового формата: Start и Stop разными сообщениями.

Гоняет настоящие обработчики бота на временной SQLite-базе, без Telegram.
    python test_flow.py
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import tempfile

os.environ.setdefault("DATABASE_URL", "")
_TMP = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP}/t.db"
os.environ["ANTHROPIC_API_KEY"] = ""          # без обращения к модели
os.environ["TELEGRAM_BOT_TOKEN"] = "0:test"

from app import analytics, bot, config, parser  # noqa: E402
from app.db import Batch, LoadEvent, StopEvent, init_db, session  # noqa: E402

SENT: list[str] = []


class FakeUser:
    id = 111
    full_name = "Operator"
    username = "op"


class FakeChat:
    def __init__(self, cid=-100500, ctype="supergroup"):
        self.id, self.type = cid, ctype


class FakeBot:
    async def send_message(self, chat_id, text, **kw):
        SENT.append(text)
        return None


class FakeMsg:
    """Ровно те поля aiogram.Message, которые трогают наши обработчики."""

    _next_id = 1000

    def __init__(self, text: str, when: dt.datetime, private=False, reply_to=None):
        FakeMsg._next_id += 1
        self.message_id = FakeMsg._next_id
        self.text, self.caption = text, None
        self.date = when
        self.photo = None
        self.from_user = FakeUser()
        self.chat = FakeChat(ctype="private" if private else "supergroup")
        self.bot = FakeBot()
        self.reply_to_message = reply_to

    async def answer(self, text, **kw):
        SENT.append(text)
        return FakeMsg("bot: " + text[:20], self.date)

    async def reply(self, text, **kw):
        return await self.answer(text, **kw)

    async def react(self, *a, **kw):
        return None


def at(day: int, hh: int, mm: int) -> dt.datetime:
    return dt.datetime(2026, 7, day, hh, mm, tzinfo=dt.timezone.utc)


async def feed(text: str, when: dt.datetime) -> None:
    """Прогоняет сообщение через тот же роутинг, что и в проде."""
    msg = FakeMsg(text, when)
    load_hm = parser.parse_load_only(text)
    if load_hm is not None and config.LOAD_REMINDER_ENABLED:
        return await bot._handle_load(msg, None, text, False, load_hm, False)
    stop_hm = parser.parse_stop_only(text)
    if stop_hm is not None and await bot._handle_stop(msg, None, text, False, stop_hm, False):
        return
    raise AssertionError(f"сообщение не распознано: {text!r}")


async def feed_ok(text: str, when: dt.datetime) -> bool:
    """То же, но возвращает True, если обработчик выгрузки взял сообщение на себя."""
    msg = FakeMsg(text, when)
    stop_hm = parser.parse_stop_only(text)
    return bool(stop_hm is not None
                and await bot._handle_stop(msg, None, text, False, stop_hm, False))


def check(cond, label):
    print(f"{'✓' if cond else '✗'} {label}")
    return 0 if cond else 1


async def main() -> int:
    await init_db()
    bad = 0

    # Ташкент = UTC+5. 18:26 UTC = 23:26 местного.
    await feed("Start vaqt 23:26 qochqor", at(20, 18, 30))
    await feed("Start vaqt 22:40 burama", at(20, 17, 45))

    async with session() as s:
        opens = (await s.execute(
            LoadEvent.__table__.select().where(LoadEvent.closed == False)  # noqa: E712
        )).all()
    bad += check(len(opens) == 2, f"два открытых захода (получили {len(opens)})")

    # выгрузка тем же продуктом на следующее утро
    await feed("Stop vaqt 08:26 qochqor", at(21, 3, 30))

    async with session() as s:
        rows = (await s.execute(Batch.__table__.select())).all()
    bad += check(len(rows) == 1, f"одна партия записана (получили {len(rows)})")
    if rows:
        b = rows[0]
        bad += check(b.duration_minutes == 9 * 60, f"9 часов ровно (получили {b.duration_minutes} мин)")
        bad += check(b.product == "Qochqor", f"продукт Qochqor (получили {b.product})")
        bad += check(b.source == "pair", "источник — пара start/stop")

    async with session() as s:
        left = (await s.execute(
            LoadEvent.__table__.select().where(LoadEvent.closed == False)  # noqa: E712
        )).all()
    bad += check(len(left) == 1, f"остался один открытый заход (получили {len(left)})")
    bad += check(left and left[0].product == "Burama", "открытым остался Burama")

    # номер сушки в тексте -> считаем по номеру, а не по продукту
    await feed("7 Start vaqt 09:00 pero", at(21, 4, 5))
    await feed("7 Stop vaqt 19:30 pero", at(21, 14, 35))
    async with session() as s:
        rows = (await s.execute(
            Batch.__table__.select().where(Batch.dryer_number == 7)
        )).all()
    bad += check(len(rows) == 1 and rows[0].duration_minutes == 630,
                 f"сушка №7: 10 ч 30 мин (получили {rows[0].duration_minutes if rows else None})")

    # дубль того же сообщения не должен удваивать партию
    m = FakeMsg("Stop vaqt 08:26 qochqor", at(21, 3, 30))
    async with session() as s:
        before = len((await s.execute(Batch.__table__.select())).all())
    await bot._handle_stop(m, None, m.text, False, (8, 26), False)
    await bot._handle_stop(m, None, m.text, False, (8, 26), False)
    async with session() as s:
        after = len((await s.execute(Batch.__table__.select())).all())
    bad += check(after <= before + 1, f"повтор не удвоил партии ({before} -> {after})")

    # выгрузка без захода: партию не выдумываем, но и не молчим
    async with session() as s:
        before = len((await s.execute(Batch.__table__.select())).all())
    SENT.clear()
    handled = await feed_ok("Stop vaqt 12:00 rakushka", at(21, 7, 5))
    async with session() as s:
        after = len((await s.execute(Batch.__table__.select())).all())
    bad += check(after == before, "без «Start» партия не создаётся")
    bad += check(handled and any("Start" in x for x in SENT),
                 f"бот попросил прислать Start (ответы: {SENT})")

    # --- сценарий из цеха: сначала Stop, потом присылают время начала ---
    SENT.clear()
    stop_msg = FakeMsg("Stop vaqt 00:06 burama", at(22, 19, 10))
    await bot._handle_stop(stop_msg, None, stop_msg.text, False, (0, 6), False)
    async with session() as s:
        pend = (await s.execute(StopEvent.__table__.select().where(
            StopEvent.message_id == stop_msg.message_id))).all()
    bad += check(len(pend) == 1 and not pend[0].closed,
                 f"выгрузка сохранена и ждёт заход (получили {len(pend)})")
    bad += check(any("masalan" in x for x in SENT),
                 "время в вопросе помечено как пример («masalan»)")
    bad += check(pend and pend[0].ask_message_id, "вопрос привязан к висящей выгрузке")

    # оператор отвечает на вопрос бота одним временем
    ask_id = pend[0].ask_message_id
    reply = FakeMsg("15:06", at(22, 19, 12),
                    reply_to=type("R", (), {"message_id": ask_id})())
    await bot.reply_with_time(reply)
    async with session() as s:
        rows = (await s.execute(Batch.__table__.select().where(
            Batch.raw_text == "Stop vaqt 00:06 burama"))).all()
    bad += check(len(rows) == 1, "партия достроилась по ответу временем")
    bad += check(rows and rows[0].duration_minutes == 9 * 60,
                 f"9 часов (15:06 → 00:06), получили {rows[0].duration_minutes if rows else None}")
    async with session() as s:
        left = (await s.execute(StopEvent.__table__.select().where(
            StopEvent.message_id == stop_msg.message_id))).all()
    bad += check(left and left[0].closed, "висящая выгрузка закрыта")

    # --- тот же случай, но время присылают обычным «Start vaqt ...» ---
    stop2 = FakeMsg("Stop vaqt 06:00 zirak", at(23, 1, 5))
    await bot._handle_stop(stop2, None, stop2.text, False, (6, 0), False)
    async with session() as s:
        before = len((await s.execute(Batch.__table__.select())).all())
    await feed("Start vaqt 20:00 zirak", at(23, 1, 20))
    async with session() as s:
        after = (await s.execute(Batch.__table__.select().where(
            Batch.raw_text == "Stop vaqt 06:00 zirak"))).all()
        total = len((await s.execute(Batch.__table__.select())).all())
    bad += check(total == before + 1 and len(after) == 1,
                 "поздний Start достроил раннюю выгрузку")
    bad += check(after and after[0].duration_minutes == 10 * 60,
                 f"10 часов (20:00 → 06:00), получили {after[0].duration_minutes if after else None}")

    # обычный новый заход не должен цепляться к старым выгрузкам
    async with session() as s:
        before = len((await s.execute(Batch.__table__.select())).all())
    await feed("Start vaqt 09:00 pautinka", at(23, 4, 5))
    async with session() as s:
        total = len((await s.execute(Batch.__table__.select())).all())
    bad += check(total == before, "новый заход не создал лишнюю партию")

    # --- процент выполнения, пока сушка в работе ---
    now = dt.datetime.now(dt.timezone.utc)
    await feed("9 Start vaqt " + (now - dt.timedelta(hours=7, minutes=30)).astimezone(
        config.TZ).strftime("%H:%M") + " burama", now)
    async with session() as s:
        pay = await analytics.dashboard_payload(s, days=30)
    d9 = next(x for x in pay["dryers"] if x["number"] == 9)
    bad += check(d9["status"] == "in_work", f"сушка №9 в работе (статус {d9['status']})")
    bad += check(d9.get("running_norm") == 600,
                 f"норма цикла 10 часов (получили {d9.get('running_norm')})")
    bad += check(d9.get("running_percent") == 75,
                 f"7,5 ч из 10 = 75% (получили {d9.get('running_percent')}%)")
    bad += check(149 <= (d9.get("running_left") or 0) <= 151,
                 f"осталось ~2,5 часа (получили {d9.get('running_left')} мин)")

    # выгрузка закрывает цикл — процента больше нет, есть готовая партия
    await feed("9 Stop vaqt " + now.astimezone(config.TZ).strftime("%H:%M") + " burama",
               now + dt.timedelta(minutes=1))
    async with session() as s:
        pay = await analytics.dashboard_payload(s, days=30)
    d9 = next(x for x in pay["dryers"] if x["number"] == 9)
    bad += check(d9["status"] != "in_work" and d9.get("running_percent") is None,
                 "после Stop сушка больше не «в работе»")
    bad += check(d9["last_duration"] in (450, 451),
                 f"записалось 7,5 часа (получили {d9['last_duration']})")

    # котлы в выдаче дашборда
    async with session() as s:
        payload = await analytics.dashboard_payload(s, days=30)
    bs = payload["boilers"]
    bad += check(len(bs) == 3, f"три котла (получили {len(bs)})")
    bad += check([b["from"] for b in bs] == [1, 13, 23], "границы котлов 1 / 13 / 23")
    b1 = next(b for b in bs if b["id"] == 1)
    bad += check(b1["batches"] >= 1, "в первом котле есть партии (сушка №7)")
    d7 = next(d for d in payload["dryers"] if d["number"] == 7)
    bad += check(d7["boiler"] == 1, "сушка №7 в первом котле")
    d25 = next(d for d in payload["dryers"] if d["number"] == 25)
    bad += check(d25["boiler"] == 3, "сушка №25 в третьем котле")

    print(f"\n{'ВСЁ ОК' if not bad else f'ОШИБОК: {bad}'}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
