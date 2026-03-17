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

# ===================== КОНФИГУРАЦИЯ =====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

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

# ===================== ФУНКЦИЯ ВЫЗОВА ИИ =====================
async def ask_gemini(prompt, photo_data=None):
    """Вызывает актуальную модель Gemini 2.5 Flash."""
    model_name = 'gemini-2.5-flash' # Самая свежая модель из твоих логов
    try:
        content = [prompt]
        if photo_data:
            content.append(photo_data)
            
        response = client.models.generate_content(model=model_name, contents=content)
        return response.text
    except Exception as e:
        print(f"Ошибка Gemini ({model_name}): {e}")
        raise e

# ===================== КЛАВИАТУРЫ =====================
def get_main_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🍲 Добавить еду"), KeyboardButton(text="📊 Статистика")],
        [KeyboardButton(text="⚙️ Настройки")]
    ], resize_keyboard=True)

# ===================== ОБРАБОТЧИКИ (АНКЕТА) =====================

@dp.message(Command("start"))
async def cmd_start(message: Message):
    init_db()
    await message.answer("💪 Привет! Я твой AI-тренер. Нажми **'Настройки'**, чтобы начать.", 
                         reply_markup=get_main_kb(), parse_mode="Markdown")

@dp.message(F.text == "⚙️ Настройки")
async def start_setup(message: Message, state: FSMContext):
    await state.set_state(ProfileStates.waiting_for_gender)
    await message.answer("Твой пол (М/Ж)?")

@dp.message(ProfileStates.waiting_for_gender)
async def set_gender(message: Message, state: FSMContext):
    await state.update_data(gender=message.text)
    await state.set_state(ProfileStates.waiting_for_age)
    await message.answer("Сколько тебе лет?")

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
    await message.answer("Уровень активности (Низкий/Средний/Высокий)?")

@dp.message(ProfileStates.waiting_for_activity)
async def set_act(message: Message, state: FSMContext):
    await state.update_data(activity=message.text)
    await state.set_state(ProfileStates.waiting_for_goal)
    await message.answer("Твоя цель (Похудение/Набор/Поддержание)?")

@dp.message(ProfileStates.waiting_for_goal)
async def finish_setup(message: Message, state: FSMContext):
    u = await state.get_data()
    msg_wait = await message.answer("🔄 Рассчитываю нормы через ИИ...")
    
    prompt = (f"Calculate daily calories (Mifflin-St Jeor) and macros. "
              f"Data: {u['gender']}, {u['age']}y, {u['height']}cm, {u['weight']}kg, "
              f"Activity: {u['activity']}, Goal: {message.text}. "
              f"Return ONLY JSON: {{\"cal\": int, \"prot\": int, \"fat\": int, \"carb\": int}}")
    
    try:
        res_text = await ask_gemini(prompt)
        match = re.search(r'\{.*\}', res_text, re.DOTALL)
        res = json.loads(match.group(0))
        
        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_settings (user_id, cal, prot, fat, carb) VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (user_id) DO UPDATE SET cal=EXCLUDED.cal, prot=EXCLUDED.prot, fat=EXCLUDED.fat, carb=EXCLUDED.carb
            """, (message.from_user.id, res['cal'], res['prot'], res['fat'], res['carb']))
        conn.commit()
        conn.close()
        
        await msg_wait.edit_text(f"✅ Нормы установлены!\n🔥 {res['cal']} ккал | Б: {res['prot']}г")
    except:
        await msg_wait.edit_text("❌ Ошибка расчета. Попробуйте позже.")
    await state.clear()

# ===================== СТАТИСТИКА =====================

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
        await message.answer("Сначала пройдите '⚙️ Настройки'!")
        return

    c, p, f, cr = [int(x or 0) for x in eaten]
    await message.answer(
        f"📅 *Отчет за сегодня ({date.today()}):*\n\n"
        f"🔥 Калории: {c} / {goal[0]} ккал\n"
        f"🍗 Белки: {p} / {goal[1]}г\n"
        f"🥑 Жиры: {f} / {goal[2]}г\n"
        f"🍞 Углеводы: {cr} / {goal[3]}г\n\n"
        f"💡 Осталось съесть: {max(0, goal[0] - c)} ккал",
        parse_mode="Markdown"
    )

# ===================== ЛОГИКА ЕДЫ =====================

@dp.message(F.photo | F.text)
async def handle_meal(message: Message, state: FSMContext):
    if message.text in ["🍲 Добавить еду", "📊 Статистика", "⚙️ Настройки"] or (message.text and message.text.startswith('/')):
        return

    msg_wait = await message.answer("🔍 Анализирую...")
    
    try:
        photo_bytes = None
        if message.photo:
            file = await bot.get_file(message.photo[-1].file_id)
            p_io = BytesIO()
            await bot.download_file(file.file_path, p_io)
            photo_bytes = p_io.getvalue()
            prompt = "Analyze photo. Return ONLY JSON: {\"calories\": int, \"protein\": int, \"fat\": int, \"carbs\": int, \"name\": \"str\"}"
        else:
            prompt = f"Analyze: '{message.text}'. Return ONLY JSON: {{\"calories\": int, \"protein\": int, \"fat\": int, \"carbs\": int, \"name\": \"str\"}}"

        res_text = await ask_gemini(prompt, photo_bytes)
        match = re.search(r'\{.*\}', res_text, re.DOTALL)
        data = json.loads(match.group(0))
        await state.update_data(temp_meal=data)
        
        await msg_wait.edit_text(
            f"🍴 *{data['name']}*\n🔥 {data['calories']} ккал | Б: {data['protein']}г\nЗаписать?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Да", callback_data="meal_confirm"),
                InlineKeyboardButton(text="🗑 Нет", callback_data="meal_cancel")
            ]]), parse_mode="Markdown"
        )
    except:
        await msg_wait.edit_text("❌ Не удалось распознать. Попробуйте описать текстом.")

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
    else:
        await callback.message.edit_text("🗑 Отменено")
    await state.clear()

async def main():
    init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
