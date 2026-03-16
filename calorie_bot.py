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
                prot INT DEFAULT 160, 
                fat INT DEFAULT 70, 
                carb INT DEFAULT 280, 
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
        [KeyboardButton(text="Низкая (офис)"), KeyboardButton(text="Средняя (3-4 тр/нед)")],
        [KeyboardButton(text="Высокая (каждый день)")]
    ], resize_keyboard=True)

def get_goal_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Похудение"), KeyboardButton(text="Поддержание"), KeyboardButton(text="Набор массы")]
    ], resize_keyboard=True)

# ===================== ЛОГИКА АНКЕТЫ (НАСТРОЙКИ) =====================

@dp.message(F.text == "⚙️ Настройки")
@dp.message(Command("setup"))
async def start_setup(message: Message, state: FSMContext):
    await state.set_state(ProfileStates.waiting_for_gender)
    await message.answer("Давай настроим твой профиль для расчета КБЖУ. Укажи свой пол:", reply_markup=get_gender_kb())

@dp.message(ProfileStates.waiting_for_gender)
async def set_gender(message: Message, state: FSMContext):
    await state.update_data(gender=message.text)
    await state.set_state(ProfileStates.waiting_for_age)
    await message.answer("Твой возраст?", reply_markup=None)

@dp.message(ProfileStates.waiting_for_age)
async def set_age(message: Message, state: FSMContext):
    await state.update_data(age=message.text)
    await state.set_state(ProfileStates.waiting_for_height)
    await message.answer("Твой рост (см)?")

@dp.message(ProfileStates.waiting_for_height)
async def set_height(message: Message, state: FSMContext):
    await state.update_data(height=message.text)
    await state.set_state(ProfileStates.waiting_for_weight)
    await message.answer("Твой текущий вес (кг)?")

@dp.message(ProfileStates.waiting_for_weight)
async def set_weight(message: Message, state: FSMContext):
    await state.update_data(weight=message.text)
    await state.set_state(ProfileStates.waiting_for_activity)
    await message.answer("Уровень активности?", reply_markup=get_activity_kb())

@dp.message(ProfileStates.waiting_for_activity)
async def set_activity(message: Message, state: FSMContext):
    await state.update_data(activity=message.text)
    await state.set_state(ProfileStates.waiting_for_goal)
    await message.answer("Какая сейчас цель?", reply_markup=get_goal_kb())

@dp.message(ProfileStates.waiting_for_goal)
async def finish_setup(message: Message, state: FSMContext):
    u = await state.get_data()
    u['goal'] = message.text
    msg_wait = await message.answer("🌀 Gemini рассчитывает твою норму...", reply_markup=get_main_kb())
    
    prompt = (f"Рассчитай суточную норму КБЖУ. Пол: {u['gender']}, Возраст: {u['age']}, "
              f"Рост: {u['height']}, Вес: {u['weight']}, Активность: {u['activity']}, Цель: {u['goal']}. "
              f"Верни ТОЛЬКО JSON: {{\"cal\": int, \"prot\": int, \"fat\": int, \"carb\": int}}")
    
    try:
        response = model.generate_content(prompt)
        match = re.search(r'\{.*\}', response.text, re.DOTALL)
        
        # Защита от AttributeError (исправлено)
        if not match:
            raise ValueError("ИИ прислал текст без JSON. Попробуй еще раз.")
            
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
            f"✅ Профиль настроен!\n\n**Твоя норма:**\n🔥 {res['cal']} ккал | 🥩 Б: {res['prot']}г | 🥑 Ж: {res['fat']}г | 🌾 У: {res['carb']}г"
        )
    except Exception as e:
        await msg_wait.edit_text(f"❌ Ошибка расчета: {e}")
    await state.clear()

# ===================== СТАТИСТИКА И ЗАМЕРЫ =====================

@dp.message(F.text == "📊 Статистика")
async def show_stats(message: Message):
    conn = psycopg2.connect(DATABASE_URL)
    with conn.cursor() as cur:
        cur.execute("SELECT SUM(calories), SUM(protein), SUM(fat), SUM(carbs) FROM meals WHERE user_id=%s AND date=%s", (message.from_user.id, date.today()))
        food = cur.fetchone()
        c, p, f, carb = [round(x or 0) for x in food]
        cur.execute("SELECT cal, prot, fat, carb FROM user_settings WHERE user_id=%s", (message.from_user.id,))
        goal = cur.fetchone() or (2500, 160, 70, 280)
        cur.execute("SELECT weight, waist, bench FROM progress WHERE user_id=%s ORDER BY date DESC LIMIT 1", (message.from_user.id,))
        prog = cur.fetchone() or (0, 0, 0)
    conn.close()
    
    text = (f"📊 **Сегодня:**\n\n🍎 **Еда:**\n🔥 {c} / {goal[0]} ккал\n"
            f"Б: {p}/{goal[1]}г | Ж: {f}/{goal[2]}г | У: {carb}/{goal[3]}г\n\n"
            f"💪 **Замеры:**\n⚖️ Вес: {prog[0]} кг | 📏 Талия: {prog[1]} см | 🏋️ Жим: {prog[2]} кг")
    await message.answer(text, parse_mode="Markdown")

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
    await message.answer("✅ Замеры сохранены!")

