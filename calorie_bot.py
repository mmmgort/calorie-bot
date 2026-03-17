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
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

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

# ===================== ОБРАБОТЧИКИ (АНКЕТА) =====================

@dp.message(Command("start"))
async def cmd_start(message: Message):
    init_db()
    await message.answer("💪 Привет! Я твой AI-тренер. Нажми **'Настройки'**, чтобы начать.", 
                         reply_markup=get_main_kb(), parse_mode="Markdown")
@dp.message(F.text == "❌ Отмена")
async def cancel_handler(message: Message, state: FSMContext):
    await state.clear() # Полностью сбрасываем состояние опроса
    await message.answer(
        "Действие отменено. Возвращаюсь в главное меню.",
        reply_markup=main_keyboard()
    )

@dp.message(F.text == "⚙️ Настройки")
async def start_settings(message: Message, state: FSMContext):
    await state.set_state(ProfileStates.gender)
    await message.answer("Укажи свой пол (Мужчина/Женщина):", reply_markup=get_cancel_kb())

@dp.message(ProfileStates.gender)
async def process_gender(message: Message, state: FSMContext):
    if message.text not in ["Мужчина", "Женщина"]:
        await message.answer("Пожалуйста, выбери вариант из предложенных или нажми Отмена.")
        return
    await state.update_data(gender=message.text)
    await state.set_state(ProfileStates.age)
    await message.answer("Сколько тебе лет?", reply_markup=get_cancel_kb())

# ... (повтори логику для роста и веса, всегда добавляя reply_markup=get_cancel_kb())

@dp.message(ProfileStates.goal)
async def process_goal(message: Message, state: FSMContext):
    await state.update_data(goal=message.text)
    data = await state.get_data()
    
    msg_wait = await message.answer("🔄 Считаю твою норму через Gemini...")
    
    try:
        # Улучшенный промпт для исключения ошибок
        prompt = (
            f"Рассчитай суточную норму КБЖУ: пол {data['gender']}, возраст {data['age']}, "
            f"рост {data['height']}, вес {data['weight']}, цель {data['goal']}. "
            "Верни ответ ТОЛЬКО в формате JSON: "
            "{\"calories\": int, \"protein\": int, \"fat\": int, \"carbs\": int}"
        )
        
        response = client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
        # Парсим JSON из ответа
        result = json.loads(re.search(r'\{.*\}', response.text, re.DOTALL).group(0))
        
        # Здесь должен быть твой код записи в базу данных Railway
        # save_to_db(message.from_user.id, result) 

        await state.clear()
        await message.answer(
            f"✅ Нормы установлены!\n🔥 {result['calories']} ккал | Б: {result['protein']}г",
            reply_markup=main_keyboard()
        )
        await msg_wait.delete()
    except Exception as e:
        await message.answer("❌ Ошибка расчета. Попробуй заполнить анкету снова.")
    
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

    msg_wait = await message.answer("🔍 Эксперт изучает блюдо...")
    
    try:
        content_parts = []
        user_comment = message.caption if message.caption else "Комментарий отсутствует"
        
        if message.photo:
            file = await bot.get_file(message.photo[-1].file_id)
            p_io = BytesIO()
            await bot.download_file(file.file_path, p_io)
            
            image_part = types.Part.from_bytes(
                data=p_io.getvalue(),
                mime_type="image/jpeg"
            )
            content_parts.append(image_part)
            
            # Твой профессиональный промпт
            prompt = (
                "Ты — эксперт по нутрициологии и визуальному анализу пищи. "
                f"Дополнительная информация от пользователя: {user_comment}. "
                "Твоя задача — максимально точно определить калорийность и БЖУ по фотографии. "
                "Алгоритм: 1. Идентифицируй ингредиенты. 2. Сопоставь размер порции с приборами. "
                "3. Учти скрытые калории. 4. Сделай краткий вердикт. "
                "Верни ТОЛЬКО JSON на русском: "
                "{\"calories\": int, \"protein\": int, \"fat\": int, \"carbs\": int, \"name\": \"название\", \"verdict\": \"совет\"}"
            )
            content_parts.append(prompt)
        else:
            prompt = (
                f"Проанализируй текст: '{message.text}'. "
                "Верни ТОЛЬКО JSON на русском: "
                "{\"calories\": int, \"protein\": int, \"fat\": int, \"carbs\": int, \"name\": \"название\", \"verdict\": \"совет\"}"
            )
            content_parts.append(prompt)

        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=content_parts
        )
        
        data = json.loads(re.search(r'\{.*\}', response.text, re.DOTALL).group(0))
        await state.update_data(temp_meal=data)
        
        await msg_wait.delete()
        
        await message.answer(
            f"🍴 *{data['name']}*\n"
            f"🔥 {data['calories']} ккал | Б: {data['protein']}г | Ж: {data['fat']}г | У: {data['carbs']}г\n\n"
            f"💡 *Вердикт:* {data.get('verdict', 'Приятного аппетита!')}\n\n"
            "Записать в дневник?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Да", callback_data="meal_confirm"),
                InlineKeyboardButton(text="🗑 Нет", callback_data="meal_cancel")
            ]]), parse_mode="Markdown"
        )
    except Exception as e:
        print(f"Ошибка: {e}")
        await msg_wait.edit_text("❌ Ошибка при анализе. Попробуй еще раз.")

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
