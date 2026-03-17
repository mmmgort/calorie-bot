import asyncio
import os
import json
import re
import psycopg2
import logging
from datetime import datetime, date
from io import BytesIO
from contextlib import suppress

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from google import genai
from google.genai import types

# Настройка логирования для Railway
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===================== КОНФИГУРАЦИЯ =====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

client = genai.Client(api_key=GEMINI_API_KEY)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

SYSTEM_INSTRUCTION = (
    "Ты — эксперт-нутрициолог. Твой JSON-ответ всегда: "
    "{\"calories\": int, \"protein\": int, \"fat\": int, \"carbs\": int, \"name\": \"название\", \"verdict\": \"совет\"}. "
    "Если на фото есть таблица БЖУ — бери данные из неё."
)

class ProfileStates(StatesGroup):
    gender, age, height, weight, activity, goal = [State() for _ in range(6)]

class MealStates(StatesGroup):
    editing = State()

class ProgressStates(StatesGroup):
    weight = State()

# ===================== БАЗА ДАННЫХ =====================
def init_db():
    conn = psycopg2.connect(DATABASE_URL)
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS user_settings (user_id BIGINT PRIMARY KEY, cal INT, prot INT, fat INT, carb INT)")
        cur.execute("CREATE TABLE IF NOT EXISTS meals (id SERIAL PRIMARY KEY, user_id BIGINT, date DATE, meal_text TEXT, calories REAL, protein REAL, fat REAL, carbs REAL)")
        cur.execute("CREATE TABLE IF NOT EXISTS progress (id SERIAL PRIMARY KEY, user_id BIGINT, date DATE, weight REAL)")
    conn.commit()
    conn.close()

async def safe_delete(message: Message):
    with suppress(TelegramBadRequest):
        await message.delete()

async def delete_bot_msg(state: FSMContext):
    data = await state.get_data()
    if msg_id := data.get("bot_msg_id"):
        with suppress(TelegramBadRequest):
            await bot.delete_message(chat_id=data.get("chat_id"), message_id=msg_id)

# ===================== КЛАВИАТУРЫ =====================
def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="⚙️ Настройки")],
            [KeyboardButton(text="⚖️ Замер (Вес)")]
        ], resize_keyboard=True
    )

def get_cancel_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Отмена")]], resize_keyboard=True)

# ===================== ПРИОРИТЕТНЫЕ ОБРАБОТЧИКИ =====================

@dp.message(Command("start"))
async def cmd_start(message: Message):
    init_db()
    await message.answer("🦾 Твой AI-нутрициолог готов к работе!", reply_markup=main_keyboard())

@dp.message(F.text == "❌ Отмена")
async def cancel_handler(message: Message, state: FSMContext):
    await safe_delete(message)
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=main_keyboard())

@dp.message(F.text == "📊 Статистика")
async def show_stats(message: Message):
    await safe_delete(message)
    conn = psycopg2.connect(DATABASE_URL)
    with conn.cursor() as cur:
        cur.execute("SELECT cal, prot, fat, carb FROM user_settings WHERE user_id = %s", (message.from_user.id,))
        goal = cur.fetchone()
        cur.execute("SELECT SUM(calories), SUM(protein), SUM(fat), SUM(carbs) FROM meals WHERE user_id = %s AND date = %s", (message.from_user.id, date.today()))
        eaten = cur.fetchone()
    conn.close()

    if not goal:
        await message.answer("Сначала пройди настройку в '⚙️ Настройки'!")
        return

    c, p, f, cr = [int(x or 0) for x in eaten]
    rem_c = goal[0] - c
    await message.answer(
        f"📅 *Сегодня:* {c}/{goal[0]} ккал\n"
        f"🍗 Б: {p}/{goal[1]}г | 🥑 Ж: {f}/{goal[2]}г | 🍞 У: {cr}/{goal[3]}г\n\n"
        f"Осталось: *{max(0, rem_c)}* ккал", parse_mode="Markdown"
    )

@dp.message(F.text == "⚖️ Замер (Вес)")
async def weight_start(message: Message, state: FSMContext):
    await safe_delete(message)
    await state.set_state(ProgressStates.weight)
    await message.answer("Введи свой вес (кг):", reply_markup=get_cancel_kb())

