"""Один вызов Claude на весь проект: с повторами и запасной моделью.

API иногда отвечает 529 «перегружен» или 429 «слишком часто». Это не ошибка кода
и не повод показывать человеку отказ — надо просто подождать и повторить.
"""
from __future__ import annotations

import asyncio
import logging
import random

from . import config

log = logging.getLogger("claude")

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}
BACKOFF = (1.5, 4.0, 9.0)          # паузы между попытками, секунды


def _status(exc: Exception) -> int | None:
    return getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None)


def _retryable(exc: Exception) -> bool:
    st = _status(exc)
    if st in RETRY_STATUS:
        return True
    name = type(exc).__name__
    return name in {"OverloadedError", "RateLimitError", "APIConnectionError",
                    "APITimeoutError", "InternalServerError"}


async def create(client, *, fallback_model: str | None = None, **kwargs):
    """messages.create с повторами. При стойкой перегрузке пробует запасную модель."""
    models = [kwargs.pop("model")]
    if fallback_model and fallback_model not in models:
        models.append(fallback_model)

    last: Exception | None = None
    for mi, model in enumerate(models):
        for attempt in range(len(BACKOFF) + 1):
            try:
                return await client.messages.create(model=model, temperature=0, **kwargs)
            except Exception as exc:  # noqa: BLE001
                # у новых моделей параметр temperature не принимается
                if "temperature" in str(exc) and "deprecated" in str(exc).lower():
                    try:
                        return await client.messages.create(model=model, **kwargs)
                    except Exception as exc2:  # noqa: BLE001
                        exc = exc2
                last = exc
                if not _retryable(exc) or attempt == len(BACKOFF):
                    break
                pause = BACKOFF[attempt] * (0.8 + 0.4 * random.random())
                log.warning("%s (%s), повтор через %.1f с — попытка %d/%d",
                            type(exc).__name__, _status(exc), pause, attempt + 2, len(BACKOFF) + 1)
                await asyncio.sleep(pause)
        if mi + 1 < len(models) and last is not None and _retryable(last):
            log.warning("модель %s не отвечает, пробую запасную %s", model, models[mi + 1])
    raise last if last else RuntimeError("не удалось выполнить запрос")


def is_overloaded(exc: Exception) -> bool:
    """Стоит ли говорить человеку «сервис занят», а не «ошибка»."""
    return _retryable(exc)
