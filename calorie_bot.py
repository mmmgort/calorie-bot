import asyncio
import os
import json
import re
import psycopg2
import logging
from datetime import datetime, date, timedelta
from io import BytesIO
from contextlib import suppress

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest

from google import genai
from google.genai import types

# Настройка логирования
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
    "Ты — эксперт-нутрициолог. Твой ответ — строго JSON: "
    "{\"calories\": int, \"protein\": int, \"fat\": int, \"carbs\": int, \"name\": \"название\", \"verdict\": \"совет\"}. "
    "Будь точен в оценке веса порций."
)

class ProfileStates(StatesGroup):
    gender, age, height, weight, activity, goal = [State() for _ in range(6)]

class MealStates(StatesGroup):
    editing = State()

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
    with suppress(Exception):
        await message.delete()

# ===================== КЛАВИАТУРЫ =====================
def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="⚙️ Профиль")],
            [KeyboardButton(text="🗑 Сброс данных")]
        ], resize_keyboard=True
    )

def stats_inline():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 День", callback_data="stats_1"), 
         InlineKeyboardButton(text="📅 Неделя", callback_data="stats_7"), 
         InlineKeyboardButton(text="📅 Месяц", callback_data="stats_30")]
    ])

def reset_inline():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Сбросить День", callback_data="reset_1")],
        [InlineKeyboardButton(text="Сбросить Неделю", callback_data="reset_7")],
        [InlineKeyboardButton(text="Очистить всё", callback_data="reset_all")]
    ])

# ===================== ЛОГИКА СТАТИСТИКИ И СБРОСА =====================

@dp.message(F.text == "📊 Статистика")
async def cmd_stats(message: Message):
    await message.answer("📊 За какой период показать отчет?", reply_markup=stats_inline())

@dp.callback_query(F.data.startswith("stats_"))
async def process_stats(callback: CallbackQuery):
    days = int(callback.data.split("_")[1])
    user_id = callback.from_user.id
    start_date = date.today() - timedelta(days=days-1)

    conn = psycopg2.connect(DATABASE_URL)
    with conn.cursor() as cur:
        cur.execute("SELECT cal, prot, fat, carb FROM user_settings WHERE user_id = %s", (user_id,))
        goal = cur.fetchone()
        cur.execute("""
            SELECT SUM(calories), SUM(protein), SUM(fat), SUM(carbs) 
            FROM meals WHERE user_id = %s AND date >= %s
        """, (user_id, start_date))
        eaten = cur.fetchone()
    conn.close()

    if not goal:
        await callback.answer("Сначала заполни профиль!", show_alert=True)
        return

    c, p, f, cr = [int(x or 0) for x in eaten]
    target_c = goal[0] * days
    
    period_text = "сегодня" if days == 1 else f"{days} дн."
    text = (f"📈 *Статистика за {period_text}:*\n\n"
            f"🔥 Калории: {c} / {target_c} ккал\n"
            f"🍗 Белки: {p} / {goal[1]*days}г\n"
            f"🥑 Жиры: {f} / {goal[2]*days}г\n"
            f"🍞 Углеводы: {cr} / {goal[3]*days}г")
    
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=stats_inline())

@dp.message(F.text == "🗑 Сброс данных")
async def cmd_reset_menu(message: Message):
    await message.answer("⚠️ Выбери период для удаления данных:", reply_markup=reset_inline())

@dp.callback_query(F.data.startswith("reset_"))
async def process_reset(callback: CallbackQuery):
    user_id = callback.from_user.id
    period = callback.data.split("_")[1]
    conn = psycopg2.connect(DATABASE_URL); cur = conn.cursor()

    if period == "all":
        cur.execute("DELETE FROM meals WHERE user_id = %s", (user_id,))
        text = "🗑 Вся история очищена."
    else:
        days = int(period)
        start_date = date.today() - timedelta(days=days-1)
        cur.execute("DELETE FROM meals WHERE user_id = %s AND date >= %s", (user_id, start_date))
        text = f"🗑 Данные за {days} дн. удалены."
    
    conn.commit(); conn.close()
    await callback.message.edit_text(text)
    await callback.answer()

