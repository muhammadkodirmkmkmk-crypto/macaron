"""Поломки в цехе: запись из группы и аналитика для дашборда."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import config, parts
from .db import Fault


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def to_local(d: dt.datetime | None):
    if not d:
        return None
    return d.replace(tzinfo=dt.timezone.utc).astimezone(config.TZ)


def _hours(a: dt.datetime, b: dt.datetime) -> float:
    return max(0.0, (b - a).total_seconds() / 3600)


async def add(s: AsyncSession, *, part: str, text: str, who: str | None = None,
              chat_id: int | None = None, message_id: int | None = None,
              source: str = "telegram", when: dt.datetime | None = None) -> Fault:
    row = Fault(part=part, text=(text or "")[:500], zone=parts.ZONES.get(part, ""),
                reported_at=when or utcnow(), who=who, chat_id=chat_id,
                message_id=message_id, source=source)
    s.add(row)
    await s.flush()
    return row


async def open_rows(s: AsyncSession) -> list[Fault]:
    return list((await s.execute(
        select(Fault).where(Fault.status == "open").order_by(Fault.id.desc())
    )).scalars().all())


def serialize(f: Fault) -> dict:
    fixed = to_local(f.fixed_at)
    rep = to_local(f.reported_at)
    return {
        "id": f.id,
        "part": f.part,
        "name": parts.NAMES.get(f.part, f.part),
        "zone": f.zone or parts.ZONES.get(f.part, ""),
        "text": f.text or "",
        "who": f.who or "—",
        "fixed_by": f.fixed_by or "",
        "status": f.status,
        "reported_at": rep.isoformat() if rep else None,
        "fixed_at": fixed.isoformat() if fixed else None,
        "hours": round(_hours(f.reported_at, f.fixed_at), 1) if f.fixed_at else None,
        "open_hours": round(_hours(f.reported_at, utcnow()), 1) if f.status == "open" else None,
        "cost": int(f.cost or 0),
        "source": f.source,
    }


async def payload(s: AsyncSession, days: int = 30) -> dict:
    """Всё, что нужно дашборду: сводка, рейтинг узлов, участки, списки."""
    since = utcnow() - dt.timedelta(days=days)
    rows = list((await s.execute(
        select(Fault).order_by(Fault.reported_at.desc()).limit(2000)
    )).scalars().all())
    period = [f for f in rows if f.reported_at >= since]
    op = [f for f in rows if f.status == "open"]
    done = [f for f in period if f.status == "fixed" and f.fixed_at]
    avg = round(sum(_hours(f.reported_at, f.fixed_at) for f in done) / len(done), 1) if done else None

    by_part: dict[str, dict] = {}
    for f in period:
        d = by_part.setdefault(f.part, {"part": f.part, "name": parts.NAMES.get(f.part, f.part),
                                        "zone": parts.ZONES.get(f.part, ""), "count": 0,
                                        "open": 0, "hours": 0.0, "done": 0, "cost": 0})
        d["count"] += 1
        d["cost"] += int(f.cost or 0)
        if f.status == "open":
            d["open"] += 1
        elif f.fixed_at:
            d["done"] += 1
            d["hours"] += _hours(f.reported_at, f.fixed_at)
    top = sorted(by_part.values(), key=lambda x: (-x["count"], x["name"]))
    for x in top:
        x["avg_hours"] = round(x["hours"] / x["done"], 1) if x["done"] else None
        x.pop("hours")

    by_zone: dict[str, int] = {}
    for f in period:
        z = f.zone or parts.ZONES.get(f.part, "—")
        by_zone[z] = by_zone.get(z, 0) + 1
    zones = [{"zone": z, "count": n} for z, n in sorted(by_zone.items(), key=lambda kv: -kv[1])]

    # по дням — чтобы видеть, когда цех «сыпется»
    by_day: dict[str, int] = {}
    for f in period:
        d = to_local(f.reported_at)
        if d:
            by_day[d.date().isoformat()] = by_day.get(d.date().isoformat(), 0) + 1
    days_list = [{"day": k, "count": v} for k, v in sorted(by_day.items())]

    return {
        "kpi": {
            "open": len(op),
            "period": len(period),
            "fixed": len(done),
            "avg_hours": avg,
            "cost": sum(int(f.cost or 0) for f in period),
            "oldest_hours": round(max((_hours(f.reported_at, utcnow()) for f in op), default=0), 1),
        },
        "top": top[:15],
        "zones": zones,
        "days": days_list,
        "open": [serialize(f) for f in op[:40]],
        "recent": [serialize(f) for f in period[:120]],
        "parts": [{"id": k, "name": n, "zone": z} for k, n, z in parts.PARTS],
    }
