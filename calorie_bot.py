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

# Состояния для анкеты и еды
class ProfileStates(StatesGroup):
    waiting_for_gender = State()
    waiting_for_age = State()
    waiting_for_height = State()
    waiting_for_weight = State()
    waiting_for_activity = State()
    waiting_for_goal = State()

class MealStates(StatesGroup):
    waiting_for_edit = State()

# ===================== РАБОТА С БД =====================
def init_db():
    conn = psycopg2.connect(DATABASE_URL)
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id BIGINT PRIMARY KEY, 
                cal INT DEFAULT 2500, 
                prot INT DEFAULT 150, 
                fat INT DEFAULT 70, 
                carb INT DEFAULT 250, 
                target_waist REAL DEFAULT 85.0
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
                weight REAL, waist REAL, bench REAL DEFAULT 0.0
            )
        """)
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
        [KeyboardButton(text="Низкая (сидячая)"), KeyboardButton(text="Средняя (3 тр/нед)")],
        [KeyboardButton(text="Высокая (5+ тр/нед)")]
    ], resize_keyboard=True)

def get_goal_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Похудение"), KeyboardButton(text="Поддержание"), KeyboardButton(text="Набор массы")]
    ], resize_keyboard=True)

# ===================== ЛОГИКА АНКЕТЫ (НАСТРОЙКИ) =====================

@dp.message(F.text == "⚙️ Настройки")
@dp.message(Command("setup"))
async def start_profile(message: Message, state: FSMContext):
    await state.set_state(ProfileStates.waiting_for_gender)
    await message.answer("Давай настроим твой профиль, чтобы Gemini рассчитал норму калорий именно для тебя.\n\nУкажи твой пол:", reply_markup=get_gender_kb())

@dp.message(ProfileStates.waiting_for_gender)
async def process_gender(message: Message, state: FSMContext):
    await state.update_data(gender=message.text)
    await state.set_state(ProfileStates.waiting_for_age)
    await message.answer("Сколько тебе полных лет?", reply_markup=None)

@dp.message(ProfileStates.waiting_for_age)
async def process_age(message: Message, state: FSMContext):
    await state.update_data(age=message.text)
    await state.set_state(ProfileStates.waiting_for_height)
    await message.answer("Твой рост в см?")

@dp.message(ProfileStates.waiting_for_height)
async def process_height(message: Message, state: FSMContext):
    await state.update_data(height=message.text)
    await state.set_state(ProfileStates.waiting_for_weight)
    await message.answer("Твой текущий вес в кг?")

@dp.message(ProfileStates.waiting_for_weight)
async def process_weight(message: Message, state: FSMContext):
    await state.update_data(weight=message.text)
    await state.set_state(ProfileStates.waiting_for_activity)
    await message.answer("Твой уровень активности?", reply_markup=get_activity_kb())

@dp.message(ProfileStates.waiting_for_activity)
async def process_activity(message: Message, state: FSMContext):
    await state.update_data(activity=message.text)
    await state.set_state(ProfileStates.waiting_for_goal)
    await message.answer("Какая твоя цель?", reply_markup=get_goal_kb())

@dp.message(ProfileStates.waiting_for_goal)
async def process_goal(message: Message, state: FSMContext):
    user_data = await state.get_data()
    user_data['goal'] = message.text
    msg_calc = await message.answer("⏳ Gemini рассчитывает твою норму КБЖУ...", reply_markup=get_main_kb())
    
    prompt = (f"Рассчитай суточную норму КБЖУ по формуле Миффлина-Сан Жеора. "
              f"Данные: пол {user_data['gender']}, возраст {user_data['age']}, "
              f"рост {user_data['height']}, вес {user_data['weight']}, "
              f"активность {user_data['activity']}, цель {user_data['goal']}. "
              f"Верни ТОЛЬКО JSON: {{\"cal\": int, \"prot\": int, \"fat\": int, \"carb\": int}}")
    
    try:
        response = model.generate_content(prompt)
        match = re.search(r'\{.*\}', response.text, re.DOTALL)
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
        
        await msg_calc.edit_text(
            f"✅ Профиль настроен!\n\n**Твоя норма:**\n🔥 {res['cal']} ккал\n🥩 Белки: {res['prot']}г\n"
            f"🥑 Жиры: {res['fat']}г\n🌾 Углеводы: {res['carb']}г\n\nТеперь статистика будет учитывать эти цели.",
            parse_mode="Markdown"
        )
    except Exception as e:
        await msg_calc.edit_text(f"❌ Ошибка расчета. Использую стандартные настройки. (Детали: {e})")
    await state.clear()

# ===================== ОСТАЛЬНАЯ ЛОГИКА (БЕЗ ИЗМЕНЕНИЙ) =====================

@dp.message(Command("start"))
async def cmd_start(message: Message):
    init_db()
    await message.answer("💪 Бот запущен! Нажми 'Настройки', чтобы рассчитать свою норму.", reply_markup=get_main_kb())

@dp.message(F.text == "📊 Статистика")
async def show_stats(message: Message):
    conn = psycopg2.connect(DATABASE_URL)
    with conn.cursor() as cur:
        cur.execute("SELECT SUM(calories), SUM(protein), SUM(fat), SUM(carbs) FROM meals WHERE user_id=%s AND date=%s", (message.from_user.id, date.today()))
        food = cur.fetchone()
        c, p, f, carb = [round(x or 0) for x in food]
        cur.execute("SELECT weight, waist, bench FROM progress WHERE user_id=%s ORDER BY date DESC LIMIT 1", (message.from_user.id,))
        prog = cur.fetchone() or (0, 0, 0)
        cur.execute("SELECT cal, prot, fat, carb FROM user_settings WHERE user_id=%s", (message.from_user.id,))
        goal = cur.fetchone() or (2500, 150, 70, 250)
    conn.close()
    
    text = (f"📊 **Твой день:**\n\n🍎 **Еда:**\n🔥 Калории: {c} / {goal[0]} ккал\n🥩 Белки: {p} / {goal[1]} г\n"
            f"🥑 Жиры: {f} / {goal[2]} г\n🌾 Угли: {carb} / {goal[3]} г\n\n💪 **Замеры:**\n⚖️ Вес: {prog[0]}кг | 📏 Талия: {prog[1]}см | 🏋️ Жим: {prog[2]}кг")
    await message.answer(text, parse_mode="Markdown")

@dp.message(F.text == "⚖️ Замер (Вес/Жим)")
async def prompt_log(message: Message):
    await message.answer("Пришли: `Вес Талия Жим` (например: `95 90 85`)")

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
    msg_wait = await message.answer("🔍 Считаю...")
    prompt = "Оцени КБЖУ. Верни ТОЛЬКО JSON: {\"calories\": int, \"protein\": int, \"fat\": int, \"carbs\": int, \"name\": \"название\"}"
    try:
        contents = [prompt]
        if message.text: contents.append(message.text)
        if message.photo:
            file = await bot.get_file(message.photo[-1].file_id)
            img = await bot.download_file(file.file_path)
            contents.append({"mime_type": "image/jpeg", "data": img.read()})
        response = model.generate_content(contents)
        data = json.loads(re.search(r'\{.*\}', response.text, re.DOTALL).group(0))
        await state.update_data(temp_meal=data)
        await msg_wait.edit_text(f"🍴 **{data['name']}**\n🔥 {data['calories']} ккал | Б: {data['protein']}г | Ж: {data['fat']}г | У: {data['carbs']}г", 
                                 reply_markup=get_meal_inline_kb(), parse_mode="Markdown")
    except: await msg_wait.edit_text("❌ Ошибка анализа.")

def get_meal_inline_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ ОК", callback_data="meal_confirm"), InlineKeyboardButton(text="🗑 Отмена", callback_data="meal_cancel")]])

@dp.callback_query(F.data.startswith("meal_"))
async def process_meal_buttons(callback: CallbackQuery, state: FSMContext):
    if callback.data == "meal_confirm":
        data = await state.get_data()
        m = data.get("temp_meal")
        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO meals (user_id, date, meal_text, calories, protein, fat, carbs) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                       (callback.from_user.id, date.today(), m['name'], m['calories'], m['protein'], m['fat'], m['carbs']))
        conn.commit()
        conn.close()
        await callback.message.edit_text(f"✅ Записано!")
    else: await callback.message.edit_text("🗑 Отменено")
    await state.clear()

async def main():
    init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
