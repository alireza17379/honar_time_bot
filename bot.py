import os, sqlite3, uuid, asyncio
from datetime import datetime, timedelta
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters

DB = os.getenv('DB_PATH', 'honar_time.db')
TOKEN = os.getenv('BOT_TOKEN', '')
ADMIN_IDS = {int(x.strip()) for x in os.getenv('ADMIN_IDS','').split(',') if x.strip().isdigit()}
HOLD_MINUTES = int(os.getenv('HOLD_MINUTES','10'))

conn = sqlite3.connect(DB, check_same_thread=False)
conn.row_factory = sqlite3.Row
conn.executescript('''
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, title TEXT NOT NULL, description TEXT DEFAULT '', poster TEXT DEFAULT '', active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS shows(id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER NOT NULL, show_at TEXT NOT NULL, hall TEXT DEFAULT 'سالن اصلی', base_price INTEGER NOT NULL, FOREIGN KEY(event_id) REFERENCES events(id));
CREATE TABLE IF NOT EXISTS seats(id INTEGER PRIMARY KEY AUTOINCREMENT, show_id INTEGER NOT NULL, label TEXT NOT NULL, status TEXT DEFAULT 'free', hold_until TEXT, UNIQUE(show_id,label), FOREIGN KEY(show_id) REFERENCES shows(id));
CREATE TABLE IF NOT EXISTS cards(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, number TEXT NOT NULL, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS discounts(user_id INTEGER PRIMARY KEY, percent REAL DEFAULT 0, fixed_price INTEGER);
CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, show_id INTEGER NOT NULL, seat_id INTEGER NOT NULL, amount INTEGER NOT NULL, status TEXT DEFAULT 'pending', ticket_code TEXT UNIQUE, receipt_file_id TEXT, created_at TEXT NOT NULL, FOREIGN KEY(show_id) REFERENCES shows(id), FOREIGN KEY(seat_id) REFERENCES seats(id));
''')
conn.commit()

def q(sql, args=(), one=False):
    cur=conn.execute(sql,args); rows=cur.fetchall(); cur.close(); return (rows[0] if rows else None) if one else rows

def execsql(sql,args=()):
    cur=conn.execute(sql,args); conn.commit(); return cur.lastrowid

def money(n): return f'{int(n):,} تومان'

def clean_holds():
    now=datetime.now().isoformat()
    conn.execute("UPDATE seats SET status='free', hold_until=NULL WHERE status='held' AND hold_until < ?",(now,)); conn.commit()

def final_price(user_id, base):
    d=q('SELECT * FROM discounts WHERE user_id=?',(user_id,),True)
    if not d: return base
    if d['fixed_price'] is not None: return max(0,int(d['fixed_price']))
    return max(0,round(base*(1-float(d['percent'])/100)))

def kb(rows): return InlineKeyboardMarkup(rows)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    buttons=[[InlineKeyboardButton('🎭 رویدادهای هنری',callback_data='events')],[InlineKeyboardButton('🎟 بلیت‌های من',callback_data='mytickets')]]
    if update.effective_user.id in ADMIN_IDS: buttons.append([InlineKeyboardButton('👨‍💼 پنل مدیریت',callback_data='admin')])
    await update.message.reply_text('🎨 به «کارگزاری به وقت هنر» خوش آمدید.\n\nاینجا می‌توانید برای سینما، تئاتر، موسیقی و سایر رویدادهای هنری بلیت تهیه کنید.',reply_markup=kb(buttons))

async def events(update, context):
    clean_holds(); rows=q('SELECT * FROM events WHERE active=1 ORDER BY id DESC')
    buttons=[]
    for r in rows: buttons.append([InlineKeyboardButton(f"{kind_icon(r['kind'])} {r['title']}",callback_data=f"event:{r['id']}")])
    buttons.append([InlineKeyboardButton('🏠 منوی اصلی',callback_data='home')])
    text='🎟 رویدادهای فعال:' if rows else 'فعلاً رویداد فعالی ثبت نشده است.'
    await send(update,text,buttons)