# ===================== АНКЕТА (ПРОФИЛЬ) =====================

@dp.message(F.text == "⚙️ Профиль")
async def start_settings(message: Message, state: FSMContext):
    await state.set_state(ProfileStates.gender)
    await message.answer("Твой пол (М/Ж)?", reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Отмена")]], resize_keyboard=True))

@dp.message(ProfileStates.gender)
async def proc_g(message: Message, state: FSMContext):
    await state.update_data(gender=message.text)
    await state.set_state(ProfileStates.age); await message.answer("Сколько тебе лет?")

@dp.message(ProfileStates.age)
async def proc_a(message: Message, state: FSMContext):
    await state.update_data(age=message.text)
    await state.set_state(ProfileStates.height); await message.answer("Твой рост (см)?")

@dp.message(ProfileStates.height)
async def proc_h(message: Message, state: FSMContext):
    await state.update_data(height=message.text)
    await state.set_state(ProfileStates.weight); await message.answer("Твой вес (кг)?")

@dp.message(ProfileStates.weight)
async def proc_w(message: Message, state: FSMContext):
    await state.update_data(weight=message.text)
    await state.set_state(ProfileStates.activity); await message.answer("Уровень активности (Низкий/Средний/Высокий)?")

@dp.message(ProfileStates.activity)
async def proc_act(message: Message, state: FSMContext):
    await state.update_data(activity=message.text)
    await state.set_state(ProfileStates.goal); await message.answer("Твоя цель (Похудение/Набор/Поддержание)?")

@dp.message(ProfileStates.goal)
async def proc_goal(message: Message, state: FSMContext):
    data = await state.get_data()
    msg_wait = await message.answer("⚡ Gemini рассчитывает нормы...")
    try:
        resp = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=f"Рассчитай КБЖУ: пол {data['gender']}, возраст {data['age']}, рост {data['height']}, вес {data['weight']}, цель {message.text}.",
            config=types.GenerateContentConfig(system_instruction="Выдай СТРОГО JSON: calories, protein, fat, carbs")
        )
        res = json.loads(re.search(r'\{.*\}', resp.text, re.DOTALL).group(0))
        conn = psycopg2.connect(DATABASE_URL); cur = conn.cursor()
        cur.execute("""
            INSERT INTO user_settings (user_id, cal, prot, fat, carb) VALUES (%s, %s, %s, %s, %s) 
            ON CONFLICT (user_id) DO UPDATE SET cal=EXCLUDED.cal, prot=EXCLUDED.prot, fat=EXCLUDED.fat, carb=EXCLUDED.carb
        """, (message.from_user.id, res['calories'], res['protein'], res['fat'], res['carbs']))
        conn.commit(); conn.close()
        await safe_delete(msg_wait)
        await message.answer(f"✅ Нормы установлены! Цель: {res['calories']} ккал.", reply_markup=main_keyboard())
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
    await state.clear()

# ===================== АНАЛИЗ ЕДЫ И РЕКОМЕНДАЦИИ =====================

async def get_gemini_analysis(parts):
    response = client.models.generate_content(
        model='gemini-2.5-flash', contents=parts,
        config=types.GenerateContentConfig(system_instruction=SYSTEM_INSTRUCTION, temperature=0.1)
    )
    match = re.search(r'\{.*\}', response.text, re.DOTALL)
    return json.loads(match.group(0)) if match else None

async def get_recommendations(user_id):
    try:
        conn = psycopg2.connect(DATABASE_URL); cur = conn.cursor()
        cur.execute("SELECT cal FROM user_settings WHERE user_id = %s", (user_id,))
        goal = cur.fetchone()
        cur.execute("SELECT SUM(calories) FROM meals WHERE user_id = %s AND date = %s", (user_id, date.today()))
        eaten = cur.fetchone()
        conn.close()
        rem_c = goal[0] - (eaten[0] or 0)
        if rem_c > 150:
            resp = client.models.generate_content(model='gemini-2.5-flash', contents=f"У меня осталось {rem_c} ккал. Что мне съесть полезного? Дай 3 коротких варианта.")
            return resp.text
        return None
    except: return None

