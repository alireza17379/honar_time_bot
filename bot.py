import os, asyncio, sqlite3, io
from datetime import datetime
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
import qrcode

TOKEN=os.getenv("BOT_TOKEN","").strip()
ADMIN_ID=int(os.getenv("ADMIN_ID","0"))
DB=os.getenv("DB_PATH","honar_time.db")
bot=Bot(TOKEN,default=DefaultBotProperties(parse_mode=ParseMode.HTML)); dp=Dispatcher()

def db():
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; return c
def init():
    c=db(); c.executescript("""
CREATE TABLE IF NOT EXISTS cards(id INTEGER PRIMARY KEY AUTOINCREMENT,bank TEXT,number TEXT,owner TEXT);
CREATE TABLE IF NOT EXISTS films(id INTEGER PRIMARY KEY AUTOINCREMENT,title TEXT,price INTEGER,active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS shows(id INTEGER PRIMARY KEY AUTOINCREMENT,film_id INTEGER,date TEXT,time TEXT,hall TEXT);
CREATE TABLE IF NOT EXISTS seats(id INTEGER PRIMARY KEY AUTOINCREMENT,show_id INTEGER,seat TEXT,status TEXT DEFAULT 'free',user_id INTEGER,receipt TEXT,code TEXT);
CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY,discount INTEGER DEFAULT 0,custom_price INTEGER);
CREATE TABLE IF NOT EXISTS tickets(id INTEGER PRIMARY KEY AUTOINCREMENT,code TEXT UNIQUE,user_id INTEGER,show_id INTEGER,seat_id INTEGER,amount INTEGER,created TEXT,status TEXT DEFAULT 'valid');
"""); c.commit(); c.close()
def K(rows): return InlineKeyboardMarkup(inline_keyboard=rows)
def mainkb(): return K([[InlineKeyboardButton(text="🎬 خرید بلیت",callback_data="films")],[InlineKeyboardButton(text="🎟️ بلیت‌های من",callback_data="mine")],[InlineKeyboardButton(text="💳 اطلاعات پرداخت",callback_data="cards")],[InlineKeyboardButton(text="ℹ️ درباره ما",callback_data="about")]])
def adminkb(): return K([[InlineKeyboardButton(text="💳 کارت‌های بانکی",callback_data="acards")],[InlineKeyboardButton(text="🎬 فیلم‌ها",callback_data="afilms")],[InlineKeyboardButton(text="🕐 سانس‌ها",callback_data="ashows")],[InlineKeyboardButton(text="💺 صندلی‌ها",callback_data="aseats")],[InlineKeyboardButton(text="🎁 تخفیف کاربران",callback_data="adiscount")],[InlineKeyboardButton(text="🧾 رسیدها",callback_data="areceipts")],[InlineKeyboardButton(text="📊 گزارش فروش",callback_data="report")]])
def admin(x): return x==ADMIN_ID
class S(StatesGroup):
    bank=State(); number=State(); owner=State()
    title=State(); price=State()
    fid=State(); date=State(); time=State(); hall=State()
    sid=State(); count=State(); cols=State()
    uid=State(); discount=State()
pending={}

def price_for(uid,base):
    c=db(); u=c.execute("SELECT * FROM users WHERE user_id=?",(uid,)).fetchone(); c.close()
    if not u:return base,0
    if u["custom_price"] is not None:return int(u["custom_price"]),None
    d=max(0,min(100,int(u["discount"]))); return round(base*(100-d)/100),d

@dp.message(CommandStart())
async def start(m:Message):
    await m.answer("🎭 <b>کارگزاری به وقت هنر</b>\n\nبه سامانه فروش بلیت خوش آمدید.",reply_markup=mainkb())
    if admin(m.from_user.id): await m.answer("🛠️ پنل مدیریت",reply_markup=adminkb())
@dp.message(Command("admin"))
async def admincmd(m:Message):
    if admin(m.from_user.id): await m.answer("🛠️ پنل مدیریت",reply_markup=adminkb())

