import asyncio
import sqlite3
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

BOT_TOKEN = "8323563478:AAE9qcdBfdvO1ptKkCXS78hJ4SuxeFOnV2w"  # ← твой последний токен
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
        [InlineKeyboardButton(text="💳 Пополнить",     callback_data="topup")],
        [InlineKeyboardButton(text="🎟 Купить билет",  callback_data="buy")],
        [InlineKeyboardButton(text="📊 Баланс",        callback_data="balance")],
        [InlineKeyboardButton(text="🤝 Реф. ссылка",   callback_data="ref")],
    ])


def admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Запустить конкурс", callback_data="admin_start")],
        [InlineKeyboardButton(text="⏹ Остановить конкурс", callback_data="admin_stop")],
        [InlineKeyboardButton(text="🏆 Установить приз",    callback_data="set_prize")],
        [InlineKeyboardButton(text="👥 Балансы игроков",    callback_data="admin_view_balances")],
    ])


async def contest_kb():
    me = await bot.get_me()
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Перейти в личный кабинет", url=f"https://t.me/{me.username}")],
    ])


def confirm_topup_kb(user_id: int, amount: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ Подтвердить {amount}", callback_data=f"confirm_{user_id}_{amount}")],
        [InlineKeyboardButton(text="❌ Отклонить",             callback_data=f"reject_{user_id}")]
    ])


# ────────────────────────────────────────────────
#                   START и статус конкурса в группах
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
        # В группах — только статус конкурса
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
#                   /addchat — добавить группу в рассылку
# ────────────────────────────────────────────────

@dp.message(Command("addchat"))
async def cmd_addchat(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return await msg.answer("Только админ может добавлять чаты")

    if msg.chat.type in ("group", "supergroup"):
        chat_id = msg.chat.id
        title = msg.chat.title or "Без названия"
        cur.execute(
            "INSERT OR IGNORE INTO allowed_chats (chat_id, title) VALUES (?, ?)",
            (chat_id, title)
        )
        conn.commit()
        await msg.answer(f"Группа {title} добавлена в рассылку конкурсов!")
    else:
        await msg.answer("Команда /addchat работает только в группах")


# ────────────────────────────────────────────────
#                   ТАЙМЕР + АВТОЗАВЕРШЕНИЕ КОНКУРСА
# ────────────────────────────────────────────────

async def contest_timer_task():
    while True:
        cur.execute("SELECT is_active, end_time, prize FROM contest WHERE id = 1")
        row = cur.fetchone()
        if not row or not row[0]:
            await asyncio.sleep(30)
            continue

        is_active, end_time, prize = row
        end = datetime.fromisoformat(end_time)
        remaining = end - datetime.utcnow()

        if remaining.total_seconds() <= 0:
            # Конкурс закончился
            cur.execute("UPDATE contest SET is_active = 0 WHERE id = 1")
            conn.commit()

            # Выбор победителя
            cur.execute("SELECT user_id, tickets FROM users WHERE tickets > 0 ORDER BY RANDOM() LIMIT 1")
            winner = cur.fetchone()

            text = "Конкурс завершён. Никто не участвовал 😔"
            winner_id = None
            if winner:
                uid, tik = winner
                text = f"🏆 Победитель: ID {uid} (билетов: {tik})\nПриз: {prize or 'не указан'}"
                winner_id = uid

            cur.execute("UPDATE users SET tickets = 0")
            conn.commit()

            # Рассылка в группы
            cur.execute("SELECT chat_id FROM allowed_chats")
            for row in cur.fetchall():
                chat_id = row[0]
                try:
                    await bot.send_message(chat_id, text)
                except Exception as e:
                    print(f"Ошибка отправки в чат {chat_id}: {e}")

            if winner_id:
                await bot.send_message(winner_id, f"Поздравляем! Вы выиграли: {prize}")
                cur.execute("SELECT username FROM users WHERE user_id = ?", (winner_id,))
                username = cur.fetchone()[0] or "нет"
                await bot.send_message(ADMIN_ID, f"Победитель: @{username} (ID {winner_id}) — отправьте приз")

            await asyncio.sleep(30)
            continue

        # Обновление таймера в группах каждые 10 секунд
        minutes, seconds = divmod(int(remaining.total_seconds()), 60)
        timer = f"{minutes:02d}:{seconds:02d}"
        text = f"Активный конкурс!\nПриз: {prize or 'не указан'}\nОсталось: {timer}"

        cur.execute("SELECT chat_id FROM allowed_chats")
        for row in cur.fetchall():
            chat_id = row[0]
            try:
                # Пытаемся редактировать последнее сообщение, если есть
                # Или просто отправляем новое (для простоты отправляем новое каждые 10 сек)
                await bot.send_message(chat_id, text, reply_markup=await contest_kb())
            except Exception as e:
                print(f"Ошибка обновления в чат {chat_id}: {e}")

        await asyncio.sleep(10)  # обновление каждые 10 секунд


# ────────────────────────────────────────────────
#                   ЗАПУСК КОНКУРСА (только в ЛС админа)
# ────────────────────────────────────────────────

@dp.callback_query(lambda c: c.data == "admin_start")
async def admin_start(c: types.CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return await c.answer("Доступ запрещён", show_alert=True)

    if c.message.chat.type != "private":
        return await c.answer("Запуск конкурса только в личных сообщениях!", show_alert=True)

    end = datetime.utcnow() + timedelta(minutes=CONTEST_DURATION_MINUTES)
    end_iso = end.isoformat()

    cur.execute("UPDATE contest SET is_active = 1, end_time = ? WHERE id = 1", (end_iso,))
    conn.commit()

    cur.execute("SELECT prize FROM contest WHERE id = 1")
    prize = cur.fetchone()[0] or "не указан"

    await c.answer("Конкурс запущен! Рассылка в группы выполнена.")

    # Запуск таймера в фоне
    asyncio.create_task(contest_timer_task())


# Остальные функции (admin_stop, set_prize, buy, balance, ref и т.д.) остаются без изменений

async def main():
    print("Бот запущен. Админ ID:", ADMIN_ID)
    # Запускаем таймер проверки в фоне
    asyncio.create_task(contest_timer_task())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())