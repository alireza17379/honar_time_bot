import os
import asyncio
import sqlite3
import io
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BufferedInputFile,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
import qrcode


# ============================================================
# تنظیمات
# ============================================================

TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = [
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
]
DB = os.getenv("DB_PATH", "honar_time.db")

# مدت نگه‌داشتن صندلی بعد از انتخاب
HOLD_MINUTES = 10

# اگر رسید ارسال شد ولی مدیر بررسی نکرد، بعد از این مدت آزاد شود
PENDING_MINUTES = 60


bot = Bot(
    TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()


# ============================================================
# دیتابیس
# ============================================================

def db():
    c = sqlite3.connect(DB, timeout=20)
    c.row_factory = sqlite3.Row
    return c


def init():
    c = db()

    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS cards(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bank TEXT,
            number TEXT,
            owner TEXT
        );

        CREATE TABLE IF NOT EXISTS films(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            price INTEGER,
            active INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS shows(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            film_id INTEGER,
            date TEXT,
            time TEXT,
            hall TEXT
        );

        CREATE TABLE IF NOT EXISTS seats(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            show_id INTEGER,
            seat TEXT,
            status TEXT DEFAULT 'free',
            user_id INTEGER,
            receipt TEXT,
            code TEXT,
            reserved_until TEXT
        );

        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            discount INTEGER DEFAULT 0,
            custom_price INTEGER
        );

        CREATE TABLE IF NOT EXISTS tickets(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE,
            user_id INTEGER,
            show_id INTEGER,
            seat_id INTEGER,
            amount INTEGER,
            created TEXT,
            status TEXT DEFAULT 'valid'
        );
        """
    )

    # مهاجرت دیتابیس‌های قدیمی
    cols = {
        row["name"]
        for row in c.execute("PRAGMA table_info(seats)").fetchall()
    }
    if "reserved_until" not in cols:
        c.execute("ALTER TABLE seats ADD COLUMN reserved_until TEXT")

    c.commit()
    c.close()


# ============================================================
# ابزارها و کیبوردها
# ============================================================

def K(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def mainkb():
    return K(
        [
            [
                InlineKeyboardButton(
                    text="🎬 خرید بلیت",
                    callback_data="films",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎟️ بلیت‌های من",
                    callback_data="mine",
                )
            ],
            [
                InlineKeyboardButton(
                    text="💳 اطلاعات پرداخت",
                    callback_data="cards",
                )
            ],
            [
                InlineKeyboardButton(
                    text="ℹ️ درباره ما",
                    callback_data="about",
                )
            ],
        ]
    )


def adminkb():
    return K(
        [
            [
                InlineKeyboardButton(
                    text="💳 کارت‌های بانکی",
                    callback_data="acards",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎬 فیلم‌ها",
                    callback_data="afilms",
                ),
                InlineKeyboardButton(
                    text="🕐 سانس‌ها",
                    callback_data="ashows",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💺 صندلی‌ها",
                    callback_data="aseats",
                ),
                InlineKeyboardButton(
                    text="🎁 تخفیف کاربران",
                    callback_data="adiscount",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🧾 رسیدهای در انتظار",
                    callback_data="areceipts",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📊 گزارش فروش",
                    callback_data="report",
                )
            ],
        ]
    )


def admin(user_id):
    return user_id in ADMIN_IDS


def back_admin():
    return K(
        [
            [
                InlineKeyboardButton(
                    text="⬅️ پنل مدیریت",
                    callback_data="admin",
                )
            ]
        ]
    )


# ============================================================
# وضعیت‌های فرم
# ============================================================

class S(StatesGroup):
    bank = State()
    number = State()
    owner = State()

    title = State()
    price = State()

    fid = State()
    date = State()
    time = State()
    hall = State()

    sid = State()
    count = State()
    cols = State()

    uid = State()
    discount = State()


# user_id -> (show_id, seat_id, amount)
pending = {}


# ============================================================
# قیمت و رزرو
# ============================================================

def price_for(uid, base):
    c = db()
    u = c.execute(
        "SELECT * FROM users WHERE user_id=?",
        (uid,),
    ).fetchone()
    c.close()

    if not u:
        return base, 0

    if u["custom_price"] is not None:
        return int(u["custom_price"]), None

    d = max(0, min(100, int(u["discount"] or 0)))
    return round(base * (100 - d) / 100), d


def now():
    return datetime.now()


def iso(dt):
    return dt.isoformat(timespec="seconds")


def expire_old_holds():
    """صندلی‌های رزرو موقت و رسیدهای خیلی قدیمی را آزاد می‌کند."""
    c = db()
    current = iso(now())

    # held = کاربر صندلی را انتخاب کرده ولی هنوز رسید نداده
    c.execute(
        """
        UPDATE seats
        SET status='free',
            user_id=NULL,
            receipt=NULL,
            reserved_until=NULL
        WHERE status='held'
          AND reserved_until IS NOT NULL
          AND reserved_until < ?
        """,
        (current,),
    )

    # pending = رسید فرستاده شده ولی بیش از یک ساعت بررسی نشده
    cutoff = iso(now() - timedelta(minutes=PENDING_MINUTES))
    c.execute(
        """
        UPDATE seats
        SET status='free',
            user_id=NULL,
            receipt=NULL,
            reserved_until=NULL
        WHERE status='pending'
          AND reserved_until IS NOT NULL
          AND reserved_until < ?
        """,
        (cutoff,),
    )

    c.commit()
    c.close()


async def cleanup_loop():
    while True:
        try:
            expire_old_holds()
        except Exception as e:
            print("cleanup error:", e)
        await asyncio.sleep(60)


def make_code():
    return "HT-" + datetime.now().strftime("%y%m%d%H%M%S%f")[-10:]


def make_qr(data):
    x = qrcode.make(data)
    b = io.BytesIO()
    x.save(b, "PNG")
    return b.getvalue()


# ============================================================
# کاربر
# ============================================================

@dp.message(CommandStart())
async def start(m: Message):
    await m.answer(
        "🎭 <b>کارگزاری به وقت هنر</b>\n\n"
        "به سامانه فروش بلیت خوش آمدید.",
        reply_markup=mainkb(),
    )

    if admin(m.from_user.id):
        await m.answer(
            "🛠️ پنل مدیریت",
            reply_markup=adminkb(),
        )


@dp.message(Command("admin"))
async def admincmd(m: Message):
    if admin(m.from_user.id):
        await m.answer(
            "🛠️ <b>پنل مدیریت</b>",
            reply_markup=adminkb(),
        )


@dp.callback_query(F.data == "about")
async def about(q: CallbackQuery):
    await q.message.edit_text(
        "🎭 <b>کارگزاری به وقت هنر</b>\n"
        "فروش بلیت سینما.",
        reply_markup=mainkb(),
    )
    await q.answer()


@dp.callback_query(F.data == "cards")
async def cards(q: CallbackQuery):
    expire_old_holds()

    c = db()
    r = c.execute("SELECT * FROM cards ORDER BY id").fetchall()
    c.close()

    text = "💳 <b>کارت‌های پرداخت</b>\n\n"

    if not r:
        text += "هنوز کارتی ثبت نشده."
    else:
        for x in r:
            text += (
                f"🏦 <b>{x['bank']}</b>\n"
                f"💳 <code>{x['number']}</code>\n"
                f"👤 {x['owner']}\n\n"
            )

    await q.message.edit_text(
        text,
        reply_markup=K(
            [
                [
                    InlineKeyboardButton(
                        text="⬅️ بازگشت",
                        callback_data="home",
                    )
                ]
            ]
        ),
    )
    await q.answer()


@dp.callback_query(F.data == "home")
async def home(q: CallbackQuery):
    await q.message.edit_text(
        "🎭 <b>منوی اصلی</b>",
        reply_markup=mainkb(),
    )
    await q.answer()


# ============================================================
# خرید بلیت
# ============================================================

@dp.callback_query(F.data == "films")
async def films(q: CallbackQuery):
    expire_old_holds()

    c = db()
    r = c.execute(
        "SELECT * FROM films WHERE active=1 ORDER BY id DESC"
    ).fetchall()
    c.close()

    rows = [
        [
            InlineKeyboardButton(
                text=f"🎬 {x['title']} | {x['price']:,} تومان",
                callback_data=f"film:{x['id']}",
            )
        ]
        for x in r
    ]

    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ بازگشت",
                callback_data="home",
            )
        ]
    )

    await q.message.edit_text(
        "🎬 <b>فیلم را انتخاب کنید:</b>",
        reply_markup=K(rows),
    )
    await q.answer()


@dp.callback_query(F.data.startswith("film:"))
async def film(q: CallbackQuery):
    expire_old_holds()

    fid = int(q.data.split(":")[1])

    c = db()
    f = c.execute(
        "SELECT * FROM films WHERE id=? AND active=1",
        (fid,),
    ).fetchone()
    s = c.execute(
        """
        SELECT *
        FROM shows
        WHERE film_id=?
        ORDER BY date,time
        """,
        (fid,),
    ).fetchall()
    c.close()

    if not f:
        await q.answer("فیلم پیدا نشد.", show_alert=True)
        return

    if not s:
        await q.message.edit_text(
            f"🎬 {f['title']}\n\nسانسی ثبت نشده.",
            reply_markup=back_admin() if admin(q.from_user.id) else mainkb(),
        )
        await q.answer()
        return

    rows = [
        [
            InlineKeyboardButton(
                text=f"📅 {x['date']} | {x['time']} | {x['hall']}",
                callback_data=f"show:{x['id']}",
            )
        ]
        for x in s
    ]

    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ فیلم‌ها",
                callback_data="films",
            )
        ]
    )

    await q.message.edit_text(
        f"🎬 <b>{f['title']}</b>\n\n🕐 <b>سانس را انتخاب کنید:</b>",
        reply_markup=K(rows),
    )
    await q.answer()


@dp.callback_query(F.data.startswith("show:"))
async def show(q: CallbackQuery):
    expire_old_holds()

    sid = int(q.data.split(":")[1])

    c = db()
    s = c.execute(
        """
        SELECT s.*, f.title, f.price
        FROM shows s
        JOIN films f ON f.id=s.film_id
        WHERE s.id=?
        """,
        (sid,),
    ).fetchone()

    seats = c.execute(
        """
        SELECT *
        FROM seats
        WHERE show_id=?
        ORDER BY id
        """,
        (sid,),
    ).fetchall()
    c.close()

    if not s:
        await q.answer("سانس پیدا نشد.", show_alert=True)
        return

    if not seats:
        await q.message.edit_text(
            "برای این سانس هنوز صندلی تعریف نشده.",
            reply_markup=K(
                [
                    [
                        InlineKeyboardButton(
                            text="⬅️ بازگشت",
                            callback_data=f"film:{s['film_id']}",
                        )
                    ]
                ]
            ),
        )
        await q.answer()
        return

    rows = []
    line = []

    for x in seats:
        if x["status"] == "free":
            mark = "🟩"
        elif x["status"] == "held":
            mark = "🟨"
        elif x["status"] == "pending":
            mark = "🟧"
        else:
            mark = "🟥"

        # صندلی‌های غیرآزاد قابل انتخاب نیستند
        button = InlineKeyboardButton(
            text=f"{mark}{x['seat']}",
            callback_data=(
                f"seat:{sid}:{x['id']}"
                if x["status"] == "free"
                else f"busy:{x['id']}"
            ),
        )

        line.append(button)

        if len(line) == 4:
            rows.append(line)
            line = []

    if line:
        rows.append(line)

    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ بازگشت",
                callback_data=f"film:{s['film_id']}",
            )
        ]
    )

    await q.message.edit_text(
        f"🎬 <b>{s['title']}</b>\n"
        f"📅 {s['date']}  🕐 {s['time']}\n"
        f"🏢 {s['hall']}\n\n"
        f"🟩 آزاد | 🟨 رزرو موقت | 🟧 در انتظار بررسی | 🟥 فروخته\n\n"
        f"💺 <b>صندلی:</b>",
        reply_markup=K(rows),
    )
    await q.answer()


@dp.callback_query(F.data.startswith("busy:"))
async def busy(q: CallbackQuery):
    await q.answer(
        "این صندلی در حال حاضر قابل انتخاب نیست.",
        show_alert=True,
    )


@dp.callback_query(F.data.startswith("seat:"))
async def seat(q: CallbackQuery):
    expire_old_holds()

    _, sid, seatid = q.data.split(":")
    sid = int(sid)
    seatid = int(seatid)

    c = db()

    # تراکنش برای جلوگیری از فروش همزمان
    try:
        c.execute("BEGIN IMMEDIATE")

        s = c.execute(
            """
            SELECT *
            FROM seats
            WHERE id=? AND show_id=? AND status='free'
            """,
            (seatid, sid),
        ).fetchone()

        sh = c.execute(
            """
            SELECT s.*, f.title, f.price
            FROM shows s
            JOIN films f ON f.id=s.film_id
            WHERE s.id=?
            """,
            (sid,),
        ).fetchone()

        if not s or not sh:
            c.rollback()
            c.close()
            await q.answer(
                "صندلی دیگر آزاد نیست.",
                show_alert=True,
            )
            return

        amount, discount = price_for(
            q.from_user.id,
            sh["price"],
        )

        until = now() + timedelta(minutes=HOLD_MINUTES)

        c.execute(
            """
            UPDATE seats
            SET status='held',
                user_id=?,
                reserved_until=?
            WHERE id=? AND status='free'
            """,
            (
                q.from_user.id,
                iso(until),
                seatid,
            ),
        )

        c.commit()

    except Exception:
        try:
            c.rollback()
        except Exception:
            pass
        c.close()
        await q.answer(
            "خطایی در رزرو صندلی رخ داد. دوباره امتحان کنید.",
            show_alert=True,
        )
        return

    c.close()

    discount_text = ""
    if discount not in (0, None):
        discount_text = f"\n🎁 تخفیف شما: <b>{discount}%</b>"
    elif discount is None:
        discount_text = "\n🎁 قیمت اختصاصی شما اعمال شده."

    await q.message.edit_text(
        f"🎟️ <b>سفارش</b>\n"
        f"🎬 {sh['title']}\n"
        f"📅 {sh['date']} {sh['time']}\n"
        f"💺 {s['seat']}\n"
        f"💰 مبلغ: <b>{amount:,} تومان</b>"
        f"{discount_text}\n\n"
        f"⏳ این صندلی تا <b>{HOLD_MINUTES} دقیقه</b> برای شما نگه داشته شده.\n"
        f"بعد از پرداخت، عکس رسید را ارسال کنید.",
        reply_markup=K(
            [
                [
                    InlineKeyboardButton(
                        text="💳 ادامه پرداخت",
                        callback_data=f"pay:{sid}:{seatid}:{amount}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="❌ لغو رزرو",
                        callback_data=f"cancelhold:{seatid}",
                    )
                ],
            ]
        ),
    )
    await q.answer("صندلی برای شما رزرو شد.")


@dp.callback_query(F.data.startswith("cancelhold:"))
async def cancelhold(q: CallbackQuery):
    seatid = int(q.data.split(":")[1])

    c = db()
    c.execute(
        """
        UPDATE seats
        SET status='free',
            user_id=NULL,
            receipt=NULL,
            reserved_until=NULL
        WHERE id=? AND status='held' AND user_id=?
        """,
        (seatid, q.from_user.id),
    )
    c.commit()
    c.close()

    await q.message.edit_text(
        "✅ رزرو لغو شد.",
        reply_markup=mainkb(),
    )
    await q.answer()


@dp.callback_query(F.data.startswith("pay:"))
async def pay(q: CallbackQuery):
    expire_old_holds()

    _, sid, seatid, amount = q.data.split(":")
    sid = int(sid)
    seatid = int(seatid)
    amount = int(amount)

    c = db()
    seat_row = c.execute(
        """
        SELECT *
        FROM seats
        WHERE id=? AND show_id=? AND status='held'
          AND user_id=?
        """,
        (seatid, sid, q.from_user.id),
    ).fetchone()

    cards_rows = c.execute(
        "SELECT * FROM cards ORDER BY id"
    ).fetchall()
    c.close()

    if not seat_row:
        await q.answer(
            "رزرو این صندلی منقضی شده است.",
            show_alert=True,
        )
        await q.message.edit_text(
            "⏰ زمان رزرو تمام شده است.",
            reply_markup=mainkb(),
        )
        return

    rows = [
        [
            InlineKeyboardButton(
                text=f"🏦 {x['bank']}",
                callback_data=f"card:{sid}:{seatid}:{amount}:{x['id']}",
            )
        ]
        for x in cards_rows
    ]

    if not rows:
        rows.append(
            [
                InlineKeyboardButton(
                    text="⬅️ بازگشت",
                    callback_data=f"show:{sid}",
                )
            ]
        )

    await q.message.edit_text(
        "💳 <b>بانک مقصد را انتخاب کنید:</b>",
        reply_markup=K(rows),
    )
    await q.answer()


@dp.callback_query(F.data.startswith("card:"))
async def card(q: CallbackQuery):
    _, sid, seatid, amount, cid = q.data.split(":")
    sid = int(sid)
    seatid = int(seatid)
    amount = int(amount)
    cid = int(cid)

    expire_old_holds()

    c = db()
    x = c.execute(
        "SELECT * FROM cards WHERE id=?",
        (cid,),
    ).fetchone()

    s = c.execute(
        """
        SELECT *
        FROM seats
        WHERE id=? AND show_id=? AND status='held' AND user_id=?
        """,
        (seatid, sid, q.from_user.id),
    ).fetchone()
    c.close()

    if not x or not s:
        await q.answer(
            "رزرو شما منقضی شده است.",
            show_alert=True,
        )
        return

    await q.message.edit_text(
        f"🏦 <b>{x['bank']}</b>\n"
        f"💳 <code>{x['number']}</code>\n"
        f"👤 {x['owner']}\n\n"
        f"💰 مبلغ قابل پرداخت: <b>{amount:,} تومان</b>\n\n"
        f"پس از کارت‌به‌کارت، عکس رسید را ارسال کنید.",
        reply_markup=K(
            [
                [
                    InlineKeyboardButton(
                        text="📤 ارسال رسید",
                        callback_data=f"receipt:{sid}:{seatid}:{amount}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ انتخاب بانک",
                        callback_data=f"pay:{sid}:{seatid}:{amount}",
                    )
                ],
            ]
        ),
    )
    await q.answer()


@dp.callback_query(F.data.startswith("receipt:"))
async def rec(q: CallbackQuery):
    _, sid, seatid, amount = q.data.split(":")
    sid = int(sid)
    seatid = int(seatid)
    amount = int(amount)

    expire_old_holds()

    c = db()
    s = c.execute(
        """
        SELECT *
        FROM seats
        WHERE id=? AND show_id=? AND status='held' AND user_id=?
        """,
        (seatid, sid, q.from_user.id),
    ).fetchone()
    c.close()

    if not s:
        await q.answer(
            "زمان رزرو شما تمام شده است.",
            show_alert=True,
        )
        return

    pending[q.from_user.id] = (
        sid,
        seatid,
        amount,
    )

    await q.message.edit_text(
        "📤 <b>عکس رسید را ارسال کنید.</b>\n\n"
        "بعد از ارسال، رسید برای مدیر فرستاده می‌شود و پس از تأیید، بلیت صادر خواهد شد.\n\n"
        "⏳ لطفاً رسید را همین الان ارسال کنید."
    )
    await q.answer()


@dp.message(F.photo)
async def photo(m: Message):
    if m.from_user.id not in pending:
        return

    sid, seatid, amount = pending.pop(m.from_user.id)

    expire_old_holds()

    c = db()

    s = c.execute(
        """
        SELECT *
        FROM seats
        WHERE id=? AND show_id=? AND status='held' AND user_id=?
        """,
        (seatid, sid, m.from_user.id),
    ).fetchone()

    if not s:
        c.close()
        await m.answer(
            "❌ زمان رزرو صندلی تمام شده است. لطفاً دوباره خرید را انجام دهید.",
            reply_markup=mainkb(),
        )
        return

    fid = m.photo[-1].file_id
    pending_until = now() + timedelta(minutes=PENDING_MINUTES)

    c.execute(
        """
        UPDATE seats
        SET status='pending',
            receipt=?,
            reserved_until=?
        WHERE id=? AND status='held' AND user_id=?
        """,
        (
            fid,
            iso(pending_until),
            seatid,
            m.from_user.id,
        ),
    )
    c.commit()
    c.close()

    sent = False

    for aid in ADMIN_IDS:
        try:
            await bot.send_photo(
                aid,
                fid,
                caption=(
                    "🧾 <b>رسید جدید</b>\n\n"
                    f"👤 کاربر: <code>{m.from_user.id}</code>\n"
                    f"💺 صندلی: <b>{s['seat']}</b>\n"
                    f"💰 مبلغ: <b>{amount:,} تومان</b>\n\n"
                    "لطفاً رسید را بررسی کنید."
                ),
                reply_markup=K(
                    [
                        [
                            InlineKeyboardButton(
                                text="✅ تأیید و صدور بلیت",
                                callback_data=f"ok:{seatid}:{m.from_user.id}:{amount}",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="❌ رد و آزاد کردن صندلی",
                                callback_data=f"no:{seatid}:{m.from_user.id}",
                            )
                        ],
                    ]
                ),
            )
            sent = True
        except Exception as e:
            print("send receipt error:", e)

    if sent:
        await m.answer(
            "✅ رسید دریافت شد و برای مدیریت ارسال شد.\n"
            "بعد از تأیید، بلیت و QR برای شما ارسال می‌شود."
        )
    else:
        await m.answer(
            "⚠️ رسید دریافت شد، اما مدیر در دسترس نیست. "
            "لطفاً کمی بعد وضعیت را بررسی کنید."
        )


# ============================================================
# تأیید / رد رسید
# ============================================================

@dp.callback_query(F.data.startswith("ok:"))
async def ok(q: CallbackQuery):
    if not admin(q.from_user.id):
        await q.answer("دسترسی ندارید.", show_alert=True)
        return

    _, seatid, uid, amount = q.data.split(":")
    seatid = int(seatid)
    uid = int(uid)
    amount = int(amount)

    c = db()

    s = c.execute(
        """
        SELECT *
        FROM seats
        WHERE id=? AND status='pending'
        """,
        (seatid,),
    ).fetchone()

    if not s:
        c.close()
        await q.answer(
            "این رسید قبلاً بررسی شده یا منقضی شده است.",
            show_alert=True,
        )
        return

    # اگر مبلغ callback صفر بود، مبلغ واقعی از همان کاربر/فیلم دوباره محاسبه شود
    if amount <= 0:
        sh_for_price = c.execute(
            """
            SELECT f.price
            FROM shows s
            JOIN films f ON f.id=s.film_id
            WHERE s.id=?
            """,
            (s["show_id"],),
        ).fetchone()

        if sh_for_price:
            amount, _ = price_for(uid, sh_for_price["price"])

    code = make_code()

    sh = c.execute(
        """
        SELECT s.*, f.title
        FROM shows s
        JOIN films f ON f.id=s.film_id
        WHERE s.id=?
        """,
        (s["show_id"],),
    ).fetchone()

    if not sh:
        c.close()
        await q.answer("سانس پیدا نشد.", show_alert=True)
        return

    c.execute(
        """
        UPDATE seats
        SET status='sold',
            code=?,
            reserved_until=NULL
        WHERE id=? AND status='pending'
        """,
        (code, seatid),
    )

    c.execute(
        """
        INSERT INTO tickets(
            code,user_id,show_id,seat_id,amount,created
        )
        VALUES(?,?,?,?,?,?)
        """,
        (
            code,
            uid,
            s["show_id"],
            seatid,
            amount,
            iso(now()),
        ),
    )

    c.commit()
    c.close()

    qr_data = (
        f"{code}|{uid}|{s['seat']}|"
        f"{sh['title']}|{sh['date']}|{sh['time']}"
    )

    try:
        await bot.send_photo(
            uid,
            BufferedInputFile(
                make_qr(qr_data),
                filename=code + ".png",
            ),
            caption=(
                "🎟️ <b>بلیت صادر شد</b>\n\n"
                f"🎬 {sh['title']}\n"
                f"📅 {sh['date']}\n"
                f"🕐 {sh['time']}\n"
                f"🏢 {sh['hall']}\n"
                f"💺 {s['seat']}\n"
                f"💰 {amount:,} تومان\n"
                f"🔑 <code>{code}</code>\n\n"
                "📌 این QR را هنگام ورود همراه داشته باشید."
            ),
        )
    except Exception as e:
        print("send ticket error:", e)

    try:
        if q.message.caption:
            await q.message.edit_caption(
                caption=q.message.caption + "\n\n"
                "✅ <b>تأیید شد و بلیت صادر شد.</b>"
            )
        else:
            await q.message.edit_text(
                "✅ تأیید شد و بلیت صادر شد."
            )
    except Exception:
        pass

    await q.answer("تأیید شد و بلیت صادر شد.")


@dp.callback_query(F.data.startswith("no:"))
async def no(q: CallbackQuery):
    if not admin(q.from_user.id):
        await q.answer("دسترسی ندارید.", show_alert=True)
        return

    _, seatid, uid = q.data.split(":")
    seatid = int(seatid)
    uid = int(uid)

    c = db()

    row = c.execute(
        """
        SELECT *
        FROM seats
        WHERE id=? AND status='pending'
        """,
        (seatid,),
    ).fetchone()

    c.execute(
        """
        UPDATE seats
        SET status='free',
            user_id=NULL,
            receipt=NULL,
            reserved_until=NULL
        WHERE id=? AND status='pending'
        """,
        (seatid,),
    )

    c.commit()
    c.close()

    if row:
        try:
            await bot.send_message(
                uid,
                "❌ <b>رسید شما تأیید نشد.</b>\n"
                "صندلی آزاد شد. اگر مایل هستید، می‌توانید دوباره خرید کنید.",
                reply_markup=mainkb(),
            )
        except Exception:
            pass

    try:
        if q.message.caption:
            await q.message.edit_caption(
                caption=q.message.caption + "\n\n❌ <b>رسید رد شد.</b>"
            )
        else:
            await q.message.edit_text("❌ رسید رد شد.")
    except Exception:
        pass

    await q.answer("رسید رد شد و صندلی آزاد شد.")


# ============================================================
# بلیت‌های من
# ============================================================

@dp.callback_query(F.data == "mine")
async def mine(q: CallbackQuery):
    c = db()

    r = c.execute(
        """
        SELECT
            t.*,
            f.title,
            s.date,
            s.time,
            s.hall,
            se.seat
        FROM tickets t
        JOIN shows s ON s.id=t.show_id
        JOIN films f ON f.id=s.film_id
        JOIN seats se ON se.id=t.seat_id
        WHERE t.user_id=?
        ORDER BY t.id DESC
        """,
        (q.from_user.id,),
    ).fetchall()

    c.close()

    text = "🎟️ <b>بلیت‌های شما</b>\n\n"

    if not r:
        text += "هنوز بلیتی ندارید."
    else:
        for x in r:
            text += (
                f"🎬 {x['title']}\n"
                f"📅 {x['date']} {x['time']}\n"
                f"🏢 {x['hall']}\n"
                f"💺 {x['seat']}\n"
                f"💰 {x['amount']:,} تومان\n"
                f"🔑 <code>{x['code']}</code>\n\n"
            )

    await q.message.edit_text(
        text,
        reply_markup=mainkb(),
    )
    await q.answer()


# ============================================================
# مدیریت کارت‌ها
# ============================================================

@dp.callback_query(F.data == "acards")
async def acards(q: CallbackQuery):
    if not admin(q.from_user.id):
        await q.answer("دسترسی ندارید.", show_alert=True)
        return

    c = db()
    r = c.execute(
        "SELECT * FROM cards ORDER BY id"
    ).fetchall()
    c.close()

    text = "💳 <b>کارت‌های پرداخت</b>\n\n"

    if not r:
        text += "هیچ کارتی ثبت نشده."
    else:
        for x in r:
            text += (
                f"{x['id']}. 🏦 {x['bank']}\n"
                f"💳 <code>{x['number']}</code>\n"
                f"👤 {x['owner']}\n\n"
            )

    await q.message.edit_text(
        text,
        reply_markup=K(
            [
                [
                    InlineKeyboardButton(
                        text="➕ افزودن کارت",
                        callback_data="addcard",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🗑 حذف کارت",
                        callback_data="delcard",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ پنل",
                        callback_data="admin",
                    )
                ],
            ]
        ),
    )
    await q.answer()


@dp.callback_query(F.data == "addcard")
async def addcard(q: CallbackQuery, state: FSMContext):
    if not admin(q.from_user.id):
        await q.answer("دسترسی ندارید.", show_alert=True)
        return

    await state.set_state(S.bank)
    await q.message.answer("🏦 نام بانک:")
    await q.answer()


@dp.message(S.bank)
async def bank(m: Message, state: FSMContext):
    await state.update_data(bank=m.text.strip())
    await state.set_state(S.number)
    await m.answer("💳 شماره کارت:")


@dp.message(S.number)
async def number(m: Message, state: FSMContext):
    await state.update_data(number=m.text.strip())
    await state.set_state(S.owner)
    await m.answer("👤 نام صاحب کارت:")


@dp.message(S.owner)
async def owner(m: Message, state: FSMContext):
    d = await state.get_data()

    c = db()
    c.execute(
        """
        INSERT INTO cards(bank,number,owner)
        VALUES(?,?,?)
        """,
        (
            d["bank"],
            d["number"],
            m.text.strip(),
        ),
    )
    c.commit()
    c.close()

    await state.clear()

    await m.answer(
        "✅ کارت ثبت شد.",
        reply_markup=adminkb(),
    )


@dp.callback_query(F.data == "delcard")
async def delcard(q: CallbackQuery):
    if not admin(q.from_user.id):
        await q.answer("دسترسی ندارید.", show_alert=True)
        return

    c = db()
    r = c.execute(
        "SELECT * FROM cards ORDER BY id"
    ).fetchall()
    c.close()

    if not r:
        await q.message.edit_text(
            "هیچ کارتی برای حذف وجود ندارد.",
            reply_markup=back_admin(),
        )
        await q.answer()
        return

    await q.message.edit_text(
        "کارت موردنظر را انتخاب کنید:",
        reply_markup=K(
            [
                [
                    InlineKeyboardButton(
                        text=f"🗑 {x['bank']}",
                        callback_data=f"dc:{x['id']}",
                    )
                ]
                for x in r
            ]
            + [
                [
                    InlineKeyboardButton(
                        text="⬅️ بازگشت",
                        callback_data="acards",
                    )
                ]
            ]
        ),
    )
    await q.answer()


@dp.callback_query(F.data.startswith("dc:"))
async def dc(q: CallbackQuery):
    if not admin(q.from_user.id):
        await q.answer("دسترسی ندارید.", show_alert=True)
        return

    c = db()
    c.execute(
        "DELETE FROM cards WHERE id=?",
        (int(q.data.split(":")[1]),),
    )
    c.commit()
    c.close()

    await q.message.edit_text(
        "✅ کارت حذف شد.",
        reply_markup=adminkb(),
    )
    await q.answer()


# ============================================================
# مدیریت فیلم‌ها
# ============================================================

@dp.callback_query(F.data == "afilms")
async def afilms(q: CallbackQuery):
    if not admin(q.from_user.id):
        await q.answer("دسترسی ندارید.", show_alert=True)
        return

    c = db()
    r = c.execute(
        "SELECT * FROM films ORDER BY id DESC"
    ).fetchall()
    c.close()

    text = "🎬 <b>فیلم‌ها</b>\n\n"

    if not r:
        text += "فیلمی ثبت نشده."
    else:
        for x in r:
            state = "فعال" if x["active"] else "غیرفعال"
            text += (
                f"{x['id']}. {x['title']} — "
                f"{x['price']:,} تومان — {state}\n"
            )

    await q.message.edit_text(
        text,
        reply_markup=K(
            [
                [
                    InlineKeyboardButton(
                        text="➕ افزودن فیلم",
                        callback_data="addfilm",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ پنل",
                        callback_data="admin",
                    )
                ],
            ]
        ),
    )
    await q.answer()


@dp.callback_query(F.data == "addfilm")
async def addfilm(q: CallbackQuery, state: FSMContext):
    if not admin(q.from_user.id):
        await q.answer("دسترسی ندارید.", show_alert=True)
        return

    await state.set_state(S.title)
    await q.message.answer("🎬 نام فیلم:")
    await q.answer()


@dp.message(S.title)
async def title(m: Message, state: FSMContext):
    await state.update_data(title=m.text.strip())
    await state.set_state(S.price)
    await m.answer("💰 قیمت پایه (تومان):")


@dp.message(S.price)
async def filmprice(m: Message, state: FSMContext):
    try:
        p = int(
            m.text.replace(",", "")
            .replace("٬", "")
            .strip()
        )
        if p <= 0:
            raise ValueError
    except Exception:
        await m.answer("فقط یک قیمت عددی صحیح وارد کنید.")
        return

    d = await state.get_data()

    c = db()
    c.execute(
        "INSERT INTO films(title,price) VALUES(?,?)",
        (d["title"], p),
    )
    c.commit()
    c.close()

    await state.clear()

    await m.answer(
        "✅ فیلم ثبت شد.",
        reply_markup=adminkb(),
    )


# ============================================================
# مدیریت سانس‌ها
# ============================================================

@dp.callback_query(F.data == "ashows")
async def ashows(q: CallbackQuery):
    if not admin(q.from_user.id):
        await q.answer("دسترسی ندارید.", show_alert=True)
        return

    c = db()
    r = c.execute(
        """
        SELECT s.*, f.title
        FROM shows s
        JOIN films f ON f.id=s.film_id
        ORDER BY s.date,s.time
        """
    ).fetchall()
    c.close()

    text = "🕐 <b>سانس‌ها</b>\n\n"

    if not r:
        text += "سانسی ثبت نشده."
    else:
        for x in r:
            text += (
                f"{x['id']}. {x['title']} | "
                f"{x['date']} {x['time']} | {x['hall']}\n"
            )

    await q.message.edit_text(
        text,
        reply_markup=K(
            [
                [
                    InlineKeyboardButton(
                        text="➕ افزودن سانس",
                        callback_data="addshow",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ پنل",
                        callback_data="admin",
                    )
                ],
            ]
        ),
    )
    await q.answer()


@dp.callback_query(F.data == "addshow")
async def addshow(q: CallbackQuery, state: FSMContext):
    if not admin(q.from_user.id):
        await q.answer("دسترسی ندارید.", show_alert=True)
        return

    c = db()
    r = c.execute(
        "SELECT * FROM films WHERE active=1 ORDER BY id DESC"
    ).fetchall()
    c.close()

    if not r:
        await q.answer(
            "اول حداقل یک فیلم فعال اضافه کنید.",
            show_alert=True,
        )
        return

    await state.set_state(S.fid)

    await q.message.edit_text(
        "🎬 فیلم را انتخاب کنید:",
        reply_markup=K(
            [
                [
                    InlineKeyboardButton(
                        text=x["title"],
                        callback_data=f"sf:{x['id']}",
                    )
                ]
                for x in r
            ]
        ),
    )
    await q.answer()


@dp.callback_query(F.data.startswith("sf:"))
async def sf(q: CallbackQuery, state: FSMContext):
    if not admin(q.from_user.id):
        await q.answer("دسترسی ندارید.", show_alert=True)
        return

    await state.update_data(
        fid=int(q.data.split(":")[1])
    )
    await state.set_state(S.date)
    await q.message.answer(
        "📅 تاریخ را وارد کنید، مثلاً:\n"
        "<code>1405/06/25</code>"
    )
    await q.answer()


@dp.message(S.date)
async def date(m: Message, state: FSMContext):
    await state.update_data(date=m.text.strip())
    await state.set_state(S.time)
    await m.answer("🕐 ساعت را وارد کنید، مثلاً <code>18:30</code>:")


@dp.message(S.time)
async def time(m: Message, state: FSMContext):
    await state.update_data(time=m.text.strip())
    await state.set_state(S.hall)
    await m.answer("🏢 نام سالن:")


@dp.message(S.hall)
async def hall(m: Message, state: FSMContext):
    d = await state.get_data()

    c = db()
    c.execute(
        """
        INSERT INTO shows(film_id,date,time,hall)
        VALUES(?,?,?,?)
        """,
        (
            d["fid"],
            d["date"],
            d["time"],
            m.text.strip(),
        ),
    )
    c.commit()
    c.close()

    await state.clear()

    await m.answer(
        "✅ سانس ثبت شد.",
        reply_markup=adminkb(),
    )


# ============================================================
# مدیریت صندلی‌ها
# ============================================================

@dp.callback_query(F.data == "aseats")
async def aseats(q: CallbackQuery, state: FSMContext):
    if not admin(q.from_user.id):
        await q.answer("دسترسی ندارید.", show_alert=True)
        return

    c = db()
    r = c.execute(
        """
        SELECT
            s.id,
            f.title,
            s.date,
            s.time,
            s.hall,
            COUNT(se.id) AS n
        FROM shows s
        JOIN films f ON f.id=s.film_id
        LEFT JOIN seats se ON se.show_id=s.id
        GROUP BY s.id
        ORDER BY s.date,s.time
        """
    ).fetchall()
    c.close()

    if not r:
        await q.message.edit_text(
            "هنوز سانسی ثبت نشده.",
            reply_markup=back_admin(),
        )
        await q.answer()
        return

    await q.message.edit_text(
        "💺 <b>سانس را انتخاب کنید:</b>",
        reply_markup=K(
            [
                [
                    InlineKeyboardButton(
                        text=(
                            f"{x['title']} | "
                            f"{x['date']} {x['time']} | "
                            f"{x['n']} صندلی"
                        ),
                        callback_data=f"ss:{x['id']}",
                    )
                ]
                for x in r
            ]
            + [
                [
                    InlineKeyboardButton(
                        text="⬅️ پنل",
                        callback_data="admin",
                    )
                ]
            ]
        ),
    )
    await q.answer()


@dp.callback_query(F.data.startswith("ss:"))
async def ss(q: CallbackQuery, state: FSMContext):
    if not admin(q.from_user.id):
        await q.answer("دسترسی ندارید.", show_alert=True)
        return

    await state.update_data(
        sid=int(q.data.split(":")[1])
    )
    await state.set_state(S.count)
    await q.message.answer("💺 تعداد صندلی:")
    await q.answer()


@dp.message(S.count)
async def count(m: Message, state: FSMContext):
    try:
        n = int(m.text)
    except Exception:
        await m.answer("عدد وارد کنید.")
        return

    if not 1 <= n <= 300:
        await m.answer("تعداد باید بین 1 تا 300 باشد.")
        return

    await state.update_data(count=n)
    await state.set_state(S.cols)
    await m.answer("↔️ تعداد صندلی در هر ردیف:")


@dp.message(S.cols)
async def cols(m: Message, state: FSMContext):
    try:
        columns = int(m.text)
    except Exception:
        await m.answer("عدد وارد کنید.")
        return

    if not 1 <= columns <= 20:
        await m.answer("تعداد ستون باید بین 1 تا 20 باشد.")
        return

    d = await state.get_data()

    c = db()

    exists = c.execute(
        """
        SELECT COUNT(*) AS n
        FROM seats
        WHERE show_id=?
        """,
        (d["sid"],),
    ).fetchone()["n"]

    if exists:
        c.close()
        await state.clear()
        await m.answer(
            "⚠️ برای این سانس قبلاً صندلی ساخته شده.",
            reply_markup=adminkb(),
        )
        return

    vals = []

    for i in range(d["count"]):
        row = i // columns

        if row < 26:
            row_name = chr(65 + row)
        else:
            row_name = "R" + str(row + 1)

        seat_name = f"{row_name}{i % columns + 1}"
        vals.append((d["sid"], seat_name))

    c.executemany(
        "INSERT INTO seats(show_id,seat) VALUES(?,?)",
        vals,
    )
    c.commit()
    c.close()

    await state.clear()

    await m.answer(
        "✅ صندلی‌ها ساخته شدند.",
        reply_markup=adminkb(),
    )


# ============================================================
# تخفیف کاربران — فقط توسط مدیر
# ============================================================

@dp.callback_query(F.data == "adiscount")
async def adiscount(q: CallbackQuery, state: FSMContext):
    if not admin(q.from_user.id):
        await q.answer("دسترسی ندارید.", show_alert=True)
        return

    await state.set_state(S.uid)

    await q.message.answer(
        "👤 <b>آیدی عددی کاربر را وارد کنید.</b>\n\n"
        "مثلاً:\n"
        "<code>123456789</code>"
    )
    await q.answer()


@dp.message(S.uid)
async def uid(m: Message, state: FSMContext):
    try:
        u = int(m.text.strip())
        if u <= 0:
            raise ValueError
    except Exception:
        await m.answer("آیدی باید یک عدد صحیح باشد.")
        return

    await state.update_data(uid=u)
    await state.set_state(S.discount)

    await m.answer(
        "🎁 درصد تخفیف را وارد کنید.\n\n"
        "مثلاً <code>20</code> یعنی ۲۰٪ تخفیف.\n\n"
        "برای حذف تخفیف، <code>0</code> وارد کنید.\n\n"
        "اگر خواستید قیمت کاملاً اختصاصی بدهید:\n"
        "<code>price:100000</code>"
    )


@dp.message(S.discount)
async def discount(m: Message, state: FSMContext):
    d = await state.get_data()
    text = m.text.strip()

    c = db()

    if text.lower().startswith("price:"):
        try:
            value = int(
                text.split(":", 1)[1]
                .replace(",", "")
                .replace("٬", "")
                .strip()
            )
            if value <= 0:
                raise ValueError
        except Exception:
            c.close()
            await m.answer(
                "فرمت اشتباه است. مثال:\n"
                "<code>price:100000</code>"
            )
            return

        c.execute(
            """
            INSERT INTO users(user_id,discount,custom_price)
            VALUES(?,0,?)
            ON CONFLICT(user_id)
            DO UPDATE SET
                discount=0,
                custom_price=excluded.custom_price
            """,
            (d["uid"], value),
        )

        message = (
            f"✅ قیمت اختصاصی <b>{value:,} تومان</b> "
            f"برای کاربر <code>{d['uid']}</code> ثبت شد."
        )

    else:
        try:
            value = int(text)
            if not 0 <= value <= 100:
                raise ValueError
        except Exception:
            c.close()
            await m.answer(
                "درصد باید عددی بین ۰ تا ۱۰۰ باشد."
            )
            return

        c.execute(
            """
            INSERT INTO users(user_id,discount,custom_price)
            VALUES(?,?,NULL)
            ON CONFLICT(user_id)
            DO UPDATE SET
                discount=excluded.discount,
                custom_price=NULL
            """,
            (d["uid"], value),
        )

        if value == 0:
            message = (
                f"✅ تخفیف کاربر <code>{d['uid']}</code> حذف شد."
            )
        else:
            message = (
                f"✅ تخفیف <b>{value}%</b> برای کاربر "
                f"<code>{d['uid']}</code> ثبت شد."
            )

    c.commit()
    c.close()

    await state.clear()

    await m.answer(
        message,
        reply_markup=adminkb(),
    )


# ============================================================
# رسیدهای در انتظار
# ============================================================

@dp.callback_query(F.data == "areceipts")
async def areceipts(q: CallbackQuery):
    if not admin(q.from_user.id):
        await q.answer("دسترسی ندارید.", show_alert=True)
        return

    expire_old_holds()

    c = db()

    r = c.execute(
        """
        SELECT
            se.id,
            se.user_id,
            se.seat,
            se.receipt,
            f.title,
            s.date,
            s.time,
            f.price
        FROM seats se
        JOIN shows s ON s.id=se.show_id
        JOIN films f ON f.id=s.film_id
        WHERE se.status='pending'
        ORDER BY se.id
        """
    ).fetchall()

    c.close()

    if not r:
        await q.message.edit_text(
            "🧾 <b>رسید در انتظار بررسی وجود ندارد.</b>",
            reply_markup=back_admin(),
        )
        await q.answer()
        return

    await q.message.edit_text(
        f"🧾 <b>{len(r)} رسید در انتظار بررسی است.</b>",
        reply_markup=K(
            [
                [
                    InlineKeyboardButton(
                        text=(
                            f"{x['title']} | "
                            f"{x['seat']} | "
                            f"{x['user_id']}"
                        ),
                        callback_data=f"vr:{x['id']}",
                    )
                ]
                for x in r
            ]
            + [
                [
                    InlineKeyboardButton(
                        text="⬅️ پنل",
                        callback_data="admin",
                    )
                ]
            ]
        ),
    )
    await q.answer()


@dp.callback_query(F.data.startswith("vr:"))
async def vr(q: CallbackQuery):
    if not admin(q.from_user.id):
        await q.answer("دسترسی ندارید.", show_alert=True)
        return

    seatid = int(q.data.split(":")[1])

    c = db()

    x = c.execute(
        """
        SELECT
            se.*,
            f.title,
            f.price,
            s.date,
            s.time,
            s.hall
        FROM seats se
        JOIN shows s ON s.id=se.show_id
        JOIN films f ON f.id=s.film_id
        WHERE se.id=? AND se.status='pending'
        """,
        (seatid,),
    ).fetchone()

    c.close()

    if not x:
        await q.answer(
            "این رسید دیگر در انتظار بررسی نیست.",
            show_alert=True,
        )
        return

    amount, _ = price_for(
        x["user_id"],
        x["price"],
    )

    await bot.send_photo(
        q.from_user.id,
        x["receipt"],
        caption=(
            "🧾 <b>بررسی رسید</b>\n\n"
            f"👤 کاربر: <code>{x['user_id']}</code>\n"
            f"🎬 {x['title']}\n"
            f"📅 {x['date']} {x['time']}\n"
            f"🏢 {x['hall']}\n"
            f"💺 {x['seat']}\n"
            f"💰 مبلغ: <b>{amount:,} تومان</b>"
        ),
        reply_markup=K(
            [
                [
                    InlineKeyboardButton(
                        text="✅ تأیید و صدور بلیت",
                        callback_data=f"ok:{seatid}:{x['user_id']}:{amount}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="❌ رد و آزاد کردن صندلی",
                        callback_data=f"no:{seatid}:{x['user_id']}",
                    )
                ],
            ]
        ),
    )

    await q.answer()


# ============================================================
# گزارش فروش
# ============================================================

@dp.callback_query(F.data == "report")
async def report(q: CallbackQuery):
    if not admin(q.from_user.id):
        await q.answer("دسترسی ندارید.", show_alert=True)
        return

    c = db()

    total_tickets = c.execute(
        "SELECT COUNT(*) AS n FROM tickets WHERE status='valid'"
    ).fetchone()["n"]

    total_amount = c.execute(
        """
        SELECT COALESCE(SUM(amount),0) AS t
        FROM tickets
        WHERE status='valid'
        """
    ).fetchone()["t"]

    pending_count = c.execute(
        """
        SELECT COUNT(*) AS n
        FROM seats
        WHERE status='pending'
        """
    ).fetchone()["n"]

    held_count = c.execute(
        """
        SELECT COUNT(*) AS n
        FROM seats
        WHERE status='held'
        """
    ).fetchone()["n"]

    today_prefix = datetime.now().strftime("%Y-%m-%d")

    today_tickets = c.execute(
        """
        SELECT COUNT(*) AS n
        FROM tickets
        WHERE status='valid'
          AND created LIKE ?
        """,
        (today_prefix + "%",),
    ).fetchone()["n"]

    today_amount = c.execute(
        """
        SELECT COALESCE(SUM(amount),0) AS t
        FROM tickets
        WHERE status='valid'
          AND created LIKE ?
        """,
        (today_prefix + "%",),
    ).fetchone()["t"]

    c.close()

    await q.message.edit_text(
        "📊 <b>گزارش فروش</b>\n\n"
        f"🎟️ کل بلیت‌های فروخته‌شده: <b>{total_tickets}</b>\n"
        f"💰 کل فروش: <b>{total_amount:,} تومان</b>\n\n"
        f"📅 فروش امروز: <b>{today_tickets}</b> بلیت\n"
        f"💵 مبلغ امروز: <b>{today_amount:,} تومان</b>\n\n"
        f"🧾 رسیدهای در انتظار: <b>{pending_count}</b>\n"
        f"🟨 رزروهای موقت: <b>{held_count}</b>",
        reply_markup=adminkb(),
    )
    await q.answer()


# ============================================================
# پنل مدیریت
# ============================================================

@dp.callback_query(F.data == "admin")
async def ap(q: CallbackQuery):
    if admin(q.from_user.id):
        await q.message.edit_text(
            "🛠️ <b>پنل مدیریت</b>\n\n"
            "از منوی زیر بخش موردنظر را انتخاب کنید.",
            reply_markup=adminkb(),
        )

    await q.answer()


# ============================================================
# اجرای ربات
# ============================================================

async def main():
    if not TOKEN:
        raise RuntimeError(
            "BOT_TOKEN را در Environment Variables تنظیم کنید."
        )

    if not ADMIN_IDS:
        raise RuntimeError(
            "ADMIN_IDS را در Environment Variables تنظیم کنید."
        )

    init()

    cleanup_task = asyncio.create_task(cleanup_loop())

    try:
        await dp.start_polling(bot)
    finally:
        cleanup_task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
