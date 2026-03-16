import asyncio
import os
import json
import re
import psycopg2
from datetime import datetime, date
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
import google.generativeai as genai

# ===================== НАСТРОЙКИ =====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

genai.configure(api_key=GEMINI_API_KEY)
# Используем стабильную версию модели
model = genai.GenerativeModel('gemini-1.5-pro')

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

class MealStates(StatesGroup):
    waiting_for_edit = State()

# ===================== КЛАВИАТУРЫ =====================
def get_main_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🍲 Добавить еду"), KeyboardButton(text="⚖️ Замер (Вес/Жим)")],
        [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="⚙️ Настройки")]
    ], resize_keyboard=True)

def get_meal_inline_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Подтвердить", callback_data="meal_confirm"),
            InlineKeyboardButton(text="✏️ Изменить", callback_data="meal_edit")
        ],
        [InlineKeyboardButton(text="🗑 Отмена", callback_data="meal_cancel")]
    ])

# ===================== БАЗА ДАННЫХ =====================
def init_db():
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    # Таблица настроек
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id BIGINT PRIMARY KEY, 
            cal INT DEFAULT 2600, 
            prot INT DEFAULT 170, 
            fat INT DEFAULT 75, 
            carb INT DEFAULT 300, 
            target_waist REAL DEFAULT 87.0, 
            last_bench REAL DEFAULT 0.0
        )
    """)
    # Таблица еды
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS meals (
            id SERIAL PRIMARY KEY, 
            user_id BIGINT, 
            date DATE, 
            meal_text TEXT, 
            calories REAL, 
            protein REAL, 
            fat REAL, 
            carbs REAL
        )
    """)
    # Таблица прогресса + проверка колонки bench
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS progress (
            id SERIAL PRIMARY KEY, 
            user_id BIGINT, 
            date DATE, 
            weight REAL, 
            waist REAL
        )
    """)
    
    # Проверка и добавление отсутствующих колонок
    cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_name='progress' AND column_name='bench'")
    if not cursor.fetchone():
        cursor.execute("ALTER TABLE progress ADD COLUMN bench REAL DEFAULT 0.0")
        
    cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_name='user_settings' AND column_name='last_bench'")
    if not cursor.fetchone():
        cursor.execute("ALTER TABLE user_settings ADD COLUMN last_bench REAL DEFAULT 0.0")

    conn.commit()
    cursor.close()
    conn.close()

# ===================== ХЕНДЛЕРЫ =====================
@dp.message(Command("start"))
async def cmd_start(message: Message):
    init_db()
    await message.answer(
        f"👋 Привет, {message.from_user.first_name}! Система обновлена.\n"
        "Теперь я точно записываю жим и еду.", 
        reply_markup=get_main_kb()
    )

@dp.message(F.text == "🍲 Добавить еду")
async def meal_instruction(message: Message):
    await message.answer("Просто пришли фото еды или опиши её текстом (например: '200г гречки и стейк').")

@dp.message(F.text == "⚖️ Замер (Вес/Жим)")
async def prompt_log(message: Message):
    await message.answer("Пришли замеры через пробел: `Вес Талия Жим` (например: `95 90 85`)")

# Улучшенный логгер (ловит и команду /log, и просто текст в формате цифр)
@dp.message(F.text.regexp(r"(\d+\.?\d*)\s+(\d+\.?\d*)\s+(\d+\.?\d*)"))
@dp.message(Command("log"))
async def log_data(message: Message):
    nums = re.findall(r"\d+\.?\d*", message.text)
    if len(nums) < 3:
        return await message.answer("⚠️ Нужно 3 числа: Вес, Талия и Жим.\nПример: `95 90 85` или `/log 95 90 85`")
    
    try:
        w, t, b = float(nums[0]), float(nums[1]), float(nums[2])
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        # Вставляем запись в прогресс
        cursor.execute("INSERT INTO progress (user_id, date, weight, waist, bench) VALUES (%s,%s,%s,%s,%s)", 
                       (message.from_user.id, date.today(), w, t, b))
        # Обновляем текущие настройки
        cursor.execute("INSERT INTO user_settings (user_id, last_bench) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET last_bench = EXCLUDED.last_bench", 
                       (message.from_user.id, b))
        conn.commit()
        cursor.close()
        conn.close()
        await message.answer(f"✅ **Данные записаны!**\n⚖️ Вес: {w}кг | 📏 Талия: {t}см | 💪 Жим: {b}кг\nИдем к сотке! 🔥", parse_mode="Markdown")
    except Exception as e:
        await message.answer(f"❌ Ошибка записи в базу. Попробуй нажать /start и повторить.")

# ===================== РАБОТА С GEMINI =====================
@dp.message(F.photo | F.text)
async def handle_meal(message: Message, state: FSMContext):
    # Фильтр системных сообщений
    if message.text in ["🍲 Добавить еду", "⚖️ Замер (Вес/Жим)", "📊 Статистика", "⚙️ Настройки"] or (message.text and message.text.startswith('/')):
        return

    msg_wait = await message.answer("🔍 Анализирую...")
    
    prompt = "Верни JSON: {\"calories\": int, \"protein\": int, \"fat\": int, \"carbs\": int, \"name\": \"str\"}. Оцени еду:"
    content = [prompt]
    
    if message.text: content.append(message.text)
    if message.caption: content.append(message.caption)
    if message.photo:
        file = await bot.get_file(message.photo[-1].file_id)
        img = await bot.download_file(file.file_path)
        content.append({"mime_type": "image/jpeg", "data": img.read()})

    try:
        response = model.generate_content(content)
        data = json.loads(re.search(r'\{.*\}', response.text, re.DOTALL).group(0))
        await state.update_data(temp_meal=data)
        
        text = (f"🍴 **{data['name']}**\n🔥 {data['calories']} ккал\n"
                f"Б: {data['protein']}г | Ж: {data['fat']}г | У: {data['carbs']}г\n\nПодтверждаешь?")
        await msg_wait.edit_text(text, reply_markup=get_meal_inline_kb(), parse_mode="Markdown")
    except:
        await msg_wait.edit_text("❌ Не удалось распознать. Попробуй описать текстом подробнее.")

@dp.callback_query(F.data.startswith("meal_"))
async def process_callback(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    meal = data.get("temp_meal")
    
    if callback.data == "meal_confirm" and meal:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO meals (user_id, date, meal_text, calories, protein, fat, carbs) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                       (callback.from_user.id, date.today(), meal['name'], meal['calories'], meal['protein'], meal['fat'], meal['carbs']))
        conn.commit()
        await callback.message.edit_text(f"✅ Записал: {meal['name']}")
        await state.clear()
    elif callback.data == "meal_cancel":
        await callback.message.edit_text("🗑 Удалено")
        await state.clear()

async def main():
    init_db()
    # Сброс вебхука решает проблему 'Conflict: terminate by other getUpdates'
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
