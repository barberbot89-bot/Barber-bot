import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, time as dtime, timedelta, timezone
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
from fastapi.responses import JSONResponse, Response
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

# ---------------------------------------------------------------- bot messages (uz / ru)
MSG = {
    "uz": {
        "welcome": "Salom! Barberga yozilish uchun pastdagi tugmani bosing 👇",
        "btn_book": "✂️ Yozilish",
        "share_phone": "Barber siz bilan bog'lana olishi uchun telefon raqamingizni yuboring:",
        "btn_phone": "📱 Raqamni yuborish",
        "phone_saved": "Rahmat, raqam saqlandi ✅",
        "own_phone": "Iltimos, o'zingizning raqamingizni yuboring.",
        "admin_now": "Siz admin bo'ldingiz ✅",
        "invite_bad": "Bu havola eskirgan yoki ishlatilgan.",
        "reminder": "⏰ Eslatma: soat {t} da «{svc}» uchun yozilgansiz.\n📍 {branch}",
        "cancelled_by_staff": "❌ {when} dagi yozilishingiz bekor qilindi.",
        "staff_new": "🆕 Yangi yozilish\n{name} — {svc}\n📍 {branch}\n{when}{phone}",
        "staff_cancelled": "❌ Bekor qilindi: {name} — {svc}, {branch}, {when}",
    },
    "ru": {
        "welcome": "Здравствуйте! Нажмите кнопку ниже, чтобы записаться 👇",
        "btn_book": "✂️ Записаться",
        "share_phone": "Отправьте номер телефона, чтобы мастер мог с вами связаться:",
        "btn_phone": "📱 Отправить номер",
        "phone_saved": "Спасибо, номер сохранён ✅",
        "own_phone": "Пожалуйста, отправьте свой номер.",
        "admin_now": "Теперь вы администратор ✅",
        "invite_bad": "Ссылка устарела или уже использована.",
        "reminder": "⏰ Напоминание: в {t} у вас запись «{svc}».\n📍 {branch}",
        "cancelled_by_staff": "❌ Ваша запись на {when} отменена.",
        "staff_new": "🆕 Новая запись\n{name} — {svc}\n📍 {branch}\n{when}{phone}",
        "staff_cancelled": "❌ Отменено: {name} — {svc}, {branch}, {when}",
    },
}


