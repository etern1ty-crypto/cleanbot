import os
import asyncio
import logging
import sqlite3
from datetime import datetime
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, WebAppInfo, InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonWebApp
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
# URL вашего Mini App (должен быть HTTPS)
APP_URL = os.getenv("APP_URL", "https://your-ngrok-url.ngrok-free.app") 
DB_PATH = os.path.join(os.path.dirname(__file__), "tasks.db")
OWNER_ID = int(os.getenv("OWNER_ID", "0")) if os.getenv("OWNER_ID") else 0

bot = Bot(token=TOKEN)
dp = Dispatcher()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def save_user(user_id, username, first_name, last_name):
    try:
        conn = sqlite3.connect(DB_PATH)
        full_name = f"{first_name or ''} {last_name or ''}".strip()
        # Проверяем, существует ли пользователь, чтобы не затереть статус активности при повторном /start
        existing = conn.execute("SELECT is_active FROM users WHERE user_id = ?", (user_id,)).fetchone()
        
        if not existing:
            is_active = 1
            
            conn.execute("""
                INSERT INTO users (user_id, username, full_name, last_seen, is_active)
                VALUES (?, ?, ?, datetime('now'), ?)
            """, (user_id, username, full_name, is_active))
        else:
            # Существующий пользователь - только обновляем данные
            conn.execute("""
                UPDATE users SET
                    username=?,
                    full_name=?,
                    last_seen=datetime('now'),
                    is_active=1
                WHERE user_id=?
            """, (username, full_name, user_id))
            
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error saving user in bot: {e}")

@dp.message(Command("start"))
async def start_cmd(message: Message):
    # Save user to DB
    save_user(
        message.from_user.id, 
        message.from_user.username, 
        message.from_user.first_name, 
        message.from_user.last_name
    )
    
    builder = InlineKeyboardBuilder()
    # Кнопка для запуска Mini App
    builder.button(
        text="🚀 Открыть CleanApp", 
        web_app=WebAppInfo(url=APP_URL)
    )
    builder.adjust(1)
    
    await message.answer(
        "Привет! Теперь ты можешь управлять задачами через современное приложение прямо в Telegram.",
        reply_markup=builder.as_markup()
    )

@dp.message(Command("setup_menu"))
async def setup_menu(message: Message):
    """Настройка кнопки меню для быстрого доступа к приложению"""
    await bot.set_chat_menu_button(
        chat_id=message.chat.id,
        menu_button=MenuButtonWebApp(
            text="CleanApp",
            web_app=WebAppInfo(url=APP_URL)
        )
    )
    await message.answer("✅ Кнопка меню CleanApp установлена!")

async def main():
    logger.info("Бот для Mini App запущен...")
    # Устанавливаем кнопку меню для всех
    # await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="CleanApp", web_app=WebAppInfo(url=APP_URL)))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
