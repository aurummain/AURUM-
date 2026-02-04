import asyncio
import os
import sqlite3
import random
import json
import re  # Для парсинга строк списка
from datetime import datetime, timedelta, timezone

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# ──────────────────── НАСТРОЙКИ ────────────────────

BOT_TOKEN = "8274963448:AAE06C6g-0A7aWoPMI51zos3IIsevhxDwSE"
ADMIN_ID = 1333099097
TON_WALLET = "UQBJNtgVfE-x7-K1uY_EhW1rdvGKhq5gM244fX89VF0bof7R"

DEFAULT_COST_PER_TICKET_AUR = 10000
DEFAULT_COST_PER_TICKET_TON = 1
DEFAULT_CONTEST_MINUTES = 10
TIMER_UPDATE_INTERVAL = 15
RATE_LIMIT_WINDOW = 60  # Окно в секундах (1 минута)
RATE_LIMIT_COUNT = 5  # Макс команд за окно
BAN_DURATION_MINUTES = 5  # Длительность блока

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Глобальные словари для rate limit и банов
rate_limit_dict = {}  # user_id: {command: [timestamps]}
ban_dict = {}  # user_id: unban_time

# ──────────────────── FSM ────────────────────

class TopUpState(StatesGroup):
    waiting_currency = State()
    waiting_amount = State()

class SetPrizesState(StatesGroup):
    waiting_prizes = State()

class BuyTicketsState(StatesGroup):
    waiting_currency = State()
    waiting_quantity = State()

class SetDurationState(StatesGroup):
    waiting_duration = State()

class SetCostAurState(StatesGroup):
    waiting_cost = State()

class SetCostTonState(StatesGroup):
    waiting_cost = State()

class RestoreListState(StatesGroup):
    waiting_list = State()  # Новый state для ожидания списка

# ──────────────────── БАЗА ДАННЫХ ────────────────────