@dp.message(ProgressStates.weight)
async def weight_process(message: Message, state: FSMContext):
    try:
        w = float(message.text.replace(',', '.'))
        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO progress (user_id, date, weight) VALUES (%s, %s, %s)", (message.from_user.id, date.today(), w))
        conn.commit()
        conn.close()
        await message.answer(f"✅ Вес {w} кг записан!", reply_markup=main_keyboard())
        await state.clear()
    except:
        await message.answer("Введи число!")

# ===================== ПОЛНАЯ АНКЕТА =====================

@dp.message(F.text == "⚙️ Настройки")
async def start_settings(message: Message, state: FSMContext):
    await safe_delete(message)
    await state.set_state(ProfileStates.gender)
    msg = await message.answer("Твой пол (М/Ж)?", reply_markup=get_cancel_kb())
    await state.update_data(bot_msg_id=msg.message_id, chat_id=message.chat.id)

@dp.message(ProfileStates.gender)
async def process_gender(message: Message, state: FSMContext):
    await safe_delete(message)
    await delete_bot_msg(state)
    await state.update_data(gender=message.text)
    await state.set_state(ProfileStates.age)
    msg = await message.answer("Твой возраст?")
    await state.update_data(bot_msg_id=msg.message_id)

@dp.message(ProfileStates.age)
async def process_age(message: Message, state: FSMContext):
    await safe_delete(message)
    await delete_bot_msg(state)
    await state.update_data(age=message.text)
    await state.set_state(ProfileStates.height)
    msg = await message.answer("Твой рост (см)?")
    await state.update_data(bot_msg_id=msg.message_id)

@dp.message(ProfileStates.height)
async def process_height(message: Message, state: FSMContext):
    await safe_delete(message)
    await delete_bot_msg(state)
    await state.update_data(height=message.text)
    await state.set_state(ProfileStates.weight)
    msg = await message.answer("Твой вес (кг)?")
    await state.update_data(bot_msg_id=msg.message_id)

@dp.message(ProfileStates.weight)
async def process_weight(message: Message, state: FSMContext):
    await safe_delete(message)
    await delete_bot_msg(state)
    await state.update_data(weight=message.text)
    await state.set_state(ProfileStates.activity)
    msg = await message.answer("Активность (Низкая/Средняя/Высокая)?")
    await state.update_data(bot_msg_id=msg.message_id)

@dp.message(ProfileStates.activity)
async def process_activity(message: Message, state: FSMContext):
    await safe_delete(message)
    await delete_bot_msg(state)
    await state.update_data(activity=message.text)
    await state.set_state(ProfileStates.goal)
    msg = await message.answer("Твоя цель (Похудение/Набор/Поддержание)?")
    await state.update_data(bot_msg_id=msg.message_id)

@dp.message(ProfileStates.goal)
async def process_goal(message: Message, state: FSMContext):
    await safe_delete(message)
    await delete_bot_msg(state)
    data = await state.get_data()
    msg_wait = await message.answer("⚡ Gemini рассчитывает нормы...")
    try:
        resp = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=f"Рассчитай суточную норму КБЖУ: пол {data['gender']}, возраст {data['age']}, рост {data['height']}, вес {data['weight']}, цель {message.text}.",
            config=types.GenerateContentConfig(system_instruction="Выдай строго JSON: calories, protein, fat, carbs")
        )
        res = json.loads(re.search(r'\{.*\}', resp.text, re.DOTALL).group(0))
        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO user_settings VALUES (%s,%s,%s,%s,%s) ON CONFLICT (user_id) DO UPDATE SET cal=EXCLUDED.cal, prot=EXCLUDED.prot, fat=EXCLUDED.fat, carb=EXCLUDED.carb",
                       (message.from_user.id, res['calories'], res['protein'], res['fat'], res['carbs']))
        conn.commit()
        conn.close()
        await msg_wait.edit_text(f"✅ Нормы установлены! Цель: {res['calories']} ккал.", reply_markup=main_keyboard())
    except Exception as e:
        await msg_wait.edit_text(f"Ошибка: {e}")
    await state.clear()

# ===================== АНАЛИЗ ЕДЫ И ИЗМЕНЕНИЕ =====================

