"""Точка входа: в одном процессе крутятся веб-дашборд и телеграм-бот."""
from __future__ import annotations

import asyncio
import logging

import uvicorn

from . import bot as bot_mod
from . import config
from .db import init_db
from .web import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("main")


async def run_web() -> None:
    cfg = uvicorn.Config(
        create_app(), host="0.0.0.0", port=config.PORT,
        log_level="info", access_log=False, loop="asyncio",
    )
    await uvicorn.Server(cfg).serve()


async def run_bot() -> None:
    if not config.TELEGRAM_BOT_TOKEN:
        log.warning("TELEGRAM_BOT_TOKEN не задан — бот не запущен, работает только дашборд")
        return
    bot, dp = bot_mod.build()
    me = await bot.get_me()
    log.info("бот @%s запущен", me.username)
    asyncio.create_task(bot_mod.daily_report_loop(bot))
    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


async def main() -> None:
    await init_db()
    log.info("БД готова: %s", config.DATABASE_URL.split("@")[-1])
    log.info("Vision: %s (%s)", "включён" if config.VISION_ENABLED else "выключен", config.ANTHROPIC_MODEL)
    await asyncio.gather(run_web(), run_bot())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
