import asyncio
import os
import sqlite3
import random
import aiohttp
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
DEFAULT_CONTEST_MINUTES = 10
TIMER_UPDATE_INTERVAL = 15  # секунд — безопаснее для Telegram

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

conn.commit()

# ──────────────────── Глобальные переменные ────────────────────

announce_chat_id: int | None = None
announce_message_id: int | None = None
timer_task: asyncio.Task | None = None

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
            text="Личный кабинет",
            url=f"https://t.me/{me.username}"
        )],
    ])

def confirm_topup_kb(user_id: int, amount: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ Подтвердить {amount}", callback_data=f"confirm_{user_id}_{amount}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{user_id}_{amount}")]
    ])

# ──────────────────── HANDLERS ────────────────────

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    args = message.text.split()
    referrer_id = int(args[1]) if len(args) > 1 and args[1].isdigit() else None

    user = message.from_user
    cur.execute(
        "INSERT OR IGNORE INTO users (user_id, username, referrer_id) VALUES (?, ?, ?)",
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
            await message.answer("Добро пожаловать в лотерею!", reply_markup=user_kb())
    else:
        if is_active and end_time:
            try:
                remaining = datetime.fromisoformat(end_time) - datetime.utcnow()
                if remaining.total_seconds() > 0:
                    m, s = divmod(int(remaining.total_seconds()), 60)
                    cur.execute("SELECT SUM(tickets) FROM users")
                    total = cur.fetchone()[0] or 0
                    text = f"🎉 Конкурс идёт!\nПриз: {prize}\nОсталось: {m:02d}:{s:02d}\nБилетов всего: {total}"
                    await message.answer(text, reply_markup=await contest_kb())
                    return
            except Exception as e:
                print(f"Ошибка в /start групповом: {e}")
        await message.answer("Нет активного конкурса.")

@dp.message(Command("addchat"))
async def cmd_addchat(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.reply("Только админ.")
        return

    if message.chat.type not in ("group", "supergroup"):
        await message.reply("Команда только в группах.")
        return

    global announce_chat_id
    announce_chat_id = message.chat.id

    kb = await contest_kb()
    await message.reply(
        "✅ Этот чат выбран для конкурсов.\nТаймер и объявления будут здесь.",
        reply_markup=kb
    )

    await bot.send_message(ADMIN_ID, f"Чат конкурсов: {message.chat.title or message.chat.id}")

# ──────────────────── ПОКУПКА БИЛЕТА (с сообщением в чат) ────────────────────

@dp.callback_query(lambda c: c.data == "buy")
async def buy_ticket(callback: types.CallbackQuery):
    uid = callback.from_user.id

    cur.execute("SELECT balance FROM users WHERE user_id = ?", (uid,))
    row = cur.fetchone()
    if not row or row[0] < COST_PER_TICKET:
        await callback.answer("Недостаточно средств", show_alert=True)
        return

    cur.execute(
        "UPDATE users SET balance = balance - ?, tickets = tickets + 1 WHERE user_id = ?",
        (COST_PER_TICKET, uid)
    )
    conn.commit()

    # Всего билетов
    cur.execute("SELECT SUM(tickets) FROM users")
    total = cur.fetchone()[0] or 0

    # Анонимное сообщение в чат
    if announce_chat_id:
        try:
            await bot.send_message(
                announce_chat_id,
                f"✨ Участник купил билет • Всего билетов в розыгрыше: {total}"
            )
        except Exception as e:
            print(f"Ошибка отправки в чат: {e}")

    await callback.message.answer("🎟 Билет куплен!")
    await callback.answer()

# ──────────────────── ОСТАЛЬНЫЕ ФУНКЦИИ (сокращённо, оставляем как было) ─────

# ... (topup, confirm, reject, balance, ref, set_prize, view_balances, admin_stop — как в предыдущем коде)

# ──────────────────── ЗАПУСК КОНКУРСА ────────────────────

@dp.callback_query(lambda c: c.data == "admin_start")
async def admin_start(callback: types.CallbackQuery):
    global announce_chat_id, announce_message_id, timer_task

    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return

    if announce_chat_id is None:
        await callback.message.answer("Сначала /addchat в нужной группе")
        await callback.answer()
        return

    cur.execute("SELECT prize FROM contest WHERE id = 1")
    prize = cur.fetchone()[0] if cur.fetchone() else "Приз не установлен"

    if not prize:
        await callback.message.answer("Установите приз сначала")
        await callback.answer()
        return

    end_time = (datetime.utcnow() + timedelta(minutes=DEFAULT_CONTEST_MINUTES)).isoformat()

    cur.execute("UPDATE contest SET is_active = 1, end_time = ? WHERE id = 1", (end_time,))
    conn.commit()

    initial_text = f"🎉 Конкурс запущен!\nПриз: {prize}\nОсталось: {DEFAULT_CONTEST_MINUTES:02d}:00\nБилетов: 0"

    msg = await bot.send_message(announce_chat_id, initial_text, reply_markup=await contest_kb())
    announce_message_id = msg.message_id

    if timer_task and not timer_task.done():
        timer_task.cancel()

    timer_task = asyncio.create_task(update_timer())

    await callback.message.answer("Конкурс запущен!")
    await callback.answer()

# ──────────────────── ТАЙМЕР + АВТОРОЗЫГРЫШ ────────────────────

async def update_timer():
    global announce_chat_id, announce_message_id

    while True:
        await asyncio.sleep(TIMER_UPDATE_INTERVAL)

        cur.execute("SELECT is_active, end_time, prize FROM contest WHERE id = 1")
        row = cur.fetchone()
        if not row or not row[0] or not row[1]:
            break

        end_time = datetime.fromisoformat(row[1])
        remaining = end_time - datetime.utcnow()

        cur.execute("SELECT SUM(tickets) FROM users")
        total_tickets = cur.fetchone()[0] or 0

        if remaining.total_seconds() <= 0:
            await perform_draw(total_tickets)
            break

        m, s = divmod(int(remaining.total_seconds()), 60)
        text = f"🎉 Конкурс идёт\nПриз: {row[2]}\nОсталось: {m:02d}:{s:02d}\nБилетов: {total_tickets}"

        try:
            await bot.edit_message_text(
                text,
                chat_id=announce_chat_id,
                message_id=announce_message_id,
                reply_markup=await contest_kb()
            )
        except Exception as e:
            print(f"Timer edit error: {e}")

async def perform_draw(total_tickets):
    if total_tickets == 0:
        text = "Конкурс завершён. Никто не купил билеты."
    else:
        cur.execute("SELECT user_id, tickets FROM users WHERE tickets > 0")
        participants = cur.fetchall()

        pool = []
        for uid, count in participants:
            pool.extend([uid] * count)

        winner_id = random.choice(pool)

        cur.execute("SELECT username FROM users WHERE user_id = ?", (winner_id,))
        winner = cur.fetchone()[0] or f"ID {winner_id}"

        text = f"🎉 Конкурс завершён!\nПобедитель: @{winner}\nПоздравляем! Напишите админу за призом."

        await bot.send_message(winner_id, "Вы выиграли! Напишите админу.")
        await bot.send_message(ADMIN_ID, f"Победитель: @{winner} (ID {winner_id})")

    await bot.edit_message_text(
        text,
        chat_id=announce_chat_id,
        message_id=announce_message_id
    )

    # Сброс
    cur.execute("UPDATE contest SET is_active = 0, end_time = NULL WHERE id = 1")
    cur.execute("UPDATE users SET tickets = 0")
    conn.commit()

# ──────────────────── SELF-PING ДЛЯ ПРОДЛЕНИЯ ЖИЗНИ НА RENDER ────────────────────

async def self_ping():
    my_url = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
    if not my_url:
        print("RENDER_EXTERNAL_HOSTNAME не найден — self-ping отключён")
        return

    url = f"https://{my_url}/health"

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(url) as resp:
                    print(f"Self-ping: {resp.status}")
            except Exception as e:
                print(f"Self-ping ошибка: {e}")
            await asyncio.sleep(300)  # каждые 5 минут

# ──────────────────── FAKE WEB SERVER (улучшенный) ────────────────────

async def fake_web_server():
    async def handle(request):
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_get("/", handle)
    app.router.add_get("/health", handle)  # именно этот маршрут пингует Render

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Сервер на порту {port}")

# ──────────────────── ЗАПУСК ────────────────────

async def main():
    print("Бот стартует...")
    await asyncio.gather(
        fake_web_server(),
        self_ping(),               # анти-idle
        dp.start_polling(
            bot,
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=True
        )
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Остановка бота")
    finally:
        asyncio.run(bot.session.close())