import asyncio
import os
import json
import re
import psycopg2
from datetime import datetime, date
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.fsm.storage.memory import MemoryStorage
import google.generativeai as genai

# ===================== НАСТРОЙКИ =====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ===================== БАЗА ДАННЫХ =====================
def init_db():
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS user_settings 
                      (user_id BIGINT PRIMARY KEY, cal INT, prot INT, fat INT, carb INT, trip_mode BOOLEAN DEFAULT FALSE)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS meals 
                      (id SERIAL PRIMARY KEY, user_id BIGINT, date DATE, meal_text TEXT, 
                       calories REAL, protein REAL, fat REAL, carbs REAL)''')
    # Новая таблица для замеров
    cursor.execute('''CREATE TABLE IF NOT EXISTS progress 
                      (id SERIAL PRIMARY KEY, user_id BIGINT, date DATE, weight REAL, waist REAL)''')
    conn.commit()
    cursor.close()
    conn.close()

def get_user_config(user_id):
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute("SELECT cal, prot, fat, carb, trip_mode FROM user_settings WHERE user_id=%s", (user_id,))
    row = cursor.fetchone()
    if not row:
        cursor.execute("INSERT INTO user_settings VALUES (%s, 2600, 170, 75, 300, FALSE)", (user_id,))
        conn.commit()
        return (2600, 170, 75, 300, False)
    return row

# ===================== ХЕНДЛЕРЫ =====================

@dp.message(Command("start"))
async def start(message: Message):
    init_db()
    await message.answer(
        "💪 Бот 'Жим 100 / Талия 87' обновлен!\n\n"
        "📸 Присылай еду (фото/текст)\n"
        "📈 Замеры: `/log 95 92` (вес 95, талия 92)\n"
        "📊 Команды: /today, /progress, /trip, /history"
    )

@dp.message(Command("log"))
async def log_progress(message: Message):
    try:
        # Формат: /log 95 92
        _, weight, waist = message.text.split()
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO progress (user_id, date, weight, waist) VALUES (%s, %s, %s, %s)",
                       (message.from_user.id, date.today(), float(weight), float(waist)))
        conn.commit()
        await message.answer(f"✅ Замеры записаны: Вес {weight} кг, Талия {waist} см. Идем к цели 87!")
    except:
        await message.answer("❌ Ошибка. Пиши так: `/log 95 92` (вес и талия через пробел)")

@dp.message(Command("progress"))
async def show_progress(message: Message):
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute("SELECT date, weight, waist FROM progress WHERE user_id=%s ORDER BY date ASC", (message.from_user.id,))
    rows = cursor.fetchall()
    
    if len(rows) < 1:
        return await message.answer("Замеров пока нет. Используй /log")
    
    first = rows[0]
    last = rows[-1]
    
    msg = (
        f"📊 **Твой прогресс:**\n\n"
        f"Старт: {first[1]} кг | {first[2]} см\n"
        f"Сейчас: {last[1]} кг | {last[2]} см\n"
        f"--- \n"
        f"Изменения: {last[1]-first[1]:.1f} кг | {last[2]-first[2]:.1f} см"
    )
    await message.answer(msg)

@dp.message(Command("today"))
async def show_today(message: Message):
    user_id = message.from_user.id
    config = get_user_config(user_id)
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute("SELECT SUM(calories), SUM(protein), SUM(fat), SUM(carbs) FROM meals WHERE user_id=%s AND date=%s", 
                   (user_id, date.today()))
    eaten = cursor.fetchone()
    
    if not eaten[0]:
        return await message.answer("Ты сегодня еще ничего не ел.")
        
    status = (
        f"📅 **Итоги за сегодня:**\n\n"
        f"Ккал: {eaten[0]:.0f} / {config[0]}\n"
        f"Белок: {eaten[1]:.1f} / {config[1]}г\n"
        f"Жиры: {eaten[2]:.1f} / {config[2]}г\n"
        f"Углеводы: {eaten[3]:.1f} / {config[3]}г\n\n"
        f"Осталось: {config[0]-eaten[0]:.0f} ккал"
    )
    await message.answer(status)

@dp.message(Command("trip"))
async def toggle_trip(message: Message):
    config = get_user_config(message.from_user.id)
    new_status = not config[4]
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute("UPDATE user_settings SET trip_mode=%s WHERE user_id=%s", (new_status, message.from_user.id))
    conn.commit()
    await message.answer(f"🚀 Режим командировки: {'ВКЛ' if new_status else 'ВЫКЛ'}")

@dp.message(F.photo | F.text)
async def handle_food(message: Message):
    user_id = message.from_user.id
    config = get_user_config(user_id)
    msg_wait = await message.answer("⏳ Анализирую...")
    
    contents = []
    if message.photo:
        file = await bot.get_file(message.photo[-1].file_id)
        file_bytes = await bot.download_file(file.file_path)
        contents.append({"mime_type": "image/jpeg", "data": file_bytes.read()})
        contents.append(f"Режим командировки: {config[4]}. Если True, добавь 15% к жирам. Верни JSON.")
    else:
        contents.append(f"Еда: {message.text}. Режим: {config[4]}. JSON.")
    
    contents.append("""Верни ТОЛЬКО JSON: {"total": {"calories": X, "protein": X, "fat": X, "carbs": X}, "comment": "текст"}""")

    try:
        response = model.generate_content(contents)
        raw_json = re.sub(r'```json\s*|```', '', response.text).strip()
        data = json.loads(raw_json)
        res = data["total"]

        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO meals (user_id, date, meal_text, calories, protein, fat, carbs) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                       (user_id, date.today(), message.text[:50] if message.text else "Фото", res["calories"], res["protein"], res["fat"], res["carbs"]))
        conn.commit()

        await msg_wait.edit_text(f"✅ Записано: {res['calories']} ккал. {data.get('comment', '')}\nИспользуй /today для статистики.")
    except:
        await msg_wait.edit_text("❌ Ошибка распознавания. Попробуй еще раз.")

async def main():
    init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