@dp.callback_query(F.data=="about")
async def about(q): await q.message.edit_text("🎭 <b>کارگزاری به وقت هنر</b>\nفروش بلیت سینما.",reply_markup=mainkb()); await q.answer()
@dp.callback_query(F.data=="cards")
async def cards(q):
    c=db(); r=c.execute("SELECT * FROM cards").fetchall(); c.close()
    t="💳 <b>کارت‌های پرداخت</b>\n\n"+("".join(f"🏦 <b>{x['bank']}</b>\n💳 <code>{x['number']}</code>\n👤 {x['owner']}\n\n" for x in r) or "هنوز کارتی ثبت نشده.")
    await q.message.edit_text(t,reply_markup=K([[InlineKeyboardButton(text="⬅️ بازگشت",callback_data="home")]])); await q.answer()
@dp.callback_query(F.data=="home")
async def home(q): await q.message.edit_text("🎭 منوی اصلی",reply_markup=mainkb()); await q.answer()

@dp.callback_query(F.data=="films")
async def films(q):
    c=db(); r=c.execute("SELECT * FROM films WHERE active=1 ORDER BY id DESC").fetchall(); c.close()
    await q.message.edit_text("🎬 <b>فیلم را انتخاب کنید:</b>",reply_markup=K([[InlineKeyboardButton(text=f"🎬 {x['title']} | {x['price']:,}",callback_data=f"film:{x['id']}")] for x in r] or [[InlineKeyboardButton(text="⬅️ بازگشت",callback_data="home")]])); await q.answer()
@dp.callback_query(F.data.startswith("film:"))
async def film(q):
    fid=int(q.data.split(":")[1]); c=db(); f=c.execute("SELECT * FROM films WHERE id=? AND active=1",(fid,)).fetchone(); s=c.execute("SELECT * FROM shows WHERE film_id=? ORDER BY date,time",(fid,)).fetchall(); c.close()
    if not s: await q.message.edit_text(f"🎬 {f['title']}\n\nسانسی ثبت نشده."); await q.answer(); return
    await q.message.edit_text(f"🎬 <b>{f['title']}</b>\n\n🕐 سانس:",reply_markup=K([[InlineKeyboardButton(text=f"📅 {x['date']} | {x['time']} | {x['hall']}",callback_data=f"show:{x['id']}")] for x in s])); await q.answer()
@dp.callback_query(F.data.startswith("show:"))
async def show(q):
    sid=int(q.data.split(":")[1]); c=db(); s=c.execute("SELECT s.*,f.title,f.price,f.id fid FROM shows s JOIN films f ON f.id=s.film_id WHERE s.id=?",(sid,)).fetchone(); seats=c.execute("SELECT * FROM seats WHERE show_id=? ORDER BY id",(sid,)).fetchall(); c.close()
    if not seats: await q.message.edit_text("برای این سانس صندلی تعریف نشده."); await q.answer(); return
    rows=[]; line=[]
    for x in seats:
        mark="🟩" if x["status"]=="free" else "🟥"; line.append(InlineKeyboardButton(text=f"{mark}{x['seat']}",callback_data=f"seat:{sid}:{x['id']}"))
        if len(line)==4: rows.append(line); line=[]
    if line: rows.append(line)
    await q.message.edit_text(f"🎬 {s['title']}\n📅 {s['date']}  🕐 {s['time']}\n🏢 {s['hall']}\n\n🟩 آزاد | 🟥 فروخته/در انتظار\n💺 صندلی:",reply_markup=K(rows)); await q.answer()
