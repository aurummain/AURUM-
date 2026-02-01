import asyncio
import os
import sqlite3
import random
from datetime import datetime, timedelta

from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# ──────────────────── НАСТРОЙКИ ────────────────────

BOT_TOKEN = "8323563478:AAE9qcdBfdvO1ptKkCXS78hJ4SuxeFOnV2w"
ADMIN_ID = 1333099097
TON_WALLET = "UQBJNtgVfE-x7-K1uY_EhW1rdvGKhq5gM244fX89VF0bof7R"

COST_PER_TICKET = 10000
DEFAULT_CONTEST_MINUTES = 10

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
            text="Перейти в личный кабинет",
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

@dp.callback_query(lambda c: c.data == "topup")
async def cb_topup(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != "private":
        await callback.answer("Только в ЛС", show_alert=True)
        return
    await callback.message.answer("Введите сумму пополнения:")
    await state.set_state(TopUpState.waiting_amount)
    await callback.answer()

@dp.message(TopUpState.waiting_amount)
async def process_topup(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Введите число")
        return

    amount = int(message.text)
    memo = f"{message.from_user.id}_{message.from_user.username or 'no_username'}"

    await message.answer(
        f"💳 Пополнение на {amount} AUR\n"
        f"Кошелёк: <code>{TON_WALLET}</code>\n"
        f"Memo: <code>{memo}</code>",
        parse_mode="HTML"
    )

    await bot.send_message(
        ADMIN_ID,
        f"🟢 Запрос пополнения\nОт: {message.from_user.id}\nСумма: {amount}",
        reply_markup=confirm_topup_kb(message.from_user.id, amount)
    )

    await state.clear()

@dp.callback_query(lambda c: c.data.startswith("confirm_"))
async def confirm_topup(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Запрещено", show_alert=True)
        return

    _, uid, amt = callback.data.split("_")
    cur.execute(
        "INSERT INTO users (user_id, balance) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET balance = balance + ?",
        (int(uid), int(amt), int(amt))
    )
    conn.commit()

    await bot.send_message(int(uid), f"✅ Баланс пополнен на {amt} AUR")
    await callback.message.edit_text("Подтверждено")
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("reject_"))
async def reject_topup(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Запрещено", show_alert=True)
        return

    _, uid, amt = callback.data.split("_")
    await bot.send_message(int(uid), f"❌ Пополнение на {amt} AUR отклонено")
    await callback.message.edit_text("Отклонено")
    await callback.answer()

@dp.callback_query(lambda c: c.data == "buy")
async def buy_ticket(callback: types.CallbackQuery):
    cur.execute("SELECT balance FROM users WHERE user_id = ?", (callback.from_user.id,))
    row = cur.fetchone()
    if not row or row[0] < COST_PER_TICKET:
        await callback.message.answer("❌ Недостаточно средств")
        await callback.answer()
        return

    cur.execute(
        "UPDATE users SET balance = balance - ?, tickets = tickets + 1 WHERE user_id = ?",
        (COST_PER_TICKET, callback.from_user.id)
    )
    conn.commit()

    await callback.message.answer("🎟 Билет куплен!")
    await callback.answer()

@dp.callback_query(lambda c: c.data == "balance")
async def balance(callback: types.CallbackQuery):
    cur.execute("SELECT balance, tickets FROM users WHERE user_id = ?", (callback.from_user.id,))
    bal, tik = cur.fetchone() or (0, 0)
    await callback.message.answer(f"💰 {bal} AUR\n🎟 {tik}")
    await callback.answer()

@dp.callback_query(lambda c: c.data == "ref")
async def ref(callback: types.CallbackQuery):
    me = await bot.get_me()
    await callback.message.answer(
        f"https://t.me/{me.username}?start={callback.from_user.id}"
    )
    await callback.answer()

# ──────────────────── АДМИН ФУНКЦИИ ────────────────────

@dp.callback_query(lambda c: c.data == "admin_start")
async def admin_start(callback: types.CallbackQuery):
    global announce_chat_id, announce_message_id, timer_task

    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return

    cur.execute("SELECT prize FROM contest WHERE id = 1")
    row = cur.fetchone()
    prize = row[0] if row and row[0] else None

    if not prize:
        await callback.message.answer("Сначала установите приз")
        await callback.answer()
        return

    if announce_chat_id is None:
        await callback.message.answer("Сначала используйте /addchat в нужном чате")
        await callback.answer()
        return

    end_time = (datetime.utcnow() + timedelta(minutes=DEFAULT_CONTEST_MINUTES)).isoformat()

    cur.execute(
        "UPDATE contest SET is_active = 1, end_time = ? WHERE id = 1",
        (end_time,)
    )
    conn.commit()

    # Отправляем начальное сообщение с таймером в чат
    initial_text = f"🎉 Конкурс запущен!\n🏆 Приз: {prize}\n⏳ Осталось: {DEFAULT_CONTEST_MINUTES:02d}:00"

    sent_msg = await bot.send_message(
        announce_chat_id,
        initial_text,
        reply_markup=await contest_kb()
    )
    announce_message_id = sent_msg.message_id

    # Запускаем таймер
    if timer_task:
        timer_task.cancel()
    timer_task = asyncio.create_task(update_countdown_timer())

    await callback.message.answer(
        f"✅ Конкурс запущен!\n"
        f"Приз: {prize}\n"
        f"Длительность: {DEFAULT_CONTEST_MINUTES} минут"
    )
    await callback.answer("Запущен")

@dp.callback_query(lambda c: c.data == "admin_stop")
async def admin_stop(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return

    cur.execute(
        "UPDATE contest SET is_active = 0, end_time = NULL WHERE id = 1"
    )
    conn.commit()

    if timer_task:
        timer_task.cancel()

    if announce_chat_id and announce_message_id:
        await bot.edit_message_text(
            "⏹ Конкурс остановлен",
            chat_id=announce_chat_id,
            message_id=announce_message_id
        )

    await callback.message.answer("⏹ Конкурс остановлен")
    await callback.answer("Остановлен")

@dp.callback_query(lambda c: c.data == "set_prize")
async def admin_set_prize(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return

    await callback.message.answer("Введите текст приза:")
    await state.set_state(SetPrizeState.waiting_prize)
    await callback.answer()

@dp.message(SetPrizeState.waiting_prize)
async def process_prize(message: types.Message, state: FSMContext):
    prize = message.text
    cur.execute(
        "UPDATE contest SET prize = ? WHERE id = 1",
        (prize,)
    )
    conn.commit()

    await message.answer(f"🏆 Приз установлен: {prize}")
    await state.clear()

@dp.callback_query(lambda c: c.data == "admin_view_balances")
async def admin_view_balances(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return

    cur.execute("SELECT user_id, username, balance, tickets FROM users")
    rows = cur.fetchall()
    if not rows:
        await callback.message.answer("Нет игроков")
        await callback.answer()
        return

    text = "Балансы игроков:\n"
    for row in rows:
        text += f"@{row[1] or row[0]}: {row[2]} AUR, {row[3]} билетов\n"

    await callback.message.answer(text)
    await callback.answer()

# ──────────────────── Команда /addchat ────────────────────

@dp.message(Command("addchat"))
async def cmd_addchat(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.reply("Только для администратора.")
        return

    chat = message.chat
    if chat.type not in ("group", "supergroup", "channel"):
        await message.reply("Используйте эту команду в группе или канале.")
        return

    # Проверка прав бота
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat.id, me.id)
        if member.status not in ("administrator", "creator"):
            await message.reply("Бот должен быть администратором в этом чате для редактирования сообщений.")
            return
    except:
        await message.reply("Не могу проверить права бота в этом чате.")
        return

    global announce_chat_id
    announce_chat_id = chat.id

    kb = await contest_kb()

    await message.reply(
        "✅ Этот чат теперь используется для конкурсов. Здесь будет отображаться таймер и объявления.",
        reply_markup=kb
    )

    await bot.send_message(
        ADMIN_ID,
        f"Чат для конкурсов установлен: {chat.title or chat.username or chat.id}"
    )

# ──────────────────── Таймер ────────────────────

async def update_countdown_timer():
    global announce_chat_id, announce_message_id

    while True:
        await asyncio.sleep(10)

        cur.execute("SELECT is_active, end_time, prize FROM contest WHERE id = 1")
        row = cur.fetchone()
        if not row or row[0] == 0 or not row[1]:
            break

        end_time = datetime.fromisoformat(row[1])
        remaining = end_time - datetime.utcnow()

        if remaining.total_seconds() <= 0:
            await perform_draw()
            break

        m, s = divmod(int(remaining.total_seconds()), 60)
        text = f"🎉 Активный конкурс!\n🏆 Приз: {row[2]}\n⏳ Осталось: {m:02d}:{s:02d}"

        try:
            await bot.edit_message_text(
                text,
                chat_id=announce_chat_id,
                message_id=announce_message_id,
                reply_markup=await contest_kb()
            )
        except Exception as e:
            print(f"Ошибка обновления таймера: {e}")

# ──────────────────── Автоматический розыгрыш ────────────────────

async def perform_draw():
    cur.execute("SELECT user_id, tickets FROM users WHERE tickets > 0")
    participants = cur.fetchall()

    if not participants:
        await bot.edit_message_text(
            "Конкурс завершён. Нет участников.",
            chat_id=announce_chat_id,
            message_id=announce_message_id
        )
        cur.execute("UPDATE contest SET is_active = 0, end_time = NULL WHERE id = 1")
        conn.commit()
        return

    ticket_pool = []
    for uid, tickets in participants:
        ticket_pool.extend([uid] * tickets)

    winner_id = random.choice(ticket_pool)

    cur.execute("SELECT username FROM users WHERE user_id = ?", (winner_id,))
    winner_username = cur.fetchone()[0]

    winner_text = f"@{winner_username}" if winner_username else f"ID {winner_id}"

    await bot.edit_message_text(
        f"⏰ Конкурс завершён!\n🎉 Победитель: {winner_text}",
        chat_id=announce_chat_id,
        message_id=announce_message_id
    )

    await bot.send_message(winner_id, "🎉 Поздравляем! Вы выиграли конкурс. Свяжитесь с админом за призом.")

    # Сброс
    cur.execute("UPDATE contest SET is_active = 0, end_time = NULL WHERE id = 1")
    cur.execute("UPDATE users SET tickets = 0")
    conn.commit()

# ──────────────────── FAKE WEB SERVER ────────────────────

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
        fake_web_server(),
        dp.start_polling(bot),
    )

if __name__ == "__main__":
    asyncio.run(main())