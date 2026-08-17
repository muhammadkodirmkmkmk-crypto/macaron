"""Текстовые сводки для Telegram."""
from __future__ import annotations

import datetime as dt

from sqlalchemy.ext.asyncio import AsyncSession

from . import analytics, config, motors


def fmt_dur(minutes: int | None) -> str:
    if not minutes:
        return "—"
    return f"{minutes // 60}s {minutes % 60:02d}m"


async def today_summary(s: AsyncSession) -> str:
    batches = await analytics.load(s, days=2)
    today = dt.datetime.now(config.TZ).date()
    rows = [b for b in batches if analytics.to_local(b.finished_at).date() == today]
    if not rows:
        return "📊 Bugun hali hisobot yo'q."

    k = analytics.kpi(rows, days=1)
    lines = [
        f"📊 <b>Bugun ({today:%d.%m})</b>",
        f"Partiyalar: <b>{k['batches_total']}</b> · sushkalar: {k['active_dryers']}/{config.DRYER_COUNT}",
        f"O'rtacha vaqt: <b>{fmt_dur(k['avg_duration'])}</b>",
    ]
    if k["defect_rate"] is not None:
        lines.append(f"Brak: {k['defects']} ta ({k['defect_rate']}%)")
    prods = analytics.by_product(rows)[:5]
    if prods:
        lines.append("")
        lines.append("<b>Mahsulot bo'yicha</b>")
        for p in prods:
            lines.append(f"• {p['product']}: {p['count']} ta · o'rt. {fmt_dur(p['avg'])}")
    if k["needs_review"]:
        lines.append(f"\n⚠️ Tekshirish kerak: {k['needs_review']} ta yozuv")
    fx = await motors.open_rows(s)
    if fx:
        top = {}
        for f in fx:
            key = f"{f.dryer or '?'}-sushka"
            top[key] = top.get(key, 0) + 1
        first = sorted(top.items(), key=lambda kv: -kv[1])[:3]
        lines.append("\n🔧 Ochiq motor nosozligi: <b>" + str(len(fx)) + "</b> — " + ", ".join(
            k2 + (f" ×{v}" if v > 1 else "") for k2, v in first))
    if config.PUBLIC_URL:
        lines.append(f"\n📈 {config.PUBLIC_URL}")
    return "\n".join(lines)


async def daily_report(s: AsyncSession) -> str:
    batches = await analytics.load(s, days=7)
    if not batches:
        return "📊 Ma'lumot yo'q."
    k = analytics.kpi(batches, days=7)
    d = analytics.dryers(batches)

    slow = sorted([x for x in d if x["avg"]], key=lambda x: -(x["vs_global"] or 0))[:3]
    silent = [x for x in d if x["status"] in ("stale", "no_data")]

    lines = [
        "🌾 <b>Sana Bogatir — 7 kunlik hisobot</b>",
        f"Partiyalar: <b>{k['batches_total']}</b> · kunlik o'rt.: {round(k['batches_total']/7, 1)}",
        f"O'rtacha sushish: <b>{fmt_dur(k['avg_duration'])}</b> (mediana {fmt_dur(k['median_duration'])})",
        f"Park yuklamasi: {k['utilization']}%",
    ]
    if k["defect_rate"] is not None:
        lines.append(f"Brak darajasi: {k['defect_rate']}% ({k['defects']}/{k['graded']})")

    if slow:
        lines.append("\n<b>Eng sekin sushkalar</b>")
        for x in slow:
            sign = "+" if (x["vs_global"] or 0) > 0 else ""
            lines.append(f"• №{x['number']}: {fmt_dur(x['avg'])} ({sign}{x['vs_global']} min)")

    if silent:
        nums = ", ".join(f"№{x['number']}" for x in silent[:12])
        lines.append(f"\n🔇 Xabar yo'q ({len(silent)}): {nums}")

    if config.PUBLIC_URL:
        lines.append(f"\n📈 {config.PUBLIC_URL}")
    return "\n".join(lines)


async def dryer_card(s: AsyncSession, number: int) -> str:
    if not (1 <= number <= config.DRYER_COUNT):
        return f"Sushka raqami 1–{config.DRYER_COUNT} orasida bo'lishi kerak."
    batches = await analytics.load(s, days=30, dryer=number)
    if not batches:
        return f"Sushka №{number}: oxirgi 30 kunda ma'lumot yo'q."
    info = next(x for x in analytics.dryers(batches) if x["number"] == number)
    last = batches[0]
    lines = [
        f"🔥 <b>Sushka №{number}</b> (30 kun)",
        f"Partiyalar: {info['batches']}",
        f"O'rtacha: <b>{fmt_dur(info['avg'])}</b> · min {fmt_dur(info['min'])} · maks {fmt_dur(info['max'])}",
    ]
    if info["defect_rate"] is not None:
        lines.append(f"Brak: {info['defects']} ta ({info['defect_rate']}%)")
    lines.append(
        f"\nOxirgi: {last.product or '?'} · {fmt_dur(last.duration_minutes)} · "
        f"{analytics.to_local(last.finished_at):%d.%m %H:%M}"
    )
    if last.temperature is not None or last.humidity is not None:
        lines.append(f"Tablo: t={last.temperature or '—'} · h={last.humidity or '—'}")
    return "\n".join(lines)
