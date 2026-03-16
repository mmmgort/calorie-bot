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

# ===================== НАСТРОЙКИ =====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

genai.configure(api_key=GEMINI_API_KEY)
# Используем flash-модель, она быстрее и лучше работает с JSON
model = genai.GenerativeModel('gemini-1.5-flash')

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

class MealStates(StatesGroup):
    waiting_for_edit = State()

# ===================== КЛАВИАТУРЫ =====================
def get_main_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🍲 Добавить еду"), KeyboardButton(text="⚖️ Замер (Вес/Жим)")],
        [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="⚙️ Настройки")]
    ], resize_keyboard=True)

def get_meal_inline_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Подтвердить", callback_data="meal_confirm"),
            InlineKeyboardButton(text="✏️ Изменить", callback_data="meal_edit")
        ],
        [InlineKeyboardButton(text="🗑 Отмена", callback_data="meal_cancel")]
    ])

# ===================== БАЗА ДАННЫХ =====================
def init_db():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE IF NOT EXISTS user_settings (user_id BIGINT PRIMARY KEY, cal INT DEFAULT 2600, prot INT DEFAULT 170, fat INT DEFAULT 75, carb INT DEFAULT 300, target_waist REAL DEFAULT 87.0, last_bench REAL DEFAULT 0.0)")
        cursor.execute("CREATE TABLE IF NOT EXISTS meals (id SERIAL PRIMARY KEY, user_id BIGINT, date DATE, meal_text TEXT, calories REAL, protein REAL, fat REAL, carbs REAL)")
        cursor.execute("CREATE TABLE IF NOT EXISTS progress (id SERIAL PRIMARY KEY, user_id BIGINT, date DATE, weight REAL, waist REAL, bench REAL DEFAULT 0.0)")
        
        cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_name='progress' AND column_name='bench'")
        if not cursor.fetchone(): 
            cursor.execute("ALTER TABLE progress ADD COLUMN bench REAL DEFAULT 0.0")
        
        conn.commit()
        cursor.close()
        conn.close()
        print("База данных успешно инициализирована.")
    except Exception as e:
        print(f"Ошибка БД при запуске: {e}")

# ===================== ХЕНДЛЕРЫ МЕНЮ =====================
@dp.message(Command("start"))
async def cmd_start(message: Message):
    init_db()
    await message.answer(
        f"👋 Привет, {message.from_user.first_name}!\n"
        "Я обновлен и готов к работе. Жмем сотку! 💪", 
        reply_markup=get_main_kb()
    )

@dp.message(F.text == "🍲 Добавить еду")
async def meal_instruction(message: Message):
    await message.answer("Просто пришли фото еды или опиши её текстом (например: '200г гречки и 3 яйца').")

@dp.message(F.text == "⚖️ Замер (Вес/Жим)")
async def prompt_log(message: Message):
    await message.answer("Пришли замеры: `/log Вес Талия Жим` (например: `/log 95 90 85`)")

# ===================== ЛОГИРОВАНИЕ ЗАМЕРОВ =====================
@dp.message(Command("log"))
async def log_data(message: Message):
    nums = re.findall(r"\d+\.?\d*", message.text)
    if len(nums) < 3: 
        return await message.answer("⚠️ Пиши так: `/log 95 90 85` (Вес, Талия, Жим)")
    try:
        w, t, b = float(nums[0]), float(nums[1]), float(nums[2])
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO progress (user_id, date, weight, waist, bench) VALUES (%s,%s,%s,%s,%s)", (message.from_user.id, date.today(), w, t, b))
        cursor.execute("UPDATE user_settings SET last_bench=%s WHERE user_id=%s", (b, message.from_user.id))
        conn.commit()
        cursor.close()
        conn.close()
        await message.answer(f"✅ Данные приняты!\n⚖️ Вес: {w}кг | 📏 Талия: {t}см | 💪 Жим: {b}кг")
    except Exception as e:
        await message.answer(f"❌ Ошибка базы данных: {e}")

