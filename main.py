import asyncio
import os
import sqlite3
from datetime import datetime

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
cur.execute("INSERT OR IGNORE INTO contest (id, is_active) VALUES (1, 0)")

cur.execute("""
CREATE TABLE IF NOT EXISTS allowed_chats (
    chat_id INTEGER PRIMARY KEY,
    title TEXT,
    added_at TEXT DEFAULT CURRENT_TIMESTAMP,
    added_by INTEGER,
    message_id INTEGER
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
async def cmd_start(message: types.Message):
    args = message.text.split()
    referrer_id = int(args[1]) if len(args) > 1 and args[1].isdigit() else None

    user = message.from_user
    cur.execute(
        """
        INSERT OR IGNORE INTO users (user_id, username, referrer_id)
        VALUES (?, ?, ?)
        """,
        (user.id, user.username, referrer_id)
    )
    conn.commit()

    cur.execute("SELECT is_active, prize, end_time FROM contest WHERE id = 1")
    row = cur.fetchone()
    is_active, prize, end_time = row if row else (0, None, None)

    if message.chat.type == "private":
        if user.id == ADMIN_ID:
            await message.answer("👑 Админ-панель", reply_markup=admin_kb())
        else:
            await message.answer("Добро пожаловать!", reply_markup=user_kb())
    else:
        if is_active and end_time:
            try:
                remaining = datetime.fromisoformat(end_time) - datetime.utcnow()
                if remaining.total_seconds() > 0:
                    m, s = divmod(int(remaining.total_seconds()), 60)
                    await message.answer(
                        f"🎉 Активный конкурс!\n🏆 Приз: {prize}\n⏳ Осталось: {m:02d}:{s:02d}",
                        reply_markup=await contest_kb()
                    )
                    return
            except:
                pass
        await message.answer("Нет активного конкурса.")

# ── Callbacks ────────────────────────────────────────

@dp.callback_query(lambda c: c.data == "topup")
async def cb_topup(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != "private":
        await callback.answer("Только в личке", show_alert=True)
        return
    await callback.message.answer("Введите сумму пополнения (число):")
    await state.set_state(TopUpState.waiting_amount)
    await callback.answer()


@dp.message(TopUpState.waiting_amount)
async def process_topup_amount(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Нужно ввести число")
        return

    amount = int(message.text)
    if amount <= 0:
        await message.answer("Сумма должна быть больше 0")
        return

    memo = f"{message.from_user.id}_{message.from_user.username or 'no_username'}"

    await message.answer(
        f"💳 Пополнение на {amount} AUR\n"
        f"Кошелёк: <code>{TON_WALLET}</code>\n"
        f"Memo: <code>{memo}</code>\n\n"
        f"После оплаты напишите админу или дождитесь подтверждения.",
        parse_mode="HTML"
    )

    await bot.send_message(
        ADMIN_ID,
        f"🟢 Запрос пополнения\n"
        f"От: {message.from_user.id} (@{message.from_user.username or 'нет'})\n"
        f"Сумма: {amount} AUR",
        reply_markup=confirm_topup_kb(message.from_user.id, amount)
    )

    await state.clear()


@dp.callback_query(lambda c: c.data.startswith("confirm_"))
async def confirm_topup(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Доступ запрещён", show_alert=True)
        return

    try:
        _, uid_str, amt_str = callback.data.split("_")
        uid = int(uid_str)
        amt = int(amt_str)
    except:
        await callback.answer("Ошибка в данных", show_alert=True)
        return

    cur.execute(
        """
        INSERT INTO users (user_id, balance)
        VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET balance = balance + ?
        """,
        (uid, amt, amt)
    )
    conn.commit()

    await bot.send_message(uid, f"✅ Баланс пополнен на {amt} AUR")
    await callback.message.edit_text(callback.message.text + "\n\n✅ Подтверждено")
    await callback.answer("Пополнение подтверждено")


@dp.callback_query(lambda c: c.data == "buy")
async def buy_ticket(callback: types.CallbackQuery):
    cur.execute("SELECT balance FROM users WHERE user_id = ?", (callback.from_user.id,))
    row = cur.fetchone()

    if not row or row[0] < COST_PER_TICKET:
        await callback.message.answer("❌ Недостаточно средств на балансе")
        await callback.answer()
        return

    cur.execute(
        """
        UPDATE users
        SET balance = balance - ?,
            tickets = tickets + 1
        WHERE user_id = ?
        """,
        (COST_PER_TICKET, callback.from_user.id)
    )
    conn.commit()

    await callback.message.answer("🎟 Билет успешно куплен!")
    await callback.answer()


@dp.callback_query(lambda c: c.data == "balance")
async def show_balance(callback: types.CallbackQuery):
    cur.execute(
        "SELECT balance, tickets FROM users WHERE user_id = ?",
        (callback.from_user.id,)
    )
    row = cur.fetchone()
    bal, tik = row if row else (0, 0)

    await callback.message.answer(f"💰 Баланс: {bal} AUR\n🎟 Билетов: {tik}")
    await callback.answer()


@dp.callback_query(lambda c: c.data == "ref")
async def referral_link(callback: types.CallbackQuery):
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start={callback.from_user.id}"
    await callback.message.answer(f"Ваша реферальная ссылка:\n{link}")
    await callback.answer()


@dp.callback_query(lambda c: c.data in ("admin_start", "admin_stop", "set_prize", "admin_view_balances"))
async def admin_buttons(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только для администратора", show_alert=True)
        return

    data = callback.data

    if data == "admin_start":
        await callback.message.answer("Логика запуска конкурса пока не реализована")
    elif data == "admin_stop":
        await callback.message.answer("Логика остановки конкурса пока не реализована")
    elif data == "set_prize":
        await callback.message.answer("Логика установки приза пока не реализована")
    elif data == "admin_view_balances":
        await callback.message.answer("Логика просмотра балансов пока не реализована")

    await callback.answer()

# ──────────────────── FAKE WEB SERVER (для Render / Railway и т.п.) ───────

async def fake_web_server():
    async def handle(request):
        return web.Response(text="Bot is alive")

    app = web.Application()
    app.router.add_get("/", handle)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Fake web server running on port {port}")


# ──────────────────── ЗАПУСК ────────────────────

async def main():
    print("Бот запускается...")
    await asyncio.gather(
        fake_web_server(),
        dp.start_polling(bot, allowed_updates=types.AllowedUpdates.MESSAGE + types.AllowedUpdates.CALLBACK_QUERY)
    )

if __name__ == "__main__":
    asyncio.run(main())