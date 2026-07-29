"""FastAPI: JSON-API + дашборд + выгрузка в Excel."""
from __future__ import annotations

import datetime as dt
import hashlib
import io

from fastapi import Body, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from sqlalchemy import select

from . import analytics, config
from .db import Batch, session

STATIC = config.BASE_DIR / "static"
COOKIE = "makaron_auth"


def _token() -> str:
    return hashlib.sha256(("makaron::" + config.DASHBOARD_PASSWORD).encode()).hexdigest()[:32]


def _authed(request: Request) -> bool:
    if not config.DASHBOARD_PASSWORD:
        return True
    if request.cookies.get(COOKIE) == _token():
        return True
    if request.headers.get("X-Auth") == config.DASHBOARD_PASSWORD:
        return True
    if request.query_params.get("key") == config.DASHBOARD_PASSWORD:
        return True
    return False


def create_app() -> FastAPI:
    app = FastAPI(title="Makaron Analytics", docs_url=None, redoc_url=None)

    LOGIN_HTML = """<!doctype html><meta charset=utf-8>
<title>Makaron Analytics</title>
<style>
body{font:16px system-ui,-apple-system,"Segoe UI",sans-serif;background:#f9f9f7;color:#0b0b0b;
display:grid;place-items:center;height:100vh;margin:0}
form{background:#fcfcfb;padding:32px;border-radius:16px;border:1px solid rgba(11,11,11,.1);
box-shadow:0 1px 3px rgba(0,0,0,.06);width:300px}
h1{font-size:18px;margin:0 0 16px}
input{width:100%;padding:10px 12px;border:1px solid #c3c2b7;border-radius:8px;font-size:15px;box-sizing:border-box}
button{width:100%;margin-top:12px;padding:10px;border:0;border-radius:8px;background:#2a78d6;color:#fff;
font-size:15px;font-weight:600;cursor:pointer}
p{color:#52514e;font-size:13px;margin:0 0 12px}
@media(prefers-color-scheme:dark){body{background:#0d0d0d;color:#fff}form{background:#1a1a19;border-color:rgba(255,255,255,.1)}
input{background:#0d0d0d;color:#fff;border-color:#383835}}
</style>
<form method=get action=/login><h1>🍝 Makaron Analytics</h1>
<p>Parolni kiriting</p>
<input name=p type=password autofocus placeholder="Parol"><button>Kirish</button></form>"""

    @app.get("/health")
    async def health():
        return {"ok": True, "time": dt.datetime.now(config.TZ).isoformat()}

    @app.get("/login")
    async def login(p: str = ""):
        if not config.DASHBOARD_PASSWORD:
            return Response(status_code=302, headers={"Location": "/"})
        if p == config.DASHBOARD_PASSWORD:
            r = Response(status_code=302, headers={"Location": "/"})
            r.set_cookie(COOKIE, _token(), max_age=60 * 60 * 24 * 90, httponly=True, samesite="lax")
            return r
        return HTMLResponse(LOGIN_HTML)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.url.path in ("/health", "/login") or _authed(request):
            resp = await call_next(request)
            if request.query_params.get("key") == config.DASHBOARD_PASSWORD and config.DASHBOARD_PASSWORD:
                resp.set_cookie(COOKIE, _token(), max_age=60 * 60 * 24 * 90, httponly=True, samesite="lax")
            return resp
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return HTMLResponse(LOGIN_HTML, status_code=401)

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "dashboard.html")

    @app.get("/api/dashboard")
    async def api_dashboard(days: int = Query(30, ge=1, le=365)):
        async with session() as s:
            return await analytics.dashboard_payload(s, days=days)

    @app.get("/api/batches")
    async def api_batches(
        days: int = Query(30, ge=1, le=365),
        dryer: int | None = None,
        product: str | None = None,
        limit: int = Query(500, ge=1, le=5000),
    ):
        async with session() as s:
            rows = await analytics.load(s, days=days, product=product, dryer=dryer)
        return {"count": len(rows), "items": [analytics.serialize(b) for b in rows[:limit]]}

    @app.patch("/api/batches/{batch_id}")
    async def api_patch(batch_id: int, payload: dict = Body(...)):
        allowed = {"dryer_number", "product", "duration_minutes", "quality",
                   "note", "temperature", "humidity"}
        async with session() as s:
            b = await s.get(Batch, batch_id)
            if not b:
                raise HTTPException(404, "not found")
            for k, v in payload.items():
                if k in allowed:
                    setattr(b, k, v)
            if b.dryer_number and b.duration_minutes:
                b.needs_review = False
                b.started_at = b.finished_at - dt.timedelta(minutes=b.duration_minutes)
            b.source = "manual"
            await s.commit()
            return analytics.serialize(b)

    @app.delete("/api/batches/{batch_id}")
    async def api_delete(batch_id: int):
        async with session() as s:
            b = await s.get(Batch, batch_id)
            if not b:
                raise HTTPException(404, "not found")
            await s.delete(b)
            await s.commit()
        return {"ok": True}

    @app.post("/api/remap-display")
    async def api_remap():
        """Пересчитать температуру и влажность из сохранённых строк табло.
        Идемпотентно: запускать можно сколько угодно раз."""
        from .parser import map_display

        changed = []
        async with session() as s:
            rows = (await s.execute(
                select(Batch).where(Batch.display_raw.is_not(None))
            )).scalars().all()
            for b in rows:
                parts = (b.display_raw or "").split("|")
                parts += [""] * (3 - len(parts))
                timer, temp, hum = map_display(*(x or None for x in parts[:3]))
                if (temp, hum) != (b.temperature, b.humidity):
                    changed.append({
                        "id": b.id, "dryer": b.dryer_number, "raw": b.display_raw,
                        "temperature": [b.temperature, temp], "humidity": [b.humidity, hum],
                    })
                    b.temperature, b.humidity = temp, hum
                    if timer and not b.timer_raw:
                        b.timer_raw = timer
            await s.commit()
        return {"scanned": len(rows), "updated": len(changed), "changes": changed[:100]}

    @app.get("/api/export.xlsx")
    async def api_export(days: int = Query(30, ge=1, le=365)):
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill

        async with session() as s:
            batches = await analytics.load(s, days=days)
            payload = await analytics.dashboard_payload(s, days=days)

        wb = Workbook()
        head_font = Font(bold=True, color="FFFFFF")
        head_fill = PatternFill("solid", fgColor="2A78D6")

        def style_head(ws, ncols):
            for c in range(1, ncols + 1):
                cell = ws.cell(row=1, column=c)
                cell.font = head_font
                cell.fill = head_fill
                cell.alignment = Alignment(horizontal="center")
            ws.freeze_panes = "A2"

        ws = wb.active
        ws.title = "Partiyalar"
        cols = ["ID", "Sushka", "Mahsulot", "Vaqt (min)", "Vaqt (soat)", "Boshlandi",
                "Tugadi", "t°", "Namlik", "Sifat", "Izoh", "Operator", "Ishonch", "Xabar matni"]
        ws.append(cols)
        for b in batches:
            ws.append([
                b.id, b.dryer_number, b.product, b.duration_minutes,
                round(b.duration_minutes / 60, 2) if b.duration_minutes else None,
                analytics.to_local(b.started_at).replace(tzinfo=None) if b.started_at else None,
                analytics.to_local(b.finished_at).replace(tzinfo=None),
                b.temperature, b.humidity, b.quality, b.note, b.user_name,
                b.confidence, b.raw_text,
            ])
        style_head(ws, len(cols))
        for i, w in enumerate([6, 8, 14, 11, 11, 18, 18, 8, 9, 10, 26, 18, 9, 40], 1):
            ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w

        ws2 = wb.create_sheet("Sushkalar")
        ws2.append(["Sushka", "Partiyalar", "O'rtacha (min)", "Mediana", "Min", "Maks",
                    "Global farq", "Brak", "Brak %", "Holat", "Oxirgi mahsulot", "Oxirgi tugash"])
        for d in payload["dryers"]:
            ws2.append([d["number"], d["batches"], d["avg"], d["median"], d["min"], d["max"],
                        d["vs_global"], d["defects"], d["defect_rate"], d["status"],
                        d["last_product"], d["last_finished_at"]])
        style_head(ws2, 12)

        ws3 = wb.create_sheet("Mahsulotlar")
        ws3.append(["Mahsulot", "Partiyalar", "O'rtacha", "Mediana", "Min", "Maks", "Std", "Brak", "Brak %", "Sushkalar"])
        for p in payload["products"]:
            ws3.append([p["product"], p["count"], p["avg"], p["median"], p["min"], p["max"],
                        p["stdev"], p["defects"], p["defect_rate"], p["dryers"]])
        style_head(ws3, 10)

        ws4 = wb.create_sheet("Kunlar")
        ws4.append(["Sana", "Partiyalar", "O'rtacha vaqt", "Brak"])
        for t in payload["timeline"]:
            ws4.append([t["date"], t["count"], t["avg"], t["defects"]])
        style_head(ws4, 4)

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        fname = f"makaron_{dt.datetime.now(config.TZ):%Y%m%d}_{days}d.xlsx"
        return StreamingResponse(
            buf,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    return app
