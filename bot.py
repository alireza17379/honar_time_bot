import os, io, sqlite3, asyncio, logging, re
from datetime import datetime, timedelta

import cv2
import numpy as np
import qrcode
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile, InputMediaPhoto, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("honar_time_bot")
BOT_TOKEN=os.getenv("BOT_TOKEN","").strip()
DB_PATH=os.getenv("DB_PATH","honar_time.db")
HOLD_MINUTES=max(1,int(os.getenv("HOLD_MINUTES","10")))
ADMIN_IDS={int(x.strip()) for x in os.getenv("ADMIN_IDS","").split(",") if x.strip().isdigit()}
MAX_SEATS=110
KINDS={"سینما":"🎬","تئاتر":"🎭","موسیقی":"🎵","فرهنگی":"🎤","سایر":"🎨"}


def db():
    c=sqlite3.connect(DB_PATH,timeout=30)
    c.row_factory=sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA busy_timeout=30000")
    return c

def now(): return datetime.now()
def now_iso(): return now().isoformat(timespec="seconds")
def money(n): return f"{int(n):,} تومان"
def icon(k): return KINDS.get(k,"🎨")
def is_admin(uid): return uid in ADMIN_IDS
def K(rows): return InlineKeyboardMarkup(inline_keyboard=rows)
def fa(n): return str(n).translate(str.maketrans("0123456789","۰۱۲۳۴۵۶۷۸۹"))