async def get_gemini_res(parts):
    response = client.models.generate_content(model='gemini-2.5-flash', contents=parts, config=types.GenerateContentConfig(system_instruction=SYSTEM_INSTRUCTION, temperature=0.1))
    match = re.search(r'\{.*\}', response.text, re.DOTALL)
    return json.loads(match.group(0)) if match else None

@dp.message(F.photo | F.text)
async def handle_meal(message: Message, state: FSMContext):
    if message.text in ["📊 Статистика", "⚙️ Настройки", "⚖️ Замер (Вес)"] or (message.text and message.text.startswith('/')):
        return
    msg_wait = await message.answer("🔍 Распознаю...")
    parts = []
    if message.photo:
        file = await bot.get_file(message.photo[-1].file_id)
        buf = BytesIO()
        await bot.download_file(file.file_path, buf)
        photo = buf.getvalue()
        parts.append(types.Part.from_bytes(data=photo, mime_type="image/jpeg"))
        await state.update_data(last_photo=photo)
    else:
        parts.append(f"Еда: {message.text}")
        await state.update_data(last_photo=None)
    
    data = await get_gemini_res(parts)
    if data:
        await state.update_data(temp_meal=data)
        await safe_delete(message)
        await msg_wait.edit_text(
            f"🍴 *{data['name']}*\n🔥 {data['calories']} ккал | Б:{data['protein']}г\n\nЗаписать?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Записать", callback_data="meal_confirm")],
                [InlineKeyboardButton(text="✏️ Изменить", callback_data="meal_edit")],
                [InlineKeyboardButton(text="🗑 Удалить", callback_data="meal_cancel")]
            ]), parse_mode="Markdown"
        )
    else:
        await msg_wait.edit_text("❌ Не удалось распознать.")

@dp.callback_query(F.data == "meal_edit")
async def meal_edit_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Что исправить? (Напр: 'тут 200г')")
    await state.set_state(MealStates.editing)
    await callback.answer()

@dp.message(MealStates.editing)
async def meal_edit_process(message: Message, state: FSMContext):
    data = await state.get_data()
    msg_wait = await message.answer("🔄 Пересчитываю...")
    parts = []
    if data.get("last_photo"):
        parts.append(types.Part.from_bytes(data=data["last_photo"], mime_type="image/jpeg"))
    parts.append(f"Уточнение: {message.text}. Пересчитай: {data.get('temp_meal')}")
    new_data = await get_gemini_res(parts)
    if new_data:
        await state.update_data(temp_meal=new_data)
        await msg_wait.edit_text(
            f"🍴 *{new_data['name']} (Правка)*\n🔥 {new_data['calories']} ккал | Б:{new_data['protein']}г\n\nЗаписать?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Записать", callback_data="meal_confirm")],
                [InlineKeyboardButton(text="✏️ Изменить снова", callback_data="meal_edit")]
            ]), parse_mode="Markdown"
        )
    await state.set_state(None)

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
            # Рекомендации
            conn = psycopg2.connect(DATABASE_URL)
            with conn.cursor() as cur:
                cur.execute("SELECT cal FROM user_settings WHERE user_id = %s", (callback.from_user.id,))
                goal = cur.fetchone()
                cur.execute("SELECT SUM(calories) FROM meals WHERE user_id = %s AND date = %s", (callback.from_user.id, date.today()))
                eaten = cur.fetchone()
            conn.close()
            rem = goal[0] - eaten[0] if goal and eaten else 0
            if rem > 200:
                resp = client.models.generate_content(model='gemini-2.5-flash', contents=f"Посоветуй 3 перекуса на {rem} ккал.")
                await callback.message.answer(f"💡 *Чем добить норму сегодня:*\n\n{resp.text}", parse_mode="Markdown")
    else:
        await safe_delete(callback.message)
    await state.clear()

# ===================== НАПОМИНАНИЯ =====================
async def send_reminder():
    conn = psycopg2.connect(DATABASE_URL)
    with conn.cursor() as cur:
        cur.execute("SELECT user_id FROM user_settings")
        users = cur.fetchall()
    conn.close()
    for user in users:
        with suppress(Exception): await bot.send_message(user[0], "🔔 Пора записать прием пищи!")

async def main():
    init_db()
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(send_reminder, 'cron', hour='9,14,19', minute=0)
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
