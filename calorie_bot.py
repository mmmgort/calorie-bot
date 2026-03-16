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

# ===================== КОНФИГУРАЦИЯ =====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-pro')

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

class ProfileStates(StatesGroup):
    waiting_for_gender = State()
    waiting_for_age = State()
    waiting_for_height = State()
    waiting_for_weight = State()
    waiting_for_activity = State()
    waiting_for_goal = State()

class MealStates(StatesGroup):
    waiting_for_edit = State()

# ===================== БЕЗОПАСНАЯ ИНИЦИАЛИЗАЦИЯ БД =====================
def init_db():
    conn = psycopg2.connect(DATABASE_URL)
    with conn.cursor() as cur:
        # Создаем таблицы, если их нет
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id BIGINT PRIMARY KEY, 
                cal INT DEFAULT 2500, prot INT DEFAULT 160, 
                fat INT DEFAULT 70, carb INT DEFAULT 280
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS meals (
                id SERIAL PRIMARY KEY, user_id BIGINT, date DATE, 
                meal_text TEXT, calories REAL, protein REAL, fat REAL, carbs REAL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS progress (
                id SERIAL PRIMARY KEY, user_id BIGINT, date DATE, 
                weight REAL, waist REAL
            )
        """)
        
        # ПРОВЕРКА И ДОБАВЛЕНИЕ КОЛОНОК (чтобы не падал запуск)
        # Для progress
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='progress' AND column_name='bench'")
        if not cur.fetchone():
            cur.execute("ALTER TABLE progress ADD COLUMN bench REAL DEFAULT 0.0")
            
        # Для user_settings (на случай если не хватает target_waist)
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='user_settings' AND column_name='target_waist'")
        if not cur.fetchone():
            cur.execute("ALTER TABLE user_settings ADD COLUMN target_waist REAL DEFAULT 85.0")

    conn.commit()
    conn.close()

# ===================== КЛАВИАТУРЫ =====================
def get_main_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🍲 Добавить еду"), KeyboardButton(text="⚖️ Замер (Вес/Жим)")],
        [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="⚙️ Настройки")]
    ], resize_keyboard=True)

def get_gender_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Мужчина"), KeyboardButton(text="Женщина")]], resize_keyboard=True)

def get_activity_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Низкая"), KeyboardButton(text="Средняя"), KeyboardButton(text="Высокая")]
    ], resize_keyboard=True)

def get_goal_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Похудение"), KeyboardButton(text="Поддержание"), KeyboardButton(text="Набор массы")]
    ], resize_keyboard=True)

# ===================== ОБРАБОТЧИКИ =====================

@dp.message(Command("start"))
async def cmd_start(message: Message):
    # Принудительно создаем запись в настройках, чтобы статистика не падала
    conn = psycopg2.connect(DATABASE_URL)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO user_settings (user_id) VALUES (%s) ON CONFLICT DO NOTHING", (message.from_user.id,))
    conn.commit()
    conn.close()
    await message.answer("💪 Бот запущен и готов к работе!", reply_markup=get_main_kb())

@dp.message(F.text == "⚙️ Настройки")
async def start_setup(message: Message, state: FSMContext):
    await state.set_state(ProfileStates.waiting_for_gender)
    await message.answer("Твой пол?", reply_markup=get_gender_kb())

@dp.message(ProfileStates.waiting_for_gender)
async def set_gender(message: Message, state: FSMContext):
    await state.update_data(gender=message.text)
    await state.set_state(ProfileStates.waiting_for_age)
    await message.answer("Возраст?", reply_markup=None)

@dp.message(ProfileStates.waiting_for_age)
async def set_age(message: Message, state: FSMContext):
    await state.update_data(age=message.text)
    await state.set_state(ProfileStates.waiting_for_height)
    await message.answer("Рост (см)?")

@dp.message(ProfileStates.waiting_for_height)
async def set_height(message: Message, state: FSMContext):
    await state.update_data(height=message.text)
    await state.set_state(ProfileStates.waiting_for_weight)
    await message.answer("Вес (кг)?")

@dp.message(ProfileStates.waiting_for_weight)
async def set_weight(message: Message, state: FSMContext):
    await state.update_data(weight=message.text)
    await state.set_state(ProfileStates.waiting_for_activity)
    await message.answer("Активность?", reply_markup=get_activity_kb())

@dp.message(ProfileStates.waiting_for_activity)
async def set_activity(message: Message, state: FSMContext):
    await state.update_data(activity=message.text)
    await state.set_state(ProfileStates.waiting_for_goal)
    await message.answer("Цель?", reply_markup=get_goal_kb())

@dp.message(ProfileStates.waiting_for_goal)
async def finish_setup(message: Message, state: FSMContext):
    u = await state.get_data()
    u['goal'] = message.text
    msg = await message.answer("🌀 Gemini считает КБЖУ...", reply_markup=get_main_kb())
    
    prompt = (f"Рассчитай суточную норму КБЖУ для: пол {u['gender']}, {u['age']} лет, {u['height']}см, {u['weight']}кг, активность {u['activity']}, цель {u['goal']}. "
              f"Верни ТОЛЬКО JSON: {{\"cal\": int, \"prot\": int, \"fat\": int, \"carb\": int}}")
    try:
        response = model.generate_content(prompt)
        match = re.search(r'\{.*\}', response.text, re.DOTALL)
        if match:
            res = json.loads(match.group(0))
            conn = psycopg2.connect(DATABASE_URL)
            with conn.cursor() as cur:
                cur.execute("UPDATE user_settings SET cal=%s, prot=%s, fat=%s, carb=%s WHERE user_id=%s",
                           (res['cal'], res['prot'], res['fat'], res['carb'], message.from_user.id))
            conn.commit()
            conn.close()
            await msg.edit_text(f"✅ Готово! Норма: {res['cal']} ккал.")
        else: raise ValueError()
    except:
        await msg.edit_text("❌ Ошибка расчета. Попробуй позже.")
    await state.clear()

@dp.message(F.text == "📊 Статистика")
async def show_stats(message: Message):
    conn = psycopg2.connect(DATABASE_URL)
    with conn.cursor() as cur:
        cur.execute("SELECT SUM(calories), SUM(protein), SUM(fat), SUM(carbs) FROM meals WHERE user_id=%s AND date=%s", (message.from_user.id, date.today()))
        food = cur.fetchone() or (0,0,0,0)
        c, p, f, carb = [round(x or 0) for x in food]
        cur.execute("SELECT cal, prot, fat, carb FROM user_settings WHERE user_id=%s", (message.from_user.id,))
        goal = cur.fetchone() or (2500, 160, 70, 280)
        cur.execute("SELECT weight, waist, bench FROM progress WHERE user_id=%s ORDER BY date DESC LIMIT 1", (message.from_user.id,))
        prog = cur.fetchone() or (0, 0, 0)
    conn.close()
    await message.answer(f"📊 **Сегодня:**\n🔥 {c}/{goal[0]} ккал\nБ: {p}/{goal[1]} | Ж: {f}/{goal[2]} | У: {carb}/{goal[3]}\n\n⚖️ {prog[0]}кг | 📏 {prog[1]}см | 🏋️ {prog[2]}кг")

@dp.message(F.text == "⚖️ Замер (Вес/Жим)")
async def prompt_log(message: Message):
    await message.answer("Пришли: `Вес Талия Жим` (например: `90 85 80`)")

@dp.message(F.text.regexp(r"^(\d+\.?\d*)\s+(\d+\.?\d*)\s+(\d+\.?\d*)$"))
async def log_numbers(message: Message):
    nums = re.findall(r"\d+\.?\d*", message.text)
    conn = psycopg2.connect(DATABASE_URL)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO progress (user_id, date, weight, waist, bench) VALUES (%s,%s,%s,%s,%s)", 
                    (message.from_user.id, date.today(), float(nums[0]), float(nums[1]), float(nums[2])))
    conn.commit()
    conn.close()
    await message.answer("✅ Записано!")

@dp.message(F.photo | F.text)
async def handle_meal(message: Message, state: FSMContext):
    if message.text in ["🍲 Добавить еду", "⚖️ Замер (Вес/Жим)", "📊 Статистика", "⚙️ Настройки"] or (message.text and message.text.startswith('/')): return
    msg = await message.answer("🔍 Считаю...")
    try:
        contents = ["Оцени КБЖУ. JSON: {\"calories\": int, \"protein\": int, \"fat\": int, \"carbs\": int, \"name\": \"название\"}"]
        if message.text: contents.append(message.text)
        if message.photo:
            file = await bot.get_file(message.photo[-1].file_id)
            img = await bot.download_file(file.file_path)
            contents.append({"mime_type": "image/jpeg", "data": img.read()})
        response = model.generate_content(contents)
        data = json.loads(re.search(r'\{.*\}', response.text, re.DOTALL).group(0))
        await state.update_data(temp_meal=data)
        await msg.edit_text(f"🍴 {data['name']}\n🔥 {data['calories']} ккал", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Да", callback_data="meal_confirm"), InlineKeyboardButton(text="🗑 Нет", callback_data="meal_cancel")]]))
    except: await msg.edit_text("❌ Не распознал еду.")

@dp.callback_query(F.data.startswith("meal_"))
async def meal_callback(callback: CallbackQuery, state: FSMContext):
    if callback.data == "meal_confirm":
        d = (await state.get_data()).get("temp_meal")
        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO meals (user_id, date, meal_text, calories, protein, fat, carbs) VALUES (%s,%s,%s,%s,%s,%s,%s)", (callback.from_user.id, date.today(), d['name'], d['calories'], d['protein'], d['fat'], d['carbs']))
        conn.commit()
        conn.close()
        await callback.message.edit_text("✅ Записано")
    else: await callback.message.edit_text("🗑 Отменено")
    await state.clear()

async def main():
    init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
