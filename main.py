import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, time as dtime, timedelta, timezone, date
from typing import Optional
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    MenuButtonWebApp,
    Message,
    ReplyKeyboardMarkup,
    Update,
    WebAppInfo,
)
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("barber")

# ---------------------------------------------------------------- config
BOT_TOKEN = os.environ["BOT_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"]  # secret key (sb_secret_...) or service_role
OWNER_ID = int(os.environ["OWNER_ID"])  # barber's Telegram ID
WEBAPP_URL = os.environ.get("WEBAPP_URL", "")  # Netlify mini app URL
BASE_URL = (os.environ.get("BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")).rstrip("/")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET") or hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:32]
TZ = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Tashkent"))
SLOT_STEP = int(os.environ.get("SLOT_STEP_MIN", "30"))
MAX_ACTIVE_BOOKINGS = int(os.environ.get("MAX_ACTIVE_BOOKINGS", "3"))
CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",")]

BOT_USERNAME = ""


# ---------------------------------------------------------------- database (Supabase REST)
class DBError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"{status}: {body}")
        self.status = status
        self.body = body


class DB:
    def __init__(self, url: str, key: str):
        headers = {"apikey": key, "Content-Type": "application/json"}
        if not key.startswith("sb_"):  # legacy JWT keys also need Authorization
            headers["Authorization"] = f"Bearer {key}"
        self.c = httpx.AsyncClient(base_url=f"{url}/rest/v1", headers=headers, timeout=15)

    async def _req(self, method, table, params=None, body=None):
        r = await self.c.request(
            method, f"/{table}", params=params, json=body,
            headers={"Prefer": "return=representation"},
        )
        if r.status_code >= 400:
            raise DBError(r.status_code, r.text)
        return r.json() if r.text else []

    async def select(self, table, params=None):
        return await self._req("GET", table, params=params)

    async def insert(self, table, data):
        return await self._req("POST", table, body=data)

    async def update(self, table, filters, data):
        return await self._req("PATCH", table, params=filters, body=data)

    async def close(self):
        await self.c.aclose()


db = DB(SUPABASE_URL, SUPABASE_KEY)
bot = Bot(BOT_TOKEN)
dp = Dispatcher()


# ---------------------------------------------------------------- helpers
def pdt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fmt_dt(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%d.%m %H:%M")


async def notify(chat_id: int, text: str):
    try:
        await bot.send_message(chat_id, text)
    except Exception:
        log.warning("notify failed for %s", chat_id)


async def staff_ids() -> list[int]:
    rows = await db.select("users", {"role": "in.(owner,admin)", "select": "telegram_id"})
    return [r["telegram_id"] for r in rows]


async def ensure_user(tid: int, name: str) -> dict:
    rows = await db.select("users", {"telegram_id": f"eq.{tid}"})
    if rows:
        u = rows[0]
    else:
        u = (await db.insert("users", {"telegram_id": tid, "name": name, "role": "client"}))[0]
    if tid == OWNER_ID and u["role"] != "owner":
        u = (await db.update("users", {"telegram_id": f"eq.{tid}"}, {"role": "owner"}))[0]
    return u


# ---------------------------------------------------------------- Telegram initData auth
def verify_init_data(init_data: str) -> dict:
    if not init_data:
        raise HTTPException(401, "Telegram orqali oching")
    data = dict(parse_qsl(init_data, keep_blank_values=True))
    recv_hash = data.pop("hash", None)
    if not recv_hash:
        raise HTTPException(401, "Noto'g'ri so'rov")
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, recv_hash):
        raise HTTPException(401, "Noto'g'ri imzo")
    if time.time() - int(data.get("auth_date", 0)) > 86400:
        raise HTTPException(401, "Sessiya eskirgan, ilovani qayta oching")
    return json.loads(data["user"])


async def get_user(x_init_data: str = Header(default="")) -> dict:
    tg = verify_init_data(x_init_data)
    name = f"{tg.get('first_name', '')} {tg.get('last_name', '')}".strip()
    return await ensure_user(tg["id"], name)


async def admin_user(u: dict = Depends(get_user)) -> dict:
    if u["role"] not in ("owner", "admin"):
        raise HTTPException(403, "Ruxsat yo'q")
    return u


async def owner_user(u: dict = Depends(get_user)) -> dict:
    if u["role"] != "owner":
        raise HTTPException(403, "Faqat egasi uchun")
    return u


# ---------------------------------------------------------------- slots
async def get_service(sid: int, active_only: bool = True) -> dict:
    rows = await db.select("services", {"id": f"eq.{sid}"})
    if not rows or (active_only and not rows[0]["active"]):
        raise HTTPException(404, "Xizmat topilmadi")
    return rows[0]


async def compute_slots(d: date, service: dict) -> list[str]:
    wh = await db.select("work_hours", {"weekday": f"eq.{d.weekday()}"})
    if not wh or wh[0]["is_off"] or not wh[0]["start_time"] or not wh[0]["end_time"]:
        return []
    st = dtime.fromisoformat(wh[0]["start_time"])
    en = dtime.fromisoformat(wh[0]["end_time"])
    day_start = datetime.combine(d, st, TZ)
    day_end = datetime.combine(d, en, TZ)
    dur = timedelta(minutes=service["duration_min"])

    booked = await db.select("bookings", [
        ("status", "eq.booked"),
        ("start_at", f"lt.{day_end.isoformat()}"),
        ("end_at", f"gt.{day_start.isoformat()}"),
    ])
    busy = [(pdt(b["start_at"]), pdt(b["end_at"])) for b in booked]

    earliest = datetime.now(TZ) + timedelta(minutes=30)
    out, cur = [], day_start
    while cur + dur <= day_end:
        if cur >= earliest and all(not (cur < be and cur + dur > bs) for bs, be in busy):
            out.append(cur.isoformat())
        cur += timedelta(minutes=SLOT_STEP)
    return out


# ---------------------------------------------------------------- models
class BookingIn(BaseModel):
    service_id: int
    start_at: str


class StatusIn(BaseModel):
    status: str


class ServiceIn(BaseModel):
    name: str
    price: int
    duration_min: int


class ServicePatch(BaseModel):
    name: Optional[str] = None
    price: Optional[int] = None
    duration_min: Optional[int] = None
    active: Optional[bool] = None


class HoursIn(BaseModel):
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    is_off: bool = False


# ---------------------------------------------------------------- Telegram bot handlers
def main_keyboard() -> Optional[InlineKeyboardMarkup]:
    if not WEBAPP_URL:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✂️ Yozilish", web_app=WebAppInfo(url=WEBAPP_URL))
    ]])


@dp.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject):
    u = await ensure_user(m.from_user.id, m.from_user.full_name)

    arg = command.args or ""
    if arg.startswith("adm_") and u["role"] == "client":
        code = arg[4:]
        used = await db.update("invites", {"code": f"eq.{code}", "used": "eq.false"}, {"used": True})
        if used:
            u = (await db.update("users", {"telegram_id": f"eq.{u['telegram_id']}"}, {"role": "admin"}))[0]
            await m.answer("Siz admin bo'ldingiz ✅")
        else:
            await m.answer("Bu havola eskirgan yoki ishlatilgan.")

    await m.answer("Salom! Barberga yozilish uchun pastdagi tugmani bosing 👇", reply_markup=main_keyboard())
    if not u.get("phone"):
        kb = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📱 Raqamni yuborish", request_contact=True)]],
            resize_keyboard=True, one_time_keyboard=True,
        )
        await m.answer("Barber siz bilan bog'lana olishi uchun telefon raqamingizni yuboring:", reply_markup=kb)


@dp.message(F.contact)
async def on_contact(m: Message):
    c = m.contact
    if c.user_id != m.from_user.id:
        await m.answer("Iltimos, o'zingizning raqamingizni yuboring.")
        return
    await ensure_user(m.from_user.id, m.from_user.full_name)
    await db.update("users", {"telegram_id": f"eq.{m.from_user.id}"}, {"phone": c.phone_number})
    await m.answer("Rahmat, raqam saqlandi ✅", reply_markup=main_keyboard())


# ---------------------------------------------------------------- reminders
async def reminder_loop():
    while True:
        try:
            now = datetime.now(timezone.utc)
            rows = await db.select("bookings", [
                ("status", "eq.booked"),
                ("reminded", "eq.false"),
                ("start_at", f"gt.{now.isoformat()}"),
                ("start_at", f"lte.{(now + timedelta(hours=2)).isoformat()}"),
                ("select", "*,services(name)"),
            ])
            for r in rows:
                await notify(
                    r["client_id"],
                    f"⏰ Eslatma: soat {pdt(r['start_at']).astimezone(TZ).strftime('%H:%M')} da "
                    f"«{r['services']['name']}» uchun yozilgansiz.",
                )
                await db.update("bookings", {"id": f"eq.{r['id']}"}, {"reminded": True})
        except Exception:
            log.exception("reminder loop error")
        await asyncio.sleep(60)


# ---------------------------------------------------------------- app
@asynccontextmanager
async def lifespan(app: FastAPI):
    global BOT_USERNAME
    BOT_USERNAME = (await bot.get_me()).username
    if BASE_URL:
        await bot.set_webhook(
            f"{BASE_URL}/webhook/{WEBHOOK_SECRET}", secret_token=WEBHOOK_SECRET,
        )
    if WEBAPP_URL:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="Yozilish", web_app=WebAppInfo(url=WEBAPP_URL))
        )
    task = asyncio.create_task(reminder_loop())
    yield
    task.cancel()
    await bot.session.close()
    await db.close()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=CORS_ORIGINS, allow_methods=["*"], allow_headers=["*"],
)


@app.exception_handler(DBError)
async def db_error_handler(request: Request, exc: DBError):
    log.error("DB error %s", exc)
    return JSONResponse({"detail": "Server xatosi"}, status_code=500)


@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    return {"ok": True}


@app.post("/webhook/{secret}")
async def webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET or request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        raise HTTPException(403)
    update = Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}


# ---------------------------------------------------------------- client API
@app.get("/api/me")
async def me(u: dict = Depends(get_user)):
    return {"id": u["telegram_id"], "name": u["name"], "role": u["role"], "phone": u.get("phone")}


@app.get("/api/services")
async def services(u: dict = Depends(get_user)):
    return await db.select("services", {"active": "eq.true", "order": "id.asc"})


@app.get("/api/slots")
async def slots(date_: str, service_id: int, u: dict = Depends(get_user)):
    try:
        d = date.fromisoformat(date_)
    except ValueError:
        raise HTTPException(400, "Sana noto'g'ri")
    today = datetime.now(TZ).date()
    if d < today or d > today + timedelta(days=30):
        return []
    svc = await get_service(service_id)
    return await compute_slots(d, svc)


