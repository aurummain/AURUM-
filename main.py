import asyncio
import os
import sqlite3
from datetime import datetime, timedelta

from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# ──────────────────── НАСТРОЙКИ ────────────────────

BOT_TOKEN = "8323563478:AAE9qcdBfdvO1ptKkCXS78hJ4SuxeFOnV2w"
ADMIN_ID = 1333099097
TON_WALLET = "UQBJNtgVfE-x7-K1uY_EhW1rdvGKhq5gM244fX89VF0bof7R"

COST_PER_TICKET = 10000
CONTEST_DURATION_MINUTES = 10

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ──────────────────── FSM ────────────────────

class TopUpState(StatesGroup):
    waiting_amount = State()

class SetPrizeState(StatesGroup):
    waiting_prize = State()

# ──────────────────── БАЗА ДАННЫХ ────────────────────

conn = sqlite3.connect("lottery.db", check_same_thread=False)
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    balance INTEGER DEFAULT 0,
    tickets INTEGER DEFAULT 0,
    referrer_id INTEGER,
    rewarded_referrer INTEGER DEFAULT 0
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS contest (
    id INTEGER PRIMARY KEY,
    prize TEXT,
    is_active INTEGER DEFAULT 0,
    end_time TEXT
)
""")
cur.execute("INSERT OR IGNORE INTO contest (id) VALUES (1)")

cur.execute("""
CREATE TABLE IF NOT EXISTS allowed_chats (
    chat_id INTEGER PRIMARY KEY,
    title TEXT,
    added_at TEXT DEFAULT CURRENT_TIMESTAMP,
    added_by INTEGER,
    message_id INTEGER DEFAULT NULL
)
""")
conn.commit()

# ──────────────────── КЛАВИАТУРЫ ────────────────────

def user_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Пополнить", callback_data="topup")],
        [InlineKeyboardButton(text="🎟 Купить билет", callback_data="buy")],
        [InlineKeyboardButton(text="📊 Баланс", callback_data="balance")],
        [InlineKeyboardButton(text="🤝 Реф. ссылка", callback_data="ref")],
    ])

def admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Запустить конкурс", callback_data="admin_start")],
        [InlineKeyboardButton(text="⏹ Остановить конкурс", callback_data="admin_stop")],
        [InlineKeyboardButton(text="🏆 Установить приз", callback_data="set_prize")],
        [InlineKeyboardButton(text="👥 Балансы игроков", callback_data="admin_view_balances")],
    ])

async def contest_kb():
    me = await bot.get_me()
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Перейти в личный кабинет",
            url=f"https://t.me/{me.username}"
        )],
    ])

def confirm_topup_kb(user_id: int, amount: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ Подтвердить {amount}", callback_data=f"confirm_{user_id}_{amount}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{user_id}")]
    ])

# ──────────────────── HANDLERS ────────────────────

@dp.message(Command("start"))
async def cmd_start(msg: types.Message):
    args = msg.text.split()
    referrer_id = int(args[1]) if len(args) > 1 and args[1].isdigit() else None

    user = msg.from_user
    cur.execute(
        "INSERT OR IGNORE INTO users (user_id, username, referrer_id) VALUES (?, ?, ?)",
        (user.id, user.username, referrer_id)
    )
    conn.commit()

    cur.execute("SELECT is_active, prize, end_time FROM contest WHERE id = 1")
    contest = cur.fetchone()
    is_active, prize, end_time = contest if contest else (0, None, None)

    if msg.chat.type == "private":
        if user.id == ADMIN_ID:
            await msg.answer("👑 Админ-панель", reply_markup=admin_kb())
        else:
            await msg.answer("Добро пожаловать!", reply_markup=user_kb())
    else:
        if is_active:
            remaining = datetime.fromisoformat(end_time) - datetime.utcnow()
            if remaining.total_seconds() > 0:
                m, s = divmod(int(remaining.total_seconds()), 60)
                await msg.answer(
                    f"🎉 Активный конкурс!\n🏆 Приз: {prize}\n⏳ Осталось: {m:02d}:{s:02d}",
                    reply_markup=await contest_kb()
                )
                return
        await msg.answer("Нет активного конкурса.")

@dp.callback_query(lambda c: c.data == "topup")
async def cb_topup(c: types.CallbackQuery, state: FSMContext):
    if c.message.chat.type != "private":
        return await c.answer("Только в ЛС")
    await c.message.answer("Введите сумму пополнения:")
    await state.set_state(TopUpState.waiting_amount)
    await c.answer()

@dp.message(TopUpState.waiting_amount)
async def process_topup(msg: types.Message, state: FSMContext):
    if not msg.text.isdigit():
        return await msg.answer("Введите число")

    amount = int(msg.text)
    memo = f"{msg.from_user.id}_{msg.from_user.username or 'no_username'}"

    await msg.answer(
        f"💳 Пополнение на {amount} AUR\n"
        f"Кошелёк: <code>{TON_WALLET}</code>\n"
        f"Memo: <code>{memo}</code>",
        parse_mode="HTML"
    )

    await bot.send_message(
        ADMIN_ID,
        f"🟢 Запрос пополнения\nОт: {msg.from_user.id}\nСумма: {amount}",
        reply_markup=confirm_topup_kb(msg.from_user.id, amount)
    )

    await state.clear()

@dp.callback_query(lambda c: c.data.startswith("confirm_"))
async def confirm_topup(c: types.CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return await c.answer("Запрещено", show_alert=True)

    _, uid, amt = c.data.split("_")
    cur.execute(
        "INSERT INTO users (user_id, balance) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET balance = balance + ?",
        (int(uid), int(amt), int(amt))
    )
    conn.commit()

    await bot.send_message(int(uid), f"✅ Баланс пополнен на {amt} AUR")
    await c.message.edit_text("Подтверждено")
    await c.answer()

@dp.callback_query(lambda c: c.data == "buy")
async def buy_ticket(c: types.CallbackQuery):
    cur.execute("SELECT balance FROM users WHERE user_id = ?", (c.from_user.id,))
    row = cur.fetchone()
    if not row or row[0] < COST_PER_TICKET:
        return await c.message.answer("❌ Недостаточно средств")

    cur.execute(
        "UPDATE users SET balance = balance - ?, tickets = tickets + 1 WHERE user_id = ?",
        (COST_PER_TICKET, c.from_user.id)
    )
    conn.commit()

    await c.message.answer("🎟 Билет куплен!")
    await c.answer()

@dp.callback_query(lambda c: c.data == "balance")
async def balance(c: types.CallbackQuery):
    cur.execute("SELECT balance, tickets FROM users WHERE user_id = ?", (c.from_user.id,))
    bal, tik = cur.fetchone() or (0, 0)
    await c.message.answer(f"💰 {bal} AUR\n🎟 {tik}")
    await c.answer()

@dp.callback_query(lambda c: c.data == "ref")
async def ref(c: types.CallbackQuery):
    me = await bot.get_me()
    await c.message.answer(
        f"https://t.me/{me.username}?start={c.from_user.id}"
    )
    await c.answer()

# ──────────────────── FAKE WEB SERVER (Костыль) ────────────────────

async def fake_web_server():
    async def handle(request):
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_get("/", handle)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    print(f"Fake web server started on port {port}")

# ──────────────────── ЗАПУСК ────────────────────

async def main():
    print("Бот запущен")
    await asyncio.gather(
        fake_web_server(),       # 👈 костыль для Render
        dp.start_polling(bot),   # 👈 бот
    )

if __name__ == "__main__":
    asyncio.run(main())