def tr(lang: Optional[str], key: str, **kw) -> str:
    return (MSG.get(lang or "uz") or MSG["uz"])[key].format(**kw)


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
        self.c = httpx.AsyncClient(base_url=f"{url}/rest/v1", headers=headers, timeout=20)

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


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def fmt_dt(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%d.%m %H:%M")


async def notify(chat_id: int, text: str):
    try:
        await bot.send_message(chat_id, text)
    except Exception:
        log.warning("notify failed for %s", chat_id)


async def staff_users() -> list[dict]:
    return await db.select("users", {"role": "in.(owner,admin)", "select": "telegram_id,lang"})


async def notify_staff(key: str, **kw):
    for s in await staff_users():
        await notify(s["telegram_id"], tr(s.get("lang"), key, **kw))


async def ensure_user(tid: int, name: str, lang_hint: Optional[str] = None) -> dict:
    rows = await db.select("users", {"telegram_id": f"eq.{tid}"})
    if rows:
        u = rows[0]
    else:
        lang = "ru" if (lang_hint or "").startswith("ru") else "uz"
        u = (await db.insert("users", {"telegram_id": tid, "name": name, "role": "client", "lang": lang}))[0]
    if tid == OWNER_ID and u["role"] != "owner":
        u = (await db.update("users", {"telegram_id": f"eq.{tid}"}, {"role": "owner"}))[0]
    return u


def img_url(path: str, ver: int) -> Optional[str]:
    return f"{path}?v={ver}" if ver else None


def branch_out(r: dict) -> dict:
    return {
        "id": r["id"], "name": r["name"], "address": r.get("address"),
        "lat": r.get("lat"), "lng": r.get("lng"), "active": r.get("active", True),
        "photo_url": img_url(f"/api/img/branch/{r['id']}", r.get("photo_ver") or 0),
    }


BRANCH_COLS = "id,name,address,lat,lng,active,photo_ver"
SHOP_COLS = "name,logo_ver,desc_uz,desc_ru,address,phone,lat,lng"


def shop_out(r: dict) -> dict:
    return {
        "name": r["name"], "desc_uz": r.get("desc_uz"), "desc_ru": r.get("desc_ru"),
        "address": r.get("address"), "phone": r.get("phone"),
        "lat": r.get("lat"), "lng": r.get("lng"),
        "logo_url": img_url("/api/img/logo", r.get("logo_ver") or 0),
    }


def check_coords(data: dict):
    for k, lim in (("lat", 90), ("lng", 180)):
        v = data.get(k)
        if v is not None and not -lim <= v <= lim:
            raise HTTPException(400, "Koordinata noto'g'ri")
    if (data.get("lat") is None) != (data.get("lng") is None) and ("lat" in data and "lng" in data):
        raise HTTPException(400, "Koordinata noto'g'ri")


def check_image(data: str):
    if not data.startswith(("data:image/jpeg;base64,", "data:image/png;base64,", "data:image/webp;base64,")):
        raise HTTPException(400, "Rasm formati noto'g'ri")
    if len(data) > 900_000:
        raise HTTPException(400, "Rasm juda katta")
    try:
        base64.b64decode(data.split(",", 1)[1], validate=True)
    except Exception:
        raise HTTPException(400, "Rasm buzilgan")


def image_response(data_url: Optional[str]) -> Response:
    if not data_url:
        raise HTTPException(404)
    head, b64 = data_url.split(",", 1)
    mime = head[5:].split(";")[0]
    return Response(
        base64.b64decode(b64), media_type=mime,
        headers={"Cache-Control": "public, max-age=604800"},
    )


# ---------------------------------------------------------------- Telegram initData auth
def verify_init_data(init_data: str) -> dict:
    if not init_data:
        raise HTTPException(401, "auth_required")
    data = dict(parse_qsl(init_data, keep_blank_values=True))
    recv_hash = data.pop("hash", None)
    if not recv_hash:
        raise HTTPException(401, "auth_invalid")
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, recv_hash):
        raise HTTPException(401, "auth_invalid")
    if time.time() - int(data.get("auth_date", 0)) > 86400:
        raise HTTPException(401, "auth_expired")
    return json.loads(data["user"])


async def get_user(x_init_data: str = Header(default="")) -> dict:
    tg = verify_init_data(x_init_data)
    name = f"{tg.get('first_name', '')} {tg.get('last_name', '')}".strip()
    return await ensure_user(tg["id"], name, tg.get("language_code"))


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
        raise HTTPException(404, "not_found")
    return rows[0]


async def get_branch(bid: int, active_only: bool = True) -> dict:
    rows = await db.select("branches", {"id": f"eq.{bid}", "select": BRANCH_COLS})
    if not rows or (active_only and not rows[0]["active"]):
        raise HTTPException(404, "not_found")
    return rows[0]


async def compute_slots(d: date, service: dict, branch_id: int) -> list[str]:
    wh = await db.select("work_hours", {"branch_id": f"eq.{branch_id}", "weekday": f"eq.{d.weekday()}"})
    if not wh or wh[0]["is_off"] or not wh[0]["start_time"] or not wh[0]["end_time"]:
        return []
    st = dtime.fromisoformat(wh[0]["start_time"])
    en = dtime.fromisoformat(wh[0]["end_time"])
    day_start = datetime.combine(d, st, TZ)
    day_end = datetime.combine(d, en, TZ)
    dur = timedelta(minutes=service["duration_min"])

    booked = await db.select("bookings", [
        ("branch_id", f"eq.{branch_id}"),
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
    branch_id: int
    service_id: int
    start_at: str


class LangIn(BaseModel):
    lang: str


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


class BranchIn(BaseModel):
    name: str
    address: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None


class BranchPatch(BaseModel):
    name: Optional[str] = None
    address: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    active: Optional[bool] = None


class ShopPatch(BaseModel):
    name: Optional[str] = None
    desc_uz: Optional[str] = None
    desc_ru: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None


class ImageIn(BaseModel):
    data: str


# ---------------------------------------------------------------- Telegram bot handlers
def main_keyboard(lang: str) -> Optional[InlineKeyboardMarkup]:
    if not WEBAPP_URL:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=tr(lang, "btn_book"), web_app=WebAppInfo(url=WEBAPP_URL))
    ]])


