import os
import io
import sqlite3
import asyncio
from datetime import datetime, timedelta

import qrcode
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    BufferedInputFile
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "honar_time.db")
HOLD_MINUTES = max(1, int(os.getenv("HOLD_MINUTES", "10")))
ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

KINDS = {
    "سینما": "🎬",
    "تئاتر": "🎭",
    "موسیقی": "🎵",
    "فرهنگی": "🎤",
    "سایر": "🎨",
}

def db():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c

def init_db():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        title TEXT NOT NULL,
        description TEXT DEFAULT '',
        poster_file_id TEXT DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS shows(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id INTEGER NOT NULL,
        show_at TEXT NOT NULL,
        hall TEXT NOT NULL DEFAULT 'سالن اصلی',
        base_price INTEGER NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        FOREIGN KEY(event_id) REFERENCES events(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS seats(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        show_id INTEGER NOT NULL,
        label TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'free',
        hold_until TEXT,
        UNIQUE(show_id,label),
        FOREIGN KEY(show_id) REFERENCES shows(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS cards(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        number TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS discounts(
        user_id INTEGER PRIMARY KEY,
        percent REAL NOT NULL DEFAULT 0,
        fixed_price INTEGER
    );
    CREATE TABLE IF NOT EXISTS orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        show_id INTEGER NOT NULL,
        seat_id INTEGER NOT NULL,
        amount INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        ticket_code TEXT UNIQUE,
        receipt_file_id TEXT,
        created_at TEXT NOT NULL,
        approved_at TEXT,
        FOREIGN KEY(show_id) REFERENCES shows(id),
        FOREIGN KEY(seat_id) REFERENCES seats(id)
    );
    CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
    CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
    CREATE INDEX IF NOT EXISTS idx_seats_show_status ON seats(show_id,status);
    """)
    c.commit()
    c.close()

def now():
    return datetime.now()

def now_iso():
    return now().isoformat(timespec="seconds")

def money(n):
    return f"{int(n):,} تومان"

def icon(kind):
    return KINDS.get(kind, "🎨")

def clean_holds():
    c = db()
    c.execute(
        "UPDATE seats SET status='free', hold_until=NULL "
        "WHERE status='held' AND hold_until IS NOT NULL AND hold_until < ?",
        (now_iso(),)
    )
    c.commit()
    c.close()

def final_price(user_id, base):
    c = db()
    d = c.execute("SELECT * FROM discounts WHERE user_id=?", (user_id,)).fetchone()
    c.close()
    if not d:
        return int(base)
    if d["fixed_price"] is not None:
        return max(0, int(d["fixed_price"]))
    return max(0, round(int(base) * (1 - float(d["percent"]) / 100)))

def K(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)

def is_admin(uid):
    return uid in ADMIN_IDS

def main_kb(uid):
    rows = [
        [InlineKeyboardButton(text="🎭 رویدادها", callback_data="events")],
        [InlineKeyboardButton(text="🎟 بلیت‌های من", callback_data="mytickets")],
    ]
    if is_admin(uid):
        rows.append([InlineKeyboardButton(text="👨‍💼 پنل مدیریت", callback_data="admin")])
    return K(rows)

def back(data):
    return K([[InlineKeyboardButton(text="⬅️ بازگشت", callback_data=data)]])

async def send_or_edit(target, text, markup=None):
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)

class AdminState(StatesGroup):
    event = State()
    show = State()
    seats = State()
    card = State()
    discount = State()
    poster = State()

async def start(message: Message):
    await message.answer(
        "🎨 <b>کارگزاری به وقت هنر</b>\n\n"
        "فروش بلیت سینما، تئاتر، موسیقی و رویدادهای فرهنگی.\n"
        "رویداد را انتخاب کنید:",
        reply_markup=main_kb(message.from_user.id)
    )

async def events(q: CallbackQuery):
    clean_holds()
    c = db()
    rows = c.execute(
        "SELECT * FROM events WHERE active=1 ORDER BY id DESC"
    ).fetchall()
    c.close()
    buttons = [
        [InlineKeyboardButton(
            text=f"{icon(r['kind'])} {r['title']}",
            callback_data=f"event:{r['id']}"
        )] for r in rows
    ]
    buttons.append([InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="home")])
    await send_or_edit(q, "🎟 <b>رویدادهای فعال</b>" if rows else "فعلاً رویداد فعالی ثبت نشده است.", K(buttons))
    await q.answer()

async def event_detail(q: CallbackQuery):
    eid = int(q.data.split(":")[1])
    c = db()
    e = c.execute("SELECT * FROM events WHERE id=? AND active=1", (eid,)).fetchone()
    shows = c.execute(
        "SELECT * FROM shows WHERE event_id=? AND active=1 ORDER BY show_at",
        (eid,)
    ).fetchall()
    c.close()
    if not e:
        await q.answer("رویداد پیدا نشد.", show_alert=True); return
    text = f"{icon(e['kind'])} <b>{e['title']}</b>\n\n{e['description'] or 'بدون توضیحات'}\n\n🕐 سانس را انتخاب کنید:"
    rows = [
        [InlineKeyboardButton(
            text=f"🕐 {s['show_at']} | {s['hall']} | {money(s['base_price'])}",
            callback_data=f"show:{s['id']}"
        )] for s in shows
    ]
    rows.append([InlineKeyboardButton(text="⬅️ رویدادها", callback_data="events")])
    await send_or_edit(q, text, K(rows))
    await q.answer()

async def show_detail(q: CallbackQuery):
    clean_holds()
    sid = int(q.data.split(":")[1])
    c = db()
    s = c.execute("""
        SELECT s.*, e.title, e.kind, e.event_id
        FROM shows s JOIN events e ON e.id=s.event_id
        WHERE s.id=? AND s.active=1
    """, (sid,)).fetchone()
    seats = c.execute(
        "SELECT * FROM seats WHERE show_id=? ORDER BY id", (sid,)
    ).fetchall() if s else []
    c.close()
    if not s:
        await q.answer("سانس پیدا نشد.", show_alert=True); return
    # سالن: هر ردیف ۱۰ صندلی دارد؛ ۵ صندلی سمت چپ، یک فاصله در وسط، و ۵ صندلی سمت راست.
    # ترتیب شماره‌ها در هر ردیف: ۱ تا ۵ | ۶ تا ۱۰
    marks = {"free":"🟩", "held":"🟨", "pending":"🟧", "sold":"🟥"}
    seat_by_label = {seat["label"]: seat for seat in seats}
    rows = []
    row_numbers = sorted({int(seat["label"].split("-", 1)[0]) for seat in seats if "-" in seat["label"]})

    for r in row_numbers:
        left, right = [], []
        for n in range(1, 11):
            seat = seat_by_label.get(f"{r}-{n}")
            if not seat:
                continue
            if seat["status"] == "free":
                b = InlineKeyboardButton(
                    text=f"🟩 {n}",
                    callback_data=f"seat:{seat['id']}"
                )
            else:
                b = InlineKeyboardButton(
                    text=f"{marks.get(seat['status'],'⬜')} {n}",
                    callback_data="noop"
                )
            (left if n <= 5 else right).append(b)
        # فاصلهٔ وسط، مطابق نقشهٔ سالن
        rows.append(left + [InlineKeyboardButton(text="↔️", callback_data="noop")] + right)
    rows.append([InlineKeyboardButton(text="🔄 به‌روزرسانی", callback_data=f"show:{sid}")])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"event:{s['event_id']}")])
    text = (
        f"{icon(s['kind'])} <b>{s['title']}</b>\n"
        f"🕐 {s['show_at']} | {s['hall']}\n"
        f"💰 قیمت پایه: {money(s['base_price'])}\n\n"
        "🟩 آزاد  🟨 رزرو موقت  🟧 در انتظار تأیید  🟥 فروخته‌شده\n\n"
        "💺 صندلی را انتخاب کنید:"
    )
    await send_or_edit(q, text, K(rows))
    await q.answer()

async def seat_pick(q: CallbackQuery, state: FSMContext):
    clean_holds()
    seat_id = int(q.data.split(":")[1])
    uid = q.from_user.id
    c = db()
    # Atomic reservation prevents two users from taking the same seat.
    until = (now() + timedelta(minutes=HOLD_MINUTES)).isoformat(timespec="seconds")
    cur = c.execute(
        "UPDATE seats SET status='held', hold_until=? "
        "WHERE id=? AND status='free'",
        (until, seat_id)
    )
    if cur.rowcount != 1:
        c.close()
        await q.answer("این صندلی دیگر آزاد نیست.", show_alert=True)
        return
    seat = c.execute("SELECT * FROM seats WHERE id=?", (seat_id,)).fetchone()
    s = c.execute("""
        SELECT s.*, e.title, e.kind
        FROM shows s JOIN events e ON e.id=s.event_id
        WHERE s.id=?
    """, (seat["show_id"],)).fetchone()
    cards = c.execute("SELECT * FROM cards WHERE active=1 ORDER BY id").fetchall()
    c.commit(); c.close()
    amount = final_price(uid, s["base_price"])
    await state.update_data(seat_id=seat_id, show_id=s["id"], amount=amount)
    rows = [
        [InlineKeyboardButton(text=f"💳 {x['title']}", callback_data=f"paycard:{x['id']}")]
        for x in cards
    ]
    rows.append([InlineKeyboardButton(text="❌ لغو", callback_data=f"show:{s['id']}")])
    await send_or_edit(
        q,
        f"🎟 {s['title']}\n💺 صندلی: <b>{seat['label']}</b>\n"
        f"💰 مبلغ نهایی: <b>{money(amount)}</b>\n\n"
        f"صندلی برای {HOLD_MINUTES} دقیقه نگه داشته شد.\nکارت پرداخت را انتخاب کنید:",
        K(rows)
    )
    await q.answer()

async def choose_card(q: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    cid = int(q.data.split(":")[1])
    c = db()
    card = c.execute("SELECT * FROM cards WHERE id=? AND active=1", (cid,)).fetchone()
    c.close()
    if not card or not data:
        await q.answer("رزرو منقضی شده است.", show_alert=True); return
    await state.update_data(waiting_receipt=True)
    await q.message.edit_text(
        f"💳 <b>{card['title']}</b>\n"
        f"شماره کارت: <code>{card['number']}</code>\n\n"
        f"مبلغ قابل پرداخت: <b>{money(data['amount'])}</b>\n\n"
        "بعد از کارت‌به‌کارت، <b>عکس رسید</b> را همینجا ارسال کنید.",
        reply_markup=K([[InlineKeyboardButton(text="❌ لغو", callback_data=f"show:{data['show_id']}")]])
    )
    await q.answer()

def ticket_text(row):
    return (
        "🎟 <b>بلیت قطعی</b>\n\n"
        f"🔑 کد بلیت: <code>{row['ticket_code']}</code>\n"
        f"{icon(row['kind'])} {row['title']}\n"
        f"🕐 {row['show_at']}\n"
        f"🏛 {row['hall']}\n"
        f"💺 صندلی: <b>{row['label']}</b>\n"
        f"💰 مبلغ: {money(row['amount'])}\n\n"
        "QR این بلیت برای کنترل ورودی قابل اسکن است."
    )

def make_qr(code):
    img = qrcode.make(code)
    bio = io.BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    return BufferedInputFile(bio.read(), filename=f"{code}.png")

async def receipt(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    if not data.get("waiting_receipt"):
        return
    if not message.photo:
        await message.answer("لطفاً عکس رسید را ارسال کنید.")
        return
    c = db()
    seat = c.execute(
        "SELECT * FROM seats WHERE id=? AND status='held'", (data["seat_id"],)
    ).fetchone()
    if not seat or (seat["hold_until"] and seat["hold_until"] < now_iso()):
        if seat:
            c.execute("UPDATE seats SET status='free',hold_until=NULL WHERE id=?", (seat["id"],))
            c.commit()
        c.close()
        await state.clear()
        await message.answer("⏰ زمان رزرو تمام شده است. دوباره صندلی انتخاب کنید.", reply_markup=main_kb(message.from_user.id))
        return
    oid = c.execute("""
        INSERT INTO orders(user_id,show_id,seat_id,amount,status,created_at,receipt_file_id)
        VALUES(?,?,?,?,'pending',?,?)
    """, (
        message.from_user.id, data["show_id"], data["seat_id"],
        data["amount"], now_iso(), message.photo[-1].file_id
    )).lastrowid
    c.execute(
        "UPDATE seats SET status='pending',hold_until=NULL WHERE id=?",
        (data["seat_id"],)
    )
    c.commit()
    c.close()
    await state.clear()
    await message.answer(
        f"🧾 رسید سفارش <b>#{oid}</b> دریافت شد.\n"
        "پس از بررسی مدیر، نتیجه و در صورت تأیید بلیت برای شما ارسال می‌شود."
    )
    for aid in ADMIN_IDS:
        try:
            await bot.send_photo(
                aid, message.photo[-1].file_id,
                caption=(
                    f"🧾 <b>رسید جدید</b>\n"
                    f"سفارش: #{oid}\nکاربر: <code>{message.from_user.id}</code>\n"
                    f"مبلغ: {money(data['amount'])}"
                ),
                reply_markup=K([[
                    InlineKeyboardButton(text="✅ تأیید", callback_data=f"approve:{oid}"),
                    InlineKeyboardButton(text="❌ رد", callback_data=f"reject:{oid}")
                ]])
            )
        except Exception:
            pass

async def mytickets(q: CallbackQuery):
    c = db()
    rows = c.execute("""
        SELECT o.*, e.title, e.kind, s.show_at, s.hall, se.label
        FROM orders o
        JOIN shows s ON s.id=o.show_id
        JOIN events e ON e.id=s.event_id
        JOIN seats se ON se.id=o.seat_id
        WHERE o.user_id=? AND o.status='approved'
        ORDER BY o.id DESC
    """, (q.from_user.id,)).fetchall()
    c.close()
    if not rows:
        text = "🎟 هنوز بلیت قطعی ندارید."
    else:
        text = "🎟 <b>بلیت‌های من</b>\n\n" + "\n\n".join(
            f"{icon(r['kind'])} <b>{r['title']}</b>\n"
            f"🕐 {r['show_at']} | 💺 {r['label']}\n"
            f"🔑 <code>{r['ticket_code']}</code>"
            for r in rows
        )
    await send_or_edit(q, text, K([
        [InlineKeyboardButton(text="🎭 رویدادها", callback_data="events")],
        [InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="home")]
    ]))
    await q.answer()

async def approve(q: CallbackQuery, bot: Bot):
    if not is_admin(q.from_user.id): return
    oid = int(q.data.split(":")[1])
    c = db()
    o = c.execute("SELECT * FROM orders WHERE id=? AND status='pending'", (oid,)).fetchone()
    if not o:
        c.close(); await q.answer("این سفارش قبلاً بررسی شده.", show_alert=True); return
    code = f"HT-{datetime.now().strftime('%y%m%d')}-{oid:06d}"
    c.execute(
        "UPDATE orders SET status='approved',ticket_code=?,approved_at=? WHERE id=?",
        (code, now_iso(), oid)
    )
    c.execute("UPDATE seats SET status='sold',hold_until=NULL WHERE id=?", (o["seat_id"],))
    row = c.execute("""
        SELECT o.*,e.title,e.kind,s.show_at,s.hall,se.label
        FROM orders o JOIN shows s ON s.id=o.show_id
        JOIN events e ON e.id=s.event_id JOIN seats se ON se.id=o.seat_id
        WHERE o.id=?
    """, (oid,)).fetchone()
    c.commit(); c.close()
    await q.message.edit_caption(caption=f"✅ سفارش #{oid} تأیید شد.")
    try:
        await bot.send_message(o["user_id"], ticket_text(row))
        await bot.send_photo(
            o["user_id"], make_qr(code),
            caption=f"📱 QR بلیت <code>{code}</code>"
        )
    except Exception:
        pass
    await q.answer("بلیت صادر شد.")

async def reject(q: CallbackQuery, bot: Bot):
    if not is_admin(q.from_user.id): return
    oid = int(q.data.split(":")[1])
    c = db()
    o = c.execute("SELECT * FROM orders WHERE id=? AND status='pending'", (oid,)).fetchone()
    if not o:
        c.close(); await q.answer("این سفارش قبلاً بررسی شده.", show_alert=True); return
    c.execute("UPDATE orders SET status='rejected' WHERE id=?", (oid,))
    c.execute("UPDATE seats SET status='free',hold_until=NULL WHERE id=?", (o["seat_id"],))
    c.commit(); c.close()
    await q.message.edit_caption(caption=f"❌ سفارش #{oid} رد شد.")
    try:
        await bot.send_message(o["user_id"], f"❌ رسید سفارش #{oid} تأیید نشد.\nصندلی آزاد شد.")
    except Exception:
        pass
    await q.answer("سفارش رد شد.")

async def admin_panel(q: CallbackQuery):
    if not is_admin(q.from_user.id): return
    rows = [
        [InlineKeyboardButton(text="➕ رویداد", callback_data="a:event")],
        [InlineKeyboardButton(text="➕ سانس", callback_data="a:show")],
        [InlineKeyboardButton(text="💺 ساخت صندلی", callback_data="a:seats")],
        [InlineKeyboardButton(text="💳 کارت‌های بانکی", callback_data="a:cards")],
        [InlineKeyboardButton(text="🎁 تخفیف کاربر", callback_data="a:discount")],
        [InlineKeyboardButton(text="🖼 پوستر رویداد", callback_data="a:poster")],
        [InlineKeyboardButton(text="📊 گزارش فروش", callback_data="a:report")],
        [InlineKeyboardButton(text="🧾 سفارش‌های در انتظار", callback_data="a:pending")],
        [InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="home")],
    ]
    await send_or_edit(q, "👨‍💼 <b>پنل مدیریت</b>", K(rows))
    await q.answer()

async def admin_action(q: CallbackQuery, state: FSMContext):
    if not is_admin(q.from_user.id): return
    action = q.data.split(":")[1]
    prompts = {
        "event": ("فرمت:\nنوع | عنوان | توضیحات\n\nنوع: سینما / تئاتر / موسیقی / فرهنگی / سایر", AdminState.event),
        "show": ("فرمت:\nشناسه رویداد | تاریخ و ساعت | سالن | قیمت\nمثال: 1 | 1405/07/20 19:30 | سالن اصلی | 250000", AdminState.show),
        "seats": ("فرمت:\nشناسه سانس | تعداد ردیف | تعداد صندلی هر ردیف\nمثال: 1 | 8 | 10", AdminState.seats),
        "card": ("فرمت:\nنام کارت | شماره کارت", AdminState.card),
        "discount": ("فرمت:\nآیدی عددی کاربر | درصد تخفیف\nیا:\nآیدی عددی کاربر | قیمت ثابت", AdminState.discount),
        "poster": ("فرمت:\nشناسه رویداد\nسپس عکس پوستر را ارسال کنید.", AdminState.poster),
    }
    if action == "cards":
        c = db(); rows = c.execute("SELECT * FROM cards ORDER BY id").fetchall(); c.close()
        text = "💳 <b>کارت‌ها</b>\n\n" + ("\n".join(f"{r['id']}. {r['title']} — {r['number']}" for r in rows) if rows else "کارتی ثبت نشده.")
        await send_or_edit(q, text, K([
            [InlineKeyboardButton(text="➕ افزودن کارت", callback_data="a:card")],
            [InlineKeyboardButton(text="⬅️ مدیریت", callback_data="admin")]
        ])); await q.answer(); return
    if action == "report":
        c = db()
        total = c.execute("SELECT COUNT(*) n,COALESCE(SUM(amount),0) amount FROM orders WHERE status='approved'").fetchone()
        pending = c.execute("SELECT COUNT(*) n FROM orders WHERE status='pending'").fetchone()["n"]
        rejected = c.execute("SELECT COUNT(*) n FROM orders WHERE status='rejected'").fetchone()["n"]
        by_kind = c.execute("""
            SELECT e.kind, COUNT(*) n, COALESCE(SUM(o.amount),0) amount
            FROM orders o JOIN shows s ON s.id=o.show_id JOIN events e ON e.id=s.event_id
            WHERE o.status='approved' GROUP BY e.kind
        """).fetchall()
        c.close()
        lines = [f"📊 <b>گزارش فروش</b>", f"🎟 بلیت قطعی: {total['n']}", f"💰 فروش: {money(total['amount'])}",
                 f"⏳ در انتظار: {pending}", f"❌ رد شده: {rejected}", "", "تفکیک:"]
        lines += [f"{icon(r['kind'])} {r['kind']}: {r['n']} بلیت — {money(r['amount'])}" for r in by_kind]
        await send_or_edit(q, "\n".join(lines), back("admin")); await q.answer(); return
    if action == "pending":
        c = db()
        rows = c.execute("""
            SELECT o.id,o.amount,o.user_id,e.title,s.show_at,se.label
            FROM orders o JOIN shows s ON s.id=o.show_id
            JOIN events e ON e.id=s.event_id JOIN seats se ON se.id=o.seat_id
            WHERE o.status='pending' ORDER BY o.id DESC LIMIT 30
        """).fetchall()
        c.close()
        if not rows:
            text = "🧾 سفارش در انتظاری وجود ندارد."
        else:
            text = "🧾 <b>سفارش‌های در انتظار</b>\n\n" + "\n".join(
                f"#{r['id']} | {r['title']} | {r['show_at']} | {r['label']} | {money(r['amount'])}"
                for r in rows
            ) + "\n\nبرای بررسی، رسید سفارش در پیام ادمین ارسال شده است."
        await send_or_edit(q, text, back("admin")); await q.answer(); return
    prompt, st = prompts[action]
    await state.set_state(st)
    await q.message.edit_text(prompt, reply_markup=back("admin"))
    await q.answer()

async def admin_text(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id): return
    st = await state.get_state()
    if st == AdminState.event.state:
        parts = [x.strip() for x in message.text.split("|", 2)]
        if len(parts) < 2 or parts[0] not in KINDS:
            await message.answer("فرمت اشتباه است. نوع باید یکی از سینما/تئاتر/موسیقی/فرهنگی/سایر باشد."); return
        desc = parts[2] if len(parts) > 2 else ""
        c = db()
        eid = c.execute(
            "INSERT INTO events(kind,title,description,created_at) VALUES(?,?,?,?)",
            (parts[0], parts[1], desc, now_iso())
        ).lastrowid
        c.commit(); c.close()
        await state.clear()
        await message.answer(f"✅ رویداد #{eid} ساخته شد.", reply_markup=main_kb(message.from_user.id))
    elif st == AdminState.show.state:
        p = [x.strip() for x in message.text.split("|")]
        if len(p) != 4:
            await message.answer("فرمت اشتباه است."); return
        try: eid, price = int(p[0]), int(p[3])
        except ValueError:
            await message.answer("شناسه و قیمت باید عدد باشند."); return
        c = db()
        exists = c.execute("SELECT id FROM events WHERE id=?", (eid,)).fetchone()
        if not exists:
            c.close(); await message.answer("رویداد پیدا نشد."); return
        sid = c.execute(
            "INSERT INTO shows(event_id,show_at,hall,base_price) VALUES(?,?,?,?)",
            (eid,p[1],p[2],price)
        ).lastrowid
        c.commit(); c.close()
        await state.clear(); await message.answer(f"✅ سانس #{sid} ساخته شد.")
    elif st == AdminState.seats.state:
        p = [x.strip() for x in message.text.split("|")]
        if len(p) != 3:
            await message.answer("فرمت اشتباه است."); return
        try: sid, nr, nc = map(int, p)
        except ValueError:
            await message.answer("هر سه مقدار باید عدد باشند."); return
        if nr < 1 or nc < 1 or nr > 50 or nc > 50:
            await message.answer("تعداد ردیف و صندلی باید بین 1 تا 50 باشد."); return
        c = db()
        if not c.execute("SELECT id FROM shows WHERE id=?", (sid,)).fetchone():
            c.close(); await message.answer("سانس پیدا نشد."); return
        created = 0
        for r in range(1, nr+1):
            for n in range(1, nc+1):
                label = f"{r}-{n}"
                try:
                    c.execute("INSERT INTO seats(show_id,label) VALUES(?,?)", (sid,label)); created += 1
                except sqlite3.IntegrityError:
                    pass
        c.commit(); c.close(); await state.clear()
        await message.answer(f"✅ {created} صندلی ساخته شد.")
    elif st == AdminState.card.state:
        p = [x.strip() for x in message.text.split("|",1)]
        if len(p) != 2:
            await message.answer("فرمت اشتباه است."); return
        c = db(); cid = c.execute("INSERT INTO cards(title,number) VALUES(?,?)", tuple(p)).lastrowid; c.commit(); c.close()
        await state.clear(); await message.answer(f"✅ کارت #{cid} ثبت شد.")
    elif st == AdminState.discount.state:
        p = [x.strip() for x in message.text.split("|")]
        if len(p) != 2:
            await message.answer("فرمت اشتباه است."); return
        try: uid = int(p[0]); value = float(p[1])
        except ValueError:
            await message.answer("آیدی و مقدار باید عدد باشند."); return
        c = db()
        if value <= 100:
            c.execute("INSERT OR REPLACE INTO discounts(user_id,percent,fixed_price) VALUES(?,?,NULL)", (uid,value))
            msg = f"تخفیف {value:g}%"
        else:
            c.execute("INSERT OR REPLACE INTO discounts(user_id,percent,fixed_price) VALUES(?,0,?)", (uid,int(value)))
            msg = f"قیمت ثابت {money(value)}"
        c.commit(); c.close(); await state.clear(); await message.answer(f"✅ {msg} برای کاربر {uid} ثبت شد.")
    elif st == AdminState.poster.state:
        try: eid = int(message.text.strip())
        except ValueError:
            await message.answer("شناسه رویداد باید عدد باشد."); return
        await state.update_data(poster_event_id=eid)
        await message.answer("حالا عکس پوستر را ارسال کنید.")
        await state.set_state(AdminState.poster)

async def admin_poster(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id) or not message.photo: return
    data = await state.get_data()
    eid = data.get("poster_event_id")
    if not eid:
        await message.answer("ابتدا شناسه رویداد را ارسال کنید."); return
    c = db()
    cur = c.execute("UPDATE events SET poster_file_id=? WHERE id=?", (message.photo[-1].file_id,eid))
    c.commit(); c.close()
    await state.clear()
    await message.answer("✅ پوستر رویداد ذخیره شد.")

async def noop(q: CallbackQuery):
    await q.answer("این صندلی در حال حاضر آزاد نیست.", show_alert=True)

async def home(q: CallbackQuery):
    await q.message.edit_text(
        "🎨 <b>کارگزاری به وقت هنر</b>\n\n"
        "سینما، تئاتر، موسیقی و رویدادهای فرهنگی.",
        reply_markup=main_kb(q.from_user.id)
    )
    await q.answer()

async def cmd_admin(message: Message):
    if is_admin(message.from_user.id):
        await message.answer("پنل مدیریت:", reply_markup=K([
            [InlineKeyboardButton(text="👨‍💼 ورود به پنل", callback_data="admin")]
        ]))
    else:
        await message.answer("دسترسی ندارید.")

async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN تنظیم نشده است.")
    init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()

    dp.message.register(start, CommandStart())
    dp.message.register(cmd_admin, Command("admin"))

    dp.callback_query.register(events, F.data == "events")
    dp.callback_query.register(mytickets, F.data == "mytickets")
    dp.callback_query.register(home, F.data == "home")
    dp.callback_query.register(admin_panel, F.data == "admin")
    dp.callback_query.register(admin_action, F.data.startswith("a:"))
    dp.callback_query.register(event_detail, F.data.startswith("event:"))
    dp.callback_query.register(show_detail, F.data.startswith("show:"))
    dp.callback_query.register(seat_pick, F.data.startswith("seat:"))
    dp.callback_query.register(choose_card, F.data.startswith("paycard:"))
    dp.callback_query.register(approve, F.data.startswith("approve:"))
    dp.callback_query.register(reject, F.data.startswith("reject:"))
    dp.callback_query.register(noop, F.data == "noop")

    dp.message.register(admin_poster, AdminState.poster, F.photo)
    dp.message.register(admin_text, F.text)
    dp.message.register(receipt, F.photo)

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