@dp.callback_query(F.data.startswith("seat:"))
async def seat(q):
    _,sid,seatid=q.data.split(":"); sid=int(sid); seatid=int(seatid); c=db(); s=c.execute("SELECT * FROM seats WHERE id=? AND show_id=?",(seatid,sid)).fetchone(); sh=c.execute("SELECT s.*,f.title,f.price FROM shows s JOIN films f ON f.id=s.film_id WHERE s.id=?",(sid,)).fetchone(); c.close()
    if not s or s["status"]!="free": await q.answer("صندلی آزاد نیست.",show_alert=True); return
    amount,d=price_for(q.from_user.id,sh["price"]); dt=f"\n🎁 تخفیف: {d}%" if d not in (0,None) else ("\n🎁 قیمت اختصاصی شما اعمال شده." if d is None else "")
    await q.message.edit_text(f"🎟️ <b>سفارش</b>\n🎬 {sh['title']}\n📅 {sh['date']} {sh['time']}\n💺 {s['seat']}\n💰 مبلغ: <b>{amount:,} تومان</b>{dt}\n\n⚠️ تا قبل از ارسال رسید، صندلی رزرو نمی‌شود.",reply_markup=K([[InlineKeyboardButton(text="💳 ادامه پرداخت",callback_data=f"pay:{sid}:{seatid}:{amount}")],[InlineKeyboardButton(text="⬅️ بازگشت",callback_data=f"show:{sid}")]])); await q.answer()
@dp.callback_query(F.data.startswith("pay:"))
async def pay(q):
    _,sid,seatid,amount=q.data.split(":"); c=db(); r=c.execute("SELECT * FROM cards").fetchall(); c.close()
    await q.message.edit_text("💳 <b>بانک مقصد را انتخاب کنید:</b>",reply_markup=K([[InlineKeyboardButton(text=f"🏦 {x['bank']}",callback_data=f"card:{sid}:{seatid}:{amount}:{x['id']}")] for x in r] or [[InlineKeyboardButton(text="⬅️ بازگشت",callback_data="films")]])); await q.answer()
@dp.callback_query(F.data.startswith("card:"))
async def card(q):
    _,sid,seatid,amount,cid=q.data.split(":"); c=db(); x=c.execute("SELECT * FROM cards WHERE id=?",(cid,)).fetchone(); c.close()
    await q.message.edit_text(f"🏦 <b>{x['bank']}</b>\n💳 <code>{x['number']}</code>\n👤 {x['owner']}\n\n💰 <b>{int(amount):,} تومان</b>\n\nپس از کارت‌به‌کارت، عکس رسید را ارسال کنید.",reply_markup=K([[InlineKeyboardButton(text="📤 ارسال رسید",callback_data=f"receipt:{sid}:{seatid}:{amount}")]])); await q.answer()
@dp.callback_query(F.data.startswith("receipt:"))
async def rec(q):
    _,sid,seatid,amount=q.data.split(":"); pending[q.from_user.id]=(int(sid),int(seatid),int(amount)); await q.message.edit_text("📤 عکس رسید را ارسال کنید.\n\nتا قبل از ارسال رسید، صندلی رزرو نیست."); await q.answer()
@dp.message(F.photo)
async def photo(m):
    if m.from_user.id not in pending:return
    sid,seatid,amount=pending.pop(m.from_user.id); c=db(); s=c.execute("SELECT * FROM seats WHERE id=? AND show_id=? AND status='free'",(seatid,sid)).fetchone()
    if not s:c.close(); await m.answer("❌ صندلی دیگر آزاد نیست."); return
    fid=m.photo[-1].file_id; c.execute("UPDATE seats SET status='pending',user_id=?,receipt=? WHERE id=?",(m.from_user.id,fid,seatid)); c.commit(); c.close()
    await bot.send_photo(ADMIN_ID,fid,caption=f"🧾 رسید جدید\n👤 {m.from_user.id}\n💺 {s['seat']}\n💰 {amount:,} تومان",reply_markup=K([[InlineKeyboardButton(text="✅ تأیید",callback_data=f"ok:{seatid}:{m.from_user.id}:{amount}")],[InlineKeyboardButton(text="❌ رد",callback_data=f"no:{seatid}:{m.from_user.id}")]]))
    await m.answer("✅ رسید دریافت شد و برای مدیریت ارسال شد.")