# ===================== АНАЛИЗ ЕДЫ =====================

@dp.message(F.photo | F.text)
async def handle_meal(message: Message, state: FSMContext):
    if message.text in ["🍲 Добавить еду", "⚖️ Замер (Вес/Жим)", "📊 Статистика", "⚙️ Настройки"] or (message.text and message.text.startswith('/')):
        return

    msg_wait = await message.answer("🔍 Анализирую...")
    prompt = "Оцени КБЖУ. Верни ТОЛЬКО JSON: {\"calories\": int, \"protein\": int, \"fat\": int, \"carbs\": int, \"name\": \"название\"}"
    
    try:
        contents = [prompt]
        if message.text: contents.append(message.text)
        if message.photo:
            file = await bot.get_file(message.photo[-1].file_id)
            img = await bot.download_file(file.file_path)
            contents.append({"mime_type": "image/jpeg", "data": img.read()})
            
        response = model.generate_content(contents)
        match = re.search(r'\{.*\}', response.text, re.DOTALL)
        
        # Защита от AttributeError (исправлено)
        if not match:
            return await msg_wait.edit_text("❌ ИИ не смог распознать еду. Попробуй описать её текстом.")
            
        data = json.loads(match.group(0))
        await state.update_data(temp_meal=data)
        
        await msg_wait.edit_text(
            f"🍴 **{data['name']}**\n🔥 {data['calories']} ккал | Б: {data['protein']}г | Ж: {data['fat']}г | У: {data['carbs']}г\n\nЗаписываем?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Да", callback_data="meal_confirm"),
                InlineKeyboardButton(text="✏️ Изменить", callback_data="meal_edit"),
                InlineKeyboardButton(text="🗑 Отмена", callback_data="meal_cancel")
            ]]), parse_mode="Markdown"
        )
    except Exception as e:
        await msg_wait.edit_text(f"❌ Ошибка анализа.")

@dp.callback_query(F.data.startswith("meal_"))
async def meal_callback(callback: CallbackQuery, state: FSMContext):
    if callback.data == "meal_confirm":
        d = (await state.get_data()).get("temp_meal")
        if d:
            conn = psycopg2.connect(DATABASE_URL)
            with conn.cursor() as cur:
                cur.execute("INSERT INTO meals (user_id, date, meal_text, calories, protein, fat, carbs) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                           (callback.from_user.id, date.today(), d['name'], d['calories'], d['protein'], d['fat'], d['carbs']))
            conn.commit()
            conn.close()
            await callback.message.edit_text(f"✅ Записано: {d['name']}")
    elif callback.data == "meal_edit":
        await callback.message.answer("Что уточнить? (например: 'тут 300 грамм')")
        await state.set_state(MealStates.waiting_for_edit)
    else:
        await callback.message.edit_text("🗑 Отменено")
    await state.clear()

@dp.message(MealStates.waiting_for_edit)
async def edit_meal(message: Message, state: FSMContext):
    old = (await state.get_data()).get("temp_meal")
    msg = await message.answer("🔄 Пересчитываю...")
    try:
        response = model.generate_content(f"Было: {old}. Уточнение: {message.text}. Дай новый JSON КБЖУ.")
        match = re.search(r'\{.*\}', response.text, re.DOTALL)
        
        if not match:
            raise ValueError("No JSON")
            
        data = json.loads(match.group(0))
        await state.update_data(temp_meal=data)
        await msg.edit_text(f"🍴 **{data['name']}**\n🔥 {data['calories']} ккал | Б: {data['protein']}г",
                           reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                               InlineKeyboardButton(text="✅ ОК", callback_data="meal_confirm"),
                               InlineKeyboardButton(text="🗑 Отмена", callback_data="meal_cancel")
                           ]]))
    except:
        await msg.edit_text("❌ Не удалось пересчитать.")

# ===================== ЗАПУСК =====================
@dp.message(Command("start"))
async def cmd_start(message: Message):
    init_db()
    await message.answer("💪 Бот-тренер запущен! Нажми 'Настройки', чтобы задать свои цели.", reply_markup=get_main_kb())

async def main():
    init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