@dp.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject):
    u = await ensure_user(m.from_user.id, m.from_user.full_name, m.from_user.language_code)

    arg = command.args or ""
    if arg.startswith("adm_") and u["role"] == "client":
        code = arg[4:]
        used = await db.update("invites", {"code": f"eq.{code}", "used": "eq.false"}, {"used": True})
        if used:
            u = (await db.update("users", {"telegram_id": f"eq.{u['telegram_id']}"}, {"role": "admin"}))[0]
            await m.answer(tr(u.get("lang"), "admin_now"))
        else:
            await m.answer(tr(u.get("lang"), "invite_bad"))

    lang = u.get("lang") or "uz"
    await m.answer(tr(lang, "welcome"), reply_markup=main_keyboard(lang))
    if not u.get("phone"):
        kb = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=tr(lang, "btn_phone"), request_contact=True)]],
            resize_keyboard=True, one_time_keyboard=True,
        )
        await m.answer(tr(lang, "share_phone"), reply_markup=kb)


@dp.message(F.contact)
async def on_contact(m: Message):
    u = await ensure_user(m.from_user.id, m.from_user.full_name, m.from_user.language_code)
    lang = u.get("lang") or "uz"
    c = m.contact
    if c.user_id != m.from_user.id:
        await m.answer(tr(lang, "own_phone"))
        return
    await db.update("users", {"telegram_id": f"eq.{m.from_user.id}"}, {"phone": c.phone_number})
    await m.answer(tr(lang, "phone_saved"), reply_markup=main_keyboard(lang))


# ---------------------------------------------------------------- reminders
async def reminder_loop():
    while True:
        try:
            now = datetime.now(timezone.utc).replace(microsecond=0)
            rows = await db.select("bookings", [
                ("status", "eq.booked"),
                ("reminded", "eq.false"),
                ("start_at", f"gt.{now.isoformat()}"),
                ("start_at", f"lte.{(now + timedelta(hours=2)).isoformat()}"),
                ("select", "*,services(name),branches(name),users(lang)"),
            ])
            for r in rows:
                await notify(r["client_id"], tr(
                    (r.get("users") or {}).get("lang"), "reminder",
                    t=pdt(r["start_at"]).astimezone(TZ).strftime("%H:%M"),
                    svc=r["services"]["name"], branch=(r.get("branches") or {}).get("name", ""),
                ))
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
        await bot.set_webhook(f"{BASE_URL}/webhook/{WEBHOOK_SECRET}", secret_token=WEBHOOK_SECRET)
    if WEBAPP_URL:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="✂️ Yozilish", web_app=WebAppInfo(url=WEBAPP_URL))
        )
    task = asyncio.create_task(reminder_loop())
    yield
    task.cancel()
    await bot.session.close()
    await db.close()


app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_methods=["*"], allow_headers=["*"])


@app.exception_handler(DBError)
async def db_error_handler(request: Request, exc: DBError):
    log.error("DB error %s", exc)
    return JSONResponse({"detail": "server_error"}, status_code=500)


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


# ---- public images (no auth: they are shop photos)
@app.get("/api/img/logo")
async def img_logo():
    rows = await db.select("shop", {"id": "eq.1", "select": "logo_data"})
    return image_response(rows[0]["logo_data"] if rows else None)


@app.get("/api/img/branch/{bid}")
async def img_branch(bid: int):
    rows = await db.select("branches", {"id": f"eq.{bid}", "select": "photo_data"})
    return image_response(rows[0]["photo_data"] if rows else None)


