"""Наполняет базу правдоподобными демо-данными, чтобы посмотреть дашборд без Telegram.

    python seed_demo.py [дней]
"""
import asyncio
import datetime as dt
import random
import sys

from app import config
from app.db import Batch, init_db, session

random.seed(7)

PRODUCTS = {
    "Burama":   (9 * 60 + 20, 55),
    "Pero":     (10 * 60 + 10, 70),
    "Pautinka": (13 * 60 + 40, 80),
    "Spiral":   (11 * 60, 60),
    "Rojok":    (12 * 60 + 15, 65),
    "Vermishel": (8 * 60 + 30, 45),
}
OPERATORS = ["Sardor A.", "Jasur T.", "Bekzod N.", "Oybek Q.", "Shohruh M."]
# у части сушек систематическое отклонение — это должно быть видно на графике
DRYER_BIAS = {3: +75, 7: +110, 12: -45, 19: +60, 26: -30, 27: +25, 31: +130}


async def main(days: int = 30):
    await init_db()
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    rows = []
    for d in range(days):
        day_start = now - dt.timedelta(days=d)
        for dryer in range(1, config.DRYER_COUNT + 1):
            if random.random() < 0.12:      # сушка простаивала
                continue
            for _ in range(random.choice([1, 1, 2])):
                product = random.choice(list(PRODUCTS))
                base, spread = PRODUCTS[product]
                dur = int(random.gauss(base + DRYER_BIAS.get(dryer, 0), spread))
                dur = max(4 * 60, min(20 * 60, dur))
                finished = day_start.replace(
                    hour=random.randint(0, 23), minute=random.choice([0, 5, 10, 20, 30, 40, 50])
                )
                if finished > now:
                    continue
                defect_p = 0.05 + (0.12 if dryer in (7, 31) else 0) + (0.06 if dur > base + 2 * spread else 0)
                quality = "defect" if random.random() < defect_p else "ok"
                rows.append(Batch(
                    dryer_number=dryer, product=product, duration_minutes=dur,
                    finished_at=finished, started_at=finished - dt.timedelta(minutes=dur),
                    temperature=round(random.uniform(55, 85), 1),
                    humidity=round(random.uniform(8, 45)),
                    timer_raw=f"{dur//60:02d}:{dur%60:02d}",
                    quality=quality,
                    note="trewena bor" if quality == "defect" else "trewena yuq",
                    raw_text=f"{product} {dur//60} soat {dur%60} minutda chiqdi",
                    source="mixed", confidence=round(random.uniform(0.75, 1.0), 2),
                    needs_review=random.random() < 0.04,
                    chat_id=-1001234567890, message_id=random.randint(1, 10**6),
                    user_id=random.randint(1, 999), user_name=random.choice(OPERATORS),
                ))
    async with session() as s:
        s.add_all(rows)
        await s.commit()
    print(f"Создано {len(rows)} партий за {days} дней.")


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 30))