@dp.message(F.photo | F.text)
async def handle_meal(message: Message, state: FSMContext):
    if message.text in ["📊 Статистика", "⚙️ Профиль", "🗑 Сброс данных"] or (message.text and message.text.startswith('/')): return
    msg_wait = await message.answer("🔍 Распознаю...")
    parts = []
    if message.photo:
        file = await bot.get_file(message.photo[-1].file_id)
        buf = BytesIO(); await bot.download_file(file.file_path, buf)
        img_bytes = buf.getvalue()
        parts.append(types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"))
        await state.update_data(last_photo=img_bytes)
    else: 
        parts.append(f"Еда: {message.text}")
        await state.update_data(last_photo=None)

    data = await get_gemini_analysis(parts)
    if data:
        await state.update_data(temp_meal=data)
        await safe_delete(msg_wait)
        await message.answer(f"🍴 *{data['name']}*\n🔥 {data['calories']} ккал | Б:{data['protein']}г\n\nЗаписать?", 
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Да", callback_data="meal_confirm")],
                [InlineKeyboardButton(text="✏️ Изменить", callback_data="meal_edit")],
                [InlineKeyboardButton(text="🗑 Нет", callback_data="meal_cancel")]
            ]), parse_mode="Markdown")
    else: await msg_wait.edit_text("❌ Не удалось разобрать еду.")

@dp.callback_query(F.data == "meal_confirm")
async def meal_confirm(callback: CallbackQuery, state: FSMContext):
    d = (await state.get_data()).get("temp_meal")
    if d:
        conn = psycopg2.connect(DATABASE_URL); cur = conn.cursor()
        cur.execute("INSERT INTO meals (user_id, date, meal_text, calories, protein, fat, carbs) VALUES (%s,%s,%s,%s,%s,%s,%s)", 
                   (callback.from_user.id, date.today(), d['name'], d['calories'], d['protein'], d['fat'], d['carbs']))
        conn.commit(); conn.close()
        await callback.message.edit_text(f"✅ Записано: {d['name']}")
        advice = await get_recommendations(callback.from_user.id)
        if advice: await callback.message.answer(f"💡 *Совет на сегодня:*\n{advice}", parse_mode="Markdown")
    await state.clear()

@dp.callback_query(F.data == "meal_edit")
async def meal_edit(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Что исправить? (Напр: 'тут 200г' или 'это без масла')")
    await state.set_state(MealStates.editing)
    await callback.answer()

@dp.message(MealStates.editing)
async def meal_edit_proc(message: Message, state: FSMContext):
    data = await state.get_data()
    msg_wait = await message.answer("🔄 Пересчитываю...")
    parts = []
    if data.get("last_photo"): parts.append(types.Part.from_bytes(data=data["last_photo"], mime_type="image/jpeg"))
    parts.append(f"Уточнение: {message.text}. Предыдущий расчет был: {data.get('temp_meal')}")
    
    new_data = await get_gemini_analysis(parts)
    if new_data:
        await state.update_data(temp_meal=new_data)
        await safe_delete(msg_wait)
        await message.answer(f"🍴 *{new_data['name']} (Обновлено)*\n🔥 {new_data['calories']} ккал", 
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Записать", callback_data="meal_confirm")]]), parse_mode="Markdown")
    await state.set_state(None)

@dp.callback_query(F.data == "meal_cancel")
async def meal_cancel(callback: CallbackQuery, state: FSMContext):
    await safe_delete(callback.message); await state.clear()

# ===================== ЗАПУСК =====================
@dp.message(Command("start"))
async def cmd_start(message: Message):
    init_db()
    await message.answer("🦾 Привет! Я Calorie Bot. Готов следить за твоим питанием.", reply_markup=main_keyboard())

@dp.message(F.text == "❌ Отмена")
async def cancel_handler(message: Message, state: FSMContext):
    await state.clear(); await message.answer("Отменено.", reply_markup=main_keyboard())

async def main():
    init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
