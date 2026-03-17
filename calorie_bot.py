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

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# ===================== КОНФИГУРАЦИЯ =====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

client = genai.Client(api_key=GEMINI_API_KEY)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

SYSTEM_INSTRUCTION = (
    "Ты — профессиональный нутрициолог. Твоя задача — выдавать точный КБЖУ. "
    "Если на фото есть таблица КБЖУ с упаковки — используй данные с неё. "
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

class MealStates(StatesGroup):
    editing = State()

# ===================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====================
async def safe_delete(message: Message):
    with suppress(TelegramBadRequest):
        await message.delete()

async def delete_previous_bot_message(chat_id: int, state: FSMContext):
    data = await state.get_data()
    bot_msg_id = data.get("bot_msg_id")
    if bot_msg_id:
        with suppress(TelegramBadRequest):
            await bot.delete_message(chat_id, bot_msg_id)

def init_db():
    conn = psycopg2.connect(DATABASE_URL)
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS user_settings (user_id BIGINT PRIMARY KEY, cal INT, prot INT, fat INT, carb INT)")
        cur.execute("CREATE TABLE IF NOT EXISTS meals (id SERIAL PRIMARY KEY, user_id BIGINT, date DATE, meal_text TEXT, calories REAL, protein REAL, fat REAL, carbs REAL)")
    conn.commit()
    conn.close()

def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="⚙️ Настройки")],
            [KeyboardButton(text="⚖️ Замер (Вес/Жим)")]
        ],
        resize_keyboard=True
    )

def get_cancel_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Отмена")]], resize_keyboard=True)

# ===================== ОБРАБОТЧИКИ АНКЕТЫ =====================

@dp.message(Command("start"))
async def cmd_start(message: Message):
    init_db()
    await safe_delete(message)
    await message.answer("🦾 AI-тренер готов. Нажми **'Настройки'** для калибровки.", 
                         reply_markup=main_keyboard(), parse_mode="Markdown")

@dp.message(F.text == "❌ Отмена")
async def cancel_handler(message: Message, state: FSMContext):
    await safe_delete(message)
    await delete_previous_bot_message(message.chat.id, state)
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=main_keyboard())

@dp.message(F.text == "⚙️ Настройки")
async def start_settings(message: Message, state: FSMContext):
    await safe_delete(message)
    await state.set_state(ProfileStates.gender)
    msg = await message.answer("Твой пол (М/Ж)?", reply_markup=get_cancel_kb())
    await state.update_data(bot_msg_id=msg.message_id)

@dp.message(ProfileStates.gender)
async def process_gender(message: Message, state: FSMContext):
    await safe_delete(message)
    await delete_previous_bot_message(message.chat.id, state)
    await state.update_data(gender=message.text)
    await state.set_state(ProfileStates.age)
    msg = await message.answer("Сколько тебе лет?", reply_markup=get_cancel_kb())
    await state.update_data(bot_msg_id=msg.message_id)

@dp.message(ProfileStates.age)
async def process_age(message: Message, state: FSMContext):
    await safe_delete(message)
    await delete_previous_bot_message(message.chat.id, state)
    await state.update_data(age=message.text)
    await state.set_state(ProfileStates.height)
    msg = await message.answer("Твой рост (см)?", reply_markup=get_cancel_kb())
    await state.update_data(bot_msg_id=msg.message_id)

@dp.message(ProfileStates.height)
async def process_height(message: Message, state: FSMContext):
    await safe_delete(message)
    await delete_previous_bot_message(message.chat.id, state)
    await state.update_data(height=message.text)
    await state.set_state(ProfileStates.weight)
    msg = await message.answer("Твой вес (кг)?", reply_markup=get_cancel_kb())
    await state.update_data(bot_msg_id=msg.message_id)

@dp.message(ProfileStates.weight)
async def process_weight(message: Message, state: FSMContext):
    await safe_delete(message)
    await delete_previous_bot_message(message.chat.id, state)
    await state.update_data(weight=message.text)
    await state.set_state(ProfileStates.activity)
    msg = await message.answer("Активность (Низкая/Средняя/Высокая)?", reply_markup=get_cancel_kb())
    await state.update_data(bot_msg_id=msg.message_id)

@dp.message(ProfileStates.activity)
async def process_activity(message: Message, state: FSMContext):
    await safe_delete(message)
    await delete_previous_bot_message(message.chat.id, state)
    await state.update_data(activity=message.text)
    await state.set_state(ProfileStates.goal)
    msg = await message.answer("Твоя цель (Похудение/Набор/Поддержание)?", reply_markup=get_cancel_kb())
    await state.update_data(bot_msg_id=msg.message_id)

@dp.message(ProfileStates.goal)
async def process_goal(message: Message, state: FSMContext):
    await safe_delete(message)
    await delete_previous_bot_message(message.chat.id, state)
    await state.update_data(goal=message.text)
    data = await state.get_data()
    msg_wait = await message.answer("⚡ Gemini 2.5 рассчитывает нормы...")
    
    try:
        prompt = (f"Рассчитай суточную норму КБЖУ: пол {data['gender']}, возраст {data['age']}, "
                  f"рост {data['height']}, вес {data['weight']}, цель {data['goal']}.")
        
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
        await msg_wait.edit_text(f"✅ Нормы установлены! Цель: {result['calories']} ккал.")
        await message.answer("Меню активировано:", reply_markup=main_keyboard())
    except Exception as e:
        await msg_wait.edit_text(f"❌ Ошибка: {e}")
        await state.clear()