conn = sqlite3.connect("lottery.db", check_same_thread=False)
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_telegram_id INTEGER UNIQUE,
    username TEXT,
    aur_balance INTEGER DEFAULT 0,
    ton_balance REAL DEFAULT 0.0,
    tickets INTEGER DEFAULT 0,
    referrer_id INTEGER,
    rewarded_referrer INTEGER DEFAULT 0
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS contest (
    id INTEGER PRIMARY KEY,
    prizes TEXT DEFAULT '[]',  -- JSON список призов
    is_active INTEGER DEFAULT 0,
    end_time TEXT,
    duration_minutes INTEGER DEFAULT 10,
    cost_per_ticket_aur INTEGER DEFAULT 10000,
    cost_per_ticket_ton REAL DEFAULT 1.0,
    prize_message_ids TEXT DEFAULT '[]'  -- JSON список message_id для призов
)
""")
cur.execute("INSERT OR IGNORE INTO contest (id, is_active, duration_minutes, cost_per_ticket_aur, cost_per_ticket_ton) VALUES (1, 0, 10, 10000, 1.0)")

conn.commit()

# ──────────────────── Глобальные переменные ────────────────────

announce_chat_id: int | None = None
announce_message_id: int | None = None
timer_task: asyncio.Task | None = None
five_min_notified = False

# ──────────────────── Антиспам функция ────────────────────

async def check_rate_limit_and_ban(user_id: int, command: str):
    now = datetime.now(timezone.utc).timestamp()
    if user_id not in rate_limit_dict:
        rate_limit_dict[user_id] = {}

    if command not in rate_limit_dict[user_id]:
        rate_limit_dict[user_id][command] = []

    # Очистка старых timestamps
    rate_limit_dict[user_id][command] = [t for t in rate_limit_dict[user_id][command] if now - t < RATE_LIMIT_WINDOW]

    # Проверка бана
    if user_id in ban_dict and now < ban_dict[user_id]:
        return True  # Заблокирован

    # Счётчик
    if len(rate_limit_dict[user_id][command]) >= RATE_LIMIT_COUNT:
        # Бан
        unban_time = now + (BAN_DURATION_MINUTES * 60)
        ban_dict[user_id] = unban_time
        try:
            await bot.send_message(user_id, f"Вы заблокированы за спам на {BAN_DURATION_MINUTES} минут!")
        except:
            pass
        # Запуск задачи на разблок
        asyncio.create_task(unban_user(user_id, unban_time))
        return True

    # Добавить timestamp
    rate_limit_dict[user_id][command].append(now)
    return False

async def unban_user(user_id: int, unban_time: float):
    await asyncio.sleep(unban_time - datetime.now(timezone.utc).timestamp())
    if user_id in ban_dict:
        del ban_dict[user_id]
    try:
        await bot.send_message(user_id, "Вы разблокированы!")
    except:
        pass

# ──────────────────── КЛАВИАТУРЫ ────────────────────

def user_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Пополнить", callback_data="topup")],
        [InlineKeyboardButton(text="🎟 Купить билеты", callback_data="buy")],
        [InlineKeyboardButton(text="📊 Баланс", callback_data="balance")],
        [InlineKeyboardButton(text="🤝 Реф. ссылка", callback_data="ref")],
        [InlineKeyboardButton(text="📈 Статистика шансов", callback_data="stats")],
        [InlineKeyboardButton(text="🔗 Buy AUR & links", callback_data="show_links")],
    ])

def links_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Купить AUR в Blum", url="https://t.me/blum/app?startapp=memepadjetton_AUR_7r9oz-ref_opfXL31Vvi")],
        [InlineKeyboardButton(text="🔗 Купить AUR в Ston.fi", url="https://app.ston.fi/swap?ft=TON&tt=EQDtrpq6zmwzfqFL9JWnXzjZoZhK9xaprFCXerxPS4ZbS5tl&chartVisible=false&chartInterval=1w")],
        [InlineKeyboardButton(text="🔗 Купить AUR в DTrade", url="https://t.me/dtrade?start=12z09jrKRK_EQDtrpq6zmwzfqFL9JWnXzjZoZhK9xaprFCXerxPS4ZbS5tl")],
        [InlineKeyboardButton(text="🔗 Tg Channel", url="https://t.me/Aurum_comunity")],
        [InlineKeyboardButton(text="🔗 Tg Chat", url="https://t.me/+AcwLYvvcLsRkZDUy")],
    ])

def admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Запустить конкурс", callback_data="admin_start")],
        [InlineKeyboardButton(text="⏹ Остановить конкурс", callback_data="admin_stop")],
        [InlineKeyboardButton(text="🏆 Установить призы", callback_data="set_prizes")],
        [InlineKeyboardButton(text="⏰ Установить время раунда", callback_data="set_duration")],
        [InlineKeyboardButton(text="💰 Установить стоимость билета AUR", callback_data="set_cost_aur")],
        [InlineKeyboardButton(text="💰 Установить стоимость билета TON", callback_data="set_cost_ton")],
        [InlineKeyboardButton(text="👥 Балансы игроков", callback_data="admin_view_balances")],
        [InlineKeyboardButton(text="🔄 Восстановить список", callback_data="admin_restore_list")],  # Новая кнопка
    ])

async def contest_kb():
    me = await bot.get_me()
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Личный кабинет",
            url=f"https://t.me/{me.username}"
        )],
    ])

def confirm_topup_kb(user_id: int, amount: int, currency: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ Подтвердить {amount} {currency}", callback_data=f"confirm_{user_id}_{amount}_{currency}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{user_id}_{amount}_{currency}")]
    ])

# ──────────────────── HANDLERS ────────────────────

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if await check_rate_limit_and_ban(message.from_user.id, "start"):
        await message.answer("Вы заблокированы за спам. Подождите.")
        return

    args = message.text.split()
    referrer_id = int(args[1]) if len(args) > 1 and args[1].isdigit() else None

    user = message.from_user
    cur.execute("SELECT referrer_id FROM users WHERE user_telegram_id = ?", (user.id,))
    existing = cur.fetchone()

    if not existing:
        try:
            cur.execute(
                "INSERT INTO users (user_telegram_id, username, referrer_id) VALUES (?, ?, ?)",
                (user.id, user.username, referrer_id)
            )
        except sqlite3.IntegrityError:
            # If conflict, update
            cur.execute(
                "UPDATE users SET username = ?, referrer_id = ? WHERE user_telegram_id = ?",
                (user.username, referrer_id, user.id)
            )
        conn.commit()
        if referrer_id:
            try:
                await bot.send_message(referrer_id, f"У вас новый реферал: @{user.username or f'ID{user.id}'}")
            except Exception as e:
                print(f"Ошибка уведомления реферера: {e}")

    cur.execute("SELECT is_active, prizes, end_time FROM contest WHERE id = 1")
    row = cur.fetchone()
    is_active, prizes_json, end_time = row if row else (0, '[]', None)
    prizes = json.loads(prizes_json)

    if message.chat.type == "private":
        if user.id == ADMIN_ID:
            await message.answer("👑 Админ-панель", reply_markup=admin_kb())
        else:
            await message.answer("Добро пожаловать!", reply_markup=user_kb())
    else:
        if is_active and end_time:
            try:
                remaining = datetime.fromisoformat(end_time) - datetime.now(timezone.utc)
                if remaining.total_seconds() > 0:
                    m, s = divmod(int(remaining.total_seconds()), 60)
                    cur.execute("SELECT SUM(tickets) FROM users")
                    total = cur.fetchone()[0] or 0
                    prizes_text = ", ".join(prizes) if prizes else "Приз не установлен"
                    text = f"🎉 Активный конкурс!\nПризы: {prizes_text}\nОсталось: {m:02d}:{s:02d}\nБилетов всего: {total}"
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
    if await check_rate_limit_and_ban(callback.from_user.id, "topup"):
        await callback.answer("Вы заблокированы за спам.", show_alert=True)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 AUR", callback_data="topup_aur")],
        [InlineKeyboardButton(text="🔵 TON", callback_data="topup_ton")],
    ])
    await callback.message.answer("Выберите валюту для пополнения:", reply_markup=kb)
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("topup_"))
async def process_topup_currency(callback: types.CallbackQuery, state: FSMContext):
    currency = callback.data.split("_")[1].upper()
    await state.update_data(currency=currency)
    await callback.message.answer(f"Введите сумму пополнения в {currency}:")
    await state.set_state(TopUpState.waiting_amount)
    await callback.answer()

@dp.message(TopUpState.waiting_amount)
async def process_topup_amount(message: types.Message, state: FSMContext):
    if await check_rate_limit_and_ban(message.from_user.id, "topup"):
        return

    data = await state.get_data()
    currency = data.get("currency", "AUR")

    if not message.text.isdigit():
        await message.answer("Введите число")
        return

    amount = int(message.text)
    memo = f"{message.from_user.id}_{message.from_user.username or 'no_username'}"

    await message.answer(
        f"💳 Пополнение на {amount} {currency}\n"
        f"Кошелёк: <code>{TON_WALLET}</code>\n"
        f"Memo: <code>{memo}</code>",
        parse_mode="HTML"
    )

    username = message.from_user.username or f"ID{message.from_user.id}"
    await bot.send_message(
        ADMIN_ID,
        f"🟢 Запрос пополнения\nОт: @{username}\nВалюта: {currency}\nСумма: {amount}",
        reply_markup=confirm_topup_kb(message.from_user.id, amount, currency)
    )

    await state.clear()

@dp.callback_query(lambda c: c.data.startswith("confirm_"))
async def confirm_topup(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Запрещено", show_alert=True)
        return

    try:
        _, uid_str, amt_str, currency = callback.data.split("_")
        uid, amt = int(uid_str), int(amt_str)
    except:
        await callback.answer("Ошибка данных", show_alert=True)
        return

    # Update username if needed
    cur.execute("SELECT username FROM users WHERE user_telegram_id = ?", (uid,))
    row = cur.fetchone()
    if row is None:
        cur.execute("INSERT INTO users (user_telegram_id) VALUES (?)", (uid,))
        conn.commit()

    if currency == "AUR":
        cur.execute(
            "UPDATE users SET aur_balance = aur_balance + ? WHERE user_telegram_id = ?",
            (amt, uid)
        )
    else:
        cur.execute(
            "UPDATE users SET ton_balance = ton_balance + ? WHERE user_telegram_id = ?",
            (amt, uid)
        )
    conn.commit()

    await bot.send_message(uid, f"✅ Баланс пополнен на {amt} {currency}")
    await callback.message.edit_text(callback.message.text + "\n\n✅ Подтверждено")
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("reject_"))
async def reject_topup(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Запрещено", show_alert=True)
        return

    try:
        _, uid_str, amt_str, currency = callback.data.split("_")
        uid = int(uid_str)
        amt = int(amt_str)
    except:
        await callback.answer("Ошибка", show_alert=True)
        return

    await bot.send_message(uid, f"❌ Пополнение на {amt} {currency} отклонено")
    await callback.message.edit_text(callback.message.text + "\n\n❌ Отклонено")
    await callback.answer()

@dp.callback_query(lambda c: c.data == "buy")
async def start_buy_tickets(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != "private":
        await callback.answer("Только в ЛС", show_alert=True)
        return
    if await check_rate_limit_and_ban(callback.from_user.id, "buy"):
        await callback.answer("Вы заблокированы за спам.", show_alert=True)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="За AUR", callback_data="buy_aur")],
        [InlineKeyboardButton(text="За TON", callback_data="buy_ton")],
    ])
    await callback.message.answer("Выберите валюту для покупки билетов:", reply_markup=kb)
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("buy_"))
async def process_buy_currency(callback: types.CallbackQuery, state: FSMContext):
    currency = callback.data.split("_")[1].upper()
    await state.update_data(currency=currency)

    uid = callback.from_user.id
    if currency == "AUR":
        cur.execute("SELECT aur_balance FROM users WHERE user_telegram_id = ?", (uid,))
        row = cur.fetchone()
        if row is None:
            cur.execute("INSERT INTO users (user_telegram_id) VALUES (?)", (uid,))
            conn.commit()
            balance = 0
        else:
            balance = row[0]
        cur.execute("SELECT cost_per_ticket_aur FROM contest WHERE id = 1")
        row = cur.fetchone()
        cost_per_ticket = row[0] if row else DEFAULT_COST_PER_TICKET_AUR
    else:
        cur.execute("SELECT ton_balance FROM users WHERE user_telegram_id = ?", (uid,))
        row = cur.fetchone()
        if row is None:
            cur.execute("INSERT INTO users (user_telegram_id) VALUES (?)", (uid,))
            conn.commit()
            balance = 0.0
        else:
            balance = row[0]
        cur.execute("SELECT cost_per_ticket_ton FROM contest WHERE id = 1")
        row = cur.fetchone()
        cost_per_ticket = row[0] if row else DEFAULT_COST_PER_TICKET_TON

    await callback.message.answer(f"Введите количество билетов для покупки за {currency}:")
    await state.set_state(BuyTicketsState.waiting_quantity)
    await callback.answer()

@dp.message(BuyTicketsState.waiting_quantity)
async def process_buy_tickets(message: types.Message, state: FSMContext):
    if await check_rate_limit_and_ban(message.from_user.id, "buy"):
        return

    if not message.text.isdigit() or int(message.text) <= 0:
        await message.answer("Введите положительное целое число")
        return

    data = await state.get_data()
    currency = data.get("currency", "AUR")

    quantity = int(message.text)
    if currency == "AUR":
        cur.execute("SELECT cost_per_ticket_aur FROM contest WHERE id = 1")
        row = cur.fetchone()
        cost_per_ticket = row[0] if row else DEFAULT_COST_PER_TICKET_AUR
    else:
        cur.execute("SELECT cost_per_ticket_ton FROM contest WHERE id = 1")
        row = cur.fetchone()
        cost_per_ticket = row[0] if row else DEFAULT_COST_PER_TICKET_TON
    cost = quantity * cost_per_ticket
    uid = message.from_user.id
    cur.execute("SELECT aur_balance, ton_balance, referrer_id, rewarded_referrer FROM users WHERE user_telegram_id = ?", (uid,))
    row = cur.fetchone()
    if row is None:
        cur.execute("INSERT INTO users (user_telegram_id) VALUES (?)", (uid,))
        conn.commit()
        balance = 0 if currency == "AUR" else 0.0
        referrer_id = None
        rewarded = 0
    else:
        balance = row[0] if currency == "AUR" else row[1]
        referrer_id, rewarded = row[2], row[3]

    if balance < cost:
        await message.answer(f"Недостаточно средств. Требуется {cost} {currency}, доступно {balance} {currency}")
        await state.clear()
        return

    if currency == "AUR":
        cur.execute(
            "UPDATE users SET aur_balance = aur_balance - ?, tickets = tickets + ? WHERE user_telegram_id = ?",
            (cost, quantity, uid)
        )
    else:
        cur.execute(
            "UPDATE users SET ton_balance = ton_balance - ?, tickets = tickets + ? WHERE user_telegram_id = ?",
            (cost, quantity, uid)
        )

    if referrer_id and rewarded == 0:
        cur.execute("UPDATE users SET tickets = tickets + 1 WHERE user_telegram_id = ?", (referrer_id,))
        cur.execute("UPDATE users SET rewarded_referrer = 1 WHERE user_telegram_id = ?", (uid,))
        
        buyer_username = message.from_user.username or f"ID{uid}"
        try:
            await bot.send_message(referrer_id, f"Ваш реферал @{buyer_username} купил билет — вы получили 1 билет!")
        except Exception as e:
            print(f"Ошибка уведомления реферера: {e}")

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

    await message.answer(f"🎟 Куплено {quantity} билет(ов) за {cost} {currency}!")
    await state.clear()

@dp.callback_query(lambda c: c.data == "balance")
async def balance(callback: types.CallbackQuery):
    if await check_rate_limit_and_ban(callback.from_user.id, "balance"):
        await callback.answer("Вы заблокированы за спам.", show_alert=True)
        return
    uid = callback.from_user.id
    cur.execute("SELECT aur_balance, ton_balance, tickets FROM users WHERE user_telegram_id = ?", (uid,))
    row = cur.fetchone()
    if row is None:
        cur.execute("INSERT INTO users (user_telegram_id) VALUES (?)", (uid,))
        conn.commit()
        aur, ton, tik = 0, 0.0, 0
    else:
        aur, ton, tik = row
    cur.execute("SELECT SUM(tickets) FROM users")
    total_tickets = cur.fetchone()[0] or 0
    if total_tickets > 0:
        win_prob = (tik / total_tickets) * 100
        await callback.message.answer(f"💰 {aur} AUR | {ton} TON\n🎟 {tik}\nШанс на победу: {win_prob:.2f}%")
    else:
        await callback.message.answer(f"💰 {aur} AUR | {ton} TON\n🎟 {tik}\nШанс на победу: 0% (нет билетов в розыгрыше)")
    await callback.answer()

@dp.callback_query(lambda c: c.data == "ref")
async def ref(callback: types.CallbackQuery):
    if await check_rate_limit_and_ban(callback.from_user.id, "ref"):
        await callback.answer("Вы заблокированы за спам.", show_alert=True)
        return
    me = await bot.get_me()
    await callback.message.answer(
        f"https://t.me/{me.username}?start={callback.from_user.id}"
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data == "stats")
async def stats(callback: types.CallbackQuery):
    if await check_rate_limit_and_ban(callback.from_user.id, "stats"):
        await callback.answer("Вы заблокированы за спам.", show_alert=True)
        return
    cur.execute("SELECT is_active FROM contest WHERE id = 1")
    is_active = cur.fetchone()[0]
    if not is_active:
        await callback.answer("Нет активного конкурса", show_alert=True)
        return

    cur.execute("SELECT username, tickets FROM users WHERE tickets > 0 AND username IS NOT NULL ORDER BY tickets DESC")
    rows = cur.fetchall()
    cur.execute("SELECT SUM(tickets) FROM users")
    total_tickets = cur.fetchone()[0] or 0

    if total_tickets == 0:
        await callback.message.answer("Нет купленных билетов")
        await callback.answer()
        return

    text = "📈 Статистика шансов на победу:\n"
    for username, tickets in rows:
        prob = (tickets / total_tickets) * 100
        text += f"@{username}: {tickets} билетов ({prob:.2f}%)\n"

    text += f"\nВсего билетов: {total_tickets}"

    await callback.message.answer(text)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "show_links")
async def show_links(callback: types.CallbackQuery):
    await callback.message.answer("Ссылки для покупки AUR и сообщества:", reply_markup=links_kb())
    await callback.answer()

@dp.message(Command("send"))
async def cmd_send(message: types.Message):
    if await check_rate_limit_and_ban(message.from_user.id, "send"):
        return
    sender_id = message.from_user.id
    cur.execute("SELECT tickets FROM users WHERE user_telegram_id = ?", (sender_id,))
    sender_row = cur.fetchone()
    if not sender_row or sender_row[0] == 0:
        await message.reply("У вас нет билетов для отправки.")
        return

    if message.reply_to_message:
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
        args = message.text.split()[1:]
        if len(args) != 2 or not args[0].startswith('@') or not args[1].isdigit():
            await message.reply("Формат: /send @username <количество>")
            return
        username = args[0][1:]
        quantity = int(args[1])
        cur.execute("SELECT user_telegram_id FROM users WHERE username = ?", (username,))
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

    cur.execute("SELECT tickets FROM users WHERE user_telegram_id = ?", (sender_id,))
    sender_tickets = cur.fetchone()[0]
    if quantity > sender_tickets:
        await message.reply(f"У вас только {sender_tickets} билетов.")
        return

    cur.execute("INSERT OR IGNORE INTO users (user_telegram_id) VALUES (?)", (recipient_id,))

    cur.execute("UPDATE users SET tickets = tickets - ? WHERE user_telegram_id = ?", (quantity, sender_id))
    cur.execute("UPDATE users SET tickets = tickets + ? WHERE user_telegram_id = ?", (quantity, recipient_id))
    conn.commit()

    sender_username = message.from_user.username or f"ID{sender_id}"
    recipient_username = (await bot.get_chat(recipient_id)).username or f"ID{recipient_id}"

    await message.reply(f"✅ Отправлено {quantity} билет(ов) пользователю @{recipient_username}")
    try:
        await bot.send_message(recipient_id, f"🎟 Получено {quantity} билет(ов) от @{sender_username}")
    except:
        pass

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
    global announce_chat_id, announce_message_id, timer_task, five_min_notified

    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return

    if announce_chat_id is None:
        await callback.message.answer("Сначала /addchat в нужной группе")
        await callback.answer()
        return

    cur.execute("SELECT prizes, duration_minutes FROM contest WHERE id = 1")
    row = cur.fetchone()
    prizes_json = row[0] or '[]'
    prizes = json.loads(prizes_json)
    prizes_text = ", ".join(prizes) if prizes else "Приз не установлен"
    duration_minutes = row[1]

    end_time = (datetime.now(timezone.utc) + timedelta(minutes=duration_minutes)).isoformat()

    cur.execute("UPDATE contest SET is_active = 1, end_time = ?, prize_message_ids = '[]' WHERE id = 1", (end_time,))
    conn.commit()

    # Первое сообщение: анонс
    announce_text = f"🎉 Конкурс запущен!\nПризы: {prizes_text}\nОсталось: {duration_minutes:02d}:00\nБилетов: 0"
    msg = await bot.send_message(announce_chat_id, announce_text, reply_markup=await contest_kb())
    announce_message_id = msg.message_id

    # Вторые сообщения: для каждого приза
    prize_message_ids = []
    for i, prize in enumerate(prizes, start=1):
        prize_text = f"{i}й приз: {prize}"  # Ссылка, если приз — URL, иначе текст
        prize_msg = await bot.send_message(announce_chat_id, prize_text)
        prize_message_ids.append(prize_msg.message_id)

    prize_ids_json = json.dumps(prize_message_ids)
    cur.execute("UPDATE contest SET prize_message_ids = ? WHERE id = 1", (prize_ids_json,))
    conn.commit()

    if timer_task and not timer_task.done():
        timer_task.cancel()

    five_min_notified = False
    timer_task = asyncio.create_task(update_timer())

    await notify_all_users("🎉 Конкурс начался! Участвуйте и покупайте билеты.")

    await callback.message.answer("Конкурс запущен!")
    await callback.answer("Запущен")

@dp.callback_query(lambda c: c.data == "admin_stop")
async def admin_stop(callback: types.CallbackQuery):
    global timer_task, five_min_notified
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

@dp.callback_query(lambda c: c.data == "set_prizes")
async def admin_set_prizes(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return

    await callback.message.answer("Введите призы через запятую (например: NFT1, NFT2, 100 AUR):")
    await state.set_state(SetPrizesState.waiting_prizes)
    await callback.answer()

@dp.message(SetPrizesState.waiting_prizes)
async def process_prizes(message: types.Message, state: FSMContext):
    prizes = [p.strip() for p in message.text.split(',') if p.strip()]
    prizes_json = json.dumps(prizes)
    cur.execute("UPDATE contest SET prizes = ? WHERE id = 1", (prizes_json,))
    conn.commit()
    await message.answer(f"Призы установлены: {', '.join(prizes)}")
    await state.clear()

@dp.callback_query(lambda c: c.data == "set_duration")
async def admin_set_duration(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return

    await callback.message.answer("Введите продолжительность раунда в минутах:")
    await state.set_state(SetDurationState.waiting_duration)
    await callback.answer()

@dp.message(SetDurationState.waiting_duration)
async def process_duration(message: types.Message, state: FSMContext):
    if not message.text.isdigit() or int(message.text) <= 0:
        await message.answer("Введите положительное целое число")
        return

    duration = int(message.text)
    cur.execute("UPDATE contest SET duration_minutes = ? WHERE id = 1", (duration,))
    conn.commit()
    await message.answer(f"Продолжительность раунда установлена: {duration} минут")
    await state.clear()

@dp.callback_query(lambda c: c.data == "set_cost_aur")
async def admin_set_cost_aur(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return

    await callback.message.answer("Введите стоимость одного билета в AUR:")
    await state.set_state(SetCostAurState.waiting_cost)
    await callback.answer()

@dp.message(SetCostAurState.waiting_cost)
async def process_cost_aur(message: types.Message, state: FSMContext):
    if not message.text.isdigit() or int(message.text) <= 0:
        await message.answer("Введите положительное целое число")
        return

    cost = int(message.text)
    cur.execute("UPDATE contest SET cost_per_ticket_aur = ? WHERE id = 1", (cost,))
    conn.commit()
    await message.answer(f"Стоимость билета в AUR установлена: {cost} AUR")
    await state.clear()

@dp.callback_query(lambda c: c.data == "set_cost_ton")
async def admin_set_cost_ton(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return

    await callback.message.answer("Введите стоимость одного билета в TON:")
    await state.set_state(SetCostTonState.waiting_cost)
    await callback.answer()

@dp.message(SetCostTonState.waiting_cost)
async def process_cost_ton(message: types.Message, state: FSMContext):
    try:
        cost = float(message.text)
        if cost <= 0:
            await message.answer("Введите положительное число")
            return
    except ValueError:
        await message.answer("Введите число (можно дробное, например 0.1)")
        return

    cur.execute("UPDATE contest SET cost_per_ticket_ton = ? WHERE id = 1", (cost,))
    conn.commit()
    await message.answer(f"Стоимость билета в TON установлена: {cost} TON")
    await state.clear()

@dp.callback_query(lambda c: c.data == "admin_view_balances")
async def admin_view_balances(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return

    cur.execute("SELECT user_telegram_id, username, aur_balance, ton_balance, tickets FROM users")
    rows = cur.fetchall()
    if not rows:
        await callback.message.answer("Нет игроков")
    else:
        text = "Балансы:\n" + "\n".join([f"@{r[1] or f'ID{r[0]}'}: {r[2]} AUR, {r[3]} TON, {r[4]} билетов" for r in rows])
        await callback.message.answer(text)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "admin_restore_list")
async def admin_restore_list(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return

    await callback.message.answer("Введите список участников в формате:\n@username: X AUR, Y TON, Z билетов\nИли @IDXXXX: ... для пользователей без username.\nОдин на строку.")
    await state.set_state(RestoreListState.waiting_list)
    await callback.answer()

@dp.message(RestoreListState.waiting_list)
async def process_restore_list(message: types.Message, state: FSMContext):
    lines = message.text.splitlines()
    updated_count = 0
    skipped = []

    for line in lines:
        line = line.strip()
        if not line:
            continue

        match = re.match(r'@(.+?):\s*(\d+)\s*AUR,\s*([\d.]+)\s*TON,\s*(\d+)\s*билет(ов|а|)?', line)
        if not match:
            skipped.append(line)
            continue

        username_part = match.group(1)
        aur = int(match.group(2))
        ton = float(match.group(3))
        tickets = int(match.group(4))

        user_telegram_id = None
        username = None

        if username_part.startswith("ID"):
            try:
                user_telegram_id = int(username_part[2:])
            except ValueError:
                skipped.append(line)
                continue
        else:
            username = username_part

        # Try to get user_telegram_id if not provided
        if user_telegram_id is None and username is not None:
            try:
                chat = await bot.get_chat(f'@{username}')
                user_telegram_id = chat.id
            except Exception as e:
                print(f"Ошибка получения user_telegram_id для {username}: {e}")

        # Find existing record
        existing_id = None
        if user_telegram_id is not None:
            cur.execute("SELECT id FROM users WHERE user_telegram_id = ?", (user_telegram_id,))
            row = cur.fetchone()
            if row:
                existing_id = row[0]
        if existing_id is None and username is not None:
            cur.execute("SELECT id FROM users WHERE username = ?", (username,))
            row = cur.fetchone()
            if row:
                existing_id = row[0]

        if existing_id is not None:
            # Update existing
            cur.execute("""
                UPDATE users SET aur_balance = ?, ton_balance = ?, tickets = ?,
                user_telegram_id = COALESCE(?, user_telegram_id),
                username = COALESCE(?, username)
                WHERE id = ?
            """, (aur, ton, tickets, user_telegram_id, username, existing_id))
        else:
            # Insert new
            cur.execute("""
                INSERT INTO users (user_telegram_id, username, aur_balance, ton_balance, tickets)
                VALUES (?, ?, ?, ?, ?)
            """, (user_telegram_id, username, aur, ton, tickets))

        updated_count += 1

    conn.commit()

    response = f"Список восстановлен: обновлено {updated_count} участников."
    if skipped:
        response += f"\nПропущено (не найдены или ошибка формата): {', '.join(skipped)}"

    await message.answer(response)
    await state.clear()

# ──────────────────── ТАЙМЕР + РОЗЫГРЫШ ────────────────────

async def update_timer():
    global announce_chat_id, announce_message_id, five_min_notified

    while True:
        await asyncio.sleep(TIMER_UPDATE_INTERVAL)

        cur.execute("SELECT is_active, end_time, prizes FROM contest WHERE id = 1")
        row = cur.fetchone()
        if not row or row[0] == 0 or not row[1]:
            print("Таймер остановлен: конкурс не активен")
            break

        end_time = datetime.fromisoformat(row[1])
        remaining = end_time - datetime.now(timezone.utc)

        cur.execute("SELECT SUM(tickets) FROM users")
        total_tickets = cur.fetchone()[0] or 0

        if remaining.total_seconds() <= 300 and not five_min_notified and remaining.total_seconds() > 0:
            await notify_all_users("⏰ Осталось 5 минут до конца конкурса! Спешите купить билеты.")
            five_min_notified = True

        if remaining.total_seconds() <= 0:
            print("Таймер завершён → запуск розыгрыша")
            await perform_draw(total_tickets)
            break

        m, s = divmod(int(remaining.total_seconds()), 60)
        prizes = json.loads(row[2] or '[]')
        prizes_text = ", ".join(prizes) if prizes else "Приз не установлен"
        text = f"🎉 Конкурс идёт\nПризы: {prizes_text}\nОсталось: {m:02d}:{s:02d}\nБилетов: {total_tickets}"

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
    cur.execute("SELECT prizes, prize_message_ids FROM contest WHERE id = 1")
    row = cur.fetchone()
    prizes = json.loads(row[0] or '[]')
    prize_message_ids = json.loads(row[1] or '[]')
    num_prizes = len(prizes)

    if total_tickets == 0:
        text = "Конкурс завершён. Никто не купил билетов."
        winners = []
    else:
        cur.execute("SELECT id, tickets FROM users WHERE tickets > 0")
        participants = cur.fetchall()

        pool = []
        for internal_id, count in participants:
            pool.extend([internal_id] * count)

        winners_ids = set()
        while len(winners_ids) < min(num_prizes, len(set(pool))):
            winner_internal_id = random.choice(pool)
            winners_ids.add(winner_internal_id)

        winners = []
        for wid in winners_ids:
            cur.execute("SELECT username, user_telegram_id FROM users WHERE id = ?", (wid,))
            row = cur.fetchone()
            if row:
                winners.append(row)

    winners_text = ", ".join([f"@{w[0]}" for w in winners if w[0]]) if winners else "Нет победителей"
    text = f"🎉 Конкурс завершён!\nПобедители: {winners_text}\nПоздравляем!"

    await bot.edit_message_text(
        text,
        chat_id=announce_chat_id,
        message_id=announce_message_id
    )

    # Редактировать сообщения призов
    for i, mid in enumerate(prize_message_ids):
        if i < len(winners):
            winner = winners[i]
            winner_username, winner_telegram_id = winner
            winner_tickets, winner_prob = await get_winner_stats(winner_username, total_tickets)
            edit_text = f"{i+1}й приз: {prizes[i]} победил @{winner_username} ({winner_tickets} билетов, {winner_prob:.2f}%)"
            await bot.edit_message_text(edit_text, chat_id=announce_chat_id, message_id=mid)
            if winner_telegram_id:
                await bot.send_message(winner_telegram_id, f"🎉 Вы выиграли {prizes[i]}! Напишите админу.")

    await notify_all_users(f"🏁 Конкурс завершился! Победители: {winners_text}")

    await send_admin_log()

    cur.execute("UPDATE contest SET is_active = 0, end_time = NULL WHERE id = 1")
    cur.execute("UPDATE users SET tickets = 0, aur_balance = 0, ton_balance = 0.0, rewarded_referrer = 0 WHERE user_telegram_id != ?", (ADMIN_ID,))
    conn.commit()
    print("Розыгрыш завершён, билеты и балансы сброшены для пользователей")

async def get_user_id_by_username(username):
    cur.execute("SELECT user_telegram_id FROM users WHERE username = ?", (username,))
    row = cur.fetchone()
    return row[0] if row else None

async def get_winner_stats(username, total_tickets):
    cur.execute("SELECT tickets FROM users WHERE username = ?", (username,))
    tickets = cur.fetchone()[0] or 0
    prob = (tickets / total_tickets * 100) if total_tickets > 0 else 0
    return tickets, prob

async def notify_all_users(text):
    cur.execute("SELECT user_telegram_id FROM users")
    users = cur.fetchall()
    for uid in users:
        if uid[0]:
            try:
                await bot.send_message(uid[0], text)
            except Exception as e:
                print(f"Ошибка рассылки: {e}")

async def send_admin_log():
    cur.execute("SELECT username, tickets FROM users WHERE tickets > 0 AND username IS NOT NULL")
    participants = cur.fetchall()
    num_participants = len(participants)
    total_tickets = sum([p[1] for p in participants]) if participants else 0

    text = f"Лог конкурса:\nУчастников: {num_participants}\nВсего билетов: {total_tickets}\n"
    for username, tickets in participants:
        prob = (tickets / total_tickets * 100) if total_tickets > 0 else 0
        text += f"@{username}: {tickets} билетов ({prob:.2f}%)\n"

    await bot.send_message(ADMIN_ID, text)

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
            await asyncio.sleep(240)

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