def make_code():return "HT-"+datetime.now().strftime("%y%m%d%H%M%S%f")[-10:]
def qr(data):
    x=qrcode.make(data); b=io.BytesIO(); x.save(b,"PNG"); return b.getvalue()

@dp.callback_query(F.data.startswith("ok:"))
async def ok(q):
    if not admin(q.from_user.id):return
    _,seatid,uid,amount=q.data.split(":"); c=db(); s=c.execute("SELECT * FROM seats WHERE id=? AND status='pending'",(seatid,)).fetchone()
    if not s:c.close(); await q.answer("قبلاً بررسی شده.",show_alert=True);return
    code=make_code(); sh=c.execute("SELECT s.*,f.title FROM shows s JOIN films f ON f.id=s.film_id WHERE s.id=?",(s["show_id"],)).fetchone()
    c.execute("UPDATE seats SET status='sold',code=? WHERE id=?",(code,seatid)); c.execute("INSERT INTO tickets(code,user_id,show_id,seat_id,amount,created) VALUES(?,?,?,?,?,?)",(code,int(uid),s["show_id"],seatid,int(amount),datetime.now().isoformat(timespec="seconds"))); c.commit(); c.close()
    await bot.send_photo(int(uid),BufferedInputFile(qr(f"{code}|{uid}|{s['seat']}|{sh['title']}|{sh['date']}|{sh['time']}"),filename=code+".png"),caption=f"🎟️ <b>بلیت صادر شد</b>\n\n🎬 {sh['title']}\n📅 {sh['date']}\n🕐 {sh['time']}\n🏢 {sh['hall']}\n💺 {s['seat']}\n💰 {int(amount):,} تومان\n🔑 <code>{code}</code>")
    await q.message.edit_caption(q.message.caption+"\n\n✅ تأیید شد و بلیت صادر شد."); await q.answer("تأیید شد")
@dp.callback_query(F.data.startswith("no:"))
async def no(q):
    if not admin(q.from_user.id):return
    _,seatid,uid=q.data.split(":"); c=db(); c.execute("UPDATE seats SET status='free',user_id=NULL,receipt=NULL WHERE id=? AND status='pending'",(seatid,)); c.commit(); c.close(); await bot.send_message(int(uid),"❌ رسید تأیید نشد و صندلی آزاد شد."); await q.message.edit_caption(q.message.caption+"\n\n❌ رد شد."); await q.answer("رد شد")

@dp.callback_query(F.data=="mine")
async def mine(q):
    c=db(); r=c.execute("""SELECT t.*,f.title,s.date,s.time,s.hall,se.seat FROM tickets t JOIN shows s ON s.id=t.show_id JOIN films f ON f.id=s.film_id JOIN seats se ON se.id=t.seat_id WHERE t.user_id=? ORDER BY t.id DESC""",(q.from_user.id,)).fetchall(); c.close()
    await q.message.edit_text("🎟️ <b>بلیت‌های شما</b>\n\n"+("".join(f"🎬 {x['title']}\n📅 {x['date']} {x['time']}\n💺 {x['seat']}\n🔑 <code>{x['code']}</code>\n\n" for x in r) or "هنوز بلیتی ندارید."),reply_markup=mainkb()); await q.answer()

# Admin cards
@dp.callback_query(F.data=="acards")
async def acards(q):
    if not admin(q.from_user.id):return
    c=db();r=c.execute("SELECT * FROM cards").fetchall();c.close();t="💳 <b>کارت‌ها</b>\n\n"+("".join(f"{x['id']}. 🏦 {x['bank']} | <code>{x['number']}</code> | {x['owner']}\n" for x in r) or "هیچ کارتی نیست.")
    await q.message.edit_text(t,reply_markup=K([[InlineKeyboardButton(text="➕ افزودن کارت",callback_data="addcard")],[InlineKeyboardButton(text="🗑 حذف کارت",callback_data="delcard")],[InlineKeyboardButton(text="⬅️ پنل",callback_data="admin")]]));await q.answer()
