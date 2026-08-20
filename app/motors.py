"""Моторы сушильных аппаратов: у каждого 6 штук — 3 слева и 3 справа.

Ребята пишут в группу как привыкли: «13 sushka chap tarafdagi 3-mator buzildi».
Здесь мы это разбираем, храним и считаем — какая сушка и какое место ломается чаще.
"""
from __future__ import annotations

import datetime as dt
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import config
from .db import MotorFault

MOTORS_PER_SIDE = 3          # 3 слева + 3 справа = 6 на аппарат
SIDES = ("chap", "ong")      # chap — слева, ong — справа

SIDE_NAME = {"chap": {"uz": "Chap", "ru": "Левая"}, "ong": {"uz": "O'ng", "ru": "Правая"}}

# как в жизни пишут «мотор»
MOTOR_RE = re.compile(
    r"(?:mator\w*|motor\w*|matr\w*|motr\w*|mtr\b|dvigat\w*|"
    r"матор\w*|мотор\w*|двигател\w*|дигател\w*)", re.I | re.U)
# сушка / аппарат / камера
DRYER_RE = re.compile(r"(?:sushka\w*|sushk|сушк\w*|apparat\w*|аппарат\w*|kamera\w*|камер\w*)", re.I | re.U)
# слева
LEFT_RE = re.compile(r"(?:chap\w*|чап\w*|left|слев\w*|лев\w*)", re.I | re.U)
# справа
RIGHT_RE = re.compile(r"(?:o['`’]?ng\w*|ung\w*|ong\w*|ўнг\w*|унг\w*|онг\w*|right|справ\w*|прав\w*)",
                      re.I | re.U)
# поломка: без этих слов сообщение о моторе — просто разговор
BREAK_RE = re.compile(
    r"(?:buz\w*|buzld\w*|ishlamay\w*|ishlamad\w*|ishlamay|ishlmay\w*|"
    r"yonib\w*|yond\w*|kuyd\w*|kuyib\w*|kuygan|sind\w*|singan|"
    r"to['`’]?xtad\w*|toxtad\w*|shovqin\w*|shovkin\w*|qizib\w*|qizd\w*|"
    r"nosoz\w*|remont\w*|problem\w*|xarob\w*|"
    r"буз\w*|ишламай\w*|ишламад\w*|куйд\w*|куйиб\w*|синд\w*|синган|"
    r"тухтад\w*|тўхтад\w*|шовқин\w*|шовкин\w*|носоз\w*|"
    r"сломал\w*|не\s*работает|неработает|горит|сгорел\w*|шумит|греет\w*|встал|стал\b|"
    r"проблем\w*|полом\w*|таъмир\w*|"
    r"o['`’]?chib\w*|o['`’]?chd\w*|uchib\w*|ketdi|kesild\w*|tushib\w*|tushd\w*|"
    r"ўчиб\w*|учиб\w*|тушиб\w*|тушди|кетди|ишдан\s*чиқ\w*|ishdan\s*chiq\w*)", re.I | re.U)

NUM_RE = re.compile(r"\d{1,2}")

# «tuzatildi», «tuzatdik», «ishlayapti», «починили» — мотор вернули в строй
# Слова про починку. «tayyor», «bo'ldi», «готово» сюда НЕ берём: так пишут
# про готовую партию макарон, а не про мотор.
FIXED_RE = re.compile(
    r"(?:tuzatildi|tuzatdik|tuzatdim|tuzatib\s*qo|tuzaldi|to['`’]?g['`’]?rilandi|togrilandi|"
    r"almashtirildi|almashtirdik|almashdik|ishlayapti|ishladi|ishga\s*tush\w*|"
    r"тузатилди|тузатдик|тузатдим|тузатиб|алмаштирилди|алмаштирдик|"
    r"ишлаяпти|ишлади|ишга\s*туш\w*|"
    r"почин\w*|исправ\w*|заработал\w*|поменял\w*|заменил\w*|fixed)", re.I | re.U)

# Однозначные слова: только с ними закрываем запись, если ничего не уточнили
STRONG_FIX_RE = re.compile(
    r"(?:tuzatildi|tuzatdik|tuzatdim|tuzaldi|almashtirildi|almashtirdik|"
    r"тузатилди|тузатдик|тузатдим|алмаштирилди|почин\w*|исправ\w*|fixed)", re.I | re.U)

# номер записи: «#12», «tuzatildi 12», «12 tuzatildi»
ID_RE = re.compile(r"[#№]\s*(\d{1,5})")


