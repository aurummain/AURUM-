import asyncio
import os
import sqlite3
import random
from datetime import datetime, timedelta

import aiohttp
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
TIMER_UPDATE_INTERVAL = 15

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
# ──────────────────── FSM ────────────────────

class TopUpState(StatesGroup):
    waiting_amount = State()

class SetPrizeState(StatesGroup):
    waiting_prize = State()

class BuyTicketsState(StatesGroup):
    waiting_quantity = State()

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
        [InlineKeyboardButton(text="🎟 Купить билеты", callback_data="buy")],
        [InlineKeyboardButton(text="📊 Баланс", callback_data="balance")],
        [InlineKeyboardButton(text="🤝 Реф. ссылка", callback_data="ref")],
        [InlineKeyboardButton(text="📈 Статистика шансов", callback_data="stats")],
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
            await message.answer("Добро пожаловать!", reply_markup=user_kb())
    else:
        if is_active and end_time:
            try:
                remaining = datetime.fromisoformat(end_time) - datetime.utcnow()
                if remaining.total_seconds() > 0:
                    m, s = divmod(int(remaining.total_seconds()), 60)
                    cur.execute("SELECT SUM(tickets) FROM users")
                    total = cur.fetchone()[0] or 0
                    text = f"🎉 Активный конкурс!\nПриз: {prize}\nОсталось: {m:02d}:{s:02d}\nБилетов всего: {total}"
                    await message.answer(text, reply_markup=await contest_kb())
                    return
            except Exception as e:
                print(f"Ошибка в групповом /start: {e}")
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

    try:
        _, uid_str, amt_str = callback.data.split("_")
        uid, amt = int(uid_str), int(amt_str)
    except:
        await callback.answer("Ошибка данных", show_alert=True)
        return

    cur.execute(
        "INSERT INTO users (user_id, balance) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET balance = balance + ?",
        (uid, amt, amt)
    )
    conn.commit()

    await bot.send_message(uid, f"✅ Баланс пополнен на {amt} AUR")
    await callback.message.edit_text(callback.message.text + "\n\n✅ Подтверждено")
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("reject_"))
async def reject_topup(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Запрещено", show_alert=True)
        return

    try:
        _, uid_str, amt_str = callback.data.split("_")
        uid = int(uid_str)
        amt = int(amt_str)
    except:
        await callback.answer("Ошибка", show_alert=True)
        return

    await bot.send_message(uid, f"❌ Пополнение на {amt} AUR отклонено")
    await callback.message.edit_text(callback.message.text + "\n\n❌ Отклонено")
    await callback.answer()

@dp.callback_query(lambda c: c.data == "buy")
async def start_buy_tickets(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != "private":
        await callback.answer("Только в ЛС", show_alert=True)
        return
    uid = callback.from_user.id
    cur.execute("SELECT balance FROM users WHERE user_id = ?", (uid,))
    row = cur.fetchone()
    if not row or row[0] < COST_PER_TICKET:
        await callback.answer("Недостаточно средств для покупки хотя бы одного билета", show_alert=True)
        return
    await callback.message.answer("Введите количество билетов для покупки:")
    await state.set_state(BuyTicketsState.waiting_quantity)
    await callback.answer()

@dp.message(BuyTicketsState.waiting_quantity)
async def process_buy_tickets(message: types.Message, state: FSMContext):
    if not message.text.isdigit() or int(message.text) <= 0:
        await message.answer("Введите положительное целое число")
        return

    quantity = int(message.text)
    cost = quantity * COST_PER_TICKET
    uid = message.from_user.id
    cur.execute("SELECT balance FROM users WHERE user_id = ?", (uid,))
    row = cur.fetchone()
    if not row or row[0] < cost:
        await message.answer(f"Недостаточно средств. Требуется {cost} AUR, доступно {row[0]} AUR")
        await state.clear()
        return

    cur.execute(
        "UPDATE users SET balance = balance - ?, tickets = tickets + ? WHERE user_id = ?",
        (cost, quantity, uid)
    )
    conn.commit()

    cur.execute("SELECT SUM(tickets) FROM users")
    total = cur.fetchone()[0] or 0

    if announce_chat_id:
        try:
            await bot.send_message(
                announce_chat_id,
                f"✨ Участник купил {quantity} билет(ов) • Всего билетов в розыгрыше: {total}"
            )
        except Exception as e:
            print(f"Ошибка отправки в чат: {e}")

    await message.answer(f"🎟 Куплено {quantity} билет(ов)!")
    await state.clear()

@dp.callback_query(lambda c: c.data == "balance")
async def balance(callback: types.CallbackQuery):
    cur.execute("SELECT balance, tickets FROM users WHERE user_id = ?", (callback.from_user.id,))
    bal, tik = cur.fetchone() or (0, 0)
    cur.execute("SELECT SUM(tickets) FROM users")
    total_tickets = cur.fetchone()[0] or 0
    if total_tickets > 0:
        win_prob = (tik / total_tickets) * 100
        await callback.message.answer(f"💰 {bal} AUR\n🎟 {tik}\nШанс на победу: {win_prob:.2f}%")
    else:
        await callback.message.answer(f"💰 {bal} AUR\n🎟 {tik}\nШанс на победу: 0% (нет билетов в розыгрыше)")
    await callback.answer()

@dp.callback_query(lambda c: c.data == "ref")
async def ref(callback: types.CallbackQuery):
    me = await bot.get_me()
    await callback.message.answer(
        f"https://t.me/{me.username}?start={callback.from_user.id}"
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data == "stats")
async def stats(callback: types.CallbackQuery):
    cur.execute("SELECT is_active FROM contest WHERE id = 1")
    is_active = cur.fetchone()[0]
    if not is_active:
        await callback.answer("Нет активного конкурса", show_alert=True)
        return

    cur.execute("SELECT user_id, username, tickets FROM users WHERE tickets > 0 ORDER BY tickets DESC")
    rows = cur.fetchall()
    cur.execute("SELECT SUM(tickets) FROM users")
    total_tickets = cur.fetchone()[0] or 0

    if total_tickets == 0:
        await callback.message.answer("Нет купленных билетов")
        await callback.answer()
        return

    text = "📈 Статистика шансов на победу:\n"
    for uid, username, tickets in rows:
        prob = (tickets / total_tickets) * 100
        text += f"@{username or uid}: {tickets} билетов ({prob:.2f}%)\n"

    text += f"\nВсего билетов: {total_tickets}"

    await callback.message.answer(text)
    await callback.answer()

@dp.message(Command("send"))
async def cmd_send(message: types.Message):
    sender_id = message.from_user.id
    cur.execute("SELECT tickets FROM users WHERE user_id = ?", (sender_id,))
    sender_row = cur.fetchone()
    if not sender_row or sender_row[0] == 0:
        await message.reply("У вас нет билетов для отправки.")
        return

    if message.reply_to_message:
        # Отправка в ответ на сообщение
        recipient_id = message.reply_to_message.from_user.id
        if recipient_id == sender_id:
            await message.reply("Нельзя отправить билеты себе.")
            return
        args = message.text.split()[1:]
        if len(args) != 1 or not args[0].isdigit():
            await message.reply("Формат: /send <количество> (в ответ на сообщение)")
            return
        quantity = int(args[0])
    else:
        # Отправка по username
        args = message.text.split()[1:]
        if len(args) != 2 or not args[0].startswith('@') or not args[1].isdigit():
            await message.reply("Формат: /send @username <количество>")
            return
        username = args[0][1:]  # убрать @
        quantity = int(args[1])
        cur.execute("SELECT user_id FROM users WHERE username = ?", (username,))
        recipient_row = cur.fetchone()
        if not recipient_row:
            await message.reply("Пользователь не найден.")
            return
        recipient_id = recipient_row[0]
        if recipient_id == sender_id:
            await message.reply("Нельзя отправить билеты себе.")
            return

    if quantity <= 0:
        await message.reply("Количество должно быть положительным.")
        return

    cur.execute("SELECT tickets FROM users WHERE user_id = ?", (sender_id,))
    sender_tickets = cur.fetchone()[0]
    if quantity > sender_tickets:
        await message.reply(f"У вас только {sender_tickets} билетов.")
        return

    # Обеспечить существование получателя в БД
    cur.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (recipient_id,))

    # Транзакция
    cur.execute("UPDATE users SET tickets = tickets - ? WHERE user_id = ?", (quantity, sender_id))
    cur.execute("UPDATE users SET tickets = tickets + ? WHERE user_id = ?", (quantity, recipient_id))
    conn.commit()

    # Уведомления
    sender_username = message.from_user.username or f"ID{sender_id}"
    recipient_username = (await bot.get_chat(recipient_id)).username or f"ID{recipient_id}"

    await message.reply(f"✅ Отправлено {quantity} билет(ов) пользователю @{recipient_username}")
    try:
        await bot.send_message(recipient_id, f"🎟 Получено {quantity} билет(ов) от @{sender_username}")
    except:
        pass  # Если нельзя отправить ЛС, игнор

    # Обновить общее количество если нужно
    if announce_chat_id:
        cur.execute("SELECT SUM(tickets) FROM users")
        total = cur.fetchone()[0] or 0
        try:
            await bot.send_message(
                announce_chat_id,
                f"🔄 Передача: {quantity} билет(ов) от @{sender_username} к @{recipient_username} • Всего: {total}"
            )
        except Exception as e:
            print(f"Ошибка отправки в чат: {e}")

# ──────────────────── АДМИН ФУНКЦИИ ────────────────────

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
    row = cur.fetchone()  # ← один вызов!
    prize = row[0] if row else "Приз не установлен"

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
    await callback.answer("Запущен")

@dp.callback_query(lambda c: c.data == "admin_stop")
async def admin_stop(callback: types.CallbackQuery):
    global timer_task
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return

    cur.execute("UPDATE contest SET is_active = 0, end_time = NULL WHERE id = 1")
    conn.commit()

    if timer_task:
        timer_task.cancel()

    if announce_chat_id and announce_message_id:
        await bot.edit_message_text(
            "⏹ Конкурс остановлен",
            chat_id=announce_chat_id,
            message_id=announce_message_id
        )

    await callback.message.answer("Конкурс остановлен")
    await callback.answer()

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
    prize = message.text.strip()
    cur.execute("UPDATE contest SET prize = ? WHERE id = 1", (prize,))
    conn.commit()
    await message.answer(f"Приз установлен: {prize}")
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
    else:
        text = "Балансы:\n" + "\n".join([f"@{r[1] or r[0]}: {r[2]} AUR, {r[3]} билетов" for r in rows])
        await callback.message.answer(text)
    await callback.answer()

# ──────────────────── ТАЙМЕР + РОЗЫГРЫШ ────────────────────

async def update_timer():
    global announce_chat_id, announce_message_id

    while True:
        await asyncio.sleep(TIMER_UPDATE_INTERVAL)

        cur.execute("SELECT is_active, end_time, prize FROM contest WHERE id = 1")
        row = cur.fetchone()
        if not row or row[0] == 0 or not row[1]:
            print("Таймер остановлен: конкурс не активен")
            break

        end_time = datetime.fromisoformat(row[1])
        remaining = end_time - datetime.utcnow()

        cur.execute("SELECT SUM(tickets) FROM users")
        total_tickets = cur.fetchone()[0] or 0

        if remaining.total_seconds() <= 0:
            print("Таймер завершён → запуск розыгрыша")
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
            print(f"Таймер обновлён: {m:02d}:{s:02d} | билетов: {total_tickets}")
        except Exception as e:
            print(f"Ошибка редактирования таймера: {e}")

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
        winner_username = cur.fetchone()[0] or f"ID{winner_id}"

        text = f"🎉 Конкурс завершён!\nПобедитель: @{winner_username}\nПоздравляем!"

        await bot.send_message(winner_id, "🎉 Вы выиграли! Напишите админу за призом.")
        await bot.send_message(ADMIN_ID, f"Победитель: @{winner_username} (ID {winner_id})")

    await bot.edit_message_text(
        text,
        chat_id=announce_chat_id,
        message_id=announce_message_id
    )

    cur.execute("UPDATE contest SET is_active = 0, end_time = NULL WHERE id = 1")
    cur.execute("UPDATE users SET tickets = 0")
    conn.commit()
    print("Розыгрыш завершён, билеты сброшены")

# ──────────────────── KEEP-ALIVE (self-ping) ────────────────────

async def keep_alive():
    my_host = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
    if not my_host:
        print("Нет RENDER_EXTERNAL_HOSTNAME → keep-alive отключён")
        return

    url = f"https://{my_host}/health"
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(url, timeout=10) as resp:
                    print(f"Keep-alive ping → {resp.status}")
            except Exception as e:
                print(f"Keep-alive ошибка: {e}")
            await asyncio.sleep(240)  # каждые 4 минуты

# ──────────────────── FAKE WEB SERVER ────────────────────

async def fake_web_server():
    async def handle(request):
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_get("/", handle)
    app.router.add_get("/health", handle)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Сервер запущен на порту {port}")

# ──────────────────── ЗАПУСК ────────────────────

async def main():
    print("Бот запущен")
    await asyncio.gather(
        fake_web_server(),
        keep_alive(),
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