@dp.callback_query(F.data=="addcard")
async def addcard(q,state:FSMContext):
    await state.set_state(S.bank);await q.message.answer("🏦 نام بانک:");await q.answer()
@dp.message(S.bank)
async def bank(m,state:FSMContext):await state.update_data(bank=m.text.strip());await state.set_state(S.number);await m.answer("💳 شماره کارت:")
@dp.message(S.number)
async def number(m,state:FSMContext):await state.update_data(number=m.text.strip());await state.set_state(S.owner);await m.answer("👤 نام صاحب کارت:")
@dp.message(S.owner)
async def owner(m,state:FSMContext):
    d=await state.get_data();c=db();c.execute("INSERT INTO cards(bank,number,owner) VALUES(?,?,?)",(d["bank"],d["number"],m.text.strip()));c.commit();c.close();await state.clear();await m.answer("✅ کارت ثبت شد.",reply_markup=adminkb())
@dp.callback_query(F.data=="delcard")
async def delcard(q):
    if not admin(q.from_user.id):return
    c=db();r=c.execute("SELECT * FROM cards").fetchall();c.close();await q.message.edit_text("کارت را انتخاب کنید:",reply_markup=K([[InlineKeyboardButton(text=f"🗑 {x['bank']}",callback_data=f"dc:{x['id']}")] for x in r]));await q.answer()
@dp.callback_query(F.data.startswith("dc:"))
async def dc(q):
    if not admin(q.from_user.id):return
    c=db();c.execute("DELETE FROM cards WHERE id=?",(int(q.data.split(":")[1]),));c.commit();c.close();await q.message.edit_text("✅ حذف شد.",reply_markup=adminkb());await q.answer()

# Admin films
@dp.callback_query(F.data=="afilms")
async def afilms(q):
    if not admin(q.from_user.id):return
    c=db();r=c.execute("SELECT * FROM films").fetchall();c.close();await q.message.edit_text("🎬 <b>فیلم‌ها</b>\n\n"+("".join(f"{x['id']}. {x['title']} — {x['price']:,}\n" for x in r) or "فیلمی نیست."),reply_markup=K([[InlineKeyboardButton(text="➕ افزودن فیلم",callback_data="addfilm")],[InlineKeyboardButton(text="⬅️ پنل",callback_data="admin")]]));await q.answer()
@dp.callback_query(F.data=="addfilm")
async def addfilm(q,state:FSMContext):await state.set_state(S.title);await q.message.answer("🎬 نام فیلم:");await q.answer()
@dp.message(S.title)
async def title(m,state:FSMContext):await state.update_data(title=m.text.strip());await state.set_state(S.price);await m.answer("💰 قیمت پایه (تومان):")
@dp.message(S.price)
async def filmprice(m,state:FSMContext):
    try:p=int(m.text.replace(",","").replace("٬",""))
    except:await m.answer("فقط عدد وارد کنید.");return
    d=await state.get_data();c=db();c.execute("INSERT INTO films(title,price) VALUES(?,?)",(d["title"],p));c.commit();c.close();await state.clear();await m.answer("✅ فیلم ثبت شد.",reply_markup=adminkb())

# Admin shows
@dp.callback_query(F.data=="ashows")
async def ashows(q):
    if not admin(q.from_user.id):return
    c=db();r=c.execute("SELECT s.*,f.title FROM shows s JOIN films f ON f.id=s.film_id ORDER BY s.date,s.time").fetchall();c.close();await q.message.edit_text("🕐 سانس‌ها\n\n"+("".join(f"{x['id']}. {x['title']} | {x['date']} {x['time']} | {x['hall']}\n" for x in r) or "سانسی نیست."),reply_markup=K([[InlineKeyboardButton(text="➕ افزودن سانس",callback_data="addshow")],[InlineKeyboardButton(text="⬅️ پنل",callback_data="admin")]]));await q.answer()
