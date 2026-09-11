import os
import io
import sqlite3
import asyncio
import logging
from datetime import datetime, timedelta

import qrcode
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile, InputMediaPhoto

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("honar_time_bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "honar_time.db")
HOLD_MINUTES = max(1, int(os.getenv("HOLD_MINUTES", "10")))
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}

KINDS = {"سینما": "🎬", "تئاتر": "🎭", "موسیقی": "🎵", "فرهنگی": "🎤", "سایر": "🎨"}
MAX_SEATS = 110


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
        kind TEXT NOT NULL, title TEXT NOT NULL, description TEXT DEFAULT '',
        poster_file_id TEXT DEFAULT '', active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS shows(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id INTEGER NOT NULL, show_at TEXT NOT NULL,
        hall TEXT NOT NULL DEFAULT 'سالن اصلی', base_price INTEGER NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        FOREIGN KEY(event_id) REFERENCES events(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS seats(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        show_id INTEGER NOT NULL, label TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'free', hold_until TEXT,
        UNIQUE(show_id,label), FOREIGN KEY(show_id) REFERENCES shows(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS cards(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL, number TEXT NOT NULL,
        owner_name TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS discounts(
        user_id INTEGER PRIMARY KEY, percent REAL NOT NULL DEFAULT 0, fixed_price INTEGER
    );
    CREATE TABLE IF NOT EXISTS orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, show_id INTEGER NOT NULL,
        seat_id INTEGER NOT NULL, amount INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        ticket_code TEXT UNIQUE, receipt_file_id TEXT, created_at TEXT NOT NULL, approved_at TEXT,
        FOREIGN KEY(show_id) REFERENCES shows(id), FOREIGN KEY(seat_id) REFERENCES seats(id)
    );
    CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
    CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
    CREATE INDEX IF NOT EXISTS idx_seats_show_status ON seats(show_id,status);
    """)
    # Migration for databases created by older versions.
    cols = {r["name"] for r in c.execute("PRAGMA table_info(cards)").fetchall()}
    if "owner_name" not in cols:
        c.execute("ALTER TABLE cards ADD COLUMN owner_name TEXT NOT NULL DEFAULT ''")
    c.commit(); c.close()


def now(): return datetime.now()
def now_iso(): return now().isoformat(timespec="seconds")
def money(n): return f"{int(n):,} تومان"
def icon(kind): return KINDS.get(kind, "🎨")
def is_admin(uid): return uid in ADMIN_IDS


def clean_holds():
    c = db(); c.execute("UPDATE seats SET status='free',hold_until=NULL WHERE status='held' AND hold_until IS NOT NULL AND hold_until < ?", (now_iso(),)); c.commit(); c.close()


def final_price(user_id, base):
    c=db(); d=c.execute("SELECT * FROM discounts WHERE user_id=?",(user_id,)).fetchone(); c.close()
    if not d: return int(base)
    if d["fixed_price"] is not None: return max(0,int(d["fixed_price"]))
    return max(0,round(int(base)*(1-float(d["percent"])/100)))


def K(rows): return InlineKeyboardMarkup(inline_keyboard=rows)

def main_kb(uid):
    rows=[[InlineKeyboardButton(text="🎭 رویدادها",callback_data="events")],[InlineKeyboardButton(text="🎟 بلیت‌های من",callback_data="mytickets")]]
    if is_admin(uid): rows.append([InlineKeyboardButton(text="👨‍💼 پنل مدیریت",callback_data="admin")])
    return K(rows)

def back(data): return K([[InlineKeyboardButton(text="⬅️ بازگشت",callback_data=data)]])


async def edit_or_send(target, text, markup=None):
    """Edit the existing bot message for callback flows; otherwise send a new message."""
    if isinstance(target, CallbackQuery):
        try: await target.message.edit_text(text, reply_markup=markup)
        except Exception: await target.message.answer(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


async def remember_prompt(q: CallbackQuery, state: FSMContext, st, text, back_data="admin"):
    await state.set_state(st)
    await state.update_data(prompt_message_id=q.message.message_id, prompt_chat_id=q.message.chat.id)
    await q.message.edit_text(text, reply_markup=back(back_data))


async def finish_flow(message: Message, state: FSMContext, text, markup=None):
    data = await state.get_data()
    chat_id = data.get("prompt_chat_id")
    message_id = data.get("prompt_message_id")
    try:
        if chat_id and message_id:
            await message.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=markup)
        else:
            await message.answer(text, reply_markup=markup)
    except Exception:
        await message.answer(text, reply_markup=markup)
    # In private chats Telegram may allow deletion of the user's input; best effort only.
    try:
        await message.delete()
    except Exception:
        pass


class AdminState(StatesGroup):
    event=State(); show=State(); seats=State(); card=State(); discount=State(); poster=State()
    edit_event=State(); edit_show=State(); edit_card=State(); edit_discount=State(); edit_poster=State(); edit_seats=State()


async def start(message: Message):
    await message.answer("🎨 <b>کارگزاری به وقت هنر</b>\n\nفروش بلیت سینما، تئاتر، موسیقی و رویدادهای فرهنگی.\nرویداد را انتخاب کنید:", reply_markup=main_kb(message.from_user.id))


async def events(q: CallbackQuery):
    clean_holds(); c=db(); rows=c.execute("SELECT * FROM events WHERE active=1 ORDER BY id DESC").fetchall(); c.close()
    buttons=[[InlineKeyboardButton(text=f"{icon(r['kind'])} {r['title']}",callback_data=f"event:{r['id']}")] for r in rows]
    buttons.append([InlineKeyboardButton(text="🏠 منوی اصلی",callback_data="home")])
    await edit_or_send(q,"🎟 <b>رویدادهای فعال</b>" if rows else "فعلاً رویداد فعالی ثبت نشده است.",K(buttons)); await q.answer()


async def event_detail(q: CallbackQuery):
    eid=int(q.data.split(":")[1])
    c=db(); e=c.execute("SELECT * FROM events WHERE id=? AND active=1",(eid,)).fetchone(); shows=c.execute("SELECT * FROM shows WHERE event_id=? AND active=1 ORDER BY show_at",(eid,)).fetchall(); c.close()
    if not e: await q.answer("رویداد پیدا نشد.",show_alert=True); return
    text=f"{icon(e['kind'])} <b>{e['title']}</b>\n\n{e['description'] or 'بدون توضیحات'}\n\n🕐 سانس را انتخاب کنید:"
    rows=[[InlineKeyboardButton(text=f"🕐 {s['show_at']} | {s['hall']} | {money(s['base_price'])}",callback_data=f"show:{s['id']}")] for s in shows]
    rows.append([InlineKeyboardButton(text="⬅️ رویدادها",callback_data="events")])
    markup=K(rows)
    if e["poster_file_id"]:
        try:
            await q.message.edit_media(InputMediaPhoto(media=e["poster_file_id"],caption=text),reply_markup=markup)
        except Exception:
            try:
                await q.message.delete()
            except Exception:
                pass
            await q.message.answer_photo(e["poster_file_id"],caption=text,reply_markup=markup)
    else:
        await edit_or_send(q,text,markup)
    await q.answer()


def fa_digits(value):
    return str(value).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


def seat_map_rows(seats, page=1, rows_per_page=5):
    """Build a paginated seat keyboard so Telegram never receives >100 buttons."""
    seat_by_label={x["label"]:x for x in seats}
    parsed=[]
    for x in seats:
        try:
            r,n=map(int,x["label"].split("-",1)); parsed.append((r,n,x))
        except Exception:
            continue
    row_numbers=sorted({r for r,_,_ in parsed})
    per_row=max([n for _,n,_ in parsed],default=0)
    if not row_numbers or not per_row:
        return [], per_row, 1, 1
    total_pages=max(1,(len(row_numbers)+rows_per_page-1)//rows_per_page)
    page=max(1,min(page,total_pages))
    selected_rows=row_numbers[(page-1)*rows_per_page:page*rows_per_page]
    marks={"free":"🟩","held":"🟨","pending":"🟧","sold":"🟥"}
    rows=[]
    for r in selected_rows:
        first=(r-1)*per_row+1
        last=r*per_row
        rows.append([InlineKeyboardButton(text=f"— ردیف {fa_digits(r)} | {fa_digits(first)} تا {fa_digits(last)} —",callback_data="noop")])
        line=[]
        for n in range(1,per_row+1):
            seat=seat_by_label.get(f"{r}-{n}")
            if not seat: continue
            global_no=(r-1)*per_row+n
            mark=marks.get(seat["status"],"⬜")
            cb=f"seat:{seat['id']}" if seat["status"]=="free" else "noop"
            line.append(InlineKeyboardButton(text=f"{mark} {fa_digits(global_no)}",callback_data=cb))
            if len(line)==5:
                rows.append(line); line=[]
        if line: rows.append(line)
    nav=[]
    if page>1: nav.append(InlineKeyboardButton(text="⬅️ صفحه قبل",callback_data=f"seatpage:{seats[0]['show_id']}:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{fa_digits(page)} / {fa_digits(total_pages)}",callback_data="noop"))
    if page<total_pages: nav.append(InlineKeyboardButton(text="صفحه بعد ➡️",callback_data=f"seatpage:{seats[0]['show_id']}:{page+1}"))
    if nav: rows.append(nav)
    rows.append([InlineKeyboardButton(text="🔄 به‌روزرسانی",callback_data=f"seatpage:{seats[0]['show_id']}:{page}")])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت به رویداد",callback_data=f"event:{seats[0]['event_id']}" if seats and 'event_id' in seats[0].keys() else "events")])
    return rows, per_row, page, total_pages


async def render_seat_message(chat, sid, page=1, edit_message=None):
    clean_holds()
    c=db()
    s=c.execute("SELECT s.*,e.title,e.kind,e.event_id FROM shows s JOIN events e ON e.id=s.event_id WHERE s.id=? AND s.active=1",(sid,)).fetchone()
    seats=c.execute("SELECT se.*,s.event_id FROM seats se JOIN shows s ON s.id=se.show_id WHERE se.show_id=? ORDER BY se.id",(sid,)).fetchall() if s else []
    if s and not seats:
        c.executemany("INSERT INTO seats(show_id,label,status) VALUES(?,?,'free')",[(sid,f"{r}-{n}") for r in range(1,12) for n in range(1,11)])
        c.commit()
        seats=c.execute("SELECT se.*,s.event_id FROM seats se JOIN shows s ON s.id=se.show_id WHERE se.show_id=? ORDER BY se.id",(sid,)).fetchall()
    c.close()
    if not s: return False
    rows, per_row, page, total_pages=seat_map_rows(seats,page)
    row_count=len({x['label'].split('-')[0] for x in seats if '-' in x['label']})
    text=(
        f"💺 <b>انتخاب صندلی</b>\n\n"
        f"🎭 {s['title']}\n"
        f"🕐 {s['show_at']} | 🏛 {s['hall']}\n"
        f"💰 قیمت هر صندلی: {money(s['base_price'])}\n\n"
        f"🟩 آزاد  🟨 رزرو موقت  🟧 در انتظار  🟥 فروخته‌شده\n\n"
        f"صفحه {fa_digits(page)} از {fa_digits(total_pages)} — هر بار {fa_digits(min(5,row_count))} ردیف نمایش داده می‌شود.\n"
        "روی صندلی آزاد بزنید:"
    )
    if edit_message:
        await edit_message.edit_text(text,reply_markup=K(rows))
    else:
        await chat.send_message(text=text,reply_markup=K(rows))
    return True


async def show_detail(q: CallbackQuery):
    try:
        ok=await render_seat_message(q.message.chat,int(q.data.split(":")[1]),1)
    except Exception as e:
        log.exception("seat map send failed")
        await q.answer("نقشه صندلی ارسال نشد. احتمالاً نسخه قبلی محدودیت تعداد دکمه داشت؛ لطفاً دوباره بزنید.",show_alert=True)
        return
    if not ok:
        await q.answer("سانس پیدا نشد.",show_alert=True); return
    await q.answer("نقشه صندلی در پیام جدید ارسال شد.")


async def seat_page(q: CallbackQuery):
    try:
        _,sid,page=q.data.split(":")
        ok=await render_seat_message(q.message.chat,int(sid),int(page),edit_message=q.message)
        await q.answer("" if ok else "سانس پیدا نشد.",show_alert=not ok)
    except Exception:
        log.exception("seat page failed")
        await q.answer("نمایش صفحه صندلی‌ها انجام نشد.",show_alert=True)


async def seat_pick(q: CallbackQuery,state:FSMContext):
    clean_holds(); seat_id=int(q.data.split(":")[1]); uid=q.from_user.id; c=db(); until=(now()+timedelta(minutes=HOLD_MINUTES)).isoformat(timespec="seconds")
    cur=c.execute("UPDATE seats SET status='held',hold_until=? WHERE id=? AND status='free'",(until,seat_id))
    if cur.rowcount!=1: c.close(); await q.answer("این صندلی دیگر آزاد نیست.",show_alert=True); return
    seat=c.execute("SELECT * FROM seats WHERE id=?",(seat_id,)).fetchone(); s=c.execute("SELECT s.*,e.title,e.kind FROM shows s JOIN events e ON e.id=s.event_id WHERE s.id=?",(seat["show_id"],)).fetchone(); cards=c.execute("SELECT * FROM cards WHERE active=1 ORDER BY id").fetchall(); c.commit(); c.close()
    amount=final_price(uid,s["base_price"]); await state.update_data(seat_id=seat_id,show_id=s["id"],amount=amount)
    rows=[[InlineKeyboardButton(text=f"💳 {x['title']}",callback_data=f"paycard:{x['id']}")] for x in cards]; rows.append([InlineKeyboardButton(text="❌ لغو",callback_data=f"cancel:{s['id']}")])
    await edit_or_send(q,f"🎟 {s['title']}\n💺 صندلی: <b>{seat['label']}</b>\n💰 مبلغ نهایی: <b>{money(amount)}</b>\n\nصندلی برای {HOLD_MINUTES} دقیقه نگه داشته شد.\nکارت پرداخت را انتخاب کنید:",K(rows)); await q.answer()


async def choose_card(q: CallbackQuery,state:FSMContext):
    data=await state.get_data(); cid=int(q.data.split(":")[1]); c=db(); card=c.execute("SELECT * FROM cards WHERE id=? AND active=1",(cid,)).fetchone(); c.close()
    if not card or not data: await q.answer("رزرو منقضی شده است.",show_alert=True); return
    await state.update_data(waiting_receipt=True)
    owner=f"\n👤 صاحب حساب: <b>{card['owner_name']}</b>" if card['owner_name'] else ""
    await q.message.edit_text(f"💳 <b>{card['title']}</b>\nشماره کارت: <code>{card['number']}</code>{owner}\n\nمبلغ قابل پرداخت: <b>{money(data['amount'])}</b>\n\nبعد از کارت‌به‌کارت، <b>عکس رسید</b> را همینجا ارسال کنید.",reply_markup=K([[InlineKeyboardButton(text="❌ لغو",callback_data=f"cancel:{data['show_id']}")]])); await q.answer()


def ticket_text(row):
    return f"🎟 <b>بلیت قطعی</b>\n\n🔑 کد بلیت: <code>{row['ticket_code']}</code>\n{icon(row['kind'])} {row['title']}\n🕐 {row['show_at']}\n🏛 {row['hall']}\n💺 صندلی: <b>{row['label']}</b>\n💰 مبلغ: {money(row['amount'])}\n\nQR این بلیت برای کنترل ورودی قابل اسکن است."


def make_qr(code):
    img=qrcode.make(code); bio=io.BytesIO(); img.save(bio,format="PNG"); bio.seek(0); return BufferedInputFile(bio.read(),filename=f"{code}.png")


async def receipt(message: Message,state:FSMContext,bot:Bot):
    data=await state.get_data()
    if not data.get("waiting_receipt"): return
    if not message.photo:
        await message.answer("لطفاً فقط عکس رسید را ارسال کنید."); return
    c=db(); seat=c.execute("SELECT * FROM seats WHERE id=? AND status='held'",(data["seat_id"],)).fetchone()
    if not seat or (seat["hold_until"] and seat["hold_until"]<now_iso()):
        if seat: c.execute("UPDATE seats SET status='free',hold_until=NULL WHERE id=?",(seat["id"],)); c.commit()
        c.close(); await state.clear(); await message.answer("⏰ زمان رزرو تمام شده است. دوباره صندلی انتخاب کنید.",reply_markup=main_kb(message.from_user.id)); return
    oid=c.execute("INSERT INTO orders(user_id,show_id,seat_id,amount,status,created_at,receipt_file_id) VALUES(?,?,?,?,'pending',?,?)",(message.from_user.id,data["show_id"],data["seat_id"],data["amount"],now_iso(),message.photo[-1].file_id)).lastrowid
    c.execute("UPDATE seats SET status='pending',hold_until=NULL WHERE id=?",(data["seat_id"],)); c.commit(); c.close(); await state.clear()
    await message.answer(f"🧾 رسید سفارش <b>#{oid}</b> دریافت شد.\nپس از بررسی مدیر، نتیجه و در صورت تأیید بلیت برای شما ارسال می‌شود.",reply_markup=main_kb(message.from_user.id))
    caption=f"🧾 <b>رسید جدید</b>\nسفارش: #{oid}\nکاربر: <code>{message.from_user.id}</code>\nمبلغ: {money(data['amount'])}\n\nبرای بررسی رسید یکی از گزینه‌های زیر را بزنید."
    sent=0
    for aid in ADMIN_IDS:
        try:
            await bot.send_photo(aid,message.photo[-1].file_id,caption=caption,reply_markup=K([[InlineKeyboardButton(text="✅ تأیید",callback_data=f"approve:{oid}"),InlineKeyboardButton(text="❌ رد",callback_data=f"reject:{oid}")]])); sent+=1
        except Exception as e: log.exception("Could not send receipt %s to admin %s: %s",oid,aid,e)
    if not sent: log.error("Receipt %s was not delivered to any admin. Check ADMIN_IDS and that admins started the bot.",oid)


async def cancel_reservation(q: CallbackQuery, state: FSMContext):
    sid = int(q.data.split(":")[1])
    data = await state.get_data()
    c = db()
    if data.get("seat_id"):
        c.execute("UPDATE seats SET status='free',hold_until=NULL WHERE id=? AND status='held'", (data["seat_id"],))
    c.commit(); c.close(); await state.clear()
    await show_detail(q)


async def mytickets(q:CallbackQuery):
    c=db(); rows=c.execute("SELECT o.*,e.title,e.kind,s.show_at,s.hall,se.label FROM orders o JOIN shows s ON s.id=o.show_id JOIN events e ON e.id=s.event_id JOIN seats se ON se.id=o.seat_id WHERE o.user_id=? AND o.status='approved' ORDER BY o.id DESC",(q.from_user.id,)).fetchall(); c.close()
    text="🎟 هنوز بلیت قطعی ندارید." if not rows else "🎟 <b>بلیت‌های من</b>\n\n"+"\n\n".join(f"{icon(r['kind'])} <b>{r['title']}</b>\n🕐 {r['show_at']} | 💺 {r['label']}\n🔑 <code>{r['ticket_code']}</code>" for r in rows)
    await edit_or_send(q,text,K([[InlineKeyboardButton(text="🎭 رویدادها",callback_data="events")],[InlineKeyboardButton(text="🏠 منوی اصلی",callback_data="home")]])); await q.answer()


async def approve(q:CallbackQuery,bot:Bot):
    if not is_admin(q.from_user.id): return
    oid=int(q.data.split(":")[1]); c=db(); o=c.execute("SELECT * FROM orders WHERE id=? AND status='pending'",(oid,)).fetchone()
    if not o: c.close(); await q.answer("این سفارش قبلاً بررسی شده.",show_alert=True); return
    code=f"HT-{datetime.now().strftime('%y%m%d')}-{oid:06d}"; c.execute("UPDATE orders SET status='approved',ticket_code=?,approved_at=? WHERE id=?",(code,now_iso(),oid)); c.execute("UPDATE seats SET status='sold',hold_until=NULL WHERE id=?",(o['seat_id'],))
    row=c.execute("SELECT o.*,e.title,e.kind,s.show_at,s.hall,se.label FROM orders o JOIN shows s ON s.id=o.show_id JOIN events e ON e.id=s.event_id JOIN seats se ON se.id=o.seat_id WHERE o.id=?",(oid,)).fetchone(); c.commit(); c.close()
    await q.message.edit_caption(caption=f"✅ سفارش #{oid} تأیید شد.\n🎟 کد بلیت: <code>{code}</code>")
    try:
        await bot.send_message(o['user_id'],ticket_text(row)); await bot.send_photo(o['user_id'],make_qr(code),caption=f"📱 QR بلیت <code>{code}</code>")
    except Exception as e: log.exception("Could not send ticket %s",oid,e)
    await q.answer("بلیت صادر شد.")


async def reject(q:CallbackQuery,bot:Bot):
    if not is_admin(q.from_user.id): return
    oid=int(q.data.split(":")[1]); c=db(); o=c.execute("SELECT * FROM orders WHERE id=? AND status='pending'",(oid,)).fetchone()
    if not o: c.close(); await q.answer("این سفارش قبلاً بررسی شده.",show_alert=True); return
    c.execute("UPDATE orders SET status='rejected' WHERE id=?",(oid,)); c.execute("UPDATE seats SET status='free',hold_until=NULL WHERE id=?",(o['seat_id'],)); c.commit(); c.close()
    await q.message.edit_caption(caption=f"❌ سفارش #{oid} رد شد.\nصندلی آزاد شد.")
    try: await bot.send_message(o['user_id'],f"❌ رسید سفارش #{oid} تأیید نشد.\nصندلی آزاد شد.")
    except Exception as e: log.exception("Could not notify user for rejected order %s: %s",oid,e)
    await q.answer("سفارش رد شد.")


async def admin_panel(q:CallbackQuery,state:FSMContext):
    if not is_admin(q.from_user.id): return
    await state.clear()
    rows=[[InlineKeyboardButton(text="➕ رویداد",callback_data="a:event"),InlineKeyboardButton(text="✏️ ویرایش رویداد",callback_data="a:edit_events")],[InlineKeyboardButton(text="➕ سانس",callback_data="a:show"),InlineKeyboardButton(text="✏️ ویرایش سانس",callback_data="a:edit_shows")],[InlineKeyboardButton(text="💺 ساخت/ویرایش صندلی‌ها",callback_data="a:seats_menu")],[InlineKeyboardButton(text="💳 کارت‌های بانکی",callback_data="a:cards")],[InlineKeyboardButton(text="🎁 تخفیف کاربر",callback_data="a:discount")],[InlineKeyboardButton(text="🖼 پوستر رویداد",callback_data="a:poster")],[InlineKeyboardButton(text="📊 گزارش فروش",callback_data="a:report")],[InlineKeyboardButton(text="🧾 سفارش‌های در انتظار",callback_data="a:pending")],[InlineKeyboardButton(text="🏠 منوی اصلی",callback_data="home")]]
    await edit_or_send(q,"👨‍💼 <b>پنل مدیریت</b>\n\nهمه گزینه‌های مدیریتی تا حد امکان در همین پیام نمایش داده می‌شوند.",K(rows)); await q.answer()


async def admin_action(q:CallbackQuery,state:FSMContext):
    if not is_admin(q.from_user.id): return
    action=q.data.split(":",1)[1]
    if action=="edit_events":
        c=db(); rows=c.execute("SELECT id,kind,title,active FROM events ORDER BY id DESC").fetchall(); c.close(); buttons=[[InlineKeyboardButton(text=f"{'✅' if r['active'] else '⛔'} #{r['id']} {icon(r['kind'])} {r['title']}",callback_data=f"evm:{r['id']}")] for r in rows]; buttons.append([InlineKeyboardButton(text="⬅️ مدیریت",callback_data="admin")]); await edit_or_send(q,"✏️ <b>انتخاب رویداد برای ویرایش</b>",K(buttons)); await q.answer(); return
    if action=="edit_shows":
        c=db(); rows=c.execute("SELECT s.id,s.show_at,s.hall,s.base_price,e.title FROM shows s JOIN events e ON e.id=s.event_id ORDER BY s.id DESC").fetchall(); c.close(); buttons=[[InlineKeyboardButton(text=f"#{r['id']} {r['title']} | {r['show_at']}",callback_data=f"shm:{r['id']}")] for r in rows]; buttons.append([InlineKeyboardButton(text="⬅️ مدیریت",callback_data="admin")]); await edit_or_send(q,"✏️ <b>انتخاب سانس برای ویرایش</b>",K(buttons)); await q.answer(); return
    if action=="seats_menu":
        c=db(); rows=c.execute("SELECT s.id,s.show_at,e.title,COUNT(se.id) n FROM shows s JOIN events e ON e.id=s.event_id LEFT JOIN seats se ON se.show_id=s.id GROUP BY s.id ORDER BY s.id DESC").fetchall(); c.close(); buttons=[[InlineKeyboardButton(text=f"💺 #{r['id']} {r['title']} | {r['show_at']} | {r['n']} صندلی",callback_data=f"sm:{r['id']}")] for r in rows]; buttons.append([InlineKeyboardButton(text="➕ ساخت صندلی جدید",callback_data="a:seats")]); buttons.append([InlineKeyboardButton(text="⬅️ مدیریت",callback_data="admin")]); await edit_or_send(q,"💺 <b>مدیریت صندلی‌ها</b>\nسانس را برای ساخت یا تغییر تعداد ردیف/صندلی انتخاب کن.",K(buttons)); await q.answer(); return
    if action=="cards":
        c=db(); rows=c.execute("SELECT * FROM cards ORDER BY id").fetchall(); c.close(); buttons=[[InlineKeyboardButton(text=f"✏️ #{r['id']} {r['title']}",callback_data=f"cm:{r['id']}")] for r in rows]; buttons.append([InlineKeyboardButton(text="➕ افزودن کارت",callback_data="a:card")]); buttons.append([InlineKeyboardButton(text="⬅️ مدیریت",callback_data="admin")]); await edit_or_send(q,"💳 <b>کارت‌ها</b>\n\nبرای ویرایش روی کارت موردنظر بزن.",K(buttons)); await q.answer(); return
    if action=="report":
        c=db(); total=c.execute("SELECT COUNT(*) n,COALESCE(SUM(amount),0) amount FROM orders WHERE status='approved'").fetchone(); pending=c.execute("SELECT COUNT(*) n FROM orders WHERE status='pending'").fetchone()['n']; rejected=c.execute("SELECT COUNT(*) n FROM orders WHERE status='rejected'").fetchone()['n']; by_kind=c.execute("SELECT e.kind,COUNT(*) n,COALESCE(SUM(o.amount),0) amount FROM orders o JOIN shows s ON s.id=o.show_id JOIN events e ON e.id=s.event_id WHERE o.status='approved' GROUP BY e.kind").fetchall(); c.close(); lines=["📊 <b>گزارش فروش</b>",f"🎟 بلیت قطعی: {total['n']}",f"💰 فروش: {money(total['amount'])}",f"⏳ در انتظار: {pending}",f"❌ رد شده: {rejected}","","تفکیک:"]+[f"{icon(r['kind'])} {r['kind']}: {r['n']} بلیت — {money(r['amount'])}" for r in by_kind]; await edit_or_send(q,"\n".join(lines),back("admin")); await q.answer(); return
    if action=="pending":
        c=db(); rows=c.execute("SELECT o.id,o.amount,o.user_id,e.title,s.show_at,se.label,o.receipt_file_id FROM orders o JOIN shows s ON s.id=o.show_id JOIN events e ON e.id=s.event_id JOIN seats se ON se.id=o.seat_id WHERE o.status='pending' ORDER BY o.id DESC LIMIT 30").fetchall(); c.close(); buttons=[[InlineKeyboardButton(text=f"🧾 #{r['id']} | {r['title']} | 💺{r['label']}",callback_data=f"receiptview:{r['id']}")] for r in rows]; buttons.append([InlineKeyboardButton(text="⬅️ مدیریت",callback_data="admin")]); await edit_or_send(q,"🧾 <b>سفارش‌های در انتظار</b>\nبرای مشاهده رسید و تأیید/رد، سفارش را انتخاب کن." if rows else "🧾 سفارش در انتظاری وجود ندارد.",K(buttons)); await q.answer(); return
    prompts={"event":("فرمت:\nنوع | عنوان | توضیحات\n\nنوع: سینما / تئاتر / موسیقی / فرهنگی / سایر",AdminState.event),"show":("فرمت:\nشناسه رویداد | تاریخ و ساعت | سالن | قیمت\nمثال: 1 | 1405/07/20 19:30 | سالن اصلی | 250000",AdminState.show),"seats":("فرمت ساخت صندلی:\nشناسه سانس | تعداد ردیف | تعداد صندلی هر ردیف\nحداکثر کل صندلی: 110\nمثال: 1 | 11 | 10\n\nشماره‌ها در نمایش سالن از 1 تا 110 به‌صورت پیوسته و به تفکیک ردیف خواهند بود.",AdminState.seats),"card":("فرمت:\nنام کارت | شماره کارت | نام صاحب حساب\nمثال:\nبانک ملی | 6037997512345678 | علی رضایی",AdminState.card),"discount":("فرمت:\nآیدی عددی کاربر | درصد تخفیف\nیا:\nآیدی عددی کاربر | قیمت ثابت",AdminState.discount),"poster":("فرمت:\nشناسه رویداد\nسپس عکس پوستر را ارسال کنید.",AdminState.poster)}
    if action not in prompts: await q.answer("گزینه نامعتبر است.",show_alert=True); return
    prompt,st=prompts[action]; await remember_prompt(q,state,st,prompt,"admin"); await q.answer()


async def event_manage(q:CallbackQuery,state:FSMContext):
    if not is_admin(q.from_user.id): return
    await state.clear()
    eid=int(q.data.split(":")[1]); c=db(); e=c.execute("SELECT * FROM events WHERE id=?",(eid,)).fetchone(); c.close()
    if not e: await q.answer("رویداد پیدا نشد.",show_alert=True); return
    rows=[[InlineKeyboardButton(text="✏️ ویرایش عنوان/نوع/توضیحات",callback_data=f"eve:{eid}")],[InlineKeyboardButton(text="🖼 تغییر پوستر",callback_data=f"evp:{eid}")],[InlineKeyboardButton(text=("⛔ غیرفعال کردن" if e['active'] else "✅ فعال کردن"),callback_data=f"evt:{eid}")],[InlineKeyboardButton(text="⬅️ رویدادها",callback_data="a:edit_events")]]
    await edit_or_send(q,f"#{eid} {icon(e['kind'])} <b>{e['title']}</b>\nوضعیت: {'فعال' if e['active'] else 'غیرفعال'}\n\n{e['description'] or 'بدون توضیحات'}",K(rows)); await q.answer()

async def event_toggle(q:CallbackQuery):
    if not is_admin(q.from_user.id): return
    eid=int(q.data.split(":")[1]); c=db(); c.execute("UPDATE events SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?",(eid,)); c.commit(); c.close(); await event_manage(q)

async def event_edit_prompt(q:CallbackQuery,state:FSMContext):
    if not is_admin(q.from_user.id): return
    eid=int(q.data.split(":")[1]); await state.update_data(edit_event_id=eid); await remember_prompt(q,state,AdminState.edit_event,"✏️ فرمت ویرایش رویداد:\nنوع | عنوان | توضیحات\n\nمثال:\nسینما | فیلم جدید | توضیحات جدید",f"evm:{eid}"); await q.answer()

async def event_poster_prompt(q:CallbackQuery,state:FSMContext):
    if not is_admin(q.from_user.id): return
    eid=int(q.data.split(":")[1]); await state.update_data(poster_event_id=eid); await remember_prompt(q,state,AdminState.edit_poster,"🖼 عکس پوستر جدید را ارسال کنید.",f"evm:{eid}"); await q.answer()


async def show_manage(q:CallbackQuery,state:FSMContext):
    if not is_admin(q.from_user.id): return
    await state.clear()
    sid=int(q.data.split(":")[1]); c=db(); s=c.execute("SELECT s.*,e.title FROM shows s JOIN events e ON e.id=s.event_id WHERE s.id=?",(sid,)).fetchone(); n=c.execute("SELECT COUNT(*) n FROM seats WHERE show_id=?",(sid,)).fetchone()['n']; c.close()
    if not s: await q.answer("سانس پیدا نشد.",show_alert=True); return
    rows=[[InlineKeyboardButton(text="✏️ ویرایش سانس",callback_data=f"she:{sid}")],[InlineKeyboardButton(text="💺 ویرایش تعداد ردیف/صندلی",callback_data=f"se:{sid}")],[InlineKeyboardButton(text=("⛔ غیرفعال کردن" if s['active'] else "✅ فعال کردن"),callback_data=f"sht:{sid}")],[InlineKeyboardButton(text="⬅️ سانس‌ها",callback_data="a:edit_shows")]]
    await edit_or_send(q,f"#{sid} <b>{s['title']}</b>\n🕐 {s['show_at']}\n🏛 {s['hall']}\n💰 {money(s['base_price'])}\n💺 {n} صندلی\nوضعیت: {'فعال' if s['active'] else 'غیرفعال'}",K(rows)); await q.answer()

async def show_toggle(q:CallbackQuery):
    if not is_admin(q.from_user.id): return
    sid=int(q.data.split(":")[1]); c=db(); c.execute("UPDATE shows SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?",(sid,)); c.commit(); c.close(); await show_manage(q)

async def show_edit_prompt(q:CallbackQuery,state:FSMContext):
    if not is_admin(q.from_user.id): return
    sid=int(q.data.split(":")[1]); await state.update_data(edit_show_id=sid); await remember_prompt(q,state,AdminState.edit_show,"✏️ فرمت ویرایش سانس:\nتاریخ و ساعت | سالن | قیمت\n\nمثال:\n1405/07/20 20:00 | سالن اصلی | 300000",f"shm:{sid}"); await q.answer()


async def seat_manage(q:CallbackQuery,state:FSMContext):
    if not is_admin(q.from_user.id): return
    await state.clear()
    sid=int(q.data.split(":")[1]); c=db(); s=c.execute("SELECT s.*,e.title FROM shows s JOIN events e ON e.id=s.event_id WHERE s.id=?",(sid,)).fetchone(); seats=c.execute("SELECT * FROM seats WHERE show_id=? ORDER BY id",(sid,)).fetchall(); c.close()
    if not s: await q.answer("سانس پیدا نشد.",show_alert=True); return
    rows_count=len({x['label'].split('-')[0] for x in seats if '-' in x['label']}); per=max([int(x['label'].split('-')[1]) for x in seats if '-' in x['label']],default=0)
    text=f"💺 <b>نقشه صندلی #{sid}</b>\n{s['title']}\nتعداد ردیف: <b>{rows_count}</b>\nصندلی هر ردیف: <b>{per}</b>\nکل: <b>{len(seats)}</b> / {MAX_SEATS}\n\nشماره‌ها از 1 تا {len(seats)} به‌صورت پیوسته نمایش داده می‌شوند."
    buttons=[[InlineKeyboardButton(text="✏️ تغییر تعداد ردیف/صندلی",callback_data=f"se:{sid}")],[InlineKeyboardButton(text="⬅️ سانس",callback_data=f"shm:{sid}")]]
    await edit_or_send(q,text,K(buttons)); await q.answer()

async def seat_edit_prompt(q:CallbackQuery,state:FSMContext):
    if not is_admin(q.from_user.id): return
    sid=int(q.data.split(":")[1]); await state.update_data(edit_seat_show_id=sid); await remember_prompt(q,state,AdminState.edit_seats,f"✏️ تغییر ظرفیت سانس #{sid}\n\nفرمت:\nتعداد ردیف | تعداد صندلی هر ردیف\nمثال: 11 | 10\n\nحداکثر کل صندلی 110 است.\n⚠️ برای جلوگیری از خراب شدن فروش، این تغییر فقط وقتی انجام می‌شود که صندلی فروخته‌شده یا سفارش در انتظار وجود نداشته باشد.",f"sm:{sid}"); await q.answer()


async def card_manage(q:CallbackQuery,state:FSMContext):
    if not is_admin(q.from_user.id): return
    await state.clear()
    cid=int(q.data.split(":")[1]); c=db(); r=c.execute("SELECT * FROM cards WHERE id=?",(cid,)).fetchone(); c.close()
    if not r: await q.answer("کارت پیدا نشد.",show_alert=True); return
    owner=f"\n👤 صاحب حساب: {r['owner_name']}" if r['owner_name'] else ""
    rows=[[InlineKeyboardButton(text="✏️ ویرایش کارت",callback_data=f"ce:{cid}")],[InlineKeyboardButton(text=("⛔ غیرفعال کردن" if r['active'] else "✅ فعال کردن"),callback_data=f"ct:{cid}")],[InlineKeyboardButton(text="⬅️ کارت‌ها",callback_data="a:cards")]]
    await edit_or_send(q,f"#{cid} 💳 <b>{r['title']}</b>\nشماره: <code>{r['number']}</code>{owner}\nوضعیت: {'فعال' if r['active'] else 'غیرفعال'}",K(rows)); await q.answer()

async def card_toggle(q:CallbackQuery):
    if not is_admin(q.from_user.id): return
    cid=int(q.data.split(":")[1]); c=db(); c.execute("UPDATE cards SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?",(cid,)); c.commit(); c.close(); await card_manage(q)

async def card_edit_prompt(q:CallbackQuery,state:FSMContext):
    if not is_admin(q.from_user.id): return
    cid=int(q.data.split(":")[1]); await state.update_data(edit_card_id=cid); await remember_prompt(q,state,AdminState.edit_card,"✏️ فرمت ویرایش کارت:\nنام کارت | شماره کارت | نام صاحب حساب",f"cm:{cid}"); await q.answer()


async def receipt_view(q:CallbackQuery,bot:Bot):
    if not is_admin(q.from_user.id): return
    oid=int(q.data.split(":")[1]); c=db(); r=c.execute("SELECT o.*,e.title,s.show_at,s.hall,se.label FROM orders o JOIN shows s ON s.id=o.show_id JOIN events e ON e.id=s.event_id JOIN seats se ON se.id=o.seat_id WHERE o.id=?",(oid,)).fetchone(); c.close()
    if not r or not r['receipt_file_id']: await q.answer("رسید پیدا نشد.",show_alert=True); return
    await bot.send_photo(q.from_user.id,r['receipt_file_id'],caption=f"🧾 سفارش #{oid}\n{r['title']}\n🕐 {r['show_at']}\n💺 {r['label']}\n💰 {money(r['amount'])}",reply_markup=K([[InlineKeyboardButton(text="✅ تأیید",callback_data=f"approve:{oid}"),InlineKeyboardButton(text="❌ رد",callback_data=f"reject:{oid}")],[InlineKeyboardButton(text="⬅️ سفارش‌ها",callback_data="a:pending")]]))
    await q.answer()


async def admin_edit_text(message:Message,state:FSMContext):
    if not is_admin(message.from_user.id) or not message.text: return
    st=await state.get_state(); p=[x.strip() for x in message.text.split("|")]; data=await state.get_data(); c=db()
    try:
        if st==AdminState.edit_event.state:
            if len(p)!=3 or p[0] not in KINDS: raise ValueError("فرمت: نوع | عنوان | توضیحات")
            eid=data.get('edit_event_id'); c.execute("UPDATE events SET kind=?,title=?,description=? WHERE id=?",(p[0],p[1],p[2],eid)); msg=f"✅ رویداد #{eid} ویرایش شد."
        elif st==AdminState.edit_show.state:
            if len(p)!=3: raise ValueError("فرمت: تاریخ و ساعت | سالن | قیمت")
            sid=data.get('edit_show_id'); price=int(p[2].replace(',','')); c.execute("UPDATE shows SET show_at=?,hall=?,base_price=? WHERE id=?",(p[0],p[1],price,sid)); msg=f"✅ سانس #{sid} ویرایش شد."
        elif st==AdminState.edit_card.state:
            if len(p)!=3: raise ValueError("فرمت: نام کارت | شماره کارت | نام صاحب حساب")
            cid=data.get('edit_card_id'); c.execute("UPDATE cards SET title=?,number=?,owner_name=? WHERE id=?",(p[0],p[1],p[2],cid)); msg=f"✅ کارت #{cid} ویرایش شد."
        elif st==AdminState.edit_seats.state:
            if len(p)!=2: raise ValueError("فرمت: تعداد ردیف | تعداد صندلی هر ردیف")
            sid=data.get('edit_seat_show_id'); rr=int(p[0]); pp=int(p[1]); total=rr*pp
            if rr<1 or pp<1 or total>MAX_SEATS: raise ValueError(f"تعداد کل صندلی نباید بیشتر از {MAX_SEATS} باشد.")
            sold=c.execute("SELECT COUNT(*) n FROM seats WHERE show_id=? AND status IN ('sold','pending')",(sid,)).fetchone()['n']
            if sold: raise ValueError("این سانس صندلی فروخته‌شده یا سفارش در انتظار دارد؛ فعلاً ظرفیت آن قابل تغییر نیست.")
            c.execute("DELETE FROM seats WHERE show_id=?",(sid,))
            c.executemany("INSERT INTO seats(show_id,label,status) VALUES(?,?,'free')",[(sid,f"{r}-{n}") for r in range(1,rr+1) for n in range(1,pp+1)])
            msg=f"✅ ظرفیت سانس #{sid} به {rr} ردیف × {pp} صندلی ({total} صندلی) تغییر کرد."
        else: return
        c.commit(); await finish_flow(message,state,msg,main_kb(message.from_user.id)); await state.clear()
    except ValueError as e:
        await finish_flow(message,state,f"❌ {e}",back("admin"))
    finally: c.close()


async def admin_text(message:Message,state:FSMContext):
    if not is_admin(message.from_user.id) or not message.text: return
    st=await state.get_state(); p=[x.strip() for x in message.text.split("|")]; c=db()
    try:
        if st==AdminState.event.state:
            if len(p)!=3 or p[0] not in KINDS: raise ValueError("فرمت: نوع | عنوان | توضیحات")
            eid=c.execute("INSERT INTO events(kind,title,description,created_at) VALUES(?,?,?,?)",(p[0],p[1],p[2],now_iso())).lastrowid; msg=f"✅ رویداد #{eid} ثبت شد."
        elif st==AdminState.show.state:
            if len(p)!=4: raise ValueError("فرمت: شناسه رویداد | تاریخ و ساعت | سالن | قیمت")
            eid=int(p[0]); price=int(p[3].replace(',','')); exists=c.execute("SELECT id FROM events WHERE id=?",(eid,)).fetchone()
            if not exists: raise ValueError("شناسه رویداد پیدا نشد.")
            sid=c.execute("INSERT INTO shows(event_id,show_at,hall,base_price) VALUES(?,?,?,?)",(eid,p[1],p[2],price)).lastrowid; msg=f"✅ سانس #{sid} ثبت شد."
        elif st==AdminState.seats.state:
            if len(p)!=3: raise ValueError("فرمت: شناسه سانس | تعداد ردیف | تعداد صندلی هر ردیف")
            sid=int(p[0]); rr=int(p[1]); pp=int(p[2]); total=rr*pp
            if rr<1 or pp<1 or total>MAX_SEATS: raise ValueError(f"حداکثر {MAX_SEATS} صندلی مجاز است.")
            if not c.execute("SELECT id FROM shows WHERE id=?",(sid,)).fetchone(): raise ValueError("شناسه سانس پیدا نشد.")
            c.execute("DELETE FROM seats WHERE show_id=?",(sid,)); c.executemany("INSERT INTO seats(show_id,label,status) VALUES(?,?,'free')",[(sid,f"{r}-{n}") for r in range(1,rr+1) for n in range(1,pp+1)]); msg=f"✅ {total} صندلی در {rr} ردیف برای سانس #{sid} ساخته شد."
        elif st==AdminState.card.state:
            if len(p)!=3: raise ValueError("فرمت: نام کارت | شماره کارت | نام صاحب حساب")
            cid=c.execute("INSERT INTO cards(title,number,owner_name) VALUES(?,?,?)",(p[0],p[1],p[2])).lastrowid; msg=f"✅ کارت #{cid} ثبت شد."
        elif st==AdminState.discount.state:
            if len(p)!=2: raise ValueError("فرمت: آیدی کاربر | درصد یا قیمت ثابت")
            uid=int(p[0]); value=float(p[1].replace(',',''))
            if value<=100: c.execute("INSERT INTO discounts(user_id,percent,fixed_price) VALUES(?,?,NULL) ON CONFLICT(user_id) DO UPDATE SET percent=excluded.percent,fixed_price=NULL",(uid,value)); msg=f"✅ تخفیف {value:g}% برای {uid} ثبت شد."
            else: c.execute("INSERT INTO discounts(user_id,percent,fixed_price) VALUES(?,0,?) ON CONFLICT(user_id) DO UPDATE SET percent=0,fixed_price=excluded.fixed_price",(uid,int(value))); msg=f"✅ قیمت ثابت {money(value)} برای {uid} ثبت شد."
        elif st==AdminState.poster.state:
            eid=int(p[0]); await state.update_data(poster_event_id=eid); await finish_flow(message,state,"🖼 حالا عکس پوستر را ارسال کن.",back(f"evm:{eid}")); await state.set_state(AdminState.edit_poster); return
        else: return
        c.commit(); await finish_flow(message,state,msg,main_kb(message.from_user.id)); await state.clear()
    except ValueError as e:
        await finish_flow(message,state,f"❌ {e}",back("admin"))
    finally: c.close()


async def admin_poster(message:Message,state:FSMContext):
    if not is_admin(message.from_user.id) or not message.photo: return
    data=await state.get_data(); eid=data.get('poster_event_id')
    if not eid: await message.answer("شناسه رویداد مشخص نیست."); return
    c=db(); c.execute("UPDATE events SET poster_file_id=? WHERE id=?",(message.photo[-1].file_id,eid)); c.commit(); c.close(); await finish_flow(message,state,f"✅ پوستر رویداد #{eid} ثبت شد.",main_kb(message.from_user.id)); await state.clear()


async def admin_edit_poster(message:Message,state:FSMContext):
    if not is_admin(message.from_user.id) or not message.photo: return
    data=await state.get_data(); eid=data.get('poster_event_id')
    if not eid: await message.answer("شناسه رویداد مشخص نیست."); return
    c=db(); c.execute("UPDATE events SET poster_file_id=? WHERE id=?",(message.photo[-1].file_id,eid)); c.commit(); c.close(); await finish_flow(message,state,f"✅ پوستر رویداد #{eid} تغییر کرد.",main_kb(message.from_user.id)); await state.clear()


async def noop(q:CallbackQuery): await q.answer("این صندلی در حال حاضر آزاد نیست.",show_alert=True)

async def home(q:CallbackQuery):
    await q.message.edit_text("🎨 <b>کارگزاری به وقت هنر</b>\n\nسینما، تئاتر، موسیقی و رویدادهای فرهنگی.",reply_markup=main_kb(q.from_user.id)); await q.answer()

async def cmd_admin(message:Message):
    if is_admin(message.from_user.id): await message.answer("پنل مدیریت:",reply_markup=K([[InlineKeyboardButton(text="👨‍💼 ورود به پنل",callback_data="admin")]]))
    else: await message.answer("دسترسی ندارید.")


async def main():
    if not BOT_TOKEN: raise RuntimeError("BOT_TOKEN تنظیم نشده است.")
    init_db(); bot=Bot(BOT_TOKEN,default=DefaultBotProperties(parse_mode=ParseMode.HTML)); dp=Dispatcher()
    dp.message.register(start,CommandStart()); dp.message.register(cmd_admin,Command("admin"))
    dp.callback_query.register(events,F.data=="events"); dp.callback_query.register(mytickets,F.data=="mytickets"); dp.callback_query.register(home,F.data=="home"); dp.callback_query.register(admin_panel,F.data=="admin")
    dp.callback_query.register(admin_action,F.data.startswith("a:")); dp.callback_query.register(event_manage,F.data.startswith("evm:")); dp.callback_query.register(event_edit_prompt,F.data.startswith("eve:")); dp.callback_query.register(event_poster_prompt,F.data.startswith("evp:")); dp.callback_query.register(event_toggle,F.data.startswith("evt:"))
    dp.callback_query.register(show_manage,F.data.startswith("shm:")); dp.callback_query.register(show_edit_prompt,F.data.startswith("she:")); dp.callback_query.register(show_toggle,F.data.startswith("sht:")); dp.callback_query.register(seat_manage,F.data.startswith("sm:")); dp.callback_query.register(seat_edit_prompt,F.data.startswith("se:"))
    dp.callback_query.register(card_manage,F.data.startswith("cm:")); dp.callback_query.register(card_edit_prompt,F.data.startswith("ce:")); dp.callback_query.register(card_toggle,F.data.startswith("ct:")); dp.callback_query.register(receipt_view,F.data.startswith("receiptview:"))
    dp.callback_query.register(event_detail,F.data.startswith("event:")); dp.callback_query.register(cancel_reservation,F.data.startswith("cancel:")); dp.callback_query.register(seat_page,F.data.startswith("seatpage:")); dp.callback_query.register(show_detail,F.data.startswith("show:")); dp.callback_query.register(seat_pick,F.data.startswith("seat:")); dp.callback_query.register(choose_card,F.data.startswith("paycard:")); dp.callback_query.register(approve,F.data.startswith("approve:")); dp.callback_query.register(reject,F.data.startswith("reject:")); dp.callback_query.register(noop,F.data=="noop")
    dp.message.register(admin_poster,AdminState.poster,F.photo); dp.message.register(admin_edit_poster,AdminState.edit_poster,F.photo)
    for st in (AdminState.edit_event,AdminState.edit_show,AdminState.edit_card,AdminState.edit_seats): dp.message.register(admin_edit_text,st,F.text)
    for st in (AdminState.event,AdminState.show,AdminState.seats,AdminState.card,AdminState.discount,AdminState.poster): dp.message.register(admin_text,st,F.text)
    dp.message.register(receipt,F.photo)
    await dp.start_polling(bot)

if __name__=="__main__": asyncio.run(main())