def kind_icon(k): return {'سینما':'🎬','تئاتر':'🎭','موسیقی':'🎵','فرهنگی':'🎤','سایر':'🎨'}.get(k,'🎨')

async def event_detail(update, context, eid):
    r=q('SELECT * FROM events WHERE id=? AND active=1',(eid,),True)
    if not r: return await send(update,'رویداد پیدا نشد.',[[InlineKeyboardButton('بازگشت',callback_data='events')]])
    shows=q('SELECT * FROM shows WHERE event_id=? ORDER BY show_at',(eid,))
    buttons=[[InlineKeyboardButton(f"🕐 {s['show_at']} | {s['hall']}",callback_data=f"show:{s['id']}")] for s in shows]
    buttons.append([InlineKeyboardButton('↩️ بازگشت',callback_data='events')])
    await send(update,f"{kind_icon(r['kind'])} {r['title']}\n\n{r['description'] or 'بدون توضیحات'}\n\nیک سانس را انتخاب کنید:",buttons)

async def show_detail(update, context, sid):
    clean_holds(); s=q('SELECT s.*,e.title,e.kind FROM shows s JOIN events e ON e.id=s.event_id WHERE s.id=?',(sid,),True)
    if not s: return await send(update,'سانس پیدا نشد.',[[InlineKeyboardButton('بازگشت',callback_data='events')]])
    seats=q('SELECT * FROM seats WHERE show_id=? ORDER BY id',(sid,))
    rows=[]
    line=[]
    for seat in seats:
        icon={'free':'🟩','held':'🟨','pending':'🟧','sold':'🟥'}.get(seat['status'],'⬜')
        b=InlineKeyboardButton(f'{icon} {seat["label"]}',callback_data=f"seat:{seat['id']}") if seat['status']=='free' else InlineKeyboardButton(f'{icon} {seat["label"]}',callback_data='noop')
        line.append(b)
        if len(line)==4: rows.append(line); line=[]
    if line: rows.append(line)
    rows.append([InlineKeyboardButton('↩️ بازگشت',callback_data=f"event:{s['event_id']}")])
    await send(update,f"{kind_icon(s['kind'])} {s['title']}\n🕐 {s['show_at']} | {s['hall']}\n💰 قیمت پایه: {money(s['base_price'])}\n\nصندلی را انتخاب کنید:",rows)

async def seat_pick(update, context, seat_id):
    clean_holds(); seat=q('SELECT * FROM seats WHERE id=? AND status="free"',(seat_id,),True)
    if not seat: return await send(update,'این صندلی دیگر آزاد نیست.',[[InlineKeyboardButton('🔄 به‌روزرسانی',callback_data='events')]])
    s=q('SELECT s.*,e.title,e.kind FROM shows s JOIN events e ON e.id=s.event_id WHERE s.id=?',(seat['show_id'],),True)
    amount=final_price(update.effective_user.id,s['base_price'])
    until=(datetime.now()+timedelta(minutes=HOLD_MINUTES)).isoformat()
    conn.execute("UPDATE seats SET status='held',hold_until=? WHERE id=? AND status='free'",(until,seat_id)); conn.commit()
    context.user_data['checkout']={'seat_id':seat_id,'show_id':s['id'],'amount':amount}
    cards=q('SELECT * FROM cards WHERE active=1 ORDER BY id')
    buttons=[[InlineKeyboardButton(c['title'],callback_data=f"paycard:{c['id']}")] for c in cards]
    buttons.append([InlineKeyboardButton('❌ لغو',callback_data=f"show:{s['id']}")])
    await send(update,f"🎟 {s['title']}\n💺 صندلی: {seat['label']}\n💰 مبلغ نهایی: {money(amount)}\n\nصندلی برای {HOLD_MINUTES} دقیقه نگه داشته شد.\nکارت پرداخت را انتخاب کنید:",buttons)