# ---------------------------------------------------------------- client API
@app.get("/api/bootstrap")
async def bootstrap(u: dict = Depends(get_user)):
    shop, branches, services = await asyncio.gather(
        db.select("shop", {"id": "eq.1", "select": SHOP_COLS}),
        db.select("branches", {"active": "eq.true", "order": "id.asc", "select": BRANCH_COLS}),
        db.select("services", {"active": "eq.true", "order": "id.asc"}),
    )
    return {
        "me": {"id": u["telegram_id"], "name": u["name"], "role": u["role"],
               "phone": u.get("phone"), "lang": u.get("lang") or "uz"},
        "shop": shop_out(shop[0]) if shop else shop_out({"name": "Barbershop"}),
        "branches": [branch_out(b) for b in branches],
        "services": services,
    }


@app.post("/api/me/lang")
async def set_lang(body: LangIn, u: dict = Depends(get_user)):
    if body.lang not in ("uz", "ru"):
        raise HTTPException(400, "bad_lang")
    await db.update("users", {"telegram_id": f"eq.{u['telegram_id']}"}, {"lang": body.lang})
    return {"ok": True}


@app.get("/api/services")
async def services(u: dict = Depends(get_user)):
    return await db.select("services", {"active": "eq.true", "order": "id.asc"})


@app.get("/api/slots")
async def slots(date_: str, service_id: int, branch_id: int, u: dict = Depends(get_user)):
    try:
        d = date.fromisoformat(date_)
    except ValueError:
        raise HTTPException(400, "bad_date")
    today = datetime.now(TZ).date()
    if d < today or d > today + timedelta(days=30):
        return []
    svc = await get_service(service_id)
    await get_branch(branch_id)
    return await compute_slots(d, svc, branch_id)


@app.post("/api/bookings")
async def create_booking(b: BookingIn, u: dict = Depends(get_user)):
    svc = await get_service(b.service_id)
    br = await get_branch(b.branch_id)
    try:
        start = datetime.fromisoformat(b.start_at)
    except ValueError:
        raise HTTPException(400, "bad_time")
    if start.tzinfo is None:
        start = start.replace(tzinfo=TZ)
    start = start.astimezone(TZ)

    free = [datetime.fromisoformat(s) for s in await compute_slots(start.date(), svc, br["id"])]
    if start not in free:
        raise HTTPException(409, "slot_unavailable")

    active = await db.select("bookings", [
        ("client_id", f"eq.{u['telegram_id']}"), ("status", "eq.booked"),
        ("start_at", f"gt.{now_utc_iso()}"), ("select", "id"),
    ])
    if len(active) >= MAX_ACTIVE_BOOKINGS:
        raise HTTPException(400, "too_many")

    end = start + timedelta(minutes=svc["duration_min"])
    try:
        rows = await db.insert("bookings", {
            "client_id": u["telegram_id"], "service_id": svc["id"], "branch_id": br["id"],
            "start_at": start.isoformat(), "end_at": end.isoformat(),
            "reminded": (start - datetime.now(TZ)) <= timedelta(hours=2),
        })
    except DBError as e:
        if "23P01" in e.body:  # exclusion constraint: slot taken at the same moment
            raise HTTPException(409, "slot_taken")
        raise

    await notify_staff(
        "staff_new", name=u["name"], svc=svc["name"], branch=br["name"], when=fmt_dt(start),
        phone=(f"\n📞 {u['phone']}" if u.get("phone") else ""),
    )
    return rows[0]


@app.get("/api/my-bookings")
async def my_bookings(scope: str = "active", u: dict = Depends(get_user)):
    params = [
        ("client_id", f"eq.{u['telegram_id']}"),
        ("select", "*,services(name,price),branches(name,address,lat,lng)"),
    ]
    now_iso = now_utc_iso()
    if scope == "history":
        params += [("or", f"(status.neq.booked,end_at.lte.{now_iso})"),
                   ("order", "start_at.desc"), ("limit", "50")]
    else:
        params += [("status", "eq.booked"), ("end_at", f"gt.{now_iso}"), ("order", "start_at.asc")]
    return await db.select("bookings", params)


