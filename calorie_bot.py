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
                    cal INT DEFAULT 2000, prot INT DEFAULT 150, 
                    fat INT DEFAULT 70, carb INT DEFAULT 200
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
        print("✅ БД инициализирована")
    except Exception as e:
        print(f"❌ Ошибка БД: {e}")

# ===================== ФУНКЦИЯ ВЫЗОВА ИИ (С ФОЛБЭКОМ) =====================
async def ask_gemini(prompt):
    """Пытается вызвать Gemini, используя актуальные модели из логов."""
    # Обновленный список на основе данных из 736.png
    models_to_try = [
        'gemini-2.5-flash', 
        'gemini-2.5-pro', 
        'gemini-2.0-flash'
    ]
    
    last_error = ""
    for model_name in models_to_try:
        try:
            print(f"🤖 Пробую актуальную модель: {model_name}...")
            response = client.models.generate_content(model=model_name, contents=prompt)
            return response.text
        except Exception as e:
            last_error = str(e)
            print(f"⚠️ Модель {model_name} ответила ошибкой: {last_error[:50]}...")
            continue
    
    # Если ни одна модель не сработала, выводим диагностику в логи
    print("‼️ ВСЕ МОДЕЛИ ОТКАЗАЛИ. Диагностика API...")
    try:
        available = [m.name for m in client.models.list()]
        print(f"Доступные вам модели: {available}")
    except:
        print("Не удалось даже получить список моделей. Проверьте API_KEY.")
        
    raise Exception(f"Ни одна модель не ответила. Последняя ошибка: {last_error}")

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
    await message.answer("💪 Привет! Я твой AI-тренер. Нажми **'Настройки'**.", reply_markup=get_main_kb(), parse_mode="Markdown")

@dp.message(F.text == "⚙️ Настройки")
async def start_setup(message: Message, state: FSMContext):
    await state.set_state(ProfileStates.waiting_for_gender)
    await message.answer("Твой пол (М/Ж)?")

@dp.message(ProfileStates.waiting_for_gender)
async def set_gender(message: Message, state: FSMContext):
    await state.update_data(gender=message.text)
    await state.set_state(ProfileStates.waiting_for_weight)
    await message.answer("Твой вес (кг)?")

@dp.message(ProfileStates.waiting_for_weight)
async def set_weight(message: Message, state: FSMContext):
    await state.update_data(weight=message.text)
    await state.set_state(ProfileStates.waiting_for_goal)
    await message.answer("Твоя цель (Похудение/Набор)?")

@dp.message(ProfileStates.waiting_for_goal)
async def finish_setup(message: Message, state: FSMContext):
    u = await state.get_data()
    msg_wait = await message.answer("🔄 Рассчитываю нормы через ИИ...")
    
    prompt = (f"Calculate daily calories (Mifflin-St Jeor) and macros. "
              f"Weight: {u['weight']}, Gender: {u['gender']}, Goal: {message.text}. "
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
    except Exception as e:
        await msg_wait.edit_text(f"❌ Ошибка ИИ. Проверь логи Railway.")
    await state.clear()

# ===================== ЛОГИКА ЕДЫ =====================

@dp.message(F.text)
async def handle_meal(message: Message, state: FSMContext):
    if message.text in ["🍲 Добавить еду", "📊 Статистика", "⚙️ Настройки"] or message.text.startswith('/'):
        return

    msg_wait = await message.answer("🔍 Анализирую...")
    try:
        prompt = f"Analyze: '{message.text}'. Return ONLY JSON: {{\"calories\": int, \"protein\": int, \"fat\": int, \"carbs\": int, \"name\": \"str\"}}"
        res_text = await ask_gemini(prompt)
        
        match = re.search(r'\{.*\}', res_text, re.DOTALL)
        data = json.loads(match.group(0))
        await state.update_data(temp_meal=data)
        
        await msg_wait.edit_text(
            f"🍴 {data['name']}\n🔥 {data['calories']} ккал | Б: {data['protein']}г\nЗаписать?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Да", callback_data="meal_confirm"),
                InlineKeyboardButton(text="🗑 Нет", callback_data="meal_cancel")
            ]]), parse_mode="Markdown"
        )
    except:
        await msg_wait.edit_text("❌ Не удалось распознать. Напишите, например: '2 банана'.")

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

# ===================== ОБРАБОТКА ФОТО ЕДЫ =====================

@dp.message(F.photo)
async def handle_photo(message: Message, state: FSMContext):
    msg_wait = await message.answer("📸 Изучаю ваше блюдо... Секунду.")
    
    # Получаем самое качественное фото
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    file_path = file.file_path
    
    # Скачиваем файл в память
    from io import BytesIO
    photo_bytes = BytesIO()
    await bot.download_file(file_path, photo_bytes)
    
    try:
        # Промпт для анализа изображения
        prompt = (
            "Analyze this food photo. Estimate portion size and contents. "
            "Return ONLY JSON: {\"calories\": int, \"protein\": int, \"fat\": int, \"carbs\": int, \"name\": \"str\"}"
        )
        
        # Отправляем фото в Gemini
        # В google-genai для этого используется список [текст, байты]
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[prompt, photo_bytes.getvalue()]
        )
        
        res_text = response.text
        match = re.search(r'\{.*\}', res_text, re.DOTALL)
        data = json.loads(match.group(0))
        
        await state.update_data(temp_meal=data)
        
        await msg_wait.edit_text(
            f"🔍 Похоже на: *{data['name']}*\n"
            f"📊 Оценка ИИ: ~{data['calories']} ккал\n"
            f"Б: {data['protein']}г | Ж: {data['fat']}г | У: {data['carbs']}г\n\n"
            "Записать в дневник?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Да", callback_data="meal_confirm"),
                InlineKeyboardButton(text="🗑 Нет", callback_data="meal_cancel")
            ]]), parse_mode="Markdown"
        )
    except Exception as e:
        print(f"Ошибка анализа фото: {e}")
        await msg_wait.edit_text("❌ Не удалось распознать еду на фото. Попробуйте сфотографировать под другим углом.")

async def main():
    init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