async def choose_card(update, context, cid):
    c=q('SELECT * FROM cards WHERE id=? AND active=1',(cid,),True); data=context.user_data.get('checkout')
    if not c or not data: return await send(update,'رزرو منقضی شده است.',[[InlineKeyboardButton('رویدادها',callback_data='events')]])
    await send(update,f"💳 {c['title']}\nشماره کارت: `{c['number']}`\n\nمبلغ قابل پرداخت: {money(data['amount'])}\n\nپس از کارت‌به‌کارت، عکس رسید را همینجا ارسال کنید.",[[InlineKeyboardButton('↩️ لغو',callback_data=f"show:{data['show_id']}")]],parse_mode='Markdown')
    context.user_data['waiting_receipt']=True

def ticket_text(o):
    return f"🎟 بلیت قطعی\n\nکد بلیت: `{o['ticket_code']}`\nرویداد: {o['title']}\nنوع: {o['kind']}\nسانس: {o['show_at']}\nسالن: {o['hall']}\nصندلی: {o['label']}\nمبلغ: {money(o['amount'])}\n\nQR/کد بلیت در زمان ورود بررسی می‌شود."

async def receipt(update, context):
    if not context.user_data.get('waiting_receipt'): return
    data=context.user_data.get('checkout')
    if not data: return
    fid=update.message.photo[-1].file_id if update.message.photo else None
    if not fid: return await update.message.reply_text('لطفاً عکس رسید را ارسال کنید.')
    seat=q('SELECT * FROM seats WHERE id=? AND status="held"',(data['seat_id'],),True)
    if not seat: return await update.message.reply_text('زمان رزرو تمام شده است. دوباره صندلی را انتخاب کنید.')
    oid=execsql("INSERT INTO orders(user_id,show_id,seat_id,amount,status,created_at,receipt_file_id) VALUES(?,?,?,?,'pending',?,?)",(update.effective_user.id,data['show_id'],data['seat_id'],data['amount'],datetime.now().isoformat(),fid))
    conn.execute("UPDATE seats SET status='pending',hold_until=NULL WHERE id=?",(data['seat_id'],)); conn.commit()
    context.user_data.clear()
    await update.message.reply_text('🧾 رسید دریافت شد و در انتظار بررسی مدیر است. پس از تأیید، بلیت و کد QR برای شما ارسال می‌شود.')
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_photo(aid,fid,caption=f'🧾 رسید جدید\nشماره سفارش: {oid}\nکاربر: {update.effective_user.id}\nمبلغ: {money(data["amount"])}',reply_markup=kb([[InlineKeyboardButton('✅ تأیید',callback_data=f'approve:{oid}'),InlineKeyboardButton('❌ رد',callback_data=f'reject:{oid}')]]))
        except Exception: pass

async def mytickets(update, context):
    rows=q("SELECT o.*,e.title,e.kind,s.show_at,s.hall,se.label FROM orders o JOIN shows s ON s.id=o.show_id JOIN events e ON e.id=s.event_id JOIN seats se ON se.id=o.seat_id WHERE o.user_id=? AND o.status='approved' ORDER BY o.id DESC",(update.effective_user.id,))
    text='🎟 بلیت‌های من:\n\n'+ '\n\n'.join([f"{r['title']} | {r['show_at']} | صندلی {r['label']} | کد `{r['ticket_code']}`" for r in rows]) if rows else 'هنوز بلیت قطعی ندارید.'
    await send(update,text,[[InlineKeyboardButton('🎭 رویدادها',callback_data='events')]],parse_mode='Markdown')

async def admin(update, context):
    if update.effective_user.id not in ADMIN_IDS: return
    await send(update,'👨‍💼 پنل مدیریت',[[InlineKeyboardButton('➕ افزودن رویداد',callback_data='a_add_event')],[InlineKeyboardButton('➕ افزودن سانس',callback_data='a_add_show')],[InlineKeyboardButton('💺 ساخت صندلی‌ها',callback_data='a_seats')],[InlineKeyboardButton('💳 کارت‌های بانکی',callback_data='a_cards')],[InlineKeyboardButton('🎁 تخفیف کاربر',callback_data='a_discount')],[InlineKeyboardButton('📊 گزارش فروش',callback_data='a_report')]])