@app.post("/api/bookings/{bid}/cancel")
async def cancel_booking(bid: int, u: dict = Depends(get_user)):
    rows = await db.select("bookings", [
        ("id", f"eq.{bid}"), ("select", "*,services(name),branches(name),users(name,lang)"),
    ])
    if not rows:
        raise HTTPException(404, "not_found")
    b = rows[0]
    staff = u["role"] in ("owner", "admin")
    if b["client_id"] != u["telegram_id"] and not staff:
        raise HTTPException(403, "forbidden")
    if b["status"] != "booked":
        raise HTTPException(400, "cannot_cancel")
    if not staff and pdt(b["start_at"]) < datetime.now(timezone.utc):
        raise HTTPException(400, "too_late")

    await db.update("bookings", {"id": f"eq.{bid}"}, {"status": "cancelled"})
    when = fmt_dt(pdt(b["start_at"]))
    if b["client_id"] == u["telegram_id"]:
        await notify_staff("staff_cancelled", name=b["users"]["name"], svc=b["services"]["name"],
                           branch=b["branches"]["name"], when=when)
    else:
        await notify(b["client_id"], tr(b["users"].get("lang"), "cancelled_by_staff", when=when))
    return {"ok": True}


# ---------------------------------------------------------------- admin API
@app.get("/api/admin/bookings")
async def admin_bookings(date_: str, branch_id: int = 0, a: dict = Depends(admin_user)):
    try:
        d = date.fromisoformat(date_)
    except ValueError:
        raise HTTPException(400, "Sana noto'g'ri")
    s = datetime.combine(d, dtime(0, 0), TZ)
    e = s + timedelta(days=1)
    params = [
        ("start_at", f"gte.{s.isoformat()}"), ("start_at", f"lt.{e.isoformat()}"),
        ("order", "start_at.asc"), ("select", "*,services(name,price),users(name,phone),branches(name)"),
    ]
    if branch_id:
        params.append(("branch_id", f"eq.{branch_id}"))
    return await db.select("bookings", params)


@app.post("/api/admin/bookings/{bid}/status")
async def admin_set_status(bid: int, body: StatusIn, a: dict = Depends(admin_user)):
    if body.status not in ("done", "no_show", "cancelled"):
        raise HTTPException(400, "Noto'g'ri holat")
    cur = await db.select("bookings", [("id", f"eq.{bid}"), ("select", "*,users(lang)")])
    if not cur:
        raise HTTPException(404, "Topilmadi")
    rows = await db.update("bookings", {"id": f"eq.{bid}"}, {"status": body.status})
    if body.status == "cancelled" and cur[0]["status"] == "booked":
        await notify(cur[0]["client_id"], tr((cur[0].get("users") or {}).get("lang"), "cancelled_by_staff",
                                             when=fmt_dt(pdt(cur[0]["start_at"]))))
    return rows[0]


@app.get("/api/admin/report")
async def admin_report(period: str = "day", branch_id: int = 0, a: dict = Depends(admin_user)):
    now = datetime.now(TZ)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "month":
        start = start.replace(day=1)
        end = (start + timedelta(days=32)).replace(day=1)
    else:
        end = start + timedelta(days=1)
    params = [
        ("start_at", f"gte.{start.isoformat()}"), ("start_at", f"lt.{end.isoformat()}"),
        ("select", "status,services(name,price)"),
    ]
    if branch_id:
        params.append(("branch_id", f"eq.{branch_id}"))
    rows = await db.select("bookings", params)
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


# ---- services
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


# ---- shop info
@app.get("/api/admin/shop")
async def admin_get_shop(a: dict = Depends(admin_user)):
    rows = await db.select("shop", {"id": "eq.1", "select": SHOP_COLS})
    return shop_out(rows[0])


@app.patch("/api/admin/shop")
async def admin_edit_shop(body: ShopPatch, a: dict = Depends(admin_user)):
    data = body.model_dump(exclude_unset=True)
    if "name" in data and not (data["name"] or "").strip():
        raise HTTPException(400, "Nomini kiriting")
    check_coords(data)
    if not data:
        raise HTTPException(400, "O'zgartirish yo'q")
    rows = await db.update("shop", {"id": "eq.1"}, data)
    return shop_out((await db.select("shop", {"id": "eq.1", "select": SHOP_COLS}))[0]) if rows else {}


