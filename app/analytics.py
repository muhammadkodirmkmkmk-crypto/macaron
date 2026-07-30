"""Все расчёты для дашборда. Данные грузим окном и считаем в Python —
так одинаково работает и на SQLite, и на Postgres."""
from __future__ import annotations

import datetime as dt
import statistics as st
from collections import Counter, defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import config
from .db import Batch, LoadEvent


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def to_local(d: dt.datetime | None) -> dt.datetime | None:
    if d is None:
        return None
    return d.replace(tzinfo=dt.timezone.utc).astimezone(config.TZ)


async def load(s: AsyncSession, days: int = 30, product: str | None = None,
               dryer: int | None = None) -> list[Batch]:
    since = utcnow() - dt.timedelta(days=days)
    q = select(Batch).where(Batch.finished_at >= since)
    if product:
        q = q.where(Batch.product == product)
    if dryer:
        q = q.where(Batch.dryer_number == dryer)
    q = q.order_by(Batch.finished_at.desc())
    return list((await s.execute(q)).scalars().all())


def _stats(vals: list[int]) -> dict:
    vals = [v for v in vals if v]
    if not vals:
        return {"count": 0, "avg": None, "median": None, "min": None, "max": None, "stdev": None}
    return {
        "count": len(vals),
        "avg": round(st.mean(vals)),
        "median": round(st.median(vals)),
        "min": min(vals),
        "max": max(vals),
        "stdev": round(st.pstdev(vals)) if len(vals) > 1 else 0,
    }


# ------------------------------------------------------------------ KPI

def kpi(batches: list[Batch], days: int) -> dict:
    now_local = dt.datetime.now(config.TZ)
    today = now_local.date()
    week_ago = today - dt.timedelta(days=7)

    today_b = [b for b in batches if (to_local(b.finished_at).date() == today)]
    week_b = [b for b in batches if (to_local(b.finished_at).date() > week_ago)]

    durations = [b.duration_minutes for b in batches if b.duration_minutes]
    graded = [b for b in batches if b.quality in ("ok", "defect")]
    defects = [b for b in graded if b.quality == "defect"]

    # Загрузка парка: суммарные часы сушки / (кол-во сушек × часы периода)
    total_min = sum(durations)
    capacity_min = config.DRYER_COUNT * days * 24 * 60
    util = round(100 * total_min / capacity_min, 1) if capacity_min else 0.0

    active_dryers = len({b.dryer_number for b in batches if b.dryer_number})

    return {
        "batches_today": len(today_b),
        "batches_week": len(week_b),
        "batches_total": len(batches),
        "avg_duration": _stats(durations)["avg"],
        "median_duration": _stats(durations)["median"],
        "defect_rate": round(100 * len(defects) / len(graded), 1) if graded else None,
        "defects": len(defects),
        "graded": len(graded),
        "utilization": util,
        "active_dryers": active_dryers,
        "dryer_count": config.DRYER_COUNT,
        "needs_review": len([b for b in batches if b.needs_review]),
        "products": len({b.product for b in batches if b.product}),
    }


# ------------------------------------------------------------------ сушки

def dryers(batches: list[Batch]) -> list[dict]:
    by_d: dict[int, list[Batch]] = defaultdict(list)
    for b in batches:
        if b.dryer_number:
            by_d[b.dryer_number].append(b)

    all_dur = [b.duration_minutes for b in batches if b.duration_minutes]
    global_avg = round(st.mean(all_dur)) if all_dur else 600
    now = utcnow()

    out = []
    for n in range(1, config.DRYER_COUNT + 1):
        rows = sorted(by_d.get(n, []), key=lambda b: b.finished_at, reverse=True)
        durs = [b.duration_minutes for b in rows if b.duration_minutes]
        s = _stats(durs)
        last = rows[0] if rows else None
        expected = s["avg"] or global_avg
        since_min = round((now - last.finished_at).total_seconds() / 60) if last else None

        if last is None:
            status = "no_data"
        elif since_min is not None and since_min > config.STALE_HOURS * 60:
            status = "stale"
        elif since_min is not None and since_min > expected * 1.3:
            status = "overdue"
        else:
            status = "running"

        graded = [b for b in rows if b.quality in ("ok", "defect")]
        defects = [b for b in graded if b.quality == "defect"]

        out.append({
            "number": n,
            "status": status,
            "batches": len(rows),
            "avg": s["avg"],
            "median": s["median"],
            "min": s["min"],
            "max": s["max"],
            "stdev": s["stdev"],
            "vs_global": (s["avg"] - global_avg) if s["avg"] else None,
            "last_product": last.product if last else None,
            "last_duration": last.duration_minutes if last else None,
            "last_finished_at": to_local(last.finished_at).isoformat() if last else None,
            "since_minutes": since_min,
            "expected_minutes": expected,
            "temperature": last.temperature if last else None,
            "humidity": last.humidity if last else None,
            "defects": len(defects),
            "defect_rate": round(100 * len(defects) / len(graded), 1) if graded else None,
            "operators": sorted({b.user_name for b in rows if b.user_name})[:4],
        })
    return out