async def admin_callback(update, context):
    if update.effective_user.id not in ADMIN_IDS: return
    key=update.callback_query.data
    if key=='a_report':
        total=q("SELECT COUNT(*) c,COALESCE(SUM(amount),0) total FROM orders WHERE status='approved'",one=True); await send(update,f"📊 فروش قطعی\nتعداد بلیت: {total['c']}\nمبلغ: {money(total['total'])}",[[InlineKeyboardButton('بازگشت',callback_data='admin')]])
    elif key=='a_cards':
        cards=q('SELECT * FROM cards'); text='💳 کارت‌ها:\n'+'\n'.join([f"{c['id']}. {c['title']} - {c['number']}" for c in cards]) if cards else 'کارتی ثبت نشده.'; await send(update,text,[[InlineKeyboardButton('➕ افزودن کارت',callback_data='a_card_add')],[InlineKeyboardButton('بازگشت',callback_data='admin')]])
    elif key=='a_card_add':
        context.user_data['admin_state']='card'; await send(update,'فرمت: نام کارت | شماره کارت')
    elif key=='a_add_event':
        context.user_data['admin_state']='event'; await send(update,'فرمت: نوع | عنوان | توضیحات\nنوع: سینما / تئاتر / موسیقی / فرهنگی / سایر')
    elif key=='a_add_show':
        context.user_data['admin_state']='show'; await send(update,'فرمت: شناسه رویداد | تاریخ و ساعت | نام سالن | قیمت\nمثال: 1 | 1405/07/20 19:30 | سالن اصلی | 250000')
    elif key=='a_seats':
        context.user_data['admin_state']='seats'; await send(update,'فرمت: شناسه سانس | تعداد ردیف | تعداد صندلی هر ردیف\nمثال: 1 | 8 | 10')
    elif key=='a_discount':
        context.user_data['admin_state']='discount'; await send(update,'فرمت درصدی: شناسه کاربر | درصد\nیا قیمت ثابت: شناسه کاربر | price:مبلغ')

async def admin_text(update, context):
    if update.effective_user.id not in ADMIN_IDS: return False
    st=context.user_data.get('admin_state'); text=update.message.text.strip()
    try:
        if st=='card':
            title,num=[x.strip() for x in text.split('|',1)]; execsql('INSERT INTO cards(title,number) VALUES(?,?)',(title,num)); await update.message.reply_text('✅ کارت اضافه شد.')
        elif st=='event':
            kind,title,desc=[x.strip() for x in text.split('|',2)]; execsql('INSERT INTO events(kind,title,description) VALUES(?,?,?)',(kind,title,desc)); await update.message.reply_text('✅ رویداد اضافه شد.')
        elif st=='show':
            eid,at,hall,price=[x.strip() for x in text.split('|')]; execsql('INSERT INTO shows(event_id,show_at,hall,base_price) VALUES(?,?,?,?)',(int(eid),at,hall,int(price))); await update.message.reply_text('✅ سانس اضافه شد.')
        elif st=='seats':
            sid,rows,cols=[int(x.strip()) for x in text.split('|')]
            for r in range(1,rows+1):
                for c in range(1,cols+1): execsql('INSERT OR IGNORE INTO seats(show_id,label) VALUES(?,?)',(sid,f'{r}-{c}'))
            await update.message.reply_text('✅ صندلی‌ها ساخته شدند.')
        elif st=='discount':
            uid,val=[x.strip() for x in text.split('|',1)]
            if val.startswith('price:'): execsql('INSERT INTO discounts(user_id,percent,fixed_price) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET percent=0,fixed_price=excluded.fixed_price',(int(uid),0,int(val[6:])))
            else: execsql('INSERT INTO discounts(user_id,percent,fixed_price) VALUES(?,?,NULL) ON CONFLICT(user_id) DO UPDATE SET percent=excluded.percent,fixed_price=NULL',(int(uid),float(val)))
            await update.message.reply_text('✅ تخفیف ثبت شد.')
        else: return False
    except Exception as e: await update.message.reply_text(f'❌ خطا در فرمت: {e}')
    context.user_data.pop('admin_state',None); return True

