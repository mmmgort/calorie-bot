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
# Используем НОВУЮ библиотеку
from google import genai 

# ===================== КОНФИГУРАЦИЯ =====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

# Инициализация клиента Gemini (новый стандарт)
client = genai.Client(api_key=GEMINI_API_KEY)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

class ProfileStates(StatesGroup):
    waiting_for_gender = State()
    waiting_for_age = State()
    waiting_for_height = State()
    waiting_for_weight = State()
    waiting_for_activity = State()
    waiting_for_goal = State()

# ===================== БАЗА ДАННЫХ =====================
def init_db():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor() as cur:
            # Тщательно проверяем отступы здесь (строка 42)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id BIGINT PRIMARY KEY, 
                    cal INT DEFAULT 2000, prot INT DEFAULT 150, 
                    fat INT DEFAULT 70, carb INT DEFAULT 200
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS progress (
                    id SERIAL PRIMARY KEY, user_id BIGINT, date DATE, 
                    weight REAL, waist REAL, bench REAL DEFAULT 0.0
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS meals (
                    id SERIAL PRIMARY KEY, user_id BIGINT, date DATE, 
                    meal_text TEXT, calories REAL, protein REAL, fat REAL, carbs REAL
                )
            """)
        conn.commit()
        conn.close()
        print("База данных инициализирована успешно")
    except Exception as e:
        print(f"Ошибка БД: {e}")

# ===================== КЛАВИАТУРЫ =====================
def get_main_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🍲 Добавить еду"), KeyboardButton(text="📊 Статистика")],
        [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text="⚖️ Замер (Вес/Жим)")]
    ], resize_keyboard=True)

def get_gender_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Мужчина"), KeyboardButton(text="Женщина")]], resize_keyboard=True)

def get_activity_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Низкая (офис)"), KeyboardButton(text="Средняя (3-4 тр/нед)")],
        [KeyboardButton(text="Высокая (каждый день)")]
    ], resize_keyboard=True)

def get_goal_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Похудение"), KeyboardButton(text="Поддержание"), KeyboardButton(text="Набор массы")]
    ], resize_keyboard=True)

# ===================== ЛОГИКА АНКЕТЫ =====================

@dp.message(Command("start"))
async def cmd_start(message: Message):
    init_db()
    await message.answer("💪 Привет! Я твой AI-тренер. Нажми **'Настройки'** для начала.", reply_markup=get_main_kb(), parse_mode="Markdown")

@dp.message(F.text == "⚙️ Настройки")
async def start_setup(message: Message, state: FSMContext):
    await state.set_state(ProfileStates.waiting_for_gender)
    await message.answer("Укажи свой пол:", reply_markup=get_gender_kb())

@dp.message(ProfileStates.waiting_for_gender)
async def set_gender(message: Message, state: FSMContext):
    await state.update_data(gender=message.text)
    await state.set_state(ProfileStates.waiting_for_age)
    await message.answer("Сколько тебе лет?", reply_markup=None)

@dp.message(ProfileStates.waiting_for_age)
async def set_age(message: Message, state: FSMContext):
    await state.update_data(age=message.text)
    await state.set_state(ProfileStates.waiting_for_height)
    await message.answer("Твой рост (см)?")

@dp.message(ProfileStates.waiting_for_height)
async def set_height(message: Message, state: FSMContext):
    await state.update_data(height=message.text)
    await state.set_state(ProfileStates.waiting_for_weight)
    await message.answer("Твой вес (кг)?")

@dp.message(ProfileStates.waiting_for_weight)
async def set_weight(message: Message, state: FSMContext):
    await state.update_data(weight=message.text)
    await state.set_state(ProfileStates.waiting_for_activity)
    await message.answer("Уровень активности?", reply_markup=get_activity_kb())

@dp.message(ProfileStates.waiting_for_activity)
async def set_activity(message: Message, state: FSMContext):
    await state.update_data(activity=message.text)
    await state.set_state(ProfileStates.waiting_for_goal)
    await message.answer("Твоя цель?", reply_markup=get_goal_kb())

@dp.message(ProfileStates.waiting_for_goal)
async def finish_setup(message: Message, state: FSMContext):
    u = await state.get_data()
    u['goal'] = message.text
    msg_wait = await message.answer("🔄 Считаю КБЖУ через Gemini...", reply_markup=get_main_kb())
    
    prompt = (f"Calculate daily calories (Mifflin-St Jeor) and macros. "
              f"Data: Gender: {u['gender']}, Age: {u['age']}, Height: {u['height']}, Weight: {u['weight']}, "
              f"Activity: {u['activity']}, Goal: {u['goal']}. "
              f"Return ONLY JSON: {{\"cal\": int, \"prot\": int, \"fat\": int, \"carb\": int}}")
    
    try:
        # Новый вызов модели
        response = client.models.generate_content(model='gemini-1.5-flash', contents=prompt)
        res_text = response.text
        
        match = re.search(r'\{.*\}', res_text, re.DOTALL)
        if not match: raise ValueError("JSON не найден")
        
        res = json.loads(match.group(0))
        
        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_settings (user_id, cal, prot, fat, carb) 
                VALUES (%s, %s, %s, %s, %s) 
                ON CONFLICT (user_id) DO UPDATE SET cal=EXCLUDED.cal, prot=EXCLUDED.prot, fat=EXCLUDED.fat, carb=EXCLUDED.carb
            """, (message.from_user.id, res['cal'], res['prot'], res['fat'], res['carb']))
        conn.commit()
        conn.close()
        
        await msg_wait.edit_text(
            f"✅ Готово!\n🔥 {res['cal']} ккал\n"
            f"Б: {res['prot']}г | Ж: {res['fat']}г | У: {res['carb']}г"
        )
    except Exception as e:
        # Используем .answer вместо .edit_text для ошибок, чтобы избежать конфликтов Telegram
        await message.answer(f"❌ Ошибка расчета. Проверь API ключ или попробуй позже.")
        print(f"Ошибка Gemini: {e}")
    await state.clear()

