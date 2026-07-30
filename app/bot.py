"""Telegram-бот: слушает группу, разбирает отчёты о сушке, пишет в БД."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, ReactionTypeEmoji
from sqlalchemy import func, select

from . import assistant, config, parser, reports
from .db import Batch, Pending, RawMessage, session

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
        "Odatdagidek yuboring: sushka rasmi + izoh\n"
        "<code>Burama 9 soat 30 minutda chiqdi</code>\n\n"
        "Rasmdan sushka raqamini va tablo ko'rsatkichlarini o'zim o'qiyman. "
        "Agar raqam ko'rinmasa — so'rayman, siz faqat raqam yuborasiz.\n\n"
        "💬 <b>Shu yerda oddiy savol ham berishingiz mumkin:</b>\n"
        "<i>«7-sushka bo'yicha hisobot ber»</i>\n"
        "<i>«кто медленнее всех за неделю»</i>\n"
        "<i>«brak bo'lganmi oxirgi 3 kunda»</i>\n"
        "O'zbekcha ham, ruscha ham tushunaman.\n\n"
        "Buyruqlar: /stats /sushka /oxirgi /dash /reset /id"
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



# ---------------------------------------------------------------- ассистент

async def _ask_assistant(msg: Message, question: str) -> None:
    """Свободный вопрос в личке: показываем «печатает», отвечаем текстом."""
    uid = msg.from_user.id if msg.from_user else 0
    try:
        await msg.bot.send_chat_action(msg.chat.id, "typing")
    except Exception:  # noqa: BLE001
        pass
    try:
        text = await assistant.answer(uid, question)
    except Exception as exc:  # noqa: BLE001
        log.exception("ассистент упал: %s", exc)
        return await msg.answer("Hozir javob bera olmadim, birozdan keyin urinib ko'ring.")

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
    if not parser.is_report_message(caption, has_photo):
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

    sent_at = (msg.date or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc).replace(tzinfo=None)
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