@dp.callback_query(F.data=="addshow")
async def addshow(q,state:FSMContext):
    c=db();r=c.execute("SELECT * FROM films WHERE active=1").fetchall();c.close();await state.set_state(S.fid);await q.message.edit_text("🎬 فیلم:",reply_markup=K([[InlineKeyboardButton(text=x["title"],callback_data=f"sf:{x['id']}")] for x in r]));await q.answer()
@dp.callback_query(F.data.startswith("sf:"))
async def sf(q,state:FSMContext):await state.update_data(fid=int(q.data.split(":")[1]));await state.set_state(S.date);await q.message.answer("📅 تاریخ (مثلاً 1405/06/25):");await q.answer()
@dp.message(S.date)
async def date(m,state:FSMContext):await state.update_data(date=m.text.strip());await state.set_state(S.time);await m.answer("🕐 ساعت:")
@dp.message(S.time)
async def time(m,state:FSMContext):await state.update_data(time=m.text.strip());await state.set_state(S.hall);await m.answer("🏢 نام سالن:")
@dp.message(S.hall)
async def hall(m,state:FSMContext):
    d=await state.get_data();c=db();c.execute("INSERT INTO shows(film_id,date,time,hall) VALUES(?,?,?,?)",(d["fid"],d["date"],d["time"],m.text.strip()));c.commit();c.close();await state.clear();await m.answer("✅ سانس ثبت شد.",reply_markup=adminkb())

# Admin seats
@dp.callback_query(F.data=="aseats")
async def aseats(q,state:FSMContext):
    if not admin(q.from_user.id):return
    c=db();r=c.execute("SELECT s.id,f.title,s.date,s.time,s.hall,COUNT(se.id)n FROM shows s JOIN films f ON f.id=s.film_id LEFT JOIN seats se ON se.show_id=s.id GROUP BY s.id").fetchall();c.close()
    await q.message.edit_text("💺 سانس را انتخاب کنید:",reply_markup=K([[InlineKeyboardButton(text=f"{x['title']} | {x['date']} {x['time']} | {x['n']} صندلی",callback_data=f"ss:{x['id']}")] for x in r]));await q.answer()
@dp.callback_query(F.data.startswith("ss:"))
async def ss(q,state:FSMContext):await state.set_state(S.sid);await state.update_data(sid=int(q.data.split(":")[1]));await state.set_state(S.count);await q.message.answer("💺 تعداد صندلی:");await q.answer()
@dp.message(S.count)
async def count(m,state:FSMContext):
    try:n=int(m.text)
    except:await m.answer("عدد وارد کنید.");return
    if not 1<=n<=300:await m.answer("بین 1 تا 300.");return
    await state.update_data(count=n);await state.set_state(S.cols);await m.answer("↔️ تعداد صندلی در هر ردیف:")
@dp.message(S.cols)
async def cols(m,state:FSMContext):
    try:cols=int(m.text)
    except:await m.answer("عدد وارد کنید.");return
    d=await state.get_data();c=db();exists=c.execute("SELECT COUNT(*)n FROM seats WHERE show_id=?",(d["sid"],)).fetchone()["n"]
    if exists:c.close();await state.clear();await m.answer("⚠️ برای این سانس قبلاً صندلی ساخته شده.");return
    vals=[]
    for i in range(d["count"]): vals.append((d["sid"],f"{chr(65+i//cols) if i//cols<26 else 'R'+str(i//cols+1)}{i%cols+1}"))
    c.executemany("INSERT INTO seats(show_id,seat) VALUES(?,?)",vals);c.commit();c.close();await state.clear();await m.answer("✅ صندلی‌ها ساخته شدند.",reply_markup=adminkb())

# Admin discount
@dp.callback_query(F.data=="adiscount")
async def adiscount(q,state:FSMContext):
    if not admin(q.from_user.id):return
    await state.set_state(S.uid);await q.message.answer("👤 آیدی عددی کاربر:");await q.answer()
