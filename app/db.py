"""Схема данных и подключение к БД."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from . import config


class Base(DeclarativeBase):
    pass


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class Batch(Base):
    """Одна завершённая партия сушки = одно распознанное сообщение."""

    __tablename__ = "batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    dryer_number: Mapped[int | None] = mapped_column(Integer, index=True)
    product: Mapped[str | None] = mapped_column(String(64), index=True)
    duration_minutes: Mapped[int | None] = mapped_column(Integer, index=True)

    finished_at: Mapped[dt.datetime] = mapped_column(DateTime, index=True)
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime)

    temperature: Mapped[float | None] = mapped_column(Float)
    humidity: Mapped[float | None] = mapped_column(Float)
    timer_raw: Mapped[str | None] = mapped_column(String(32))
    # Сырые строки с красного табло, через "|" — чтобы можно было перемапить потом
    display_raw: Mapped[str | None] = mapped_column(String(64))

    # ok | defect | unknown
    quality: Mapped[str] = mapped_column(String(16), default="unknown", index=True)
    note: Mapped[str | None] = mapped_column(Text)

    raw_text: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(16), default="regex")  # regex|vision|mixed|manual
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    chat_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    user_name: Mapped[str | None] = mapped_column(String(128))
    photo_file_id: Mapped[str | None] = mapped_column(String(256))

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


Index("ix_batches_dryer_finished", Batch.dryer_number, Batch.finished_at)


class RawMessage(Base):
    """Сырое сообщение — на случай пересчёта задним числом."""

    __tablename__ = "raw_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    message_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    user_name: Mapped[str | None] = mapped_column(String(128))
    text: Mapped[str | None] = mapped_column(Text)
    has_photo: Mapped[bool] = mapped_column(Boolean, default=False)
    photo_file_id: Mapped[str | None] = mapped_column(String(256))
    sent_at: Mapped[dt.datetime] = mapped_column(DateTime, index=True)
    processed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("batches.id"))
    parse_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


Index("ix_raw_unique_msg", RawMessage.chat_id, RawMessage.message_id, unique=True)


class Pending(Base):
    """Бот переспросил номер сушки — ждём ответ реплаем."""

    __tablename__ = "pending"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    ask_message_id: Mapped[int] = mapped_column(BigInteger, index=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"))
    field: Mapped[str] = mapped_column(String(32), default="dryer_number")
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class DryerMeta(Base):
    """Необязательные подписи сушек (цех, бригада)."""

    __tablename__ = "dryer_meta"

    number: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str | None] = mapped_column(String(64))
    zone: Mapped[str | None] = mapped_column(String(64))


_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def engine():
    global _engine
    if _engine is None:
        kw = {"echo": False, "pool_pre_ping": True}
        if DATABASE_IS_SQLITE:
            kw.pop("pool_pre_ping")
        _engine = create_async_engine(config.DATABASE_URL, **kw)
    return _engine


DATABASE_IS_SQLITE = config.DATABASE_URL.startswith("sqlite")


def session() -> AsyncSession:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(engine(), expire_on_commit=False)
    return _session_factory()


async def init_db() -> None:
    async with engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
