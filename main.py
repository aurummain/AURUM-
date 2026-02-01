import asyncio
import sqlite3
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# ────────────────────────────────────────────────
#                   НАСТРОЙКИ
# ────────────────────────────────────────────────

BOT_TOKEN = "8570594380:AAGDgUcpyZYOSc7xNkcK8KIYrxZGEf0T4Lw"
ADMIN_ID = 1333099097
TON_WALLET = "UQBJNtgVfE-x7-K1uY_EhW1rdvGKhq5gM244fX89VF0bof7R"

COST_PER_TICKET = 10000
CONTEST_DURATION_MINUTES = 10

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ────────────────────────────────────────────────
#                   СОСТОЯНИЯ
# ────────────────────────────────────────────────

class TopUpState(StatesGroup):
    waiting_amount = State()

class SetPrizeState(StatesGroup):
    waiting_prize = State()

# ────────────────────────────────────────────────
#                   БАЗА ДАННЫХ
# ────────────────────────────────────────────────

conn = sqlite3.connect("lottery.db", check_same_thread=False)
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id           INTEGER PRIMARY KEY,
    username          TEXT,
    balance           INTEGER DEFAULT 0,
    tickets           INTEGER DEFAULT 0,
    referrer_id       INTEGER,
    rewarded_referrer INTEGER DEFAULT 0
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS contest (
    id        INTEGER PRIMARY KEY,
    prize     TEXT,
    is_active INTEGER DEFAULT 0,
    end_time  TEXT
)
""")
cur.execute("INSERT OR IGNORE INTO contest (id) VALUES (1)")

cur.execute("""
CREATE TABLE IF NOT EXISTS allowed_chats (
    chat_id   INTEGER PRIMARY KEY,
    title     TEXT,
    added_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    added_by  INTEGER
)
""")
conn.commit()

# ────────────────────────────────────────────────
#                   КЛАВИАТУРЫ
# ────────────────────────────────────────────────

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
    me = await bot.get_me()  # ← добавлен await
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Перейти в личный кабинет", url=f"https://t.me/{me.username}")],
    ])


def confirm_topup_kb(user_id: int, amount: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ Подтвердить {amount}", callback_data=f"confirm_{user_id}_{amount}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{user_id}")]
    ])


# ────────────────────────────────────────────────
#                   START
# ────────────────────────────────────────────────

@dp.message(Command("start"))
async def cmd_start(msg: types.Message):
    args = msg.text.split()
    referrer_id = None
    if len(args) > 1:
        try:
            referrer_id = int(args[1])
        except:
            pass

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
            await msg.answer("Добро пожаловать в личный кабинет!", reply_markup=user_kb())
    else:
        if is_active:
            end = datetime.fromisoformat(end_time)
            remaining = end - datetime.utcnow()
            if remaining.total_seconds() <= 0:
                is_active = 0
            else:
                minutes, seconds = divmod(int(remaining.total_seconds()), 60)
                timer = f"{minutes:02d}:{seconds:02d}"
                text = f"Активный конкурс!\nПриз: {prize or 'не указан'}\nОсталось: {timer}"
                await msg.answer(text, reply_markup=await contest_kb())
                return
        await msg.answer("Нет активного конкурса.")


# ────────────────────────────────────────────────
#                   /ADMIN
# ────────────────────────────────────────────────

@dp.message(Command("admin"))
async def cmd_admin(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return await msg.answer("Доступ запрещён")

    if msg.chat.type != "private":
        return await msg.answer("Команда /admin работает только в личных сообщениях.")

    await msg.answer("👑 Админ-панель", reply_markup=admin_kb())


# ────────────────────────────────────────────────
#                   ПОПОЛНЕНИЕ
# ────────────────────────────────────────────────

@dp.callback_query(lambda c: c.data == "topup")
async def cb_topup(c: types.CallbackQuery, state: FSMContext):
    await c.message.answer("Введите сумму пополнения в AUR:")
    await state.set_state(TopUpState.waiting_amount)
    await c.answer()


@dp.message(TopUpState.waiting_amount)
async def process_topup_amount(msg: types.Message, state: FSMContext):
    if not msg.text.isdigit():
        return await msg.answer("Введите число")

    amount = int(msg.text)
    if amount < 1:
        return await msg.answer("Сумма должна быть больше 0")

    user = msg.from_user
    memo = f"{user.id}_{user.username or 'no_username'}"

    text = (
        f"💳 Пополните <b>{amount}</b> AUR\n"
        f"Кошелёк: <code>{TON_WALLET}</code>\n"
        f"Memo: <code>{memo}</code>\n\n"
        "После перевода нажмите кнопку ниже ↓"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Я оплатил", callback_data=f"paid_{amount}")]
    ])

    await msg.answer(text, parse_mode="HTML", reply_markup=kb)

    try:
        await bot.send_message(
            ADMIN_ID,
            f"🟢 Запрос пополнения\nОт: @{user.username or user.id} ({user.id})\nСумма: {amount} AUR",
            reply_markup=confirm_topup_kb(user.id, amount)
        )
    except Exception as e:
        print(f"Ошибка отправки админу: {e}")

    await state.clear()


@dp.callback_query(lambda c: c.data.startswith("paid_"))
async def cb_paid(c: types.CallbackQuery):
    amount = int(c.data.split("_")[1])

    await c.message.delete()
    await c.message.answer(f"💡 Сообщили об оплате {amount} AUR.\nОжидайте подтверждения.")
    await c.answer()


@dp.callback_query(lambda c: c.data.startswith("confirm_"))
async def cb_confirm(c: types.CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return await c.answer("Доступ запрещён", show_alert=True)

    _, uid_str, amt_str = c.data.split("_")
    user_id = int(uid_str)
    amount = int(amt_str)

    cur.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()

    await bot.send_message(user_id, f"✅ Пополнено {amount} AUR")
    await c.message.edit_text(f"Подтверждено: {amount} AUR → {user_id}")
    await c.answer()


@dp.callback_query(lambda c: c.data.startswith("reject_"))
async def cb_reject(c: types.CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return await c.answer("Доступ запрещён", show_alert=True)

    user_id = int(c.data.split("_")[1])
    await bot.send_message(user_id, "❌ Платёж отклонён.")
    await c.message.edit_text("Отклонено")
    await c.answer()


# ────────────────────────────────────────────────
#                   ПОКУПКА БИЛЕТА
# ────────────────────────────────────────────────

@dp.callback_query(lambda c: c.data == "buy")
async def cb_buy(c: types.CallbackQuery):
    user = c.from_user

    if c.message.chat.type != "private":
        await c.answer("Покупка билетов только в ЛС бота")
        return

    cur.execute("SELECT balance FROM users WHERE user_id = ?", (user.id,))
    row = cur.fetchone()
    if not row or row[0] < COST_PER_TICKET:
        await c.message.answer(f"❌ Недостаточно средств (нужно {COST_PER_TICKET} AUR)")
        return await c.answer()

    cur.execute(
        "UPDATE users SET balance = balance - ?, tickets = tickets + 1 WHERE user_id = ?",
        (COST_PER_TICKET, user.id)
    )
    conn.commit()

    cur.execute("SELECT balance, tickets FROM users WHERE user_id = ?", (user.id,))
    bal, tik = cur.fetchone()

    username = f"@{user.username}" if user.username else f"ID{user.id}"

    await c.message.answer(
        f"✅ Куплен 1 билет!\n\nБаланс: {bal} AUR\nБилетов: {tik}"
    )

    # Анонимное уведомление в группы
    text = "🎟 Кто-то купил 1 билет!"
    cur.execute("SELECT chat_id FROM allowed_chats")
    for row in cur.fetchall():
        chat_id = row[0]
        try:
            await bot.send_message(chat_id, text)
        except Exception as e:
            print(f"Ошибка отправки в чат {chat_id}: {e}")

    await c.answer()


# ────────────────────────────────────────────────
#                   БАЛАНС
# ────────────────────────────────────────────────

@dp.callback_query(lambda c: c.data == "balance")
async def cb_balance(c: types.CallbackQuery):
    cur.execute("SELECT balance, tickets FROM users WHERE user_id = ?", (c.from_user.id,))
    bal, tik = cur.fetchone() or (0, 0)
    await c.message.answer(f"💰 Баланс: {bal} AUR\n🎟 Билетов: {tik}")
    await c.answer()


# ────────────────────────────────────────────────
#                   РЕФЕРАЛЬНАЯ ССЫЛКА
# ────────────────────────────────────────────────

@dp.callback_query(lambda c: c.data == "ref")
async def cb_ref(c: types.CallbackQuery):
    user = c.from_user
    me = await bot.get_me()  # ← await
    link = f"https://t.me/{me.username}?start={user.id}"
    await c.message.answer(f"🤝 Ваша реферальная ссылка:\n{link}")
    await c.answer()


# ────────────────────────────────────────────────
#                   АДМИН — ЗАПУСК КОНКУРСА
# ────────────────────────────────────────────────

@dp.callback_query(lambda c: c.data == "admin_start")
async def admin_start(c: types.CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return await c.answer("Доступ запрещён", show_alert=True)

    end = datetime.utcnow() + timedelta(minutes=CONTEST_DURATION_MINUTES)
    end_iso = end.isoformat()

    cur.execute("UPDATE contest SET is_active = 1, end_time = ? WHERE id = 1", (end_iso,))
    conn.commit()

    cur.execute("SELECT prize FROM contest WHERE id = 1")
    prize = cur.fetchone()[0] or "не указан"

    text = (
        f"🎉 <b>Конкурс запущен!</b>\n"
        f"🏆 Приз: {prize}\n"
        f"⏳ Завершится через {CONTEST_DURATION_MINUTES} минут"
    )

    cur.execute("SELECT chat_id FROM allowed_chats")
    for row in cur.fetchall():
        chat_id = row[0]
        try:
            await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=await contest_kb())
        except Exception as e:
            print(f"Ошибка отправки в чат {chat_id}: {e}")
            await bot.send_message(ADMIN_ID, f"Ошибка отправки в чат {chat_id}: {e}")

    await c.answer("Конкурс запущен")


# Остальные функции (admin_stop, set_prize, admin_view_balances) остаются без изменений, как в твоём коде

async def main():
    print("Бот запущен. Админ ID:", ADMIN_ID)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())