@app.put("/api/admin/shop/logo")
async def admin_set_logo(body: ImageIn, a: dict = Depends(admin_user)):
    check_image(body.data)
    cur = await db.select("shop", {"id": "eq.1", "select": "logo_ver"})
    ver = (cur[0]["logo_ver"] if cur else 0) + 1
    await db.update("shop", {"id": "eq.1"}, {"logo_data": body.data, "logo_ver": ver})
    return {"logo_url": img_url("/api/img/logo", ver)}


# ---- branches
@app.get("/api/admin/branches")
async def admin_branches(a: dict = Depends(admin_user)):
    rows = await db.select("branches", {"order": "id.asc", "select": BRANCH_COLS})
    return [branch_out(r) for r in rows]


@app.post("/api/admin/branches")
async def admin_add_branch(body: BranchIn, a: dict = Depends(admin_user)):
    data = body.model_dump()
    if not data["name"].strip():
        raise HTTPException(400, "Nomini kiriting")
    check_coords(data)
    row = (await db.insert("branches", data))[0]
    hours = [{"branch_id": row["id"], "weekday": w, "start_time": "09:00", "end_time": "20:00", "is_off": False}
             for w in range(6)]
    hours.append({"branch_id": row["id"], "weekday": 6, "start_time": None, "end_time": None, "is_off": True})
    await db.insert("work_hours", hours)
    return branch_out(row)


@app.patch("/api/admin/branches/{bid}")
async def admin_edit_branch(bid: int, body: BranchPatch, a: dict = Depends(admin_user)):
    data = body.model_dump(exclude_unset=True)
    if "name" in data and not (data["name"] or "").strip():
        raise HTTPException(400, "Nomini kiriting")
    if data.get("active") is None:
        data.pop("active", None)
    check_coords(data)
    if not data:
        raise HTTPException(400, "O'zgartirish yo'q")
    rows = await db.update("branches", {"id": f"eq.{bid}"}, data)
    if not rows:
        raise HTTPException(404, "Topilmadi")
    return branch_out(rows[0])


@app.put("/api/admin/branches/{bid}/photo")
async def admin_branch_photo(bid: int, body: ImageIn, a: dict = Depends(admin_user)):
    check_image(body.data)
    cur = await db.select("branches", {"id": f"eq.{bid}", "select": "photo_ver"})
    if not cur:
        raise HTTPException(404, "Topilmadi")
    ver = (cur[0]["photo_ver"] or 0) + 1
    await db.update("branches", {"id": f"eq.{bid}"}, {"photo_data": body.data, "photo_ver": ver})
    return {"photo_url": img_url(f"/api/img/branch/{bid}", ver)}


# ---- work hours (per branch)
@app.get("/api/admin/work-hours")
async def admin_hours(branch_id: int, a: dict = Depends(admin_user)):
    return await db.select("work_hours", {"branch_id": f"eq.{branch_id}", "order": "weekday.asc"})


@app.put("/api/admin/work-hours/{branch_id}/{weekday}")
async def admin_set_hours(branch_id: int, weekday: int, body: HoursIn, a: dict = Depends(admin_user)):
    if not 0 <= weekday <= 6:
        raise HTTPException(400, "Kun noto'g'ri")
    if not body.is_off and not (body.start_time and body.end_time):
        raise HTTPException(400, "Boshlanish va tugash vaqtini kiriting")
    data = ({"start_time": None, "end_time": None, "is_off": True} if body.is_off
            else {"start_time": body.start_time, "end_time": body.end_time, "is_off": False})
    rows = await db.update("work_hours", {"branch_id": f"eq.{branch_id}", "weekday": f"eq.{weekday}"}, data)
    if not rows:
        rows = await db.insert("work_hours", {"branch_id": branch_id, "weekday": weekday, **data})
    return rows[0]


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
