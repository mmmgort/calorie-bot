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

# СИСТЕМНАЯ ИНСТРУКЦИЯ (Для Gemini 2.5 Flash)
SYSTEM_INSTRUCTION = (
    "Ты — профессиональный нутрициолог. Анализируй фото или текст. "
    "Твоя задача — выдать точный КБЖУ. Оценивай порции по столовым приборам на фото. "
    "Ответ СТРОГО в формате JSON на русском языке: "
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
    await message.answer("🦾 AI-тренер на базе Gemini 2.5 готов. Нажми **'Настройки'** для калибровки.", 
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
    await message.answer("Уровень активности (Низкий/Средний/Высокий)?", reply_markup=get_cancel_kb())

@dp.message(ProfileStates.activity)
async def process_activity(message: Message, state: FSMContext):
    await state.update_data(activity=message.text)
    await state.set_state(ProfileStates.goal)
    await message.answer("Твоя цель (Похудение/Набор/Поддержание)?", reply_markup=get_cancel_kb())

@dp.message(ProfileStates.goal)
async def process_goal(message: Message, state: FSMContext):
    await state.update_data(goal=message.text)
    data = await state.get_data()
    msg_wait = await message.answer("⚡ Gemini 2.5 рассчитывает нормы...")
    
    try:
        prompt = (f"Рассчитай суточную норму КБЖУ: пол {data['gender']}, возраст {data['age']}, "
                  f"рост {data['height']}, вес {data['weight']}, цель {data['goal']}.")
        
        # ВОЗВРАЩАЕМ GEMINI 2.5 FLASH!
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(system_instruction="Выдай строго JSON: calories, protein, fat, carbs")
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
        await message.answer(f"✅ Нормы установлены! Цель: {result['calories']} ккал.", reply_markup=main_keyboard())
    except Exception as e:
        if "429" in str(e):
            await message.answer("⏳ Лимит запросов ИИ. Подожди 30 секунд и попробуй снова.")
        else:
            await message.answer(f"❌ Ошибка: {e}")

# ===================== АНАЛИЗ ЕДЫ (Gemini 2.5 Flash) =====================

@dp.message(F.photo | F.text)
async def handle_meal(message: Message, state: FSMContext):
    if message.text in ["📊 Статистика", "⚙️ Настройки", "⚖️ Замер (Вес/Жим)"] or (message.text and message.text.startswith('/')):
        return

    msg_wait = await message.answer("🔍 Анализ через Gemini 2.5...")
    
    try:
        content_parts = []
        if message.photo:
            file_id = message.photo[-1].file_id
            file = await bot.get_file(file_id)
            buffer = BytesIO()
            await bot.download_file(file.file_path, buffer)
            
            content_parts.append(types.Part.from_bytes(data=buffer.getvalue(), mime_type="image/jpeg"))
            content_parts.append(f"Проанализируй фото. Комментарий: {message.caption or 'нет'}")
        else:
            content_parts.append(f"Проанализируй: {message.text}")

        # ВОЗВРАЩАЕМ GEMINI 2.5 FLASH!
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=content_parts,
            config=types.GenerateContentConfig(system_instruction=SYSTEM_INSTRUCTION, temperature=0.1)
        )
        
        match = re.search(r'\{.*\}', response.text, re.DOTALL)
        if not match:
            raise ValueError("ИИ не прислал JSON")

        data = json.loads(match.group(0))
        await state.update_data(temp_meal=data)
        
        await msg_wait.delete()
        await message.answer(
            f"🍴 *{data['name']}*\n🔥 {data['calories']} ккал | Б:{data['protein']} Ж:{data['fat']} У:{data['carbs']}\n\nЗаписать?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Да", callback_data="meal_confirm"),
                InlineKeyboardButton(text="🗑 Нет", callback_data="meal_cancel")
            ]]), parse_mode="Markdown"
        )
    except Exception as e:
        if "429" in str(e):
            await msg_wait.edit_text("⏳ Слишком много запросов. Подожди 30 сек.")
        else:
            print(f"Ошибка: {e}")
            await msg_wait.edit_text("❌ Не удалось распознать. Опиши еду текстом.")

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
    try:
        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor() as cur:
            cur.execute("SELECT cal, prot, fat, carb FROM user_settings WHERE user_id = %s", (message.from_user.id,))
            goal = cur.fetchone()
            cur.execute("SELECT SUM(calories), SUM(protein), SUM(fat), SUM(carbs) FROM meals WHERE user_id = %s AND date = %s", 
                       (message.from_user.id, date.today()))
            eaten = cur.fetchone()
        conn.close()

        if not goal:
            await message.answer("Сначала пройди '⚙️ Настройки'!")
            return

        c, p, f, cr = [int(x or 0) for x in eaten]
        await message.answer(
            f"📅 *Сегодня ({date.today()}):*\n\n"
            f"🔥 Калории: {c} / {goal[0]}\n"
            f"🍗 Белки: {p}/{goal[1]}г | 🥑 Жиры: {f}/{goal[2]}г | 🍞 Углеводы: {cr}/{goal[3]}г",
            parse_mode="Markdown"
        )
    except Exception as e:
        await message.answer("Ошибка при получении статистики.")

async def main():
    init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
    
