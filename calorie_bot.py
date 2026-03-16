import asyncio
import os
import json
import re
import psycopg2
from datetime import datetime, date
from aiogram import Bot, Dispatcher, F, types
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

# Состояния для редактирования
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

# ===================== БАЗА ДАННЫХ (БЕЗ ИЗМЕНЕНИЙ) =====================
# (Используем твою рабочую init_db из прошлого шага)
def init_db():
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS user_settings (user_id BIGINT PRIMARY KEY, cal INT DEFAULT 2600, prot INT DEFAULT 170, fat INT DEFAULT 75, carb INT DEFAULT 300, target_waist REAL DEFAULT 87.0, last_bench REAL DEFAULT 0.0)")
    cursor.execute("CREATE TABLE IF NOT EXISTS meals (id SERIAL PRIMARY KEY, user_id BIGINT, date DATE, meal_text TEXT, calories REAL, protein REAL, fat REAL, carbs REAL)")
    cursor.execute("CREATE TABLE IF NOT EXISTS progress (id SERIAL PRIMARY KEY, user_id BIGINT, date DATE, weight REAL, waist REAL, bench REAL DEFAULT 0.0)")
    cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_name='progress' AND column_name='bench'")
    if not cursor.fetchone(): cursor.execute("ALTER TABLE progress ADD COLUMN bench REAL DEFAULT 0.0")
    conn.commit()
    cursor.close()
    conn.close()

# ===================== ХЕНДЛЕРЫ =====================

@dp.message(Command("start"))
async def cmd_start(message: Message):
    init_db()
    await message.answer(f"👋 Привет, {message.from_user.first_name}! Жмем сотку? 💪", reply_markup=get_main_kb())

@dp.message(F.text == "⚖️ Замер (Вес/Жим)")
async def prompt_log(message: Message):
    await message.answer("Пришли замеры в формате: `/log Вес Талия Жим` (например: `/log 76 87 100`)", parse_mode="Markdown")

@dp.message(F.photo | F.text)
async def handle_meal_input(message: Message, state: FSMContext):
    if message.text and (message.text.startswith('/') or message.text in ["📊 Статистика", "⚙️ Настройки", "⚖️ Замер (Вес/Жим)"]): return
    
    msg_wait = await message.answer("🔍 Анализирую состав...")
    
    contents = []
    if message.photo:
        file = await bot.get_file(message.photo[-1].file_id)
        file_bytes = await bot.download_file(file.file_path)
        contents.append({"mime_type": "image/jpeg", "data": file_bytes.read()})
    
    prompt = "Верни ТОЛЬКО JSON: {\"calories\": X, \"protein\": X, \"fat\": X, \"carbs\": X, \"name\": \"название\"}"
    contents.append(message.text or "Еда на фото")
    contents.append(prompt)

    try:
        response = model.generate_content(contents)
        data = json.loads(re.sub(r'```json\s*|```', '', response.text).strip())
        
        # Сохраняем временные данные в состояние FSM
        await state.update_data(temp_meal=data)
        
        text = (f"🍴 **{data['name']}**\n"
                f"🔥 Калории: {data['calories']} ккал\n"
                f"🥩 Белки: {data['protein']}г | 🥑 Жиры: {data['fat']}г | 🌾 Углеводы: {data['carbs']}г\n\n"
                f"Записываем?")
        
        await msg_wait.edit_text(text, reply_markup=get_meal_inline_kb(), parse_mode="Markdown")
    except:
        await msg_wait.edit_text("❌ Не удалось распознать. Попробуй описать текст в свободном стиле.")

# Обработка Inline-кнопок
@dp.callback_query(F.data.startswith("meal_"))
async def process_meal_callback(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    meal = data.get("temp_meal")

    if callback.data == "meal_confirm" and meal:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO meals (user_id, date, meal_text, calories, protein, fat, carbs) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                       (callback.from_user.id, date.today(), meal['name'], meal['calories'], meal['protein'], meal['fat'], meal['carbs']))
        conn.commit()
        cursor.close()
        conn.close()
        await callback.message.edit_text(f"✅ Записано: {meal['name']} ({meal['calories']} ккал)")
        await state.clear()

    elif callback.data == "meal_edit":
        await callback.message.answer("✍️ Что именно изменить? (Например: 'там 200г курицы' или 'добавь еще стакан сока')")
        await state.set_state(MealStates.waiting_for_edit)
        await callback.answer()

    elif callback.data == "meal_cancel":
        await callback.message.edit_text("❌ Отменено.")
        await state.clear()

@dp.message(MealStates.waiting_for_edit)
async def process_meal_edit(message: Message, state: FSMContext):
    data = await state.get_data()
    old_meal = data.get("temp_meal")
    
    msg_wait = await message.answer("🔄 Пересчитываю...")
    prompt = f"Ранее было: {old_meal}. Уточнение: {message.text}. Пересчитай и верни новый JSON."
    
    try:
        response = model.generate_content(prompt)
        new_data = json.loads(re.sub(r'```json\s*|```', '', response.text).strip())
        await state.update_data(temp_meal=new_data)
        
        text = (f"🔄 **Обновлено: {new_data['name']}**\n"
                f"🔥 Калории: {new_data['calories']} ккал\n"
                f"🥩 Б: {new_data['protein']}г | 🥑 Ж: {new_data['fat']}г | 🌾 У: {new_data['carbs']}г\n\n"
                f"Теперь верно?")
        await msg_wait.edit_text(text, reply_markup=get_meal_inline_kb(), parse_mode="Markdown")
    except:
        await msg_wait.edit_text("❌ Ошибка пересчета.")

# (Остальной код main() без изменений)