def init_db():
    c=db(); c.executescript("""
    CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT NOT NULL,title TEXT NOT NULL,description TEXT DEFAULT '',poster_file_id TEXT DEFAULT '',active INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS shows(id INTEGER PRIMARY KEY AUTOINCREMENT,event_id INTEGER NOT NULL,show_at TEXT NOT NULL,hall TEXT NOT NULL DEFAULT 'سالن اصلی',base_price INTEGER NOT NULL,active INTEGER NOT NULL DEFAULT 1,FOREIGN KEY(event_id) REFERENCES events(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS seats(id INTEGER PRIMARY KEY AUTOINCREMENT,show_id INTEGER NOT NULL,label TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'free',hold_until TEXT,UNIQUE(show_id,label),FOREIGN KEY(show_id) REFERENCES shows(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS cards(id INTEGER PRIMARY KEY AUTOINCREMENT,title TEXT NOT NULL,number TEXT NOT NULL,owner_name TEXT NOT NULL DEFAULT '',active INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE IF NOT EXISTS discounts(user_id INTEGER PRIMARY KEY,percent REAL NOT NULL DEFAULT 0,fixed_price INTEGER);
    CREATE TABLE IF NOT EXISTS customers(user_id INTEGER PRIMARY KEY,first_name TEXT NOT NULL DEFAULT '',last_name TEXT NOT NULL DEFAULT '',phone TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,show_id INTEGER NOT NULL,seat_id INTEGER NOT NULL,amount INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'pending',ticket_code TEXT UNIQUE,receipt_file_id TEXT,checkout_id TEXT,created_at TEXT NOT NULL,approved_at TEXT,used_at TEXT,used_by INTEGER,FOREIGN KEY(show_id) REFERENCES shows(id),FOREIGN KEY(seat_id) REFERENCES seats(id));
    CREATE TABLE IF NOT EXISTS keywords(id INTEGER PRIMARY KEY AUTOINCREMENT,keyword TEXT UNIQUE NOT NULL,response TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1);
    CREATE INDEX IF NOT EXISTS idx_orders_checkout ON orders(checkout_id); CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status); CREATE INDEX IF NOT EXISTS idx_seats_show_status ON seats(show_id,status);
    """)
    # Safe migrations for older databases.
    for table, col, ddl in [
        ("cards","owner_name","ALTER TABLE cards ADD COLUMN owner_name TEXT NOT NULL DEFAULT ''"),
        ("orders","used_at","ALTER TABLE orders ADD COLUMN used_at TEXT"),
        ("orders","used_by","ALTER TABLE orders ADD COLUMN used_by INTEGER"),
        ("orders","checkout_id","ALTER TABLE orders ADD COLUMN checkout_id TEXT"),
    ]:
        cols={r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
        if col not in cols: c.execute(ddl)
    c.commit(); c.close()

def clean_holds():
    c=db(); c.execute("UPDATE seats SET status='free',hold_until=NULL WHERE status='held' AND hold_until IS NOT NULL AND hold_until<?",(now_iso(),)); c.commit(); c.close()

def final_price(uid,base):
    c=db(); d=c.execute("SELECT * FROM discounts WHERE user_id=?",(uid,)).fetchone(); c.close()
    if not d:return int(base)
    if d["fixed_price"] is not None:return max(0,int(d["fixed_price"]))
    return max(0,round(int(base)*(1-float(d["percent"])/100)))

def back(data): return K([[InlineKeyboardButton(text="⬅️ بازگشت",callback_data=data)]])

async def edit_or_send(q,text,markup=None):
    try: await q.message.edit_text(text,reply_markup=markup)
    except Exception: await q.message.answer(text,reply_markup=markup)

class UserState(StatesGroup):
    first=State(); last=State(); phone=State(); waiting_receipt=State(); scan_qr=State()
class AdminState(StatesGroup):
    event=State(); show=State(); seats=State(); card=State(); discount=State(); poster=State(); keyword=State(); edit_event=State(); edit_show=State(); edit_card=State(); edit_seats=State(); edit_poster=State(); edit_keyword=State()

def main_kb(uid):
    rows=[[InlineKeyboardButton(text="🎭 رویدادها",callback_data="events")],[InlineKeyboardButton(text="🎟 بلیت‌های من",callback_data="mytickets")]]
    if is_admin(uid): rows.append([InlineKeyboardButton(text="👨‍💼 پنل مدیریت",callback_data="admin")])
    return K(rows)

async def ensure_customer(message,state):
    c=db(); r=c.execute("SELECT * FROM customers WHERE user_id=?",(message.from_user.id,)).fetchone(); c.close()
    return r

async def start(message:Message,state:FSMContext):
    await state.clear()
    c=db(); r=c.execute("SELECT * FROM customers WHERE user_id=?",(message.from_user.id,)).fetchone(); c.close()
    if not r:
        await state.set_state(UserState.first); await message.answer("👤 برای ثبت اطلاعات مشتری، نام خود را وارد کنید:"); return
    await message.answer("🎨 <b>کارگزاری به وقت هنر</b>\n\nرویداد را انتخاب کنید:",reply_markup=main_kb(message.from_user.id))

async def profile_first(message,state):
    if not message.text:return
    await state.update_data(first_name=message.text.strip()); await state.set_state(UserState.last); await message.answer("نام خانوادگی را وارد کنید:")
async def profile_last(message,state):
    if not message.text:return
    await state.update_data(last_name=message.text.strip()); await state.set_state(UserState.phone)
    kb=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📱 ارسال شماره تماس",request_contact=True)]],resize_keyboard=True,one_time_keyboard=True)
    await message.answer("شماره تماس را ارسال کنید یا به‌صورت عددی بنویسید:",reply_markup=kb)
async def profile_phone(message,state):
    phone=message.contact.phone_number if message.contact else (message.text or "").strip()
    if not re.fullmatch(r"\+?[0-9۰-۹\- ]{7,20}",phone): await message.answer("❌ شماره تماس معتبر نیست. دوباره وارد کنید."); return
    d=await state.get_data(); c=db(); c.execute("INSERT INTO customers(user_id,first_name,last_name,phone,created_at,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET first_name=excluded.first_name,last_name=excluded.last_name,phone=excluded.phone,updated_at=excluded.updated_at",(message.from_user.id,d["first_name"],d["last_name"],phone,now_iso(),now_iso())); c.commit(); c.close(); await state.clear(); await message.answer("✅ اطلاعات شما ثبت شد.",reply_markup=ReplyKeyboardRemove()); await message.answer("🎨 منوی اصلی:",reply_markup=main_kb(message.from_user.id))

async def events(q):
    clean_holds(); c=db(); rows=c.execute("SELECT * FROM events WHERE active=1 ORDER BY id DESC").fetchall(); c.close(); buttons=[[InlineKeyboardButton(text=f"{icon(r['kind'])} {r['title']}",callback_data=f"event:{r['id']}")] for r in rows]; buttons.append([InlineKeyboardButton(text="🏠 منوی اصلی",callback_data="home")]); await edit_or_send(q,"🎟 <b>رویدادهای فعال</b>" if rows else "فعلاً رویداد فعالی ثبت نشده است.",K(buttons)); await q.answer()

async def event_detail(q):
    eid=int(q.data.split(":")[1]); c=db(); e=c.execute("SELECT * FROM events WHERE id=? AND active=1",(eid,)).fetchone(); shows=c.execute("SELECT * FROM shows WHERE event_id=? AND active=1 ORDER BY show_at",(eid,)).fetchall(); c.close()
    if not e: await q.answer("رویداد پیدا نشد.",show_alert=True); return
    text=f"{icon(e['kind'])} <b>{e['title']}</b>\n\n{e['description'] or 'بدون توضیحات'}\n\n🕐 سانس را انتخاب کنید:"
    rows=[[InlineKeyboardButton(text=f"🕐 {s['show_at']} | {s['hall']} | {money(s['base_price'])}",callback_data=f"show:{s['id']}")] for s in shows]; rows.append([InlineKeyboardButton(text="⬅️ رویدادها",callback_data="events")]); mk=K(rows)
    if e["poster_file_id"]:
        try: await q.message.edit_media(InputMediaPhoto(media=e["poster_file_id"],caption=text),reply_markup=mk)
        except Exception:
            try: await q.message.delete()
            except Exception: pass
            await q.message.answer_photo(e["poster_file_id"],caption=text,reply_markup=mk)
    else: await edit_or_send(q,text,mk)
    await q.answer()

def get_layout(c,sid):
    seats=c.execute("SELECT * FROM seats WHERE show_id=? ORDER BY id",(sid,)).fetchall()
    if not seats:
        c.executemany("INSERT INTO seats(show_id,label,status) VALUES(?,?,'free')",[(sid,f"{r}-{n}") for r in range(1,12) for n in range(1,11)]); c.commit(); seats=c.execute("SELECT * FROM seats WHERE show_id=? ORDER BY id",(sid,)).fetchall()
    return seats

def seat_rows(seats,sid,page=1,per_page=4):
    parsed=[]
    for s in seats:
        try:r,n=map(int,s["label"].split("-",1)); parsed.append((r,n,s))
        except: pass
    if not parsed:return [],1
    rownums=sorted({r for r,_,_ in parsed}); cols=max(n for _,n,_ in parsed); pages=max(1,(len(rownums)+per_page-1)//per_page); page=max(1,min(page,pages)); selected=rownums[(page-1)*per_page:page*per_page]; by={(r,n):s for r,n,s in parsed}; marks={"free":"🟩","held":"🟨","pending":"🟧","sold":"🟥"}; out=[]
    for r in selected:
        line=[]
        for n in range(1,cols+1):
            s=by.get((r,n));
            if not s:continue
            num=(r-1)*cols+n; cb=f"seat:{sid}:{s['id']}" if s["status"]=="free" else "noop"; line.append(InlineKeyboardButton(text=f"{marks.get(s['status'],'⬜')}{fa(num)}",callback_data=cb))
            if len(line)==5:out.append(line);line=[]
        if line:out.append(line)
    nav=[]
    if page>1:nav.append(InlineKeyboardButton(text="⬅️ قبلی",callback_data=f"seatpage:{sid}:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"صفحه {fa(page)}/{fa(pages)}",callback_data="noop"))
    if page<pages:nav.append(InlineKeyboardButton(text="بعدی ➡️",callback_data=f"seatpage:{sid}:{page+1}"))
    out.append(nav); return out,pages

async def render_seats(chat,sid,page=1,selected=None,message=None):
    clean_holds(); c=db(); s=c.execute("SELECT s.*,e.title,e.kind FROM shows s JOIN events e ON e.id=s.event_id WHERE s.id=? AND s.active=1",(sid,)).fetchone()
    if not s:c.close();return False
    seats=get_layout(c,sid); rows,pages=seat_rows(seats,sid,page); c.close(); selected=selected or []
    selected_labels=[]
    byid={x["id"]:x for x in seats}
    for i in selected:
        if i in byid:selected_labels.append(byid[i]["label"])
    text=f"💺 <b>انتخاب صندلی</b>\n\n🎭 {s['title']}\n🕐 {s['show_at']} | 🏛 {s['hall']}\n💰 هر صندلی: {money(s['base_price'])}\n\n🟩 آزاد  🟨 رزرو موقت  🟧 در انتظار  🟥 فروخته‌شده\n"
    if selected_labels:text+=f"\nانتخاب شما: <b>{', '.join(selected_labels)}</b>\n"
    text+=f"\nصفحه {fa(page)} از {fa(pages)}\nروی صندلی آزاد بزنید:"
    if selected_labels: rows.insert(-1,[InlineKeyboardButton(text=f"✅ ادامه پرداخت ({fa(len(selected_labels))} صندلی)",callback_data=f"checkout:{sid}")])
    rows.append([InlineKeyboardButton(text="❌ لغو انتخاب",callback_data=f"clearselect:{sid}")])
    try:
        if message: await message.edit_text(text,reply_markup=K(rows),parse_mode=ParseMode.HTML)
        else: await chat.send_message(text,reply_markup=K(rows),parse_mode=ParseMode.HTML)
    except Exception: log.exception("SEAT_MAP_SEND_ERROR show_id=%s page=%s",sid,page); raise
    return True

async def show_detail(q,state):
    try:ok=await render_seats(q.message.chat,int(q.data.split(":")[1]),1,[])
    except Exception as e:log.exception("seat map send failed: %r",e);await q.answer(f"خطای نمایش صندلی: {type(e).__name__}",show_alert=True);return
    await q.answer("نقشه صندلی ارسال شد." if ok else "سانس پیدا نشد.",show_alert=not ok)

async def seat_page(q,state):
    _,sid,page=q.data.split(":"); d=await state.get_data(); await render_seats(q.message.chat,int(sid),int(page),d.get("selected_seats",[]),q.message); await q.answer()

async def seat_pick(q,state):
    _,sid,seatid=q.data.split(":"); sid=int(sid); seatid=int(seatid); uid=q.from_user.id
    if not await ensure_customer(q.message,state): await q.answer("ابتدا اطلاعات مشتری را ثبت کنید.",show_alert=True); return
    d=await state.get_data(); selected=list(d.get("selected_seats",[]))
    if seatid in selected:
        selected.remove(seatid); c=db(); c.execute("UPDATE seats SET status='free',hold_until=NULL WHERE id=? AND status='held'",(seatid,)); c.commit(); c.close()
    else:
        c=db(); until=(now()+timedelta(minutes=HOLD_MINUTES)).isoformat(timespec="seconds"); cur=c.execute("UPDATE seats SET status='held',hold_until=? WHERE id=? AND show_id=? AND status='free'",(until,seatid,sid)); c.commit(); c.close()
        if cur.rowcount!=1: await q.answer("این صندلی دیگر آزاد نیست.",show_alert=True); return
        selected.append(seatid)
    await state.update_data(show_id=sid,selected_seats=selected); await render_seats(q.message.chat,sid,1,selected,q.message); await q.answer("انتخاب شد." if seatid in selected else "از انتخاب خارج شد.")

async def clear_select(q,state):
    d=await state.get_data(); c=db();
    for sid in d.get("selected_seats",[]):c.execute("UPDATE seats SET status='free',hold_until=NULL WHERE id=? AND status='held'",(sid,))
    c.commit();c.close();await state.update_data(selected_seats=[]);await render_seats(q.message.chat,int(q.data.split(":")[1]),1,[],q.message);await q.answer("انتخاب‌ها لغو شد.")

async def checkout(q,state):
    d=await state.get_data(); sid=int(q.data.split(":")[1]); selected=d.get("selected_seats",[])
    if not selected:await q.answer("هنوز صندلی انتخاب نکرده‌اید.",show_alert=True);return
    c=db(); rows=c.execute("SELECT * FROM seats WHERE show_id=? AND id IN (%s) AND status='held'"%(",".join("?"*len(selected)),),[sid,*selected]).fetchall(); s=c.execute("SELECT * FROM shows WHERE id=?",(sid,)).fetchone(); cards=c.execute("SELECT * FROM cards WHERE active=1 ORDER BY id").fetchall();c.close()
    if len(rows)!=len(selected):await q.answer("یکی از صندلی‌ها دیگر قابل رزرو نیست. نقشه را بروزرسانی کنید.",show_alert=True);return
    amount=final_price(q.from_user.id,s["base_price"])*len(rows);await state.update_data(amount=amount,show_id=sid)
    lines=", ".join(r["label"] for r in rows); kb=[[InlineKeyboardButton(text=f"💳 {x['title']}",callback_data=f"paycard:{x['id']}")] for x in cards];kb.append([InlineKeyboardButton(text="⬅️ برگشت به صندلی‌ها",callback_data=f"backseats:{sid}")]);
    await q.message.edit_text(f"🎟 <b>خلاصه سفارش</b>\n\n💺 صندلی‌ها: <b>{lines}</b>\n💰 مبلغ کل: <b>{money(amount)}</b>\n\nکارت پرداخت را انتخاب کنید:",reply_markup=K(kb));await q.answer()

async def back_seats(q,state):
    d=await state.get_data();await render_seats(q.message.chat,int(q.data.split(":")[1]),1,d.get("selected_seats",[]),q.message);await q.answer()

async def choose_card(q,state):
    d=await state.get_data();cid=int(q.data.split(":")[1]);c=db();card=c.execute("SELECT * FROM cards WHERE id=? AND active=1",(cid,)).fetchone();c.close()
    if not card or not d.get("selected_seats"):await q.answer("سفارش منقضی شده است.",show_alert=True);return
    await state.set_state(UserState.waiting_receipt);await state.update_data(card_id=cid)
    await q.message.edit_text(f"💳 <b>{card['title']}</b>\nشماره کارت: <code>{card['number']}</code>\n👤 صاحب حساب: <b>{card['owner_name']}</b>\n\nمبلغ: <b>{money(d['amount'])}</b>\n\nبعد از پرداخت، <b>عکس رسید</b> را همینجا ارسال کنید.",reply_markup=K([[InlineKeyboardButton(text="❌ لغو",callback_data=f"clearselect:{d['show_id']}")]]));await q.answer()

async def receipt(message,state,bot):
    if not message.photo or await state.get_state()!=UserState.waiting_receipt.state:return
    d=await state.get_data(); selected=d.get("selected_seats",[]); sid=d.get("show_id");
    if not selected:return
    c=db(); seats=c.execute("SELECT * FROM seats WHERE show_id=? AND id IN (%s) AND status='held'"%(",".join("?"*len(selected)),),[sid,*selected]).fetchall()
    if len(seats)!=len(selected):c.close();await state.clear();await message.answer("⏰ یکی از رزروها منقضی شده است.",reply_markup=main_kb(message.from_user.id));return
    cust=c.execute("SELECT * FROM customers WHERE user_id=?",(message.from_user.id,)).fetchone(); checkout=f"C{int(datetime.now().timestamp()*1000)}-{message.from_user.id}"; nowx=now_iso()
    ids=[]
    for seat in seats:
        oid=c.execute("INSERT INTO orders(user_id,show_id,seat_id,amount,status,receipt_file_id,checkout_id,created_at) VALUES(?,?,?,?,?,?,?,?)",(message.from_user.id,sid,seat["id"],final_price(message.from_user.id,c.execute("SELECT base_price FROM shows WHERE id=?",(sid,)).fetchone()[0]),"pending",message.photo[-1].file_id,checkout,nowx)).lastrowid;ids.append(oid);c.execute("UPDATE seats SET status='pending',hold_until=NULL WHERE id=?",(seat["id"],))
    c.commit();c.close();await state.clear();await message.answer(f"🧾 رسید دریافت شد. سفارش <b>{checkout}</b> در انتظار بررسی مدیر است.",reply_markup=main_kb(message.from_user.id))
    c=db();s=c.execute("SELECT s.*,e.title FROM shows s JOIN events e ON e.id=s.event_id WHERE s.id=?",(sid,)).fetchone();c.close();name=f"{cust['first_name']} {cust['last_name']}" if cust else "-";labels=", ".join(x["label"] for x in seats);caption=f"🧾 <b>رسید جدید</b>\nسفارش: <code>{checkout}</code>\nنام: {name}\n📞 {cust['phone'] if cust else '-'}\n🎭 {s['title']}\n🕐 {s['show_at']}\n💺 صندلی‌ها: {labels}\n💰 مبلغ: {money(sum(final_price(message.from_user.id,s['base_price']) for _ in seats))}"
    kb=K([[InlineKeyboardButton(text="✅ تأیید همه",callback_data=f"approve:{checkout}"),InlineKeyboardButton(text="❌ رد همه",callback_data=f"reject:{checkout}")]])
    for aid in ADMIN_IDS:
        try:await bot.send_photo(aid,message.photo[-1].file_id,caption=caption,reply_markup=kb)
        except Exception:log.exception("receipt delivery failed admin=%s",aid)

async def approve(q,bot):
    if not is_admin(q.from_user.id):return
    key=q.data.split(":",1)[1];c=db();rows=c.execute("SELECT o.*,e.title,e.kind,s.show_at,s.hall,se.label FROM orders o JOIN shows s ON s.id=o.show_id JOIN events e ON e.id=s.event_id JOIN seats se ON se.id=o.seat_id WHERE o.checkout_id=? AND o.status='pending'",(key,)).fetchall()
    if not rows:c.close();await q.answer("این سفارش قبلاً بررسی شده.",show_alert=True);return
    codes=[]
    for o in rows:
        code=f"HT-{datetime.now().strftime('%y%m%d')}-{o['id']:06d}";codes.append((o,code));c.execute("UPDATE orders SET status='approved',ticket_code=?,approved_at=? WHERE id=?",(code,now_iso(),o['id']));c.execute("UPDATE seats SET status='sold',hold_until=NULL WHERE id=?",(o['seat_id'],))
    c.commit();c.close();await q.message.edit_caption(caption=f"✅ سفارش {key} تأیید شد.\n🎟 {len(rows)} بلیت صادر شد.")
    for o,code in codes:
        try:
            await bot.send_message(o['user_id'],f"🎟 <b>بلیت قطعی</b>\n{icon(o['kind'])} {o['title']}\n🕐 {o['show_at']}\n🏛 {o['hall']}\n💺 صندلی: <b>{o['label']}</b>\n🔑 <code>{code}</code>")
            await bot.send_photo(o['user_id'],make_qr(code),caption=f"📱 QR بلیت {code}")
        except Exception:log.exception("ticket delivery failed %s",code)
    await q.answer("همه بلیت‌ها صادر شدند.")

def make_qr(code):
    b=io.BytesIO();qrcode.make(code).save(b,format="PNG");return BufferedInputFile(b.getvalue(),filename=f"{code}.png")

async def reject(q,bot):
    if not is_admin(q.from_user.id):return
    key=q.data.split(":",1)[1];c=db();rows=c.execute("SELECT * FROM orders WHERE checkout_id=? AND status='pending'",(key,)).fetchall()
    if not rows:c.close();await q.answer("قبلاً بررسی شده.",show_alert=True);return
    uid=rows[0]['user_id']
    for o in rows:c.execute("UPDATE orders SET status='rejected' WHERE id=?",(o['id'],));c.execute("UPDATE seats SET status='free',hold_until=NULL WHERE id=?",(o['seat_id'],))
    c.commit();c.close();await q.message.edit_caption(caption=f"❌ سفارش {key} رد شد و صندلی‌ها آزاد شدند.");
    try:await bot.send_message(uid,f"❌ سفارش {key} تأیید نشد و صندلی‌ها آزاد شدند.")
    except:pass
    await q.answer("سفارش رد شد.")

async def mytickets(q):
    c=db();rows=c.execute("SELECT o.*,e.title,e.kind,s.show_at,s.hall,se.label FROM orders o JOIN shows s ON s.id=o.show_id JOIN events e ON e.id=s.event_id JOIN seats se ON se.id=o.seat_id WHERE o.user_id=? AND o.status='approved' ORDER BY o.id DESC",(q.from_user.id,)).fetchall();c.close();text="🎟 هنوز بلیت قطعی ندارید." if not rows else "🎟 <b>بلیت‌های من</b>\n\n"+"\n\n".join(f"{icon(r['kind'])} {r['title']}\n🕐 {r['show_at']} | 💺 {r['label']}\n🔑 <code>{r['ticket_code']}</code>" for r in rows);await edit_or_send(q,text,back("home"));await q.answer()

async def home(q):await q.message.edit_text("🎨 <b>کارگزاری به وقت هنر</b>",reply_markup=main_kb(q.from_user.id));await q.answer()
async def noop(q):await q.answer("این صندلی آزاد نیست.",show_alert=True)

# --- Minimal but complete admin CRUD ---
async def admin_panel(q,state):
    if not is_admin(q.from_user.id):return
    await state.clear();rows=[[InlineKeyboardButton(text="➕ رویداد",callback_data="a:event"),InlineKeyboardButton(text="✏️ رویدادها",callback_data="a:edit_events")],[InlineKeyboardButton(text="➕ سانس",callback_data="a:show"),InlineKeyboardButton(text="✏️ سانس‌ها",callback_data="a:edit_shows")],[InlineKeyboardButton(text="💺 صندلی‌ها",callback_data="a:seats_menu")],[InlineKeyboardButton(text="💳 کارت‌ها",callback_data="a:cards")],[InlineKeyboardButton(text="🧾 سفارش‌های در انتظار",callback_data="a:pending")],[InlineKeyboardButton(text="🔑 کلیدواژه‌ها",callback_data="a:keywords")],[InlineKeyboardButton(text="📱 اسکن QR ورود",callback_data="scanqr")]];await edit_or_send(q,"👨‍💼 <b>پنل مدیریت</b>",K(rows));await q.answer()

async def admin_action(q,state):
    if not is_admin(q.from_user.id):return
    a=q.data.split(":",1)[1];c=db()
    if a=="edit_events": rows=c.execute("SELECT id,kind,title,active FROM events ORDER BY id DESC").fetchall();c.close();await edit_or_send(q,"✏️ انتخاب رویداد:",K([[InlineKeyboardButton(text=f"#{r['id']} {icon(r['kind'])} {r['title']}",callback_data=f"evm:{r['id']}")] for r in rows]+[[InlineKeyboardButton(text="⬅️ مدیریت",callback_data="admin")]]));await q.answer();return
    if a=="edit_shows": rows=c.execute("SELECT s.id,s.show_at,e.title FROM shows s JOIN events e ON e.id=s.event_id ORDER BY s.id DESC").fetchall();c.close();await edit_or_send(q,"✏️ انتخاب سانس:",K([[InlineKeyboardButton(text=f"#{r['id']} {r['title']} | {r['show_at']}",callback_data=f"shm:{r['id']}")] for r in rows]+[[InlineKeyboardButton(text="⬅️ مدیریت",callback_data="admin")]]));await q.answer();return
    if a=="seats_menu": rows=c.execute("SELECT s.id,s.show_at,e.title,COUNT(se.id)n FROM shows s JOIN events e ON e.id=s.event_id LEFT JOIN seats se ON se.show_id=s.id GROUP BY s.id ORDER BY s.id DESC").fetchall();c.close();await edit_or_send(q,"💺 انتخاب سانس برای مدیریت صندلی:",K([[InlineKeyboardButton(text=f"#{r['id']} {r['title']} | {r['show_at']} | {r['n']} صندلی",callback_data=f"sm:{r['id']}")] for r in rows]+[[InlineKeyboardButton(text="➕ ساخت صندلی",callback_data="a:seats"),InlineKeyboardButton(text="⬅️ مدیریت",callback_data="admin")]]));await q.answer();return
    if a=="cards": rows=c.execute("SELECT * FROM cards ORDER BY id").fetchall();c.close();await edit_or_send(q,"💳 کارت‌های بانکی:",K([[InlineKeyboardButton(text=f"#{r['id']} {r['title']}",callback_data=f"cm:{r['id']}")] for r in rows]+[[InlineKeyboardButton(text="➕ افزودن کارت",callback_data="a:card")],[InlineKeyboardButton(text="⬅️ مدیریت",callback_data="admin")]]));await q.answer();return
    if a=="pending": rows=c.execute("SELECT checkout_id,MAX(id) id,COUNT(*) n,MAX(receipt_file_id) receipt FROM orders WHERE status='pending' GROUP BY checkout_id ORDER BY MAX(id) DESC LIMIT 30").fetchall();c.close();await edit_or_send(q,"🧾 سفارش‌های در انتظار:",K([[InlineKeyboardButton(text=f"🧾 {r['checkout_id']} | {r['n']} صندلی",callback_data=f"receiptview:{r['checkout_id']}")] for r in rows]+[[InlineKeyboardButton(text="⬅️ مدیریت",callback_data="admin")]]));await q.answer();return
    if a=="keywords": rows=c.execute("SELECT * FROM keywords ORDER BY id").fetchall();c.close();await edit_or_send(q,"🔑 کلیدواژه‌ها:",K([[InlineKeyboardButton(text=f"{'✅' if r['active'] else '⛔'} {r['keyword']}",callback_data=f"km:{r['id']}")] for r in rows]+[[InlineKeyboardButton(text="➕ افزودن کلیدواژه",callback_data="a:keyword")],[InlineKeyboardButton(text="⬅️ مدیریت",callback_data="admin")]]));await q.answer();return
    prompts={"event":("نوع | عنوان | توضیحات",AdminState.event),"show":("شناسه رویداد | تاریخ و ساعت | سالن | قیمت",AdminState.show),"seats":("شناسه سانس | تعداد ردیف | تعداد صندلی هر ردیف (حداکثر 110)",AdminState.seats),"card":("نام کارت | شماره کارت | نام صاحب حساب",AdminState.card),"keyword":("کلیدواژه | پاسخ",AdminState.keyword)}
    if a in prompts:
        t,st=prompts[a];await state.set_state(st);await q.message.edit_text("✏️ "+t,reply_markup=back("admin"));await q.answer()

async def event_manage(q,state):
    if not is_admin(q.from_user.id):return
    eid=int(q.data.split(":")[1]);c=db();e=c.execute("SELECT * FROM events WHERE id=?",(eid,)).fetchone();c.close();
    await edit_or_send(q,f"#{eid} {icon(e['kind'])} <b>{e['title']}</b>\n{e['description']}",K([[InlineKeyboardButton(text="✏️ ویرایش",callback_data=f"eve:{eid}")],[InlineKeyboardButton(text="🖼 تغییر پوستر",callback_data=f"evp:{eid}")],[InlineKeyboardButton(text="⬅️ رویدادها",callback_data="a:edit_events")]]));await q.answer()
async def show_manage(q,state):
    if not is_admin(q.from_user.id):return
    sid=int(q.data.split(":")[1]);c=db();s=c.execute("SELECT s.*,e.title,COUNT(se.id)n FROM shows s JOIN events e ON e.id=s.event_id LEFT JOIN seats se ON se.show_id=s.id WHERE s.id=?",(sid,)).fetchone();c.close();await edit_or_send(q,f"#{sid} {s['title']}\n🕐 {s['show_at']}\n🏛 {s['hall']}\n💰 {money(s['base_price'])}\n💺 {s['n']} صندلی",K([[InlineKeyboardButton(text="✏️ ویرایش سانس",callback_data=f"she:{sid}")],[InlineKeyboardButton(text="💺 تغییر ظرفیت",callback_data=f"se:{sid}")],[InlineKeyboardButton(text="⬅️ سانس‌ها",callback_data="a:edit_shows")]]));await q.answer()
async def seat_manage(q,state):
    if not is_admin(q.from_user.id):return
    sid=int(q.data.split(":")[1]);c=db();s=c.execute("SELECT s.*,e.title FROM shows s JOIN events e ON e.id=s.event_id WHERE s.id=?",(sid,)).fetchone();seats=get_layout(c,sid);c.close();await edit_or_send(q,f"💺 #{sid} {s['title']}\nتعداد صندلی: {len(seats)}\n\nفرمت تغییر ظرفیت: تعداد ردیف | تعداد صندلی هر ردیف",K([[InlineKeyboardButton(text="✏️ تغییر ظرفیت",callback_data=f"se:{sid}")],[InlineKeyboardButton(text="⬅️ سانس",callback_data=f"shm:{sid}")]]));await q.answer()
async def card_manage(q,state):
    cid=int(q.data.split(":")[1]);c=db();r=c.execute("SELECT * FROM cards WHERE id=?",(cid,)).fetchone();c.close();await edit_or_send(q,f"#{cid} 💳 {r['title']}\nشماره: <code>{r['number']}</code>\n👤 صاحب حساب: {r['owner_name']}",K([[InlineKeyboardButton(text="✏️ ویرایش",callback_data=f"ce:{cid}")],[InlineKeyboardButton(text="⬅️ کارت‌ها",callback_data="a:cards")]]));await q.answer()
async def keyword_manage(q,state):
    kid=int(q.data.split(":")[1]);c=db();r=c.execute("SELECT * FROM keywords WHERE id=?",(kid,)).fetchone();c.close();await edit_or_send(q,f"🔑 <b>{r['keyword']}</b>\n{r['response']}",K([[InlineKeyboardButton(text="✏️ ویرایش",callback_data=f"ke:{kid}")],[InlineKeyboardButton(text="🗑 حذف",callback_data=f"kd:{kid}")],[InlineKeyboardButton(text="⬅️ کلیدواژه‌ها",callback_data="a:keywords")]]));await q.answer()
async def receipt_view(q,bot):
    if not is_admin(q.from_user.id):return
    key=q.data.split(":",1)[1];c=db();r=c.execute("SELECT o.receipt_file_id,e.title,s.show_at FROM orders o JOIN shows s ON s.id=o.show_id JOIN events e ON e.id=s.event_id WHERE o.checkout_id=? LIMIT 1",(key,)).fetchone();c.close();
    if not r:await q.answer("رسید پیدا نشد.",show_alert=True);return
    await bot.send_photo(q.from_user.id,r['receipt_file_id'],caption=f"🧾 {key}\n{r['title']}\n🕐 {r['show_at']}",reply_markup=K([[InlineKeyboardButton(text="✅ تأیید همه",callback_data=f"approve:{key}"),InlineKeyboardButton(text="❌ رد همه",callback_data=f"reject:{key}")],[InlineKeyboardButton(text="⬅️ سفارش‌ها",callback_data="a:pending")]]));await q.answer()

async def admin_text(message,state):
    if not is_admin(message.from_user.id) or not message.text:return
    st=await state.get_state();p=[x.strip() for x in message.text.split("|",1)];c=db()
    try:
        if st==AdminState.event.state:
            p=message.text.split("|",2); 
            if len(p)!=3 or p[0].strip() not in KINDS:raise ValueError("نوع | عنوان | توضیحات")
            c.execute("INSERT INTO events(kind,title,description,created_at) VALUES(?,?,?,?)",[x.strip() for x in p]+[now_iso()]);msg="✅ رویداد ثبت شد."
        elif st==AdminState.show.state:
            p=message.text.split("|",3); 
            if len(p)!=4:raise ValueError("شناسه رویداد | تاریخ و ساعت | سالن | قیمت")
            c.execute("INSERT INTO shows(event_id,show_at,hall,base_price) VALUES(?,?,?,?)",(int(p[0]),p[1].strip(),p[2].strip(),int(p[3].replace(",",""))));msg="✅ سانس ثبت شد."
        elif st==AdminState.seats.state:
            p=message.text.split("|");
            if len(p)!=3:raise ValueError("شناسه سانس | تعداد ردیف | تعداد صندلی هر ردیف")
            sid,rr,cols=map(lambda x:int(x.strip()),p); 
            if rr<1 or cols<1 or rr*cols>MAX_SEATS:raise ValueError("تعداد کل باید بین 1 تا 110 باشد.")
            if c.execute("SELECT COUNT(*)n FROM seats WHERE show_id=? AND status IN ('sold','pending')",(sid,)).fetchone()['n']:raise ValueError("این سانس بلیت فعال دارد و ظرفیتش قابل تغییر نیست.")
            c.execute("DELETE FROM seats WHERE show_id=?",(sid,));c.executemany("INSERT INTO seats(show_id,label,status) VALUES(?,?,'free')",[(sid,f"{r}-{n}") for r in range(1,rr+1) for n in range(1,cols+1)]);msg=f"✅ {rr*cols} صندلی ساخته شد."
        elif st==AdminState.card.state:
            if len(p)!=3:raise ValueError("نام کارت | شماره کارت | نام صاحب حساب")
            c.execute("INSERT INTO cards(title,number,owner_name) VALUES(?,?,?)",tuple(p));msg="✅ کارت ثبت شد."
        elif st==AdminState.keyword.state:
            if len(p)!=2 or not p[0] or not p[1]:raise ValueError("کلیدواژه | پاسخ")
            c.execute("INSERT INTO keywords(keyword,response) VALUES(?,?)",tuple(p));msg="✅ کلیدواژه ثبت شد."
        else:return
        c.commit();await message.answer(msg,reply_markup=main_kb(message.from_user.id));await state.clear()
    except Exception as e:await message.answer(f"❌ {e}")
    finally:c.close()

async def edit_prompt(q,state):
    if not is_admin(q.from_user.id):return
    data=q.data;prefix=data[:3];i=int(data.split(":")[1])
    mapping={"eve":(AdminState.edit_event,"نوع | عنوان | توضیحات","edit_event_id"),"she":(AdminState.edit_show,"تاریخ و ساعت | سالن | قیمت","edit_show_id"),"ce:":(AdminState.edit_card,"نام کارت | شماره کارت | نام صاحب حساب","edit_card_id"),"ke:":(AdminState.edit_keyword,"کلیدواژه | پاسخ","edit_keyword_id"),"se:":(AdminState.edit_seats,"تعداد ردیف | تعداد صندلی هر ردیف","edit_seat_show_id")}
    key=prefix if prefix in mapping else data[:3]
    if key not in mapping:return
    st,prompt,dk=mapping[key];await state.update_data(**{dk:i});await state.set_state(st);await q.message.edit_text("✏️ "+prompt);await q.answer()

async def admin_edit_text(message,state):
    if not is_admin(message.from_user.id) or not message.text:return
    st=await state.get_state();d=await state.get_data();c=db();p=[x.strip() for x in message.text.split("|")]
    try:
        if st==AdminState.edit_event.state and len(p)==3:c.execute("UPDATE events SET kind=?,title=?,description=? WHERE id=?",(*p,d['edit_event_id']))
        elif st==AdminState.edit_show.state and len(p)==3:c.execute("UPDATE shows SET show_at=?,hall=?,base_price=? WHERE id=?",(p[0],p[1],int(p[2].replace(',','')),d['edit_show_id']))
        elif st==AdminState.edit_card.state and len(p)==3:c.execute("UPDATE cards SET title=?,number=?,owner_name=? WHERE id=?",(*p,d['edit_card_id']))
        elif st==AdminState.edit_keyword.state and len(p)==2:c.execute("UPDATE keywords SET keyword=?,response=? WHERE id=?",(*p,d['edit_keyword_id']))
        elif st==AdminState.edit_seats.state and len(p)==2:
            rr,cols=map(int,p);sid=d['edit_seat_show_id'];
            if rr*cols>MAX_SEATS:raise ValueError("حداکثر 110 صندلی")
            if c.execute("SELECT COUNT(*)n FROM seats WHERE show_id=? AND status IN ('sold','pending')",(sid,)).fetchone()['n']:raise ValueError("این سانس بلیت فعال دارد.")
            c.execute("DELETE FROM seats WHERE show_id=?",(sid,));c.executemany("INSERT INTO seats(show_id,label,status) VALUES(?,?,'free')",[(sid,f"{r}-{n}") for r in range(1,rr+1) for n in range(1,cols+1)])
        else:raise ValueError("فرمت صحیح نیست.")
        c.commit();await message.answer("✅ با موفقیت انجام شد.",reply_markup=main_kb(message.from_user.id));await state.clear()
    except Exception as e:await message.answer(f"❌ {e}")
    finally:c.close()

async def poster_prompt(q,state):
    eid=int(q.data.split(":")[1]);await state.update_data(poster_event_id=eid);await state.set_state(AdminState.edit_poster);await q.message.edit_text("🖼 عکس پوستر جدید را ارسال کنید.");await q.answer()
async def admin_poster(message,state):
    if not is_admin(message.from_user.id) or not message.photo:return
    d=await state.get_data();eid=d.get('poster_event_id');c=db();c.execute("UPDATE events SET poster_file_id=? WHERE id=?",(message.photo[-1].file_id,eid));c.commit();c.close();await state.clear();await message.answer("✅ پوستر ثبت شد.",reply_markup=main_kb(message.from_user.id))

async def scan_prompt(q,state):
    if not is_admin(q.from_user.id):return
    await state.clear();await state.set_state(UserState.scan_qr);await q.message.edit_text("📱 QR بلیت را به‌صورت عکس ارسال کنید. پس از اولین اسکن، بلیت مصرف‌شده می‌شود.");await q.answer()
async def decode_qr(message):
    try:
        f=await message.bot.get_file(message.photo[-1].file_id);b=io.BytesIO();await message.bot.download_file(f.file_path,b);arr=np.frombuffer(b.getvalue(),np.uint8);img=cv2.imdecode(arr,cv2.IMREAD_COLOR);text,_,_=cv2.QRCodeDetector().detectAndDecode(img);return text.strip() if text else None
    except Exception:log.exception("QR decode failed");return None
async def scan_photo(message,state):
    if not is_admin(message.from_user.id) or await state.get_state()!=UserState.scan_qr.state:return
    code=await decode_qr(message)
    if not code:await message.answer("❌ QR خوانده نشد.");return
    c=db();row=c.execute("SELECT o.*,e.title,e.kind,s.show_at,s.hall,se.label FROM orders o JOIN shows s ON s.id=o.show_id JOIN events e ON e.id=s.event_id JOIN seats se ON se.id=o.seat_id WHERE o.ticket_code=? AND o.status='approved'",(code,)).fetchone()
    if not row:c.close();await message.answer("❌ QR نامعتبر است.");return
    if row['used_at']:c.close();await message.answer(f"⛔ قبلاً استفاده شده است.\nزمان: {row['used_at'].replace('T',' ')}");return
    cur=c.execute("UPDATE orders SET used_at=?,used_by=? WHERE id=? AND status='approved' AND used_at IS NULL",(now_iso(),message.from_user.id,row['id']));c.commit();c.close()
    await message.answer(f"{'✅ ورود تأیید شد' if cur.rowcount==1 else '⛔ قبلاً مصرف شده'}\n🎟 {code}\n💺 صندلی: {row['label']}")

async def scan_text(message,state):
    if not is_admin(message.from_user.id) or await state.get_state()!=UserState.scan_qr.state:return
    # Manual fallback: reuse same atomic logic by code.
    code=message.text.strip();c=db();row=c.execute("SELECT * FROM orders WHERE ticket_code=? AND status='approved'",(code,)).fetchone()
    if not row:c.close();await message.answer("❌ کد بلیت معتبر نیست.");return
    if row['used_at']:c.close();await message.answer("⛔ این بلیت قبلاً استفاده شده است.");return
    cur=c.execute("UPDATE orders SET used_at=?,used_by=? WHERE id=? AND status='approved' AND used_at IS NULL",(now_iso(),message.from_user.id,row['id']));c.commit();c.close();await message.answer("✅ ورود تأیید شد و QR مصرف شد." if cur.rowcount else "⛔ قبلاً مصرف شده است.")

async def cmd_admin(message):
    if is_admin(message.from_user.id):await message.answer("پنل مدیریت:",reply_markup=K([[InlineKeyboardButton(text="👨‍💼 ورود",callback_data="admin")]]))
    else:await message.answer("دسترسی ندارید.")

async def main():
    if not BOT_TOKEN:raise RuntimeError("BOT_TOKEN تنظیم نشده است")
    init_db();bot=Bot(BOT_TOKEN,default=DefaultBotProperties(parse_mode=ParseMode.HTML));dp=Dispatcher()
    dp.message.register(start,CommandStart());dp.message.register(cmd_admin,Command("admin"));dp.message.register(profile_first,UserState.first);dp.message.register(profile_last,UserState.last);dp.message.register(profile_phone,UserState.phone);dp.message.register(receipt,UserState.waiting_receipt,F.photo);dp.message.register(scan_photo,UserState.scan_qr,F.photo);dp.message.register(scan_text,UserState.scan_qr,F.text)
    dp.callback_query.register(events,F.data=="events");dp.callback_query.register(mytickets,F.data=="mytickets");dp.callback_query.register(home,F.data=="home");dp.callback_query.register(admin_panel,F.data=="admin");dp.callback_query.register(admin_action,F.data.startswith("a:"));dp.callback_query.register(event_detail,F.data.startswith("event:"));dp.callback_query.register(show_detail,F.data.startswith("show:"));dp.callback_query.register(seat_page,F.data.startswith("seatpage:"));dp.callback_query.register(seat_pick,F.data.startswith("seat:"));dp.callback_query.register(clear_select,F.data.startswith("clearselect:"));dp.callback_query.register(checkout,F.data.startswith("checkout:"));dp.callback_query.register(back_seats,F.data.startswith("backseats:"));dp.callback_query.register(noop,F.data=="noop");dp.callback_query.register(choose_card,F.data.startswith("paycard:"));dp.callback_query.register(approve,F.data.startswith("approve:"));dp.callback_query.register(reject,F.data.startswith("reject:"));dp.callback_query.register(receipt_view,F.data.startswith("receiptview:"));dp.callback_query.register(scan_prompt,F.data=="scanqr")
    dp.callback_query.register(event_manage,F.data.startswith("evm:"));dp.callback_query.register(show_manage,F.data.startswith("shm:"));dp.callback_query.register(seat_manage,F.data.startswith("sm:"));dp.callback_query.register(card_manage,F.data.startswith("cm:"));dp.callback_query.register(keyword_manage,F.data.startswith("km:"));dp.callback_query.register(edit_prompt,F.data.startswith("eve:"));dp.callback_query.register(edit_prompt,F.data.startswith("she:"));dp.callback_query.register(edit_prompt,F.data.startswith("ce:"));dp.callback_query.register(edit_prompt,F.data.startswith("ke:"));dp.callback_query.register(edit_prompt,F.data.startswith("se:"));dp.callback_query.register(poster_prompt,F.data.startswith("evp:"));
    dp.message.register(admin_poster,AdminState.edit_poster,F.photo);dp.message.register(admin_edit_text,AdminState.edit_event,F.text);dp.message.register(admin_edit_text,AdminState.edit_show,F.text);dp.message.register(admin_edit_text,AdminState.edit_card,F.text);dp.message.register(admin_edit_text,AdminState.edit_keyword,F.text);dp.message.register(admin_edit_text,AdminState.edit_seats,F.text)
    for st in (AdminState.event,AdminState.show,AdminState.seats,AdminState.card,AdminState.keyword):dp.message.register(admin_text,st,F.text)
    await dp.start_polling(bot)

if __name__=='__main__':asyncio.run(main())
