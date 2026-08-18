"""Telegram-бот: слушает группу, разбирает отчёты о сушке, пишет в БД."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, ReactionTypeEmoji
from sqlalchemy import func, select

from . import assistant, config, motors, parser, reports
from .db import Batch, LoadEvent, MotorFault, Pending, RawMessage, StopEvent, session

log = logging.getLogger("bot")
router = Router()

MAX_PHOTO_BYTES = 6 * 1024 * 1024


def _is_private(msg: Message) -> bool:
    return msg.chat.type == "private"


def _allowed(msg: Message) -> bool:
    """Можно ли принимать отчёты из этого чата."""
    if _is_private(msg):
        if not config.PRIVATE_ENABLED:
            return False
        if config.ALLOWED_USER_IDS and msg.from_user:
            return msg.from_user.id in config.ALLOWED_USER_IDS
        return True
    if not config.ALLOWED_CHAT_IDS:
        return True
    return msg.chat.id in config.ALLOWED_CHAT_IDS


def _uname(msg: Message) -> str:
    u = msg.from_user
    if not u:
        return "—"
    return (u.full_name or u.username or str(u.id))[:120]


def _sent_at(msg: Message) -> dt.datetime:
    """Когда сообщение было написано НА САМОМ ДЕЛЕ.

    Историю чата бот читать не может, но пересланное старое сообщение несёт
    исходную дату — по ней и считаем, иначе вчерашняя партия уедет на сегодня.
    """
    when = None
    origin = getattr(msg, "forward_origin", None)
    if origin is not None:
        when = getattr(origin, "date", None)
    if when is None:
        when = getattr(msg, "forward_date", None)
    when = when or msg.date or dt.datetime.now(dt.timezone.utc)
    return when.astimezone(dt.timezone.utc).replace(tzinfo=None)


def _fmt_dur(minutes: int | None) -> str:
    if not minutes:
        return "—"
    return f"{minutes // 60} soat {minutes % 60:02d} min"


# ---------------------------------------------------------------- команды

@router.message(CommandStart())
async def cmd_start(msg: Message):
    await msg.answer(
        "🌾 <b>Sana Bogatir</b> — quritish sexi\n\n"
        "Sushka hisobotlarini o'qib, dashboardga yig'aman.\n"
        "<b>Guruhda</b> ham, <b>shu yerda</b> ham ishlayman — farqi yo'q.\n\n"
        "<b>Yangi format:</b>\n"
        "<code>Start vaqt 23:26 qochqor</code> — partiya solindi\n"
        "<code>Stop vaqt 08:26 qochqor</code> — chiqdi\n"
        "Ikkalasini juftlab, sushka qancha ishlaganini o'zim hisoblayman.\n"
        "Sushka raqamini yozsangiz aniqroq bo'ladi: <code>7 Start vaqt 23:26 qochqor</code>\n\n"
        "Eski format ham ishlaydi: sushka rasmi + izoh\n"
        "<code>Burama 9 soat 30 minutda chiqdi</code>\n\n"
        "Rasmdan sushka raqamini va tablo ko'rsatkichlarini o'zim o'qiyman. "
        "Agar raqam ko'rinmasa — so'rayman, siz faqat raqam yuborasiz.\n\n"
        "💬 <b>Shu yerda oddiy savol ham berishingiz mumkin:</b>\n"
        "<i>«7-sushka bo'yicha hisobot ber»</i>\n"
        "<i>«кто медленнее всех за неделю»</i>\n"
        "<i>«brak bo'lganmi oxirgi 3 kunda»</i>\n"
        "O'zbekcha ham, ruscha ham tushunaman.\n\n"
        "🔧 <b>Motor buzilsa</b> — shunchaki yozing:\n"
        "<code>13 sushka chap tarafdagi 3-mator buzildi</code>\n"
        "Sushka raqamini, tomonini va motor raqamini o'zim ajratib olaman.\n"
        "Tuzatilgach shunchaki yozing: <code>1 sushka chap 1 mator tuzatildi</code>\n"
        "yoki <code>Tuzatildi 12</code>, yoki mening xabarimga javob qilib «tuzatildi».\n\n"
        "Buyruqlar: /stats /sushka /oxirgi /dash /nosozlik /reset /id"
    )


@router.message(Command("id"))
async def cmd_id(msg: Message):
    await msg.answer(
        f"Chat ID: <code>{msg.chat.id}</code>\n"
        f"Sizning ID: <code>{msg.from_user.id if msg.from_user else '—'}</code>"
    )


@router.message(Command("dash", "dashboard"))
async def cmd_dash(msg: Message):
    url = config.PUBLIC_URL or "— PUBLIC_URL hali sozlanmagan —"
    await msg.answer(f"📊 Dashboard: {url}")


@router.message(Command("stats", "statistika"))
async def cmd_stats(msg: Message):
    async with session() as s:
        text = await reports.today_summary(s)
    await msg.answer(text)


@router.message(Command("report", "hisobot"))
async def cmd_report(msg: Message):
    async with session() as s:
        text = await reports.daily_report(s)
    await msg.answer(text)


@router.message(Command("oxirgi", "last"))
async def cmd_last(msg: Message):
    async with session() as s:
        rows = (await s.execute(
            select(Batch).order_by(Batch.finished_at.desc()).limit(10)
        )).scalars().all()
    if not rows:
        return await msg.answer("Hozircha ma'lumot yo'q.")
    lines = ["<b>Oxirgi 10 partiya</b>"]
    for b in rows:
        local = b.finished_at.replace(tzinfo=dt.timezone.utc).astimezone(config.TZ)
        mark = "⚠️" if b.quality == "defect" else ""
        lines.append(
            f"#{b.dryer_number or '?'} · {b.product or '?'} · {_fmt_dur(b.duration_minutes)} "
            f"· {local:%d.%m %H:%M} {mark}"
        )
    await msg.answer("\n".join(lines))


@router.message(Command("reset", "yangi"))
async def cmd_reset(msg: Message):
    assistant.reset(msg.from_user.id if msg.from_user else 0)
    await msg.answer("🧹 Suhbatni tozaladim. Yangi savol bering.")


@router.message(Command("sushka"))
async def cmd_sushka(msg: Message):
    parts = (msg.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await msg.answer("Foydalanish: <code>/sushka 27</code>")
    n = int(parts[1])
    async with session() as s:
        text = await reports.dryer_card(s, n)
    await msg.answer(text)




# ---------------------------------------------------------------- моторы сушек
def _mf_line(f: MotorFault, now: dt.datetime) -> str:
    h = max(0.0, (now - f.reported_at).total_seconds() / 3600)
    when = f"{h:.0f} soat" if h < 48 else f"{h/24:.0f} kun"
    return f"#{f.id} <b>{motors.title(f.dryer, f.side, f.motor)}</b> — {when} · {f.who or '—'}"


@router.message(Command("nosozlik", "nosozliklar", "motor"))
async def cmd_faults(msg: Message):
    async with session() as s:
        rows = await motors.open_rows(s)
    if not rows:
        return await msg.answer("✅ Ochiq nosozlik yo'q — hamma motor ishlayapti.")
    now = motors.utcnow()
    await msg.answer("🔧 <b>Ochiq nosozliklar</b>\n\n" +
                     "\n".join(_mf_line(f, now) for f in rows[:25]) +
                     "\n\nTuzatilgach: <code>/tuzatildi 12</code>")


async def _close_fault(s, f: MotorFault, who: str) -> str:
    f.status = "fixed"
    f.fixed_at = motors.utcnow()
    f.fixed_by = who
    h = (f.fixed_at - f.reported_at).total_seconds() / 3600
    when = f"{h:.1f} soat" if h < 48 else f"{h/24:.1f} kun"
    return f"#{f.id} {motors.title(f.dryer, f.side, f.motor)} — {when}"


async def _find_open(s, *, fid=None, dryer=None, side=None, motor=None):
    """Ищем открытую запись: по номеру или по месту (сушка · сторона · мотор)."""
    if fid:
        f = await s.get(MotorFault, int(fid))
        return f if f and f.status == "open" else None
    rows = await motors.open_rows(s)
    if dryer:
        rows = [f for f in rows if f.dryer == dryer]
    if side:
        rows = [f for f in rows if f.side == side]
    if motor:
        rows = [f for f in rows if f.motor == motor]
    return rows[0] if len(rows) >= 1 else None


async def _try_fixed(msg: Message, text: str) -> bool:
    """«1 sushka chap 1 mator tuzatildi», «Tuzatildi 2» или ответ на сообщение бота."""
    hit = motors.parse_fixed(text)
    if not hit:
        return False
    # ответ на «Yozib oldim … (#12)» — номер берём оттуда
    if not hit["id"] and msg.reply_to_message:
        rt = (msg.reply_to_message.text or msg.reply_to_message.caption or "")
        m = re.search(r"[#№]\s*(\d{1,5})", rt)
        if m:
            hit["id"] = int(m.group(1))
    has_place = any(hit[k] for k in ("dryer", "side", "motor"))
    if not hit["id"] and not has_place:
        if not hit.get("strong"):
            return False        # слишком расплывчато — не трогаем
        # «tuzatildi» без уточнений: закрываем, только если открыта ровно одна
        async with session() as s:
            rows = await motors.open_rows(s)
        if len(rows) != 1:
            if not rows:
                return False
            await msg.reply("Qaysi biri tuzatildi? Raqamini yozing: <code>/tuzatildi 12</code>\n"
                            "Ro'yxat: /nosozlik")
            return True
        hit["id"] = rows[0].id

    async with session() as s:
        f = await _find_open(s, fid=hit["id"], dryer=hit["dryer"],
                             side=hit["side"], motor=hit["motor"])
        if not f:
            await s.commit()
            await msg.reply("Bunday ochiq nosozlik topilmadi. Ro'yxat: /nosozlik")
            return True
        line = await _close_fault(s, f, _uname(msg))
        await s.commit()
    await msg.reply(f"✅ Tuzatildi: {line}")
    return True


@router.message(Command("tuzatildi", "tuzat", "tuzatdim"))
async def cmd_fault_fix(msg: Message):
    nums = [w.strip("#№.,") for w in (msg.text or "").split()[1:]]
    nums = [int(w) for w in nums if w.isdigit()]
    if not nums and msg.reply_to_message:      # ответ на сообщение бота
        m = re.search(r"[#№]\s*(\d{1,5})", msg.reply_to_message.text or "")
        if m:
            nums = [int(m.group(1))]
    if not nums:
        async with session() as s:
            rows = await motors.open_rows(s)
        if len(rows) == 1:
            nums = [rows[0].id]
        else:
            return await msg.answer("Qaysi nosozlik? Masalan: <code>/tuzatildi 12</code>\nRo'yxat: /nosozlik")
    done = []
    async with session() as s:
        for n in nums:
            f = await s.get(MotorFault, n)
            if not f or f.status == "fixed":
                continue
            done.append(await _close_fault(s, f, _uname(msg)))
        await s.commit()
    await msg.answer(("✅ Tuzatildi:\n" + "\n".join(done)) if done
                     else "Bunday ochiq nosozlik topilmadi. Ro'yxat: /nosozlik")


async def _try_motor(msg: Message, text: str) -> bool:
    """Сообщение про мотор — записываем. Обычную переписку не трогаем."""
    hit = motors.parse(text)
    if not hit:
        return False
    async with session() as s:
        row = await motors.add(
            s, dryer=hit["dryer"], side=hit["side"], motor=hit["motor"], text=hit["text"],
            who=_uname(msg), chat_id=msg.chat.id, message_id=msg.message_id,
            when=_sent_at(msg), source="telegram")
        await s.commit()
        fid, dryer = row.id, row.dryer
    tail = ("\nQaysi sushka ekanini ham yozib qo'ying." if not dryer else "")
    await msg.reply(f"🔧 Yozib oldim: <b>{motors.title(hit['dryer'], hit['side'], hit['motor'])}</b> "
                    f"(#{fid}){tail}\nTuzatilgach yozing: <code>Tuzatildi {fid}</code> "
                    f"yoki shu xabarga javob qilib «tuzatildi»")
    return True


# ---------------------------------------------------------------- загрузка партии

async def _dryer_from_photo(msg: Message, bot: Bot, caption: str) -> tuple[int | None, str | None]:
    """Номер сушки и продукт: из текста, а если не вышло — с фото."""
    p = parser.parse_text(caption)
    if p.dryer_number or not msg.photo or not config.VISION_ENABLED:
        return p.dryer_number, p.product
    try:
        buf = await bot.download(msg.photo[-1].file_id)
        data = buf.read() if buf else b""
        if 0 < len(data) <= MAX_PHOTO_BYTES:
            v = await parser.parse_with_vision(data, caption)
            return v.dryer_number, v.product or p.product
    except Exception as exc:  # noqa: BLE001
        log.warning("не смог прочитать фото загрузки: %s", exc)
    return p.dryer_number, p.product


async def _handle_load(msg: Message, bot: Bot, caption: str, has_photo: bool,
                       hm, private: bool) -> None:
    """Записываем факт загрузки и ставим напоминание через N часов."""
    sent_at = _sent_at(msg)
    sent_local = sent_at.replace(tzinfo=dt.timezone.utc).astimezone(config.TZ)

    async with session() as s:
        dup = (await s.execute(select(LoadEvent.id).where(
            LoadEvent.chat_id == msg.chat.id, LoadEvent.message_id == msg.message_id
        ))).scalar_one_or_none()
        if dup:
            return

    dryer, product = await _dryer_from_photo(msg, bot, caption)
    started_local = parser.resolve_single(sent_local, hm)
    started = started_local.astimezone(dt.timezone.utc).replace(tzinfo=None)
    remind = started + dt.timedelta(hours=config.LOAD_REMINDER_HOURS)

    async with session() as s:
        # новый заход этой же сушки — старый явно недозакрыли, снимаем
        if dryer:
            for old in (await s.execute(select(LoadEvent).where(
                LoadEvent.closed == False,  # noqa: E712
                LoadEvent.dryer_number == dryer,
            ))).scalars().all():
                old.closed = True
        load = LoadEvent(
            chat_id=msg.chat.id, message_id=msg.message_id,
            dryer_number=dryer, product=product,
            started_at=started, remind_at=remind,
            user_name=_uname(msg), raw_text=caption,
        )
        s.add(load)
        await s.flush()
        # выгрузка могла прийти раньше захода — тогда достраиваем партию сразу
        done = await _close_pending_stop(s, msg.chat.id, dryer, product, started)
        if done:
            load.closed = True
            load.closed_batch_id = done["batch_id"]
        await s.commit()

    if done:
        text = _pair_text(done["dryer"], done["product"], done["duration"],
                          done["started"], done["finished"])
        try:
            await msg.bot.send_message(msg.chat.id, text,
                                       reply_to_message_id=done["message_id"])
        except Exception:  # исходную выгрузку могли удалить
            await msg.answer(text)
        log.info("заход достроил выгрузку: сушка %s, %s, %s мин",
                 done["dryer"], done["product"], done["duration"])
        return

    log.info("загрузка: сушка %s, %s, напомню %s", dryer, product, remind)

    head = f"Sushka №{dryer}" if dryer else "Sushka"
    body = (
        f"📥 <b>{head} yuklandi</b>\n"
        f"{('🍝 ' + product + chr(10)) if product else ''}"
        f"⏱ {started_local:%H:%M} dan boshlandi · {config.LOAD_REMINDER_HOURS} soatdan keyin eslataman"
    )
    if private:
        await msg.answer(body)
    else:
        try:
            await msg.react([ReactionTypeEmoji(emoji="👌")])
        except Exception:  # noqa: BLE001
            pass


MAX_PAIR_MINUTES = 30 * 60   # дольше 30 часов — значит, заход не тот


def _pick_load(rows: list[LoadEvent], dryer: int | None, product: str | None) -> LoadEvent | None:
    """Какой заход закрывает эта выгрузка. rows — открытые заходы, свежие первыми.

    Номер сушки читается с фото и на заходе может не определиться, а на выгрузке
    определиться (или наоборот) — поэтому одного совпадения по номеру мало.
    """
    if dryer:
        same = [e for e in rows if e.dryer_number == dryer]
        if same:
            return same[0]
    if product:
        # тот же продукт: первым выходит тот, кого раньше заложили
        same = [e for e in rows
                if (e.product or "").lower() == product.lower()
                and (not dryer or e.dryer_number in (None, dryer))]
        if same:
            return same[-1]
    # номер знаем, продукт нет: подходит заход без номера, но только если он один —
    # иначе непонятно, чей он, и лучше спросить
    if dryer:
        blank = [e for e in rows if e.dryer_number is None]
        if len(blank) == 1:
            return blank[0]
    if not dryer and not product and len(rows) == 1:
        return rows[0]
    return None


def _pair_minutes(started: dt.datetime, finished: dt.datetime):
    """Минуты между заходом и выходом. Заход вечером, выход утром — плюс сутки.
    Возвращает (минуты | None, исправленный выход)."""
    if finished <= started:
        finished = finished + dt.timedelta(days=1)
    mins = round((finished - started).total_seconds() / 60)
    return (mins if 0 < mins <= MAX_PAIR_MINUTES else None), finished


async def _save_pair(s, *, chat_id, message_id, user_id, user_name, dryer, product,
                     started, finished, duration, raw_text, photo_file_id=None,
                     quality="unknown", note=None) -> int:
    """Готовая партия из пары Start + Stop."""
    batch = Batch(
        dryer_number=dryer, product=product, duration_minutes=duration,
        started_at=started, finished_at=finished,
        quality=quality, note=note, raw_text=raw_text,
        source="pair", confidence=0.9 if dryer else 0.6,
        needs_review=dryer is None,
        chat_id=chat_id, message_id=message_id,
        user_id=user_id, user_name=user_name, photo_file_id=photo_file_id,
    )
    s.add(batch)
    await s.flush()
    return batch.id


def _pair_text(dryer, product, duration, started, finished) -> str:
    st = started.replace(tzinfo=dt.timezone.utc).astimezone(config.TZ)
    fin = finished.replace(tzinfo=dt.timezone.utc).astimezone(config.TZ)
    head = f"Sushka <b>№{dryer}</b>" if dryer else "Sushka"
    text = (
        f"✅ <b>{head} ishladi: {_fmt_dur(duration)}</b>\n"
        f"{('🍝 ' + product + chr(10)) if product else ''}"
        f"⏱ {st:%H:%M} → {fin:%H:%M}"
    )
    if not (config.NORM_MIN_MINUTES <= duration <= config.NORM_MAX_MINUTES):
        text += (f"\n⚠️ me'yordan chetda "
                 f"({config.NORM_MIN_MINUTES//60}–{config.NORM_MAX_MINUTES//60} soat)")
    return text


async def _ask_start_text(s, dryer, product, finished: dt.datetime) -> str:
    """Вопрос о времени начала — с зацепкой, от чего оператору плясать."""
    who = " · ".join(x for x in (f"№{dryer}" if dryer else None, product) if x)
    hint = ""
    if dryer:
        prev = (await s.execute(select(Batch).where(
            Batch.dryer_number == dryer, Batch.finished_at < finished,
        ).order_by(Batch.finished_at.desc()).limit(1))).scalar_one_or_none()
        if prev:
            pl = prev.finished_at.replace(tzinfo=dt.timezone.utc).astimezone(config.TZ)
            gap = round((finished - prev.finished_at).total_seconds() / 60)
            hint = (f"\n\n📌 Shu sushkada oldingi partiya <b>{pl:%d.%m %H:%M}</b> da tugagan — "
                    f"start o'shandan keyin bo'lgan (oradan {_fmt_dur(gap)} o'tdi).")
    fin_local = finished.replace(tzinfo=dt.timezone.utc).astimezone(config.TZ)
    return (
        f"🤔 <b>{who or 'Bu partiya'}</b> — «Start» xabari topilmadi, "
        f"shuning uchun hali hisoblamadim.\nChiqqan vaqti: <b>{fin_local:%H:%M}</b>.{hint}\n\n"
        "Javob qilib yozing — <b>ikkitasidan biri</b>:\n"
        "• boshlanish vaqti — masalan <code>23:26</code>\n"
        "• yoki qancha ishlagani — masalan <code>10 soat 30 min</code>\n\n"
        "Bilmasangiz — «Start vaqt ...» xabarini qaytadan yuboring, o'zim juftlayman."
    )


async def _close_pending_stop(s, chat_id: int, dryer: int | None, product: str | None,
                              started: dt.datetime):
    """Заход пришёл позже выгрузки — достраиваем партию задним числом.

    Берём только те выгрузки, что случились ПОСЛЕ этого захода: обычная новая
    загрузка так ни с чем не спутается.
    """
    rows = (await s.execute(select(StopEvent).where(
        StopEvent.closed == False,  # noqa: E712
        StopEvent.chat_id == chat_id,
        StopEvent.finished_at > started,
    ).order_by(StopEvent.finished_at.asc()).limit(50))).scalars().all()

    pick = None
    if dryer:
        pick = next((x for x in rows if x.dryer_number == dryer), None)
    if pick is None and product:
        pick = next((x for x in rows
                     if (x.product or "").lower() == product.lower()
                     and (not dryer or x.dryer_number in (None, dryer))), None)
    if pick is None and dryer:
        blank = [x for x in rows if x.dryer_number is None]
        if len(blank) == 1:
            pick = blank[0]
    if pick is None and not dryer and not product and len(rows) == 1:
        pick = rows[0]
    if pick is None:
        return None

    duration, finished = _pair_minutes(started, pick.finished_at)
    if duration is None:
        return None

    dryer = dryer or pick.dryer_number
    product = product or pick.product
    batch_id = await _save_pair(
        s, chat_id=pick.chat_id, message_id=pick.message_id,
        user_id=pick.user_id, user_name=pick.user_name,
        dryer=dryer, product=product, started=started, finished=finished,
        duration=duration, raw_text=pick.raw_text, photo_file_id=pick.photo_file_id,
    )
    pick.closed = True
    pick.closed_batch_id = batch_id
    if dryer and not pick.dryer_number:
        pick.dryer_number = dryer
    return {"batch_id": batch_id, "dryer": dryer, "product": product,
            "duration": duration, "started": started, "finished": finished,
            "message_id": pick.message_id}


async def _handle_stop(msg: Message, bot: Bot, caption: str, has_photo: bool,
                       hm, private: bool) -> bool:
    """«Stop vaqt 22:46 burama» — ищем заход этой сушки и считаем, сколько она работала.

    Возвращает True, если пара нашлась и партия записана. False — пусть сообщение
    идёт по обычному пути разбора.
    """
    sent_at = _sent_at(msg)
    sent_local = sent_at.replace(tzinfo=dt.timezone.utc).astimezone(config.TZ)

    async with session() as s:
        seen = (await s.execute(select(RawMessage.id).where(
            RawMessage.chat_id == msg.chat.id, RawMessage.message_id == msg.message_id
        ))).scalar_one_or_none()
    if seen:
        return True

    dryer, product = await _dryer_from_photo(msg, bot, caption)
    finished_local = parser.resolve_single(sent_local, hm)
    finished = finished_local.astimezone(dt.timezone.utc).replace(tzinfo=None)

    async with session() as s:
        rows = (await s.execute(select(LoadEvent).where(
            LoadEvent.closed == False  # noqa: E712
        ).order_by(LoadEvent.started_at.desc()).limit(120))).scalars().all()
        rows = [e for e in rows if e.chat_id == msg.chat.id or private or not config.ALLOWED_CHAT_IDS]
        ev = _pick_load(rows, dryer, product)
        duration = None
        if ev is not None:
            duration, finished = _pair_minutes(ev.started_at, finished)

        photo_id = msg.photo[-1].file_id if has_photo else None
        if ev is None or duration is None:
            # заход не нашёлся: сообщение не теряем, а ждём время начала
            if not parser.START_STOP_RE.search(caption):
                return False
            s.add(RawMessage(
                chat_id=msg.chat.id, message_id=msg.message_id,
                user_id=msg.from_user.id if msg.from_user else None,
                user_name=_uname(msg), text=caption, has_photo=has_photo,
                photo_file_id=photo_id, sent_at=sent_at,
                parse_error="выгрузка без захода — ждём время начала",
            ))
            pend = StopEvent(
                chat_id=msg.chat.id, message_id=msg.message_id,
                dryer_number=dryer, product=product, finished_at=finished,
                user_id=msg.from_user.id if msg.from_user else None,
                user_name=_uname(msg), raw_text=caption, photo_file_id=photo_id,
            )
            s.add(pend)
            await s.flush()
            pend_id = pend.id
            await s.commit()

        else:
            dryer = dryer or ev.dryer_number
            product = product or ev.product
            parsed = parser.parse_text(caption)
            s.add(RawMessage(
                chat_id=msg.chat.id, message_id=msg.message_id,
                user_id=msg.from_user.id if msg.from_user else None,
                user_name=_uname(msg), text=caption, has_photo=has_photo,
                photo_file_id=photo_id, sent_at=sent_at, processed=True,
            ))
            batch_id = await _save_pair(
                s, chat_id=msg.chat.id, message_id=msg.message_id,
                user_id=msg.from_user.id if msg.from_user else None,
                user_name=_uname(msg), dryer=dryer, product=product,
                started=ev.started_at, finished=finished, duration=duration,
                raw_text=f"{ev.raw_text or ''} || {caption}".strip(" |"),
                photo_file_id=photo_id, quality=parsed.quality, note=parsed.note,
            )
            started = ev.started_at
            ev.closed = True
            ev.closed_batch_id = batch_id
            if dryer and not ev.dryer_number:
                ev.dryer_number = dryer
            await s.commit()

    if duration is None or ev is None:
        async with session() as s:
            ask_text = await _ask_start_text(s, dryer, product, finished)
        ask = await msg.reply(ask_text)
        async with session() as s:
            p = await s.get(StopEvent, pend_id)
            if p:
                p.ask_message_id = ask.message_id
                await s.commit()
        log.info("выгрузка без захода: сушка %s, %s — жду время начала", dryer, product)
        return True

    text = _pair_text(dryer, product, duration, started, finished)
    sent = await (msg.answer(text) if private else msg.reply(text))
    if dryer is None:
        ask = await sent.reply(
            f"🤔 Qaysi sushka? Javob qilib <b>raqam</b> yuboring (1–{config.DRYER_COUNT})."
        )
        async with session() as s:
            s.add(Pending(chat_id=msg.chat.id, ask_message_id=ask.message_id, batch_id=batch_id))
            await s.commit()
    log.info("пара start→stop: сушка %s, %s, %s мин", dryer, product, duration)
    return True


async def _close_loads(s, chat_id: int | None, dryer: int | None, batch_id: int) -> None:
    """Пришёл отчёт о выходе — снимаем напоминание по этой сушке."""
    if not dryer:
        return
    rows = (await s.execute(select(LoadEvent).where(
        LoadEvent.closed == False,  # noqa: E712
        LoadEvent.dryer_number == dryer,
    ))).scalars().all()
    for ev in rows:
        if chat_id and ev.chat_id != chat_id and ev.chat_id not in config.ALLOWED_CHAT_IDS:
            continue
        ev.closed = True
        ev.closed_batch_id = batch_id


async def load_reminder_loop(bot: Bot) -> None:
    """Раз в минуту смотрим, по каким загрузкам пора напомнить."""
    if not config.LOAD_REMINDER_ENABLED:
        return
    while True:
        await asyncio.sleep(60)
        try:
            now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
            async with session() as s:
                due = (await s.execute(select(LoadEvent).where(
                    LoadEvent.reminded == False,  # noqa: E712
                    LoadEvent.closed == False,    # noqa: E712
                    LoadEvent.remind_at <= now,
                ).limit(20))).scalars().all()

                for ev in due:
                    passed = round((now - ev.started_at).total_seconds() / 60)
                    st = ev.started_at.replace(tzinfo=dt.timezone.utc).astimezone(config.TZ)
                    head = f"🔥 Sushka <b>№{ev.dryer_number}</b>" if ev.dryer_number else "🔥 Shu sushka"
                    text = (
                        f"⏰ <b>{config.LOAD_REMINDER_HOURS} soat bo'ldi</b>\n"
                        f"{head}{(' · ' + ev.product) if ev.product else ''}\n"
                        f"Boshlandi: {st:%d.%m %H:%M} · o'tdi {_fmt_dur(passed)}\n"
                        f"Holati qanday? Chiqqan bo'lsa, vaqtini yozing."
                    )
                    try:
                        await bot.send_message(ev.chat_id, text,
                                               reply_to_message_id=ev.message_id)
                    except Exception as exc:  # исходное сообщение могли удалить
                        log.warning("напоминание без реплая (%s)", exc)
                        try:
                            await bot.send_message(ev.chat_id, text)
                        except Exception as exc2:  # noqa: BLE001
                            log.warning("напоминание не ушло: %s", exc2)
                    ev.reminded = True

                # старые незакрытые убираем, чтобы не копились
                stale = now - dt.timedelta(hours=48)
                for ev in (await s.execute(select(LoadEvent).where(
                    LoadEvent.closed == False,  # noqa: E712
                    LoadEvent.reminded == True,  # noqa: E712
                    LoadEvent.remind_at <= stale,
                ).limit(50))).scalars().all():
                    ev.closed = True
                # выгрузки, к которым так и не прислали время начала
                for sv in (await s.execute(select(StopEvent).where(
                    StopEvent.closed == False,  # noqa: E712
                    StopEvent.finished_at <= stale,
                ).limit(50))).scalars().all():
                    sv.closed = True
                await s.commit()
        except Exception as exc:  # noqa: BLE001
            log.exception("цикл напоминаний: %s", exc)


# ---------------------------------------------------------------- ассистент

async def _typing_loop(bot: Bot, chat_id: int) -> None:
    """Telegram гасит «печатает» через 5 секунд — держим, пока думаем."""
    try:
        while True:
            await bot.send_chat_action(chat_id, "typing")
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001
        pass


async def _ask_assistant(msg: Message, question: str) -> None:
    """Свободный вопрос в личке: показываем «печатает», отвечаем текстом."""
    uid = msg.from_user.id if msg.from_user else 0
    ru = assistant.detect_lang(question) == "русский"
    typing = asyncio.create_task(_typing_loop(msg.bot, msg.chat.id))
    try:
        text = await assistant.answer(uid, question)
    except Exception as exc:  # noqa: BLE001
        log.exception("ассистент упал: %s", exc)
        from . import claude as _claude
        if _claude.is_overloaded(exc):
            return await msg.answer(
                "⏳ Сервис сейчас перегружен. Повторите вопрос через минуту."
                if ru else
                "⏳ Xizmat hozir band. Bir daqiqadan keyin qayta so'rang."
            )
        return await msg.answer(
            "Не смог обработать вопрос. Попробуйте сформулировать иначе."
            if ru else
            "Savolni qayta ishlay olmadim. Boshqacha yozib ko'ring."
        )
    finally:
        typing.cancel()

    for chunk in _split(text):
        try:
            await msg.answer(chunk)
        except Exception:  # модель могла прислать невалидный HTML
            await msg.answer(chunk, parse_mode=None)


def _split(text: str, limit: int = 3800):
    """Telegram не принимает больше 4096 символов за раз."""
    if len(text) <= limit:
        return [text]
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit:
            out.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        out.append(cur)
    return out


# ---------------------------------------------------------------- ответ-уточнение

# Ответ на вопрос бота: либо час:мин начала, либо сколько сушка отработала
ANSWER_RE = (r"^\s*\d{1,2}\s*(?:[:.\-]\s*\d{2}"
             r"|\s*(?:soat|soa|соат|час(?:ов|а)?|ч)\b[^\n]{0,20})\s*$")


@router.message(F.reply_to_message, F.text.regexp(ANSWER_RE))
async def reply_with_time(msg: Message):
    """Ответ на вопрос «когда начали?»: время начала ИЛИ сколько отработала."""
    if not _allowed(msg):
        return
    txt = (msg.text or "").strip()
    hm = worked = None
    m = re.match(r"^\s*(\d{1,2})\s*[:.\-]\s*(\d{2})\s*$", txt)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if h > 23 or mi > 59:
            return
        hm = (h, mi)
    else:
        hh, mm = parser.HOUR_RE.search(txt), parser.MIN_RE.search(txt)
        if not hh and not mm:
            return
        worked = (int(hh.group(1)) if hh else 0) * 60 + (int(mm.group(1)) if mm else 0)
        if not (0 < worked <= MAX_PAIR_MINUTES):
            return
    ref = msg.reply_to_message.message_id

    async with session() as s:
        pend = (await s.execute(select(StopEvent).where(
            StopEvent.chat_id == msg.chat.id,
            StopEvent.closed == False,  # noqa: E712
            (StopEvent.ask_message_id == ref) | (StopEvent.message_id == ref),
        ).order_by(StopEvent.id.desc()).limit(1))).scalar_one_or_none()
        if not pend:
            return

        if worked is not None:
            # написали, сколько отработала: отсчитываем назад от выгрузки
            started = pend.finished_at - dt.timedelta(minutes=worked)
        else:
            # время начала — до выгрузки; если получилось позже, значит, накануне
            fin_local = pend.finished_at.replace(tzinfo=dt.timezone.utc).astimezone(config.TZ)
            st_local = fin_local.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
            if st_local >= fin_local:
                st_local -= dt.timedelta(days=1)
            started = st_local.astimezone(dt.timezone.utc).replace(tzinfo=None)

        duration, finished = _pair_minutes(started, pend.finished_at)
        if duration is None:
            return await msg.reply(
                "🤔 Bu vaqt to'g'ri kelmadi — chiqish vaqtidan keyin yoki juda uzoq. "
                "Iltimos, qaytadan yuboring."
            )

        batch_id = await _save_pair(
            s, chat_id=pend.chat_id, message_id=pend.message_id,
            user_id=pend.user_id, user_name=pend.user_name,
            dryer=pend.dryer_number, product=pend.product,
            started=started, finished=finished, duration=duration,
            raw_text=pend.raw_text, photo_file_id=pend.photo_file_id,
        )
        pend.closed = True
        pend.closed_batch_id = batch_id
        raw = (await s.execute(select(RawMessage).where(
            RawMessage.chat_id == pend.chat_id, RawMessage.message_id == pend.message_id
        ))).scalar_one_or_none()
        if raw:
            raw.processed = True
            raw.batch_id = batch_id
            raw.parse_error = None
        dryer, product = pend.dryer_number, pend.product
        await s.commit()

    await msg.reply(_pair_text(dryer, product, duration, started, finished))
    if dryer is None:
        ask = await msg.answer(
            f"🤔 Qaysi sushka? Javob qilib <b>raqam</b> yuboring (1–{config.DRYER_COUNT})."
        )
        async with session() as s:
            s.add(Pending(chat_id=msg.chat.id, ask_message_id=ask.message_id, batch_id=batch_id))
            await s.commit()
    log.info("время начала ответом: сушка %s, %s, %s мин", dryer, product, duration)


@router.message(F.reply_to_message, F.text.regexp(r"^\s*\d{1,2}\s*$"))
async def reply_with_number(msg: Message):
    """Пользователь ответил числом на вопрос бота «какая сушка?»."""
    if not _allowed(msg):
        return
    number = int(msg.text.strip())
    if not (1 <= number <= config.DRYER_COUNT):
        return
    async with session() as s:
        pend = (await s.execute(
            select(Pending).where(
                Pending.chat_id == msg.chat.id,
                Pending.ask_message_id == msg.reply_to_message.message_id,
                Pending.resolved == False,  # noqa: E712
            )
        )).scalar_one_or_none()
        if not pend:
            # может, ответили на собственное исходное сообщение
            batch = (await s.execute(
                select(Batch).where(
                    Batch.chat_id == msg.chat.id,
                    Batch.message_id == msg.reply_to_message.message_id,
                )
            )).scalar_one_or_none()
            if not batch:
                return
        else:
            batch = await s.get(Batch, pend.batch_id)
            pend.resolved = True
        if not batch:
            return
        batch.dryer_number = number
        batch.needs_review = False
        batch.source = "manual" if batch.source == "regex" else batch.source
        await s.commit()
    await msg.reply(f"✅ Sushka №{number} sifatida yozib qo'ydim.")


@router.message(F.chat.type == "private", F.text.regexp(r"^\s*\d{1,2}\s*$"))
async def private_plain_number(msg: Message):
    """В личке достаточно прислать просто число — закроем последний открытый вопрос."""
    if not _allowed(msg):
        return
    number = int(msg.text.strip())
    if not (1 <= number <= config.DRYER_COUNT):
        return await msg.answer(f"Sushka raqami 1–{config.DRYER_COUNT} orasida bo'lishi kerak.")
    async with session() as s:
        pend = (await s.execute(
            select(Pending).where(
                Pending.chat_id == msg.chat.id,
                Pending.resolved == False,  # noqa: E712
            ).order_by(Pending.id.desc()).limit(1)
        )).scalar_one_or_none()
        if not pend:
            return await msg.answer(
                "Hozir savol yo'q. Rasm va izohni yuboring — o'zim o'qib olaman."
            )
        batch = await s.get(Batch, pend.batch_id)
        if not batch:
            return
        pend.resolved = True
        batch.dryer_number = number
        batch.needs_review = batch.duration_minutes is None
        await s.commit()
        card = _card(batch)
    await msg.answer(f"✅ Sushka №{number}\n{card}")


def _card(b: Batch) -> str:
    """Короткая карточка распознанной партии."""
    parts = [
        f"🍝 <b>{b.product or '—'}</b> · {_fmt_dur(b.duration_minutes)}",
    ]
    if b.started_at and b.finished_at:
        st = b.started_at.replace(tzinfo=dt.timezone.utc).astimezone(config.TZ)
        fin = b.finished_at.replace(tzinfo=dt.timezone.utc).astimezone(config.TZ)
        parts.append(f"⏱ {st:%H:%M} kirdi → {fin:%H:%M} chiqdi")
    if b.dryer_number:
        parts.insert(0, f"🔥 Sushka <b>№{b.dryer_number}</b>")
    if b.temperature is not None or b.humidity is not None:
        parts.append(
            f"🌡 {b.temperature if b.temperature is not None else '—'}°"
            f" · 💧 {b.humidity if b.humidity is not None else '—'}%"
        )
    if b.quality == "defect":
        parts.append("⚠️ brak belgilandi")
    elif b.quality == "ok":
        parts.append("✅ sifat me'yorida")
    if b.needs_review:
        parts.append("⚠️ ma'lumot to'liq emas")
    return "\n".join(parts)


# ---------------------------------------------------------------- основной хендлер

@router.message(F.chat.type.in_({"group", "supergroup", "private"}))
async def on_report_message(msg: Message, bot: Bot):
    """Отчёт о выгрузке — одинаково из группы и из лички с ботом."""
    if not _allowed(msg):
        return

    private = _is_private(msg)
    caption = (msg.caption or msg.text or "").strip()
    has_photo = bool(msg.photo)
    if not has_photo and not caption:
        return
    # «Start vaqt 23:26 qochqor» / «00:28 kirdi burama» — партию заложили, выхода ещё нет
    load_hm = parser.parse_load_only(caption)
    if load_hm is not None and config.LOAD_REMINDER_ENABLED:
        return await _handle_load(msg, bot, caption, has_photo, load_hm, private)

    # «Stop vaqt 22:46 burama» — выгрузка отдельным сообщением: ищем её заход
    stop_hm = parser.parse_stop_only(caption)
    if stop_hm is not None and await _handle_stop(msg, bot, caption, has_photo, stop_hm, private):
        return

    if not parser.is_report_message(caption, has_photo):
        # не отчёт о сушке — может быть, это поломка: «elak setkasi yirtildi»
        if caption and not caption.startswith("/"):
            if await _try_fixed(msg, caption):     # «… mator tuzatildi», «Tuzatildi 2»
                return
            if await _try_motor(msg, caption):     # «… mator buzildi»
                return
        # в личке всё, что не отчёт, — это вопрос к ассистенту
        if private and caption and config.ASSISTANT_ENABLED:
            await _ask_assistant(msg, caption)
        elif private:
            await msg.answer(
                "Bu xabarni hisobot sifatida tanimadim.\n\n"
                "Guruhdagidek yozing — rasm + izoh:\n"
                "<code>Burama 9 soat 30 minutda chiqdi</code>\n\n"
                "Buyruqlar: /stats /sushka /oxirgi /dash"
            )
        return

    sent_at = _sent_at(msg)
    photo_file_id = msg.photo[-1].file_id if has_photo else None

    async with session() as s:
        exists = (await s.execute(
            select(RawMessage.id).where(
                RawMessage.chat_id == msg.chat.id, RawMessage.message_id == msg.message_id
            )
        )).scalar_one_or_none()
        if exists:
            return
        raw = RawMessage(
            chat_id=msg.chat.id, message_id=msg.message_id,
            user_id=msg.from_user.id if msg.from_user else None,
            user_name=_uname(msg), text=caption, has_photo=has_photo,
            photo_file_id=photo_file_id, sent_at=sent_at,
        )
        s.add(raw)
        await s.commit()
        raw_id = raw.id

    # 1) быстрый разбор текста
    parsed = parser.parse_text(caption)

    # 2) фото -> Claude Vision (номер сушки + показания табло)
    if has_photo and config.VISION_ENABLED:
        try:
            buf = await bot.download(msg.photo[-1].file_id)
            data = buf.read() if buf else b""
            if 0 < len(data) <= MAX_PHOTO_BYTES:
                parsed = await parser.parse_with_vision(data, caption)
        except Exception as exc:  # noqa: BLE001
            log.warning("photo download/vision failed: %s", exc)

    if not parsed.duration_minutes and not parsed.dryer_number:
        async with session() as s:
            r = await s.get(RawMessage, raw_id)
            if r:
                r.parse_error = parsed.vision_error or "ничего не распознано"
                await s.commit()
        if private:
            await msg.answer(
                "😕 Bu xabardan hech narsa o'qib olmadim.\n"
                "Iltimos, izohda vaqtni yozing: <code>Burama 9 soat 30 minutda chiqdi</code>"
            )
        return

    # защита от двойного учёта: тот же отчёт из группы и из лички
    dup_anchor = sent_at
    if parsed.finished_hm and parsed.duration_minutes:
        _sl = sent_at.replace(tzinfo=dt.timezone.utc).astimezone(config.TZ)
        dup_anchor = parser.resolve_times(_sl, parsed.started_hm or parsed.finished_hm,
                                          parsed.finished_hm, parsed.duration_minutes)[1] \
                     .astimezone(dt.timezone.utc).replace(tzinfo=None)
    if parsed.dryer_number and parsed.duration_minutes:
        async with session() as s:
            dup = (await s.execute(
                select(Batch).where(
                    Batch.dryer_number == parsed.dryer_number,
                    Batch.product == parsed.product,
                    Batch.duration_minutes == parsed.duration_minutes,
                    Batch.finished_at >= dup_anchor - dt.timedelta(minutes=config.DUP_WINDOW_MINUTES),
                ).order_by(Batch.id.desc()).limit(1)
            )).scalar_one_or_none()
            if dup:
                r = await s.get(RawMessage, raw_id)
                if r:
                    r.processed = True
                    r.batch_id = dup.id
                    r.parse_error = "дубль"
                    await s.commit()
        if dup:
            log.info("дубль отчёта: сушка %s, %s мин", parsed.dryer_number, parsed.duration_minutes)
            if private:
                await msg.answer(
                    f"ℹ️ Bu partiya allaqachon yozilgan (sushka №{parsed.dryer_number}, "
                    f"{_fmt_dur(parsed.duration_minutes)}) — ikki marta hisoblamadim."
                )
            else:
                try:
                    await msg.react([ReactionTypeEmoji(emoji="👌")])
                except Exception:  # noqa: BLE001
                    pass
            return

    # если написали часы захода и выхода — берём их, они точнее момента отправки
    finished_at = sent_at
    started = None
    if parsed.started_hm and parsed.finished_hm and parsed.duration_minutes:
        sent_local = sent_at.replace(tzinfo=dt.timezone.utc).astimezone(config.TZ)
        st_local, fin_local = parser.resolve_times(
            sent_local, parsed.started_hm, parsed.finished_hm, parsed.duration_minutes)
        started = st_local.astimezone(dt.timezone.utc).replace(tzinfo=None)
        finished_at = fin_local.astimezone(dt.timezone.utc).replace(tzinfo=None)
    elif parsed.duration_minutes:
        started = sent_at - dt.timedelta(minutes=parsed.duration_minutes)

    async with session() as s:
        batch = Batch(
            dryer_number=parsed.dryer_number,
            product=parsed.product,
            duration_minutes=parsed.duration_minutes,
            finished_at=finished_at,
            started_at=started,
            temperature=parsed.temperature,
            humidity=parsed.humidity,
            timer_raw=parsed.timer_raw,
            display_raw=parsed.display_raw,
            quality=parsed.quality,
            note=parsed.note,
            raw_text=caption,
            source=parsed.source,
            confidence=parsed.confidence,
            needs_review=parsed.dryer_number is None or parsed.duration_minutes is None,
            chat_id=msg.chat.id, message_id=msg.message_id,
            user_id=msg.from_user.id if msg.from_user else None,
            user_name=_uname(msg), photo_file_id=photo_file_id,
        )
        s.add(batch)
        await s.flush()
        batch_id = batch.id
        r = await s.get(RawMessage, raw_id)
        if r:
            r.processed = True
            r.batch_id = batch_id
        await _close_loads(s, msg.chat.id, parsed.dryer_number, batch_id)
        await s.commit()

    # 3) если номер сушки не определён — переспросить
    if parsed.dryer_number is None:
        found = []
        if parsed.product:
            found.append(parsed.product)
        if parsed.duration_minutes:
            found.append(_fmt_dur(parsed.duration_minutes))
        hint = (" O'qiganim: " + " · ".join(found) + ".") if found else ""
        ask = await msg.reply(
            f"🤔 Sushka raqamini o'qiy olmadim.{hint}\n"
            f"Javob qilib <b>raqam</b> yuboring (1–{config.DRYER_COUNT})."
            if not private else
            f"🤔 Sushka raqamini rasmdan o'qiy olmadim.{hint}\n"
            f"Shunchaki <b>raqam</b> yuboring (1–{config.DRYER_COUNT})."
        )
        async with session() as s:
            s.add(Pending(chat_id=msg.chat.id, ask_message_id=ask.message_id, batch_id=batch_id))
            await s.commit()
        return

    abnormal = bool(parsed.duration_minutes) and not (
        config.NORM_MIN_MINUTES <= parsed.duration_minutes <= config.NORM_MAX_MINUTES
    )
    warn = (
        f"⚠️ {_fmt_dur(parsed.duration_minutes)} — odatdagidan chetda "
        f"({config.NORM_MIN_MINUTES//60}–{config.NORM_MAX_MINUTES//60} soat)."
    )

    if private:
        # в личке отвечаем карточкой — человек должен видеть, что именно записалось
        async with session() as s:
            batch = await s.get(Batch, batch_id)
            card = _card(batch) if batch else ""
        text = "✅ <b>Yozib oldim</b>\n" + card
        if abnormal:
            text += "\n" + warn
        await msg.answer(text)
        return

    # в группе не спамим: ставим реакцию, пишем только при аномалии
    try:
        await msg.react([ReactionTypeEmoji(emoji="👌")])
    except Exception:  # noqa: BLE001
        pass
    if abnormal:
        await msg.reply(f"⚠️ Sushka №{parsed.dryer_number}: {warn[2:]}")


# ---------------------------------------------------------------- правки сообщений

@router.edited_message(F.chat.type.in_({"group", "supergroup", "private"}))
async def on_edited_message(msg: Message, bot: Bot):
    """Подпись к фото часто дописывают уже после отправки.

    Telegram присылает такие сообщения отдельным типом обновления, и раньше бот
    их просто не видел: заход оставался неучтённым, а сушка — «не в работе».
    """
    if not _allowed(msg):
        return
    caption = (msg.caption or msg.text or "").strip()
    if not caption:
        return

    async with session() as s:
        ev = (await s.execute(select(LoadEvent).where(
            LoadEvent.chat_id == msg.chat.id,
            LoadEvent.message_id == msg.message_id,
        ))).scalar_one_or_none()
        seen = (await s.execute(select(RawMessage.id).where(
            RawMessage.chat_id == msg.chat.id,
            RawMessage.message_id == msg.message_id,
        ))).scalar_one_or_none()

    if ev is not None:
        # заход уже записан — правка может менять время начала или продукт
        hm = parser.parse_load_only(caption)
        if hm is None:
            return
        sent_at = _sent_at(msg)
        sent_local = sent_at.replace(tzinfo=dt.timezone.utc).astimezone(config.TZ)
        started_local = parser.resolve_single(sent_local, hm)
        started = started_local.astimezone(dt.timezone.utc).replace(tzinfo=None)
        product = parser.parse_text(caption).product
        async with session() as s:
            row = await s.get(LoadEvent, ev.id)
            if not row or row.closed:
                return
            changed = row.started_at != started or (product and row.product != product)
            row.started_at = started
            row.remind_at = started + dt.timedelta(hours=config.LOAD_REMINDER_HOURS)
            if product:
                row.product = product
            row.raw_text = caption
            if changed:
                row.reminded = False
            await s.commit()
        if changed:
            log.info("правка захода: сушка %s, начало %s", row.dryer_number, started_local)
            try:
                await msg.react([ReactionTypeEmoji(emoji="👌")])
            except Exception:  # noqa: BLE001
                pass
        return

    if seen:
        return          # партию по этому сообщению уже посчитали, повторно не берём

    # содержимое видим впервые — обрабатываем как обычное сообщение
    await on_report_message(msg, bot)


async def daily_report_loop(bot: Bot):
    """Каждое утро отправляет сводку в группы."""
    if config.DAILY_REPORT_HOUR < 0:
        return
    while True:
        now = dt.datetime.now(config.TZ)
        target = now.replace(hour=config.DAILY_REPORT_HOUR, minute=0, second=0, microsecond=0)
        if target <= now:
            target += dt.timedelta(days=1)
        await asyncio.sleep(max(60, (target - now).total_seconds()))
        try:
            async with session() as s:
                text = await reports.daily_report(s)
                chat_ids = config.ALLOWED_CHAT_IDS or set(
                    (await s.execute(select(Batch.chat_id).distinct())).scalars().all()
                )
            for cid in chat_ids:
                if cid:
                    try:
                        await bot.send_message(cid, text)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("daily report to %s failed: %s", cid, exc)
        except Exception as exc:  # noqa: BLE001
            log.exception("daily report failed: %s", exc)


def build() -> tuple[Bot, Dispatcher]:
    bot = Bot(
        token=config.TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)
    return bot, dp