# ===================== СТАТИСТИКА И ЗАМЕРЫ =====================

@dp.message(F.text == "📊 Статистика")
async def show_stats(message: Message):
    conn = psycopg2.connect(DATABASE_URL)
    with conn.cursor() as cur:
        cur.execute("SELECT SUM(calories), SUM(protein), SUM(fat), SUM(carbs) FROM meals WHERE user_id=%s AND date=%s", (message.from_user.id, date.today()))
        food = cur.fetchone() or (0,0,0,0)
        c, p, f, carb = [round(x or 0) for x in food]
        cur.execute("SELECT cal, prot, fat, carb FROM user_settings WHERE user_id=%s", (message.from_user.id,))
        goal = cur.fetchone() or (2000, 150, 70, 200)
    conn.close()
    await message.answer(f"📊 **Сегодня:**\n🔥 {c}/{goal[0]} ккал\nБ: {p}/{goal[1]}г | Ж: {f}/{goal[2]}г | У: {carb}/{goal[3]}г", parse_mode="Markdown")

@dp.message(F.text == "⚖️ Замер (Вес/Жим)")
async def prompt_log(message: Message):
    await message.answer("Введи через пробел: `Вес Талия Жим` (например: `95 90 85`)")

@dp.message(F.text.regexp(r"^(\d+\.?\d*)\s+(\d+\.?\d*)\s+(\d+\.?\d*)$"))
async def log_numbers(message: Message):
    nums = re.findall(r"\d+\.?\d*", message.text)
    conn = psycopg2.connect(DATABASE_URL)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO progress (user_id, date, weight, waist, bench) VALUES (%s,%s,%s,%s,%s)", 
                    (message.from_user.id, date.today(), float(nums[0]), float(nums[1]), float(nums[2])))
    conn.commit()
    conn.close()
    await message.answer("✅ Сохранено!")

# ===================== ОБРАБОТКА ЕДЫ =====================

@dp.message(F.text)
async def handle_meal(message: Message, state: FSMContext):
    if message.text in ["🍲 Добавить еду", "⚖️ Замер (Вес/Жим)", "📊 Статистика", "⚙️ Настройки"] or message.text.startswith('/'):
        return

    msg_wait = await message.answer("🔍 Анализирую...")
    try:
        prompt = f"Оцени КБЖУ еды: '{message.text}'. Верни ТОЛЬКО JSON: {{\"calories\": int, \"protein\": int, \"fat\": int, \"carbs\": int, \"name\": \"название\"}}"
        response = client.models.generate_content(model='gemini-1.5-flash', contents=prompt)
        
        match = re.search(r'\{.*\}', response.text, re.DOTALL)
        data = json.loads(match.group(0))
        await state.update_data(temp_meal=data)
        
        await msg_wait.edit_text(
            f"🍴 **{data['name']}**\n🔥 {data['calories']} ккал | Б: {data['protein']}г\nЗаписываем?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Да", callback_data="meal_confirm"),
                InlineKeyboardButton(text="🗑 Отмена", callback_data="meal_cancel")
            ]]), parse_mode="Markdown"
        )
    except Exception as e:
        await msg_wait.edit_text(f"❌ Не удалось распознать еду. Попробуй описать иначе.")

@dp.callback_query(F.data.startswith("meal_"))
async def meal_callback(callback: CallbackQuery, state: FSMContext):
    if callback.data == "meal_confirm":
        d = (await state.get_data()).get("temp_meal")
        if d:
            conn = psycopg2.connect(DATABASE_URL)
            with conn.cursor() as cur:
                cur.execute("INSERT INTO meals (user_id, date, meal_text, calories, protein, fat, carbs) VALUES (%s,%s,%s,%s,%s,%s,%s)", (callback.from_user.id, date.today(), d['name'], d['calories'], d['protein'], d['fat'], d['carbs']))
            conn.commit()
            conn.close()
            await callback.message.edit_text(f"✅ Записано: {d['name']}")
    else:
        await callback.message.edit_text("🗑 Отменено")
    await state.clear()

async def main():
    init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