@app.post("/api/bookings")
async def create_booking(b: BookingIn, u: dict = Depends(get_user)):
    svc = await get_service(b.service_id)
    try:
        start = datetime.fromisoformat(b.start_at)
    except ValueError:
        raise HTTPException(400, "Vaqt noto'g'ri")
    if start.tzinfo is None:
        start = start.replace(tzinfo=TZ)
    start = start.astimezone(TZ)

    free = [datetime.fromisoformat(s) for s in await compute_slots(start.date(), svc)]
    if start not in free:
        raise HTTPException(409, "Bu vaqt band yoki mavjud emas")

    now_iso = datetime.now(timezone.utc).isoformat()
    active = await db.select("bookings", [
        ("client_id", f"eq.{u['telegram_id']}"), ("status", "eq.booked"),
        ("start_at", f"gt.{now_iso}"), ("select", "id"),
    ])
    if len(active) >= MAX_ACTIVE_BOOKINGS:
        raise HTTPException(400, f"Bir vaqtda {MAX_ACTIVE_BOOKINGS} tadan ortiq yozilib bo'lmaydi")

    end = start + timedelta(minutes=svc["duration_min"])
    try:
        rows = await db.insert("bookings", {
            "client_id": u["telegram_id"], "service_id": svc["id"],
            "start_at": start.isoformat(), "end_at": end.isoformat(),
            "reminded": (start - datetime.now(TZ)) <= timedelta(hours=2),
        })
    except DBError as e:
        if "23P01" in e.body:  # exclusion constraint: slot taken at the same moment
            raise HTTPException(409, "Bu vaqt hozirgina band bo'ldi")
        raise

    text = (f"🆕 Yangi yozilish\n{u['name']} — {svc['name']}\n{fmt_dt(start)}"
            + (f"\n📞 {u['phone']}" if u.get("phone") else ""))
    for sid in await staff_ids():
        await notify(sid, text)
    return rows[0]


@app.get("/api/my-bookings")
async def my_bookings(u: dict = Depends(get_user)):
    now_iso = datetime.now(timezone.utc).isoformat()
    return await db.select("bookings", [
        ("client_id", f"eq.{u['telegram_id']}"), ("status", "eq.booked"),
        ("start_at", f"gt.{now_iso}"), ("order", "start_at.asc"),
        ("select", "*,services(name,price)"),
    ])


@app.post("/api/bookings/{bid}/cancel")
async def cancel_booking(bid: int, u: dict = Depends(get_user)):
    rows = await db.select("bookings", [
        ("id", f"eq.{bid}"), ("select", "*,services(name),users(name)"),
    ])
    if not rows:
        raise HTTPException(404, "Topilmadi")
    b = rows[0]
    staff = u["role"] in ("owner", "admin")
    if b["client_id"] != u["telegram_id"] and not staff:
        raise HTTPException(403, "Ruxsat yo'q")
    if b["status"] != "booked":
        raise HTTPException(400, "Bu yozilishni bekor qilib bo'lmaydi")
    if not staff and pdt(b["start_at"]) < datetime.now(timezone.utc):
        raise HTTPException(400, "Vaqti o'tib ketgan")

    await db.update("bookings", {"id": f"eq.{bid}"}, {"status": "cancelled"})
    when = fmt_dt(pdt(b["start_at"]))
    if b["client_id"] == u["telegram_id"]:
        for sid in await staff_ids():
            await notify(sid, f"❌ Bekor qilindi: {b['users']['name']} — {b['services']['name']}, {when}")
    else:
        await notify(b["client_id"], f"❌ {when} dagi yozilishingiz bekor qilindi.")
    return {"ok": True}


# ---------------------------------------------------------------- admin API
@app.get("/api/admin/bookings")
async def admin_bookings(date_: str, a: dict = Depends(admin_user)):
    try:
        d = date.fromisoformat(date_)
    except ValueError:
        raise HTTPException(400, "Sana noto'g'ri")
    s = datetime.combine(d, dtime(0, 0), TZ)
    e = s + timedelta(days=1)
    return await db.select("bookings", [
        ("start_at", f"gte.{s.isoformat()}"), ("start_at", f"lt.{e.isoformat()}"),
        ("order", "start_at.asc"), ("select", "*,services(name,price),users(name,phone)"),
    ])