# ------------------------------------------------------------------ разрезы

def by_product(batches: list[Batch]) -> list[dict]:
    by_p: dict[str, list[Batch]] = defaultdict(list)
    for b in batches:
        by_p[b.product or "—"].append(b)
    out = []
    for name, rows in by_p.items():
        s = _stats([b.duration_minutes for b in rows])
        graded = [b for b in rows if b.quality in ("ok", "defect")]
        defects = [b for b in graded if b.quality == "defect"]
        out.append({
            "product": name, **s,
            "defects": len(defects),
            "defect_rate": round(100 * len(defects) / len(graded), 1) if graded else None,
            "dryers": len({b.dryer_number for b in rows if b.dryer_number}),
        })
    return sorted(out, key=lambda x: -x["count"])


def timeline(batches: list[Batch], days: int) -> list[dict]:
    today = dt.datetime.now(config.TZ).date()
    buckets: dict[dt.date, list[Batch]] = defaultdict(list)
    for b in batches:
        buckets[to_local(b.finished_at).date()].append(b)
    out = []
    for i in range(days - 1, -1, -1):
        d = today - dt.timedelta(days=i)
        rows = buckets.get(d, [])
        s = _stats([b.duration_minutes for b in rows])
        graded = [b for b in rows if b.quality in ("ok", "defect")]
        out.append({
            "date": d.isoformat(),
            "count": len(rows),
            "avg": s["avg"],
            "defects": len([b for b in graded if b.quality == "defect"]),
        })
    return out


def hour_histogram(batches: list[Batch]) -> list[dict]:
    c = Counter(to_local(b.finished_at).hour for b in batches)
    return [{"hour": h, "count": c.get(h, 0)} for h in range(24)]