# ===================== ЛОГИКА ЕДЫ И РЕКОМЕНДАЦИЙ =====================

async def get_gemini_analysis(content_parts):
    response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=content_parts,
        config=types.GenerateContentConfig(system_instruction=SYSTEM_INSTRUCTION, temperature=0.1)
    )
    match = re.search(r'\{.*\}', response.text, re.DOTALL)
    return json.loads(match.group(0)) if match else None

async def get_recommendations(user_id):
    conn = psycopg2.connect(DATABASE_URL)
    with conn.cursor() as cur:
        cur.execute("SELECT cal, prot FROM user_settings WHERE user_id = %s", (user_id,))
        goal = cur.fetchone()
        cur.execute("SELECT SUM(calories), SUM(protein) FROM meals WHERE user_id = %s AND date = %s", (user_id, date.today()))
        eaten = cur.fetchone()
    conn.close()

    if not goal: return None
    rem_c = goal[0] - (eaten[0] or 0)
    rem_p = goal[1] - (eaten[1] or 0)
    
    if rem_c <= 150: return "Ты уже набрал норму! Хорошая работа."

    prompt = f"У пользователя осталось {rem_c} ккал и {rem_p}г белка. Предложи 3 конкретных перекуса или блюда с КБЖУ."
    response = client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
    return response.text

@dp.message(F.photo | F.text)
async def handle_meal(message: Message, state: FSMContext):
    if message.text in ["📊 Статистика", "⚙️ Настройки", "⚖️ Замер (Вес/Жим)"] or (message.text and message.text.startswith('/')):
        return

    msg_wait = await message.answer("🔍 Распознаю...")
    content_parts = []
    
    if message.photo:
        file = await bot.get_file(message.photo[-1].file_id)
        buffer = BytesIO()
        await bot.download_file(file.file_path, buffer)
        photo_bytes = buffer.getvalue()
        content_parts.append(types.Part.from_bytes(data=photo_bytes, mime_type="image/jpeg"))
        content_parts.append(f"Описание: {message.caption or 'анализ фото'}")
        await state.update_data(last_photo=photo_bytes)
    else:
        content_parts.append(f"Текст: {message.text}")
        await state.update_data(last_photo=None)

    data = await get_gemini_analysis(content_parts)
    if data:
        await state.update_data(temp_meal=data)
        await safe_delete(message)
        await msg_wait.edit_text(
            f"🍴 *{data['name']}*\n🔥 {data['calories']} ккал | Б:{data['protein']}г\n\nЗаписать?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Записать", callback_data="meal_confirm")],
                [InlineKeyboardButton(text="✏️ Изменить", callback_data="meal_edit")],
                [InlineKeyboardButton(text="🗑 Отмена", callback_data="meal_cancel")]
            ]), parse_mode="Markdown"
        )
    else:
        await msg_wait.edit_text("❌ Не удалось распознать.")

@dp.callback_query(F.data == "meal_edit")
async def edit_meal_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Что именно не так? Напиши правку (напр. 'тут 300 грамм'):")
    await state.set_state(MealStates.editing)
    await callback.answer()

@dp.message(MealStates.editing)
async def edit_meal_process(message: Message, state: FSMContext):
    data = await state.get_data()
    msg_wait = await message.answer("🔄 Пересчитываю...")
    
    content_parts = []
    if data.get("last_photo"):
        content_parts.append(types.Part.from_bytes(data=data["last_photo"], mime_type="image/jpeg"))
    content_parts.append(f"Уточнение: {message.text}. Пересчитай предыдущий JSON: {data.get('temp_meal')}")

    new_data = await get_gemini_analysis(content_parts)
    if new_data:
        await state.update_data(temp_meal=new_data)
        await msg_wait.edit_text(
            f"🍴 *{new_data['name']} (Правка)*\n🔥 {new_data['calories']} ккал | Б:{new_data['protein']}г\n\nЗаписать?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Да", callback_data="meal_confirm")],
                [InlineKeyboardButton(text="✏️ Снова изменить", callback_data="meal_edit")]
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
            
            recs = await get_recommendations(callback.from_user.id)
            if recs:
                await callback.message.answer(f"💡 *Советы на сегодня:*\n\n{recs}", parse_mode="Markdown")
    else:
        await safe_delete(callback.message)
    await state.clear()

# ===================== СТАТИСТИКА И НАПОМИНАНИЯ =====================

@dp.message(F.text == "📊 Статистика")
async def show_stats(message: Message):
    await safe_delete(message)
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
    rem_c = goal[0] - c
    await message.answer(
        f"📅 *Сегодня:* {c}/{goal[0]} ккал\n"
        f"🍗 Б: {p}/{goal[1]}г | 🥑 Ж: {f}/{goal[2]}г | 🍞 У: {cr}/{goal[3]}г\n\n"
        f"Осталось: *{max(0, rem_c)}* ккал", parse_mode="Markdown"
    )

async def send_reminder():
    conn = psycopg2.connect(DATABASE_URL)
    with conn.cursor() as cur:
        cur.execute("SELECT user_id FROM user_settings")
        users = cur.fetchall()
    conn.close()
    for user in users:
        with suppress(Exception):
            await bot.send_message(user[0], "🔔 Не забудь записать прием пищи!")

async def main():
    init_db()
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(send_reminder, 'cron', hour='9,14,19', minute=0)
    scheduler.start()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
        