# ===================== ОБРАБОТКА ЕДЫ (УСИЛЕННАЯ) =====================
@dp.message(F.photo | F.text)
async def handle_meal_input(message: Message, state: FSMContext):
    text_input = message.text or message.caption
    
    # Игнорируем команды и старые/новые кнопки меню
    ignore_list = [
        "📊 Статистика", "⚙️ Настройки", "⚖️ Замер (Вес/Жим)", 
        "🍲 Добавить еду", "💪 Мой Жим", "📝 История"
    ]
    if text_input and (text_input.startswith('/') or text_input in ignore_list):
        return

    msg_wait = await message.answer("🔍 Анализирую...")
    
    prompt = (
        "Ты нутрициолог. Оцени КБЖУ еды на фото или в тексте. "
        "Верни ТОЛЬКО чистый JSON формата: "
        "{\"calories\": 0, \"protein\": 0, \"fat\": 0, \"carbs\": 0, \"name\": \"Название блюда\"}. "
        "Не пиши никакого текста до или после JSON."
    )
    
    contents = [prompt]
    if text_input:
        contents.append(text_input)
        
    if message.photo:
        file = await bot.get_file(message.photo[-1].file_id)
        file_bytes = await bot.download_file(file.file_path)
        contents.append({"mime_type": "image/jpeg", "data": file_bytes.read()})

    try:
        response = model.generate_content(contents)
        reply_text = response.text
        
        # Умный поиск JSON (решает проблему, когда ИИ добавляет лишний текст)
        json_match = re.search(r'\{.*\}', reply_text, re.DOTALL)
        if not json_match:
            raise ValueError(f"Не найден JSON в ответе: {reply_text}")
            
        data = json.loads(json_match.group(0))
        await state.update_data(temp_meal=data)
        
        text = (f"🍴 **{data['name']}**\n"
                f"🔥 {data['calories']} ккал\n"
                f"🥩 Б: {data['protein']}г | 🥑 Ж: {data['fat']}г | 🌾 У: {data['carbs']}г\n\n"
                f"Всё верно?")
        await msg_wait.edit_text(text, reply_markup=get_meal_inline_kb(), parse_mode="Markdown")
        
    except Exception as e:
        await msg_wait.edit_text(f"❌ Ошибка распознавания.\nДетали: `{e}`\nПопробуй описать точнее.", parse_mode="Markdown")

# ===================== ИНЛАЙН КНОПКИ ДЛЯ ЕДЫ =====================
@dp.callback_query(F.data.startswith("meal_"))
async def process_meal_callback(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    meal = data.get("temp_meal")

    if callback.data == "meal_confirm" and meal:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO meals (user_id, date, meal_text, calories, protein, fat, carbs) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                       (callback.from_user.id, date.today(), meal['name'], meal['calories'], meal['protein'], meal['fat'], meal['carbs']))
        conn.commit()
        cursor.close()
        conn.close()
        await callback.message.edit_text(f"✅ Записано в базу: **{meal['name']}** ({meal['calories']} ккал)", parse_mode="Markdown")
        await state.clear()

    elif callback.data == "meal_edit":
        await callback.message.answer("📝 Что изменить? (например: 'добавь 100г риса' или 'тут 500 ккал')")
        await state.set_state(MealStates.waiting_for_edit)
        await callback.answer()

    elif callback.data == "meal_cancel":
        await callback.message.edit_text("🗑 Отменено.")
        await state.clear()

@dp.message(MealStates.waiting_for_edit)
async def process_meal_edit(message: Message, state: FSMContext):
    data = await state.get_data()
    old_meal = data.get("temp_meal")
    msg_wait = await message.answer("🔄 Пересчитываю...")
    
    prompt = (
        f"Ранее ты рассчитал это: {old_meal}. "
        f"Пользователь просит изменить: {message.text}. "
        "Пересчитай всё с учетом правки и верни ТОЛЬКО чистый JSON: "
        "{\"calories\": 0, \"protein\": 0, \"fat\": 0, \"carbs\": 0, \"name\": \"Название\"}."
    )
    
    try:
        response = model.generate_content(prompt)
        json_match = re.search(r'\{.*\}', response.text, re.DOTALL)
        if not json_match:
            raise ValueError("Не найден JSON в ответе")
            
        new_data = json.loads(json_match.group(0))
        await state.update_data(temp_meal=new_data)
        
        text = (f"🔄 **Обновлено: {new_data['name']}**\n"
                f"🔥 {new_data['calories']} ккал\n"
                f"🥩 Б: {new_data['protein']}г | 🥑 Ж: {new_data['fat']}г | 🌾 У: {new_data['carbs']}г\n\n"
                f"Теперь записываем?")
        await msg_wait.edit_text(text, reply_markup=get_meal_inline_kb(), parse_mode="Markdown")
    except Exception as e:
        await msg_wait.edit_text(f"❌ Ошибка пересчета: {e}\nНапиши уточнение еще раз.")

# ===================== ЗАПУСК =====================
async def main():
    init_db()
    # Сброс очереди обновлений перед запуском, чтобы не было конфликтов
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