@dp.message(S.uid)
async def uid(m,state:FSMContext):
    try:u=int(m.text)
    except:await m.answer("آیدی باید عددی باشد.");return
    await state.update_data(uid=u);await state.set_state(S.discount);await m.answer("🎁 درصد تخفیف (مثلاً 20) یا قیمت اختصاصی به شکل price:100000")
@dp.message(S.discount)
async def discount(m,state:FSMContext):
    d=await state.get_data();t=m.text.strip();c=db()
    if t.startswith("price:"):
        try:v=int(t.split(":",1)[1].replace(",","").replace("٬",""))
        except:await m.answer("فرمت اشتباه.");return
        c.execute("INSERT INTO users(user_id,discount,custom_price) VALUES(?,0,?) ON CONFLICT(user_id) DO UPDATE SET discount=0,custom_price=excluded.custom_price",(d["uid"],v))
    else:
        try:v=max(0,min(100,int(t)))
        except:await m.answer("درصد اشتباه.");return
        c.execute("INSERT INTO users(user_id,discount,custom_price) VALUES(?,?,NULL) ON CONFLICT(user_id) DO UPDATE SET discount=excluded.discount,custom_price=NULL",(d["uid"],v))
    c.commit();c.close();await state.clear();await m.answer("✅ ثبت شد.",reply_markup=adminkb())

@dp.callback_query(F.data=="areceipts")
async def areceipts(q):
    if not admin(q.from_user.id):return
    c=db();r=c.execute("""SELECT se.id,se.user_id,se.seat,se.receipt,f.title,s.date,s.time FROM seats se JOIN shows s ON s.id=se.show_id JOIN films f ON f.id=s.film_id WHERE se.status='pending'""").fetchall();c.close()
    await q.message.edit_text(f"🧾 {len(r)} رسید در انتظار بررسی است.",reply_markup=K([[InlineKeyboardButton(text=f"{x['title']} | {x['seat']} | {x['user_id']}",callback_data=f"vr:{x['id']}")] for x in r] or [[InlineKeyboardButton(text="⬅️ پنل",callback_data="admin")]]));await q.answer()
@dp.callback_query(F.data.startswith("vr:"))
async def vr(q):
    if not admin(q.from_user.id):return
    sid=int(q.data.split(":")[1]);c=db();x=c.execute("SELECT * FROM seats WHERE id=? AND status='pending'",(sid,)).fetchone();c.close()
    await bot.send_photo(ADMIN_ID,x["receipt"],caption=f"🧾 بررسی\n👤 {x['user_id']}\n💺 {x['seat']}",reply_markup=K([[InlineKeyboardButton(text="✅ تأیید",callback_data=f"ok:{sid}:{x['user_id']}:0")],[InlineKeyboardButton(text="❌ رد",callback_data=f"no:{sid}:{x['user_id']}")]]));await q.answer()
@dp.callback_query(F.data=="report")
async def report(q):
    if not admin(q.from_user.id):return
    c=db();n=c.execute("SELECT COUNT(*)n FROM tickets").fetchone()["n"];tot=c.execute("SELECT COALESCE(SUM(amount),0)t FROM tickets").fetchone()["t"];p=c.execute("SELECT COUNT(*)n FROM seats WHERE status='pending'").fetchone()["n"];c.close()
    await q.message.edit_text(f"📊 <b>گزارش فروش</b>\n\n🎟️ فروش: {n} بلیت\n💰 مبلغ: {tot:,} تومان\n🧾 در انتظار: {p}",reply_markup=adminkb());await q.answer()
@dp.callback_query(F.data=="admin")
async def ap(q):
    if admin(q.from_user.id):await q.message.edit_text("🛠️ <b>پنل مدیریت</b>",reply_markup=adminkb())
    await q.answer()

async def main():
    if not TOKEN or not ADMIN_ID: raise RuntimeError("BOT_TOKEN و ADMIN_ID را تنظیم کنید.")
    init(); await dp.start_polling(bot)
if __name__=="__main__":asyncio.run(main())
