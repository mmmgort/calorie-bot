import asyncio
import os
import json
import re
import psycopg2
from datetime import datetime, date
from io import BytesIO
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from google import genai
from google.genai import types

# ===================== КОНФИГУРАЦИЯ =====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

client = genai.Client(api_key=GEMINI_API_KEY)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# СИСТЕМНАЯ ИНСТРУКЦИЯ
SYSTEM_INSTRUCTION = (
    "Ты — эксперт-нутрициолог. Анализируй фото или текст. "
    "Ответ СТРОГО в JSON на русском: "
    "{\"calories\": int, \"protein\": int, \"fat\": int, \"carbs\": int, \"name\": \"название\", \"verdict\": \"совет\"}"
)

class ProfileStates(StatesGroup):
    gender = State()
    age = State()
    height = State()
    weight = State()
    activity = State()
    goal = State()

# ===================== БАЗА ДАННЫХ =====================
def init_db():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id BIGINT PRIMARY KEY, 
                    cal INT, prot INT, fat INT, carb INT
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
    except Exception as e:
        print(f"Ошибка БД: {e}")

# ===================== КЛАВИАТУРЫ =====================
def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="⚙️ Настройки")],
            [KeyboardButton(text="⚖️ Замер (Вес/Жим)")]
        ],
        resize_keyboard=True
    )

def get_cancel_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True
    )

# ===================== ОБРАБОТЧИКИ АНКЕТЫ =====================

@dp.message(Command("start"))
async def cmd_start(message: Message):
    init_db()
    await message.answer("💪 Бот готов! Нажми **'Настройки'**.", 
                         reply_markup=main_keyboard(), parse_mode="Markdown")

@dp.message(F.text == "❌ Отмена")
async def cancel_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_keyboard())

@dp.message(F.text == "⚙️ Настройки")
async def start_settings(message: Message, state: FSMContext):
    await state.set_state(ProfileStates.gender)
    await message.answer("Твой пол (М/Ж)?", reply_markup=get_cancel_kb())

@dp.message(ProfileStates.gender)
async def process_gender(message: Message, state: FSMContext):
    await state.update_data(gender=message.text)
    await state.set_state(ProfileStates.age)
    await message.answer("Сколько тебе лет?", reply_markup=get_cancel_kb())

@dp.message(ProfileStates.age)
async def process_age(message: Message, state: FSMContext):
    await state.update_data(age=message.text)
    await state.set_state(ProfileStates.height)
    await message.answer("Твой рост (см)?", reply_markup=get_cancel_kb())

@dp.message(ProfileStates.height)
async def process_height(message: Message, state: FSMContext):
    await state.update_data(height=message.text)
    await state.set_state(ProfileStates.weight)
    await message.answer("Твой вес (кг)?", reply_markup=get_cancel_kb())

@dp.message(ProfileStates.weight)
async def process_weight(message: Message, state: FSMContext):
    await state.update_data(weight=message.text)
    await state.set_state(ProfileStates.activity)
    await message.answer("Активность (Низкая/Средняя/Высокая)?", reply_markup=get_cancel_kb())

@dp.message(ProfileStates.activity)
async def process_activity(message: Message, state: FSMContext):
    await state.update_data(activity=message.text)
    await state.set_state(ProfileStates.goal)
    await message.answer("Твоя цель (Похудение/Набор/Поддержание)?", reply_markup=get_cancel_kb())