def duration_histogram(batches: list[Batch], bin_min: int = 60) -> list[dict]:
    durs = [b.duration_minutes for b in batches if b.duration_minutes]
    if not durs:
        return []
    lo = (min(durs) // bin_min) * bin_min
    hi = (max(durs) // bin_min + 1) * bin_min
    c = Counter((d // bin_min) * bin_min for d in durs)
    return [{"bin": b, "label": f"{b//60}–{(b+bin_min)//60}ч", "count": c.get(b, 0)}
            for b in range(lo, hi, bin_min)]


def operators(batches: list[Batch]) -> list[dict]:
    by_u: dict[str, list[Batch]] = defaultdict(list)
    for b in batches:
        by_u[b.user_name or "—"].append(b)
    out = []
    for name, rows in by_u.items():
        graded = [b for b in rows if b.quality in ("ok", "defect")]
        out.append({
            "name": name,
            "count": len(rows),
            "defects": len([b for b in graded if b.quality == "defect"]),
            "dryers": len({b.dryer_number for b in rows if b.dryer_number}),
            "last": to_local(max(b.finished_at for b in rows)).isoformat(),
        })
    return sorted(out, key=lambda x: -x["count"])


def product_dryer_matrix(batches: list[Batch]) -> dict:
    """Тепловая карта: среднее время по паре (продукт × сушка)."""
    prods = [p["product"] for p in by_product(batches)][:8]
    cells: dict[tuple[str, int], list[int]] = defaultdict(list)
    for b in batches:
        if b.product in prods and b.dryer_number and b.duration_minutes:
            cells[(b.product, b.dryer_number)].append(b.duration_minutes)
    return {
        "products": prods,
        "dryers": list(range(1, config.DRYER_COUNT + 1)),
        "cells": [
            {"product": p, "dryer": d, "avg": round(st.mean(v)), "count": len(v)}
            for (p, d), v in cells.items()
        ],
    }


def outliers(batches: list[Batch], limit: int = 15) -> list[dict]:
    """Партии, сильнее всего отклонившиеся от среднего по своему продукту."""
    by_p: dict[str, list[int]] = defaultdict(list)
    for b in batches:
        if b.product and b.duration_minutes:
            by_p[b.product].append(b.duration_minutes)
    means = {p: st.mean(v) for p, v in by_p.items() if len(v) >= 3}
    scored = []
    for b in batches:
        if b.product in means and b.duration_minutes:
            delta = b.duration_minutes - means[b.product]
            scored.append((abs(delta), delta, b))
    scored.sort(key=lambda x: -x[0])
    return [{
        "id": b.id, "dryer": b.dryer_number, "product": b.product,
        "duration": b.duration_minutes, "delta": round(delta),
        "finished_at": to_local(b.finished_at).isoformat(),
        "operator": b.user_name, "quality": b.quality,
    } for _, delta, b in scored[:limit]]


def by_day(batches: list[Batch]) -> list[dict]:
    """Разбивка по дням: сколько партий и сколько в среднем сушили."""
    buckets: dict[str, list[Batch]] = defaultdict(list)
    for b in batches:
        buckets[to_local(b.finished_at).date().isoformat()].append(b)
    out = []
    for day in sorted(buckets, reverse=True):
        rows = buckets[day]
        st_ = _stats([b.duration_minutes for b in rows])
        graded = [b for b in rows if b.quality in ("ok", "defect")]
        out.append({
            "date": day,
            "batches": len(rows),
            "avg_minutes": st_["avg"],
            "min_minutes": st_["min"],
            "max_minutes": st_["max"],
            "defects": len([b for b in graded if b.quality == "defect"]),
            "products": sorted({b.product for b in rows if b.product}),
        })
    return out


async def dryer_report(s: AsyncSession, number: int, days: int) -> dict:
    """Полный отчёт по одной сушке: итог, разбивка по дням и список партий."""
    rows = await load(s, days=days, dryer=number)
    if not rows:
        return {"dryer": number, "days": days, "batches": 0,
                "note": "за этот период по этой сушке отчётов не было"}
    info = next(x for x in dryers(rows) if x["number"] == number)
    prod = by_product(rows)
    return {
        "dryer": number,
        "days": days,
        "batches": info["batches"],
        "avg_minutes": info["avg"],
        "median_minutes": info["median"],
        "min_minutes": info["min"],
        "max_minutes": info["max"],
        "defects": info["defects"],
        "defect_rate_percent": info["defect_rate"],
        "status": info["status"],
        "last_finished_at": info["last_finished_at"],
        "last_product": info["last_product"],
        "last_duration_minutes": info["last_duration"],
        "temperature_last": info["temperature"],
        "humidity_last": info["humidity"],
        "operators": info["operators"],
        "by_day": by_day(rows),
        "by_product": [{"product": x["product"], "batches": x["count"],
                        "avg_minutes": x["avg"], "min_minutes": x["min"],
                        "max_minutes": x["max"]} for x in prod],
        # компактный список: длинные поля модели не нужны, а токены стоят денег
        "batches_list": [{
            "product": b.product, "minutes": b.duration_minutes,
            "started_at": to_local(b.started_at).strftime("%d.%m %H:%M") if b.started_at else None,
            "finished_at": to_local(b.finished_at).strftime("%d.%m %H:%M"),
            "quality": b.quality, "operator": b.user_name,
            "temperature": b.temperature, "humidity": b.humidity,
        } for b in rows[:30]],
    }


def serialize(b: Batch) -> dict:
    return {
        "id": b.id,
        "dryer": b.dryer_number,
        "product": b.product,
        "duration": b.duration_minutes,
        "started_at": to_local(b.started_at).isoformat() if b.started_at else None,
        "finished_at": to_local(b.finished_at).isoformat(),
        "temperature": b.temperature,
        "humidity": b.humidity,
        "timer": b.timer_raw,
        "quality": b.quality,
        "note": b.note,
        "operator": b.user_name,
        "confidence": b.confidence,
        "needs_review": b.needs_review,
        "raw_text": b.raw_text,
    }


async def open_loads(s: AsyncSession) -> dict[int, dict]:
    """Сушки, которые сейчас в работе: заложили и о выходе ещё не отчитались."""
    rows = (await s.execute(
        select(LoadEvent).where(LoadEvent.closed == False)  # noqa: E712
        .order_by(LoadEvent.started_at.desc())
    )).scalars().all()
    now = utcnow()
    out: dict[int, dict] = {}
    for ev in rows:
        if not ev.dryer_number or ev.dryer_number in out:
            continue
        out[ev.dryer_number] = {
            "since": to_local(ev.started_at).isoformat(),
            "minutes": round((now - ev.started_at).total_seconds() / 60),
            "product": ev.product,
        }
    return out


async def dashboard_payload(s: AsyncSession, days: int = 30) -> dict:
    batches = await load(s, days=days)
    running = await open_loads(s)
    ds = dryers(batches)
    for d in ds:
        r = running.get(d["number"])
        if r:
            d["running_since"] = r["since"]
            d["running_minutes"] = r["minutes"]
            d["running_product"] = r["product"] or d["last_product"]
            d["status"] = "in_work"
    return {
        "generated_at": dt.datetime.now(config.TZ).isoformat(),
        "days": days,
        "timezone": config.TIMEZONE,
        "norm": {"min": config.NORM_MIN_MINUTES, "max": config.NORM_MAX_MINUTES},
        "kpi": kpi(batches, days),
        "dryers": ds,
        "products": by_product(batches),
        "timeline": timeline(batches, min(days, 60)),
        "hours": hour_histogram(batches),
        "durations": duration_histogram(batches),
        "operators": operators(batches),
        "matrix": product_dryer_matrix(batches),
        "outliers": outliers(batches),
        "defects": [serialize(b) for b in batches if b.quality == "defect"][:50],
        "review": [serialize(b) for b in batches if b.needs_review][:50],
        "recent": [serialize(b) for b in batches[:300]],
    }
