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
from .parser import norm
from .db import MotorEvent, MotorFault

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
RIGHT_RE = re.compile(r"(?:[oóòö]['`’]?ng\w*|ung\w*|ong\w*|ўнг\w*|унг\w*|онг\w*|right|справ\w*|прав\w*)",
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
    t = norm(text).strip()
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
    t = norm(text).strip()
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
    s.add(MotorEvent(fault_id=row.id, kind="break", at=row.reported_at, who=who,
                     text=(text or "")[:500], source=source, chat_id=chat_id,
                     message_id=message_id))
    return row


async def close(s: AsyncSession, f: MotorFault, *, who: str | None, text: str | None = None,
                source: str = "telegram", chat_id=None, message_id=None, when=None) -> float:
    """Мотор вернули в строй. Возвращает, сколько часов он простоял."""
    f.status = "fixed"
    f.fixed_at = when or utcnow()
    f.fixed_by = (who or "—")[:128]
    s.add(MotorEvent(fault_id=f.id, kind="fix", at=f.fixed_at, who=f.fixed_by,
                     text=(text or "")[:500] or None, source=source, chat_id=chat_id,
                     message_id=message_id))
    return _hours(f.reported_at, f.fixed_at)


async def reopen(s: AsyncSession, f: MotorFault, *, who: str | None = None) -> None:
    """«Починено» нажали по ошибке — запись снова открыта, и это тоже видно в истории."""
    f.status, f.fixed_at, f.fixed_by = "open", None, None
    s.add(MotorEvent(fault_id=f.id, kind="reopen", at=utcnow(), who=who or "Dashboard",
                     source="dashboard"))


async def backfill_events(s: AsyncSession) -> int:
    """Старые поломки появились раньше ленты событий — достраиваем её один раз.

    Текст сообщения о починке тогда не сохраняли, поэтому у таких «починок»
    будет только кто и когда. Пишем лишь в новую таблицу, motor_faults не трогаем.
    """
    have = set((await s.execute(select(MotorEvent.fault_id).distinct())).scalars().all())
    rows = (await s.execute(select(MotorFault))).scalars().all()
    n = 0
    for f in rows:
        if f.id in have:
            continue
        s.add(MotorEvent(fault_id=f.id, kind="break", at=f.reported_at, who=f.who,
                         text=f.text, source="backfill", chat_id=f.chat_id,
                         message_id=f.message_id))
        n += 1
        if f.status == "fixed" and f.fixed_at:
            s.add(MotorEvent(fault_id=f.id, kind="fix", at=f.fixed_at, who=f.fixed_by,
                             text=None, source="backfill"))
            n += 1
    return n


async def open_rows(s: AsyncSession) -> list[MotorFault]:
    return list((await s.execute(
        select(MotorFault).where(MotorFault.status == "open").order_by(MotorFault.id.desc())
    )).scalars().all())


def place(f: MotorFault) -> tuple[int | None, str | None, int | None]:
    """Где сломалось: сушка, сторона, мотор.

    Первые записи разбирались хуже — «Oʻng», «óng» бот тогда не узнавал, и сторона
    осталась пустой. Дочитываем её из текста сообщения, если сушка та же.
    В базе ничего не меняем — только для показа и подсчёта.
    """
    d, sd, m = f.dryer, f.side, f.motor
    if (sd is None or m is None) and f.text:
        hit = parse(f.text)
        if hit and hit["dryer"] in (None, d):
            sd = sd or hit["side"]
            m = m or hit["motor"]
    return d, sd, m


def serialize(f: MotorFault) -> dict:
    rep, fx = to_local(f.reported_at), to_local(f.fixed_at)
    d, sd, m = place(f)
    return {
        "id": f.id, "dryer": d, "side": sd, "motor": m,
        "name": title(d, sd, m),
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


def _down(f: MotorFault, now: dt.datetime) -> float:
    """Сколько часов мотор не работал: до починки или, если ещё открыт, до сейчас."""
    return _hours(f.reported_at, f.fixed_at or now)


REPEAT_DAYS = 7     # сломался снова в течение недели после ремонта — ремонт был плохой


def motor_rank(rows: list[MotorFault], since: dt.datetime, now: dt.datetime) -> list[dict]:
    """Каждый конкретный мотор: «13-сушка · справа · 2-мотор» — сколько раз, как часто, сколько простоял."""
    by: dict[tuple, list[MotorFault]] = {}
    for f in rows:
        d, sd, m = place(f)
        if not d or not sd or not m:
            continue                     # непонятно, какой именно мотор, — в рейтинг не берём
        by.setdefault((d, sd, m), []).append(f)
    out = []
    for (d, sd, m), fs in by.items():
        fs.sort(key=lambda f: f.reported_at)
        gaps = [(b.reported_at - a.reported_at).total_seconds() / 86400 for a, b in zip(fs, fs[1:])]
        repeat = 0
        for a, b in zip(fs, fs[1:]):
            # раньше починки — это тот же случай, написанный второй раз, а не новая поломка
            if a.fixed_at and 0 <= (b.reported_at - a.fixed_at).total_seconds() <= REPEAT_DAYS * 86400:
                repeat += 1
        done = [f for f in fs if f.fixed_at]
        last = to_local(fs[-1].reported_at)
        out.append({
            "dryer": d, "side": sd, "motor": m, "name": title(d, sd, m),
            "count": len(fs),
            "period": len([f for f in fs if f.reported_at >= since]),
            "open": len([f for f in fs if f.status == "open"]),
            "down_hours": round(sum(_down(f, now) for f in fs), 1),
            "avg_repair": round(sum(_hours(f.reported_at, f.fixed_at) for f in done) / len(done), 1)
            if done else None,
            "every_days": round(sum(gaps) / len(gaps), 1) if gaps else None,
            "repeat": repeat,
            "last": last.isoformat() if last else None,
            "ids": [f.id for f in fs],
        })
    out.sort(key=lambda x: (-x["count"], -(x["down_hours"] or 0), x["dryer"]))
    return out


def people(rows: list[MotorFault]) -> list[dict]:
    """Кто пишет о поломках и кто чинит — и как быстро."""
    acc: dict[str, dict] = {}
    for f in rows:
        if f.who:
            a = acc.setdefault(f.who, {"name": f.who, "reported": 0, "fixed": 0, "hours": 0.0})
            a["reported"] += 1
        if f.status == "fixed" and f.fixed_by and f.fixed_at:
            a = acc.setdefault(f.fixed_by, {"name": f.fixed_by, "reported": 0, "fixed": 0, "hours": 0.0})
            a["fixed"] += 1
            a["hours"] += _hours(f.reported_at, f.fixed_at)
    out = []
    for a in acc.values():
        a["avg_fix"] = round(a["hours"] / a["fixed"], 1) if a["fixed"] else None
        a.pop("hours")
        out.append(a)
    out.sort(key=lambda x: (-(x["fixed"] + x["reported"]), x["name"]))
    return out


async def history(s: AsyncSession, faults: dict[int, MotorFault], limit: int = 600) -> list[dict]:
    """Лента событий из группы, новые сверху: сломался / починили / открыли снова."""
    evs = (await s.execute(
        select(MotorEvent).order_by(MotorEvent.at.desc(), MotorEvent.id.desc()).limit(limit)
    )).scalars().all()
    out = []
    for e in evs:
        f = faults.get(e.fault_id)
        if not f:
            continue                     # запись о поломке удалили — событие не показываем
        d, sd, m = place(f)
        at = to_local(e.at)
        item = {
            "id": e.id, "fault_id": e.fault_id, "kind": e.kind,
            "at": at.isoformat() if at else None,
            "who": e.who or "—", "text": e.text or "", "source": e.source,
            "dryer": d, "side": sd, "motor": m, "name": title(d, sd, m),
            "status": f.status,
        }
        if e.kind == "fix" and f.fixed_at:
            item["hours"] = round(_hours(f.reported_at, e.at), 1)
        if e.kind == "break" and f.status == "open":
            item["open_hours"] = round(_hours(f.reported_at, utcnow()), 1)
        out.append(item)
    return out


async def payload(s: AsyncSession, days: int = 30) -> dict:
    now = utcnow()
    since = now - dt.timedelta(days=days)
    rows = list((await s.execute(
        select(MotorFault).order_by(MotorFault.reported_at.desc()).limit(3000)
    )).scalars().all())
    period = [f for f in rows if f.reported_at >= since]
    op = [f for f in rows if f.status == "open"]
    done = [f for f in period if f.status == "fixed" and f.fixed_at]
    avg = round(sum(_hours(f.reported_at, f.fixed_at) for f in done) / len(done), 1) if done else None

    by_dryer: dict[int, dict] = {}
    for f in period:
        d, sd, m = place(f)
        if not d:
            continue
        x = by_dryer.setdefault(d, {"dryer": d, "count": 0, "open": 0,
                                    "hours": 0.0, "done": 0, "slots": {}})
        x["count"] += 1
        x["slots"][slot(sd, m)] = x["slots"].get(slot(sd, m), 0) + 1
        if f.status == "open":
            x["open"] += 1
        elif f.fixed_at:
            x["done"] += 1
            x["hours"] += _hours(f.reported_at, f.fixed_at)
    dryers = sorted(by_dryer.values(), key=lambda x: (-x["count"], x["dryer"]))
    for x in dryers:
        x["avg_hours"] = round(x["hours"] / x["done"], 1) if x["done"] else None
        x.pop("hours")

    # какое место чаще горит: слева-1 … справа-3 — за период и за всё время
    slots = []
    for sd in SIDES:
        for i in range(1, MOTORS_PER_SIDE + 1):
            n = len([f for f in period if place(f)[1:] == (sd, i)])
            n_all = len([f for f in rows if place(f)[1:] == (sd, i)])
            slots.append({"side": sd, "motor": i, "key": f"{sd}{i}", "count": n, "all": n_all})
    unknown = len([f for f in period if None in place(f)[1:]])

    by_day: dict[str, int] = {}
    for f in period:
        d = to_local(f.reported_at)
        if d:
            k = d.date().isoformat()
            by_day[k] = by_day.get(k, 0) + 1

    first = to_local(rows[-1].reported_at) if rows else None
    return {
        "kpi": {
            "open": len(op), "period": len(period), "fixed": len(done), "avg_hours": avg,
            "cost": sum(int(f.cost or 0) for f in period),
            "dryers": len(by_dryer),
            "oldest_hours": round(max((_hours(f.reported_at, now) for f in op), default=0), 1),
            "all": len(rows),
            "down_hours": round(sum(_down(f, now) for f in period), 1),
        },
        "since": first.isoformat() if first else None,
        "dryers": dryers,
        "slots": slots,
        "unknown": unknown,
        "days": [{"day": k, "count": v} for k, v in sorted(by_day.items())],
        "open": [serialize(f) for f in op[:40]],
        "recent": [serialize(f) for f in period[:150]],
        "motors": motor_rank(rows, since, now),
        "people": people(rows),
        "history": await history(s, {f.id: f for f in rows}),
        "repeat_days": REPEAT_DAYS,
        "per_side": MOTORS_PER_SIDE,
        "dryer_count": config.DRYER_COUNT,
    }