def parse_fixed(text: str) -> dict | None:
    """Написали, что починили. Возвращает, что удалось понять: номер записи и/или место.

    None — если про починку речи нет.
    """
    t = (text or "").strip()
    if not t or not FIXED_RE.search(t):
        return None
    low = t.lower()
    out = {"id": None, "dryer": None, "side": None, "motor": None, "text": t[:500],
           "strong": bool(STRONG_FIX_RE.search(t))}

    m = ID_RE.search(low)
    if m:
        out["id"] = int(m.group(1))

    has_place = bool(DRYER_RE.search(low) or MOTOR_RE.search(low)
                     or LEFT_RE.search(low) or RIGHT_RE.search(low))
    if has_place:
        # разбираем так же, как поломку: сушка, сторона, мотор
        fake = t + " buzildi"          # BREAK_RE нужен только для отсева болтовни
        hit = parse(fake)
        if hit:
            out.update({k: hit[k] for k in ("dryer", "side", "motor")})
    elif out["id"] is None:
        # «Tuzatildi 2» — одно число и никаких слов о месте: это номер записи
        nums = [int(x) for x in NUM_RE.findall(low)]
        if len(nums) == 1:
            out["id"] = nums[0]
    return out


def _near(text: str, pos: int, span: int = 18) -> str:
    return text[max(0, pos - span): pos + span]


def parse(text: str) -> dict | None:
    """«13 sushka chap tarafdagi 3-mator buzildi» → сушка 13, слева, мотор 3.

    Возвращает None, если это не про мотор — обычную переписку не трогаем.
    """
    t = (text or "").strip()
    if not t or not MOTOR_RE.search(t):
        return None
    if not BREAK_RE.search(t):
        return None

    low = t.lower()
    side = None
    ls, rs = LEFT_RE.search(low), RIGHT_RE.search(low)
    if ls and (not rs or ls.start() < rs.start()):
        side = "chap"
    elif rs:
        side = "ong"

    # все числа с их позициями
    nums = [(int(m.group(0)), m.start(), m.end()) for m in NUM_RE.finditer(low)]
    nums = [(n, a, b) for n, a, b in nums if n > 0]

    motor = dryer = None
    mm = MOTOR_RE.search(low)
    dm = DRYER_RE.search(low)

    def closest(word_start: int, word_end: int, limit: int):
        """Число, стоящее вплотную к слову: «3-mator», «mator 3», «13 sushka»."""
        best, best_d = None, 99
        for n, a, b in nums:
            if n > limit:
                continue
            d = min(abs(a - word_end), abs(word_start - b))
            if d <= 6 and d < best_d:
                best, best_d = (n, a, b), d
        return best

    m_hit = closest(mm.start(), mm.end(), MOTORS_PER_SIDE * 2) if mm else None
    if m_hit:
        motor = m_hit[0]
    d_hit = closest(dm.start(), dm.end(), config.DRYER_COUNT) if dm else None
    if d_hit:
        dryer = d_hit[0]

    # то, что осталось: если номер сушки не назван словом, берём другое число
    if dryer is None:
        left = [n for n, a, b in nums
                if (not m_hit or (a, b) != (m_hit[1], m_hit[2])) and 1 <= n <= config.DRYER_COUNT]
        if len(left) == 1:
            dryer = left[0]
    if motor is None and dryer is not None:
        left = [n for n, a, b in nums if n != dryer and 1 <= n <= MOTORS_PER_SIDE * 2]
        if len(left) == 1:
            motor = left[0]

    # «5-mator» без стороны: 1-3 — левые, 4-6 — правые, так их и нумеруют подряд
    if motor and motor > MOTORS_PER_SIDE and side is None:
        side, motor = "ong", motor - MOTORS_PER_SIDE
    if motor and motor > MOTORS_PER_SIDE:
        motor = motor - MOTORS_PER_SIDE

    return {"dryer": dryer, "side": side, "motor": motor, "text": t[:500]}


def title(dryer: int | None, side: str | None, motor: int | None, lang: str = "uz") -> str:
    d = f"{dryer}-sushka" if dryer else ("Sushka: ?" if lang == "uz" else "Сушка: ?")
    s = SIDE_NAME.get(side or "", {}).get(lang, "?" if side is None else side)
    m = f"{motor}-motor" if motor else ("motor" if lang == "uz" else "мотор")
    return f"{d} · {s} · {m}"


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def to_local(d: dt.datetime | None):
    return d.replace(tzinfo=dt.timezone.utc).astimezone(config.TZ) if d else None