@app.post("/api/admin/bookings/{bid}/status")
async def admin_set_status(bid: int, body: StatusIn, a: dict = Depends(admin_user)):
    if body.status not in ("done", "no_show", "cancelled"):
        raise HTTPException(400, "Noto'g'ri holat")
    rows = await db.update("bookings", {"id": f"eq.{bid}"}, {"status": body.status})
    if not rows:
        raise HTTPException(404, "Topilmadi")
    if body.status == "cancelled":
        await notify(rows[0]["client_id"], f"❌ {fmt_dt(pdt(rows[0]['start_at']))} dagi yozilishingiz bekor qilindi.")
    return rows[0]


@app.get("/api/admin/report")
async def admin_report(period: str = "day", a: dict = Depends(admin_user)):
    now = datetime.now(TZ)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "month":
        start = start.replace(day=1)
        end = (start + timedelta(days=32)).replace(day=1)
    else:
        end = start + timedelta(days=1)
    rows = await db.select("bookings", [
        ("start_at", f"gte.{start.isoformat()}"), ("start_at", f"lt.{end.isoformat()}"),
        ("select", "status,services(name,price)"),
    ])
    counts = {"booked": 0, "done": 0, "no_show": 0, "cancelled": 0}
    revenue, by_service = 0, {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        if r["status"] == "done":
            revenue += r["services"]["price"]
            n = r["services"]["name"]
            by_service[n] = by_service.get(n, 0) + 1
    top = sorted(by_service.items(), key=lambda x: -x[1])
    return {"period": period, "revenue": revenue, "counts": counts,
            "by_service": [{"name": n, "count": c} for n, c in top]}


@app.get("/api/admin/services")
async def admin_services(a: dict = Depends(admin_user)):
    return await db.select("services", {"order": "id.asc"})


@app.post("/api/admin/services")
async def admin_add_service(body: ServiceIn, a: dict = Depends(admin_user)):
    return (await db.insert("services", body.model_dump()))[0]


@app.patch("/api/admin/services/{sid}")
async def admin_edit_service(sid: int, body: ServicePatch, a: dict = Depends(admin_user)):
    data = body.model_dump(exclude_none=True)
    if not data:
        raise HTTPException(400, "O'zgartirish yo'q")
    rows = await db.update("services", {"id": f"eq.{sid}"}, data)
    if not rows:
        raise HTTPException(404, "Topilmadi")
    return rows[0]


@app.get("/api/admin/work-hours")
async def admin_hours(a: dict = Depends(admin_user)):
    return await db.select("work_hours", {"order": "weekday.asc"})


@app.put("/api/admin/work-hours/{weekday}")
async def admin_set_hours(weekday: int, body: HoursIn, a: dict = Depends(admin_user)):
    if not 0 <= weekday <= 6:
        raise HTTPException(400, "Kun noto'g'ri")
    data = ({"start_time": None, "end_time": None, "is_off": True} if body.is_off
            else {"start_time": body.start_time, "end_time": body.end_time, "is_off": False})
    if not body.is_off and not (body.start_time and body.end_time):
        raise HTTPException(400, "Boshlanish va tugash vaqtini kiriting")
    rows = await db.update("work_hours", {"weekday": f"eq.{weekday}"}, data)
    return rows[0] if rows else {}


# ---- owner only: admins management
@app.post("/api/admin/invite")
async def create_invite(o: dict = Depends(owner_user)):
    code = secrets.token_urlsafe(8)
    await db.insert("invites", {"code": code})
    return {"link": f"https://t.me/{BOT_USERNAME}?start=adm_{code}"}


@app.get("/api/admin/admins")
async def list_admins(o: dict = Depends(owner_user)):
    return await db.select("users", {"role": "eq.admin", "select": "telegram_id,name,phone"})


@app.delete("/api/admin/admins/{tid}")
async def remove_admin(tid: int, o: dict = Depends(owner_user)):
    rows = await db.update("users", {"telegram_id": f"eq.{tid}", "role": "eq.admin"}, {"role": "client"})
    if not rows:
        raise HTTPException(404, "Admin topilmadi")
    return {"ok": True}