@dp.message(ProfileStates.goal)
async def process_goal(message: Message, state: FSMContext):
    await state.update_data(goal=message.text)
    data = await state.get_data()
    msg_wait = await message.answer("🔄 Считаю...")
    
    try:
        prompt = f"Рассчитай КБЖУ: пол {data['gender']}, возраст {data['age']}, рост {data['height']}, вес {data['weight']}, активность {data['activity']}, цель {data['goal']}."
        response = client.models.generate_content(
            model='gemini-1.5-flash', # Используем 1.5 для больших лимитов
            contents=prompt,
            config=types.GenerateContentConfig(system_instruction="Return ONLY JSON.")
        )
        result = json.loads(re.search(r'\{.*\}', response.text, re.DOTALL).group(0))
        
        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_settings (user_id, cal, prot, fat, carb) VALUES (%s, %s, %s, %s, %s) 
                ON CONFLICT (user_id) DO UPDATE SET cal=EXCLUDED.cal, prot=EXCLUDED.prot, fat=EXCLUDED.fat, carb=EXCLUDED.carb
            """, (message.from_user.id, result['calories'], result['protein'], result['fat'], result['carbs']))
        conn.commit()
        conn.close()

        await state.clear()
        await msg_wait.delete()
        await message.answer(f"✅ Готово! Норма: {result['calories']} ккал.", reply_markup=main_keyboard())
    except Exception as e:
        if "429" in str(e):
            await message.answer("⏳ Лимит запросов ИИ исчерпан. Подожди 1 минуту.")
        else:
            await message.answer(f" Ошибка: {e}")

# ===================== АНАЛИЗ ЕДЫ =====================

@dp.message(F.photo | F.text)
async def handle_meal(message: Message, state: FSMContext):
    if message.text in ["📊 Статистика", "⚙️ Настройки", "⚖️ Замер (Вес/Жим)"] or (message.text and message.text.startswith('/')):
        return

    msg_wait = await message.answer("🔍 Анализ...")
    
    try:
        content_parts = []
        if message.photo:
            file_id = message.photo[-1].file_id
            file = await bot.get_file(file_id)
            buffer = BytesIO()
            await bot.download_file(file.file_path, buffer)
            content_parts.append(types.Part.from_bytes(data=buffer.getvalue(), mime_type="image/jpeg"))
            content_parts.append(f"Еда. Описание: {message.caption or 'нет'}")
        else:
            content_parts.append(f"Еда: {message.text}")

        response = client.models.generate_content(
            model='gemini-1.5-flash', 
            contents=content_parts,
            config=types.GenerateContentConfig(system_instruction=SYSTEM_INSTRUCTION, temperature=0.1)
        )
        
        match = re.search(r'\{.*\}', response.text, re.DOTALL)
        data = json.loads(match.group(0))
        await state.update_data(temp_meal=data)
        
        await msg_wait.delete()
        await message.answer(
            f"🍴 *{data['name']}*\n🔥 {data['calories']} ккал | Б:{data['protein']}г\n\nЗаписать?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Да", callback_data="meal_confirm"),
                InlineKeyboardButton(text="🗑 Нет", callback_data="meal_cancel")
            ]]), parse_mode="Markdown"
        )
    except Exception as e:
        if "429" in str(e):
            await msg_wait.edit_text("⏳ Подожди немного, ИИ отдыхает (лимит запросов).")
        else:
            await msg_wait.edit_text("❌ Опиши еду текстом.")

@dp.callback_query(F.data.startswith("meal_"))
async def meal_callback(callback: CallbackQuery, state: FSMContext):
    if callback.data == "meal_confirm":
        data = await state.get_data()
        d = data.get("temp_meal")
        if d:
            conn = psycopg2.connect(DATABASE_URL)
            with conn.cursor() as cur:
                cur.execute("INSERT INTO meals (user_id, date, meal_text, calories, protein, fat, carbs) VALUES (%s,%s,%s,%s,%s,%s,%s)", 
                           (callback.from_user.id, date.today(), d['name'], d['calories'], d['protein'], d['fat'], d['carbs']))
            conn.commit()
            conn.close()
            await callback.message.edit_text(f"✅ Записано: {d['name']}")
    else:
        await callback.message.edit_text("🗑 Отменено")
    await state.clear()

@dp.message(F.text == "📊 Статистика")
async def show_stats(message: Message):
    conn = psycopg2.connect(DATABASE_URL)
    with conn.cursor() as cur:
        cur.execute("SELECT cal, prot, fat, carb FROM user_settings WHERE user_id = %s", (message.from_user.id,))
        goal = cur.fetchone()
        cur.execute("SELECT SUM(calories), SUM(protein), SUM(fat), SUM(carbs) FROM meals WHERE user_id = %s AND date = %s", 
                   (message.from_user.id, date.today()))
        eaten = cur.fetchone()
    conn.close()

    if not goal:
        await message.answer("Сначала настрой профиль!")
        return

    c, p, f, cr = [int(x or 0) for x in eaten]
    await message.answer(
        f"📅 *Сегодня ({date.today()}):*\n\n"
        f"🔥 Калории: {c} / {goal[0]}\n🍗 Белки: {p}/{goal[1]}г\n🥑 Жиры: {f}/{goal[2]}г\n🍞 Углеводы: {cr}/{goal[3]}г",
        parse_mode="Markdown"
    )

async def main():
    init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
    