async def approve(update, context, oid, ok):
    if update.effective_user.id not in ADMIN_IDS: return
    o=q("SELECT o.*,e.title,e.kind,s.show_at,s.hall,se.label FROM orders o JOIN shows s ON s.id=o.show_id JOIN events e ON e.id=s.event_id JOIN seats se ON se.id=o.seat_id WHERE o.id=? AND o.status='pending'",(oid,),True)
    if not o: return await send(update,'سفارش پیدا نشد یا قبلاً بررسی شده.')
    if ok:
        code='HT-'+uuid.uuid4().hex[:10].upper(); conn.execute("UPDATE orders SET status='approved',ticket_code=? WHERE id=?",(code,oid)); conn.execute("UPDATE seats SET status='sold' WHERE id=?",(o['seat_id'],)); conn.commit()
        await context.bot.send_message(o['user_id'],ticket_text({**dict(o),'ticket_code':code}),parse_mode='Markdown')
        await send(update,'✅ بلیت تأیید و صادر شد.')
    else:
        conn.execute("UPDATE orders SET status='rejected' WHERE id=?",(oid,)); conn.execute("UPDATE seats SET status='free',hold_until=NULL WHERE id=?",(o['seat_id'],)); conn.commit(); await context.bot.send_message(o['user_id'],'❌ رسید شما تأیید نشد و صندلی آزاد شد.'); await send(update,'❌ سفارش رد شد.')

async def send(update,text,buttons=None,**kwargs):
    if update.callback_query:
        try: await update.callback_query.answer()
        except: pass
        return await update.callback_query.message.reply_text(text,reply_markup=kb(buttons or []),**kwargs)
    return await update.message.reply_text(text,reply_markup=kb(buttons or []),**kwargs)

async def cb(update, context):
    d=update.callback_query.data
    if d=='home': return await send(update,'منوی اصلی',[[InlineKeyboardButton('🎭 رویدادهای هنری',callback_data='events')],[InlineKeyboardButton('🎟 بلیت‌های من',callback_data='mytickets')]])
    if d=='events': return await events(update,context)
    if d=='mytickets': return await mytickets(update,context)
    if d=='admin': return await admin(update,context)
    if d=='noop': return await update.callback_query.answer('این صندلی قابل انتخاب نیست.',show_alert=True)
    if d.startswith('event:'): return await event_detail(update,context,int(d.split(':')[1]))
    if d.startswith('show:'): return await show_detail(update,context,int(d.split(':')[1]))
    if d.startswith('seat:'): return await seat_pick(update,context,int(d.split(':')[1]))
    if d.startswith('paycard:'): return await choose_card(update,context,int(d.split(':')[1]))
    if d.startswith('approve:'): return await approve(update,context,int(d.split(':')[1]),True)
    if d.startswith('reject:'): return await approve(update,context,int(d.split(':')[1]),False)
    if d.startswith('a_'): return await admin_callback(update,context)

async def message(update, context):
    if update.effective_user.id in ADMIN_IDS and await admin_text(update,context): return
    if update.message.photo and context.user_data.get('waiting_receipt'): await receipt(update,context); return
    if update.message.text and context.user_data.get('waiting_receipt'): await update.message.reply_text('لطفاً عکس رسید را ارسال کنید.'); return

async def post_init(app):
    asyncio.create_task(cleaner())
async def cleaner():
    while True:
        clean_holds(); await asyncio.sleep(30)

def main():
    if not TOKEN: raise RuntimeError('BOT_TOKEN is not set')
    app=Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler('start',start)); app.add_handler(CallbackQueryHandler(cb)); app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND,message)); app.run_polling()
if __name__=='__main__': main()
