import asyncio
import os
import json
import re
import psycopg2 # Библиотека для работы с PostgreSQL
from datetime import datetime, date
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.fsm.storage.memory import MemoryStorage
import google.generativeai as genai

# ===================== НАСТРОЙКИ =====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL") # Берем из переменных Railway

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ===================== РАБОТА С БАЗОЙ (PostgreSQL) =====================
def init_db():
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    # Таблица целей и настроек
    cursor.execute('''CREATE TABLE IF NOT EXISTS user_settings 
                      (user_id BIGINT PRIMARY KEY, cal INT, prot INT, fat INT, carb INT, trip_mode BOOLEAN DEFAULT FALSE)''')
    # Таблица приемов пищи
    cursor.execute('''CREATE TABLE IF NOT EXISTS meals 
                      (id SERIAL PRIMARY KEY, user_id BIGINT, date DATE, meal_text TEXT, 
                       calories REAL, protein REAL, fat REAL, carbs REAL)''')
    conn.commit()
    cursor.close()
    conn.close()

def get_user_config(user_id):
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute("SELECT cal, prot, fat, carb, trip_mode FROM user_settings WHERE user_id=%s", (user_id,))
    row = cursor.fetchone()
    if not row:
        # Стандарт: 2600 ккал | Б 170 | Ж 75 | У 300
        cursor.execute("INSERT INTO user_settings VALUES (%s, 2600, 170, 75, 300, FALSE)", (user_id,))
        conn.commit()
        return (2600, 170, 75, 300, False)
    return row

# ===================== ПРОМПТЫ =====================
ANALYSIS_PROMPT = """
Ты — эксперт-диетолог. Проанализируй еду (фото или текст). 
Если вес не указан — оцени на глаз. 
Если включен режим "Командировка" (trip_mode=True), добавь +15% к жирам.
Верни ТОЛЬКО чистый JSON без лишних слов:
{"total": {"calories": X, "protein": X, "fat": X, "carbs": X}, "comment": "краткий совет"}
"""

# ===================== ХЕНДЛЕРЫ =====================
@dp.message(Command("start"))
async def start(message: Message):
    init_db()
    await message.answer(
        "💪 Бот 'Жим 100 / Талия 87' готов!\n\n"
        "📸 Просто пришли фото еды или напиши текстом.\n"
        "📍 Команды:\n"
        "/today - итоги дня\n"
        "/trip - вкл/выкл режим командировки\n"
        "/history - что я ел сегодня"
    )

@dp.message(Command("trip"))
async def toggle_trip(message: Message):
    config = get_user_config(message.from_user.id)
    new_status = not config[4]
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute("UPDATE user_settings SET trip_mode=%s WHERE user_id=%s", (new_status, message.from_user.id))
    conn.commit()
    status_text = "ВКЛЮЧЕН (ИИ добавит +15% жиров)" if new_status else "ВЫКЛЮЧЕН"
    await message.answer(f"🚀 Режим командировки: {status_text}")

@dp.message(F.photo | F.text)
async def handle_food(message: Message):
    user_id = message.from_user.id
    config = get_user_config(user_id) # (cal, p, f, c, trip_mode)
    
    msg_wait = await message.answer("⏳ Магия ИИ в процессе...")
    
    contents = []
    if message.photo:
        file = await bot.get_file(message.photo[-1].file_id)
        file_bytes = await bot.download_file(file.file_path)
        contents.append({"mime_type": "image/jpeg", "data": file_bytes.read()})
        contents.append(f"Режим командировки: {config[4]}. " + ANALYSIS_PROMPT)
    else:
        contents.append(f"Еда: {message.text}. Режим командировки: {config[4]}. " + ANALYSIS_PROMPT)

    try:
        response = model.generate_content(contents)
        # Чистим JSON от возможных кавычек ```json
        raw_json = re.sub(r'```json\s*|```', '', response.text).strip()
        data = json.loads(raw_json)
        res = data["total"]

        # Сохраняем в базу
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO meals (user_id, date, meal_text, calories, protein, fat, carbs) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (user_id, date.today(), message.text or "Фото", res["calories"], res["protein"], res["fat"], res["carbs"])
        )
        conn.commit()

        # Считаем остаток
        cursor.execute("SELECT SUM(calories), SUM(protein), SUM(fat), SUM(carbs) FROM meals WHERE user_id=%s AND date=%s", 
                       (user_id, date.today()))
        eaten = cursor.fetchone()
        
        status = (
            f"✅ **Записано: {res['calories']} ккал**\n"
            f"💬 {data.get('comment', '')}\n\n"
            f"📊 За сегодня:\n"
            f"Ккал: {eaten[0]:.0f} / {config[0]}\n"
            f"Белок: {eaten[1]:.1f} / {config[1]}г\n"
            f"Осталось: {config[0] - eaten[0]:.0f} ккал"
        )
        await msg_wait.edit_text(status)

    except Exception as e:
        print(e)
        await msg_wait.edit_text("❌ Не распознал. Напиши подробнее текстом.")

# ===================== ЗАПУСК =====================
async def main():
    init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
