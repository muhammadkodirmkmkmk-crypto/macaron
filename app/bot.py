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

from . import config, parser, reports
from .db import Batch, Pending, RawMessage, session

log = logging.getLogger("bot")
router = Router()

MAX_PHOTO_BYTES = 6 * 1024 * 1024


def _allowed(msg: Message) -> bool:
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
        "🍝 <b>Makaron Analytics</b>\n\n"
        "Men guruhdagi sushka hisobotlarini o'qib, dashboardga yig'aman.\n"
        "Shunchaki odatdagidek yozing:\n"
        "<code>Burama 9 soat 30 minutda chiqdi</code> + sushka rasmi.\n\n"
        "Buyruqlar: /stats /sushka /oxirgi /dash /id"
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


@router.message(Command("sushka"))
async def cmd_sushka(msg: Message):
    parts = (msg.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await msg.answer("Foydalanish: <code>/sushka 27</code>")
    n = int(parts[1])
    async with session() as s:
        text = await reports.dryer_card(s, n)
    await msg.answer(text)


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


# ---------------------------------------------------------------- основной хендлер

@router.message(F.chat.type.in_({"group", "supergroup"}))
async def on_group_message(msg: Message, bot: Bot):
    if not _allowed(msg):
        return

    caption = (msg.caption or msg.text or "").strip()
    has_photo = bool(msg.photo)
    if not has_photo and not caption:
        return
    if not parser.is_report_message(caption, has_photo):
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
                r.parse_error = "ничего не распознано"
                await s.commit()
        return

    started = None
    if parsed.duration_minutes:
        started = sent_at - dt.timedelta(minutes=parsed.duration_minutes)

    async with session() as s:
        batch = Batch(
            dryer_number=parsed.dryer_number,
            product=parsed.product,
            duration_minutes=parsed.duration_minutes,
            finished_at=sent_at,
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

    # 3) если номер сушки не определён — переспросить реплаем
    if parsed.dryer_number is None:
        ask = await msg.reply(
            "🤔 Sushka raqamini o'qiy olmadim. Shu xabarga <b>raqam</b> bilan javob bering (1–%d)."
            % config.DRYER_COUNT
        )
        async with session() as s:
            s.add(Pending(chat_id=msg.chat.id, ask_message_id=ask.message_id, batch_id=batch_id))
            await s.commit()
        return

    # 4) тихая реакция — не спамим чат
    try:
        await msg.react([ReactionTypeEmoji(emoji="👌")])
    except Exception:  # noqa: BLE001
        pass

    # предупреждение об аномальном времени
    if parsed.duration_minutes and not (
        config.NORM_MIN_MINUTES <= parsed.duration_minutes <= config.NORM_MAX_MINUTES
    ):
        await msg.reply(
            f"⚠️ Sushka №{parsed.dryer_number}: {_fmt_dur(parsed.duration_minutes)} — "
            f"odatdagidan chetda ({config.NORM_MIN_MINUTES//60}–{config.NORM_MAX_MINUTES//60} soat)."
        )


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