def _hours(a: dt.datetime, b: dt.datetime) -> float:
    return max(0.0, (b - a).total_seconds() / 3600)


async def add(s: AsyncSession, *, dryer, side, motor, text, who=None,
              chat_id=None, message_id=None, source="telegram", when=None) -> MotorFault:
    row = MotorFault(dryer=dryer, side=side, motor=motor, text=(text or "")[:500],
                     who=who, chat_id=chat_id, message_id=message_id, source=source,
                     reported_at=when or utcnow())
    s.add(row)
    await s.flush()
    return row


async def open_rows(s: AsyncSession) -> list[MotorFault]:
    return list((await s.execute(
        select(MotorFault).where(MotorFault.status == "open").order_by(MotorFault.id.desc())
    )).scalars().all())


def serialize(f: MotorFault) -> dict:
    rep, fx = to_local(f.reported_at), to_local(f.fixed_at)
    return {
        "id": f.id, "dryer": f.dryer, "side": f.side, "motor": f.motor,
        "name": title(f.dryer, f.side, f.motor),
        "text": f.text or "", "who": f.who or "—", "fixed_by": f.fixed_by or "",
        "status": f.status,
        "reported_at": rep.isoformat() if rep else None,
        "fixed_at": fx.isoformat() if fx else None,
        "hours": round(_hours(f.reported_at, f.fixed_at), 1) if f.fixed_at else None,
        "open_hours": round(_hours(f.reported_at, utcnow()), 1) if f.status == "open" else None,
        "cost": int(f.cost or 0), "source": f.source,
    }


def slot(side: str | None, motor: int | None) -> str:
    return f"{side or '?'}{motor or '?'}"


async def payload(s: AsyncSession, days: int = 30) -> dict:
    since = utcnow() - dt.timedelta(days=days)
    rows = list((await s.execute(
        select(MotorFault).order_by(MotorFault.reported_at.desc()).limit(3000)
    )).scalars().all())
    period = [f for f in rows if f.reported_at >= since]
    op = [f for f in rows if f.status == "open"]
    done = [f for f in period if f.status == "fixed" and f.fixed_at]
    avg = round(sum(_hours(f.reported_at, f.fixed_at) for f in done) / len(done), 1) if done else None

    by_dryer: dict[int, dict] = {}
    for f in period:
        if not f.dryer:
            continue
        d = by_dryer.setdefault(f.dryer, {"dryer": f.dryer, "count": 0, "open": 0,
                                          "hours": 0.0, "done": 0, "slots": {}})
        d["count"] += 1
        d["slots"][slot(f.side, f.motor)] = d["slots"].get(slot(f.side, f.motor), 0) + 1
        if f.status == "open":
            d["open"] += 1
        elif f.fixed_at:
            d["done"] += 1
            d["hours"] += _hours(f.reported_at, f.fixed_at)
    dryers = sorted(by_dryer.values(), key=lambda x: (-x["count"], x["dryer"]))
    for x in dryers:
        x["avg_hours"] = round(x["hours"] / x["done"], 1) if x["done"] else None
        x.pop("hours")

    # какое место чаще горит: слева-1 … справа-3
    slots = []
    for sd in SIDES:
        for i in range(1, MOTORS_PER_SIDE + 1):
            n = len([f for f in period if f.side == sd and f.motor == i])
            slots.append({"side": sd, "motor": i, "key": f"{sd}{i}", "count": n})
    unknown = len([f for f in period if not f.side or not f.motor])

    by_day: dict[str, int] = {}
    for f in period:
        d = to_local(f.reported_at)
        if d:
            k = d.date().isoformat()
            by_day[k] = by_day.get(k, 0) + 1

    return {
        "kpi": {
            "open": len(op), "period": len(period), "fixed": len(done), "avg_hours": avg,
            "cost": sum(int(f.cost or 0) for f in period),
            "dryers": len(by_dryer),
            "oldest_hours": round(max((_hours(f.reported_at, utcnow()) for f in op), default=0), 1),
        },
        "dryers": dryers,
        "slots": slots,
        "unknown": unknown,
        "days": [{"day": k, "count": v} for k, v in sorted(by_day.items())],
        "open": [serialize(f) for f in op[:40]],
        "recent": [serialize(f) for f in period[:150]],
        "per_side": MOTORS_PER_SIDE,
        "dryer_count": config.DRYER_COUNT,
    }
