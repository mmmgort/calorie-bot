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
model = genai.GenerativeModel('gemini-1.5-flash')

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
    cursor.execute("CREATE TABLE IF NOT EXISTS user_settings (user_id BIGINT PRIMARY KEY, cal INT DEFAULT 2600, prot INT DEFAULT 170, fat INT DEFAULT 75, carb INT DEFAULT 300, target_waist REAL DEFAULT 87.0, last_bench REAL DEFAULT 0.0)")
    cursor.execute("CREATE TABLE IF NOT EXISTS meals (id SERIAL PRIMARY KEY, user_id BIGINT, date DATE, meal_text TEXT, calories REAL, protein REAL, fat REAL, carbs REAL)")
    cursor.execute("CREATE TABLE IF NOT EXISTS progress (id SERIAL PRIMARY KEY, user_id BIGINT, date DATE, weight REAL, waist REAL, bench REAL DEFAULT 0.0)")
    
    # Проверка структуры (миграции)
    for col in ['bench']:
        cursor.execute(f"SELECT column_name FROM information_schema.columns WHERE table_name='progress' AND column_name='{col}'")
        if not cursor.fetchone():
            cursor.execute(f"ALTER TABLE progress ADD COLUMN {col} REAL DEFAULT 0.0")
    
    for col in ['last_bench']:
        cursor.execute(f"SELECT column_name FROM information_schema.columns WHERE table_name='user_settings' AND column_name='{col}'")
        if not cursor.fetchone():
            cursor.execute(f"ALTER TABLE user_settings ADD COLUMN {col} REAL DEFAULT 0.0")
            
    conn.commit()
    cursor.close()
    conn.close()

# ===================== ЛОГИКА ЗАМЕРОВ =====================
async def save_progress(user_id, w, t, b):
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO progress (user_id, date, weight, waist, bench) VALUES (%s,%s,%s,%s,%s)", (user_id, date.today(), w, t, b))
    cursor.execute("INSERT INTO user_settings (user_id, last_bench) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET last_bench = EXCLUDED.last_bench", (user_id, b))
    conn.commit()
    cursor.close()
    conn.close()

# ===================== ХЕНДЛЕРЫ =====================

@dp.message(Command("start"))
async def cmd_start(message: Message):
    init_db()
    await message.answer(f"💪 Привет, {message.from_user.first_name}! Я готов трекать твой путь к сотке.", reply_markup=get_main_kb())

@dp.message(F.text == "⚖️ Замер (Вес/Жим)")
async def prompt_log(message: Message):
    await message.answer("Просто напиши три числа через пробел: `Вес Талия Жим` (например: `95 90 85`)")

# Ловим формат "число число число"
@dp.message(F.text.regexp(r"^(\d+\.?\d*)\s+(\d+\.?\d*)\s+(\d+\.?\d*)$"))
async def log_numbers(message: Message):
    nums = re.findall(r"\d+\.?\d*", message.text)
    try:
        w, t, b = float(nums[0]), float(nums[1]), float(nums[2])
        await save_progress(message.from_user.id, w, t, b)
        await message.answer(f"✅ Записал!\n⚖️ {w}кг | 📏 {t}см | 💪 {b}кг")
    except Exception as e:
        await message.answer("❌ Ошибка при сохранении.")

@dp.message(F.photo | F.text)
async def handle_meal(message: Message, state: FSMContext):
    if message.text in ["🍲 Добавить еду", "⚖️ Замер (Вес/Жим)", "📊 Статистика", "⚙️ Настройки"] or (message.text and message.text.startswith('/')):
        return

    msg_wait = await message.answer("🔍 Анализирую...")
    
    prompt = "Оцени КБЖУ. Верни ТОЛЬКО JSON: {\"calories\": int, \"protein\": int, \"fat\": int, \"carbs\": int, \"name\": \"название\"}"
    contents = [prompt]
    
    if message.text: contents.append(message.text)
    if message.caption: contents.append(message.caption)
    if message.photo:
        file = await bot.get_file(message.photo[-1].file_id)
        img = await bot.download_file(file.path)
        contents.append({"mime_type": "image/jpeg", "data": img.read()})

    try:
        response = model.generate_content(contents)
        # Очистка JSON от лишнего текста ИИ
        json_str = re.search(r'\{.*\}', response.text, re.DOTALL).group(0)
        data = json.loads(json_str)
        await state.update_data(temp_meal=data)
        
        res_text = (f"🍴 **{data['name']}**\n🔥 {data['calories']} ккал\n"
                    f"Б: {data['protein']}г | Ж: {data['fat']}г | У: {data['carbs']}г\n\nЗаписываем?")
        await msg_wait.edit_text(res_text, reply_markup=get_meal_inline_kb(), parse_mode="Markdown")
    except:
        await msg_wait.edit_text("❌ Не смог распознать. Опиши еду текстом.")

@dp.callback_query(F.data.startswith("meal_"))
async def process_meal_buttons(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    meal = data.get("temp_meal")
    
    if callback.data == "meal_confirm" and meal:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO meals (user_id, date, meal_text, calories, protein, fat, carbs) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                       (callback.from_user.id, date.today(), meal['name'], meal['calories'], meal['protein'], meal['fat'], meal['carbs']))
        conn.commit()
        await callback.message.edit_text(f"✅ Записано: {meal['name']}")
        await state.clear()
    
    elif callback.data == "meal_edit":
        await callback.message.answer("📝 Что не так? Напиши уточнение (например: 'там было 2 котлеты' или 'убери жиры')")
        await state.set_state(MealStates.waiting_for_edit)
        await callback.answer()
        
    elif callback.data == "meal_cancel":
        await callback.message.edit_text("🗑 Отменено")
        await state.clear()

@dp.message(MealStates.waiting_for_edit)
async def edit_meal_logic(message: Message, state: FSMContext):
    data = await state.get_data()
    old_meal = data.get("temp_meal")
    msg_wait = await message.answer("🔄 Пересчитываю...")
    
    prompt = f"Было: {old_meal}. Уточнение: {message.text}. Дай новый JSON КБЖУ."
    try:
        response = model.generate_content(prompt)
        json_str = re.search(r'\{.*\}', response.text, re.DOTALL).group(0)
        new_data = json.loads(json_str)
        await state.update_data(temp_meal=new_data)
        
        res_text = (f"🔄 **Обновлено: {new_data['name']}**\n🔥 {new_data['calories']} ккал\n"
                    f"Б: {new_data['protein']}г | Ж: {new_data['fat']}г | У: {new_data['carbs']}г\n\nТеперь верно?")
        await msg_wait.edit_text(res_text, reply_markup=get_meal_inline_kb(), parse_mode="Markdown")
    except:
        await msg_wait.edit_text("❌ Ошибка пересчета.")

async def main():
    init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
