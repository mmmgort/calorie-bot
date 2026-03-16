import asyncio
import os
import json
import re
import psycopg2
from datetime import datetime, date
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler # ИСПРАВЛЕНО ЗДЕСЬ
import google.generativeai as genai

# ===================== НАСТРОЙКИ =====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler() # ИСПРАВЛЕНО ЗДЕСЬ

def get_main_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="💪 Мой Жим")],
        [KeyboardButton(text="📝 История"), KeyboardButton(text="⚙️ Настройки")],
    ], resize_keyboard=True)

# ===================== БАЗА ДАННЫХ =====================
def init_db():
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS user_settings 
                      (user_id BIGINT PRIMARY KEY, cal INT, prot INT, fat INT, carb INT, 
                       trip_mode BOOLEAN DEFAULT FALSE, train_mode BOOLEAN DEFAULT FALSE,
                       target_waist REAL DEFAULT 87.0, last_bench REAL DEFAULT 0.0)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS meals 
                      (id SERIAL PRIMARY KEY, user_id BIGINT, date DATE, meal_text TEXT, 
                       calories REAL, protein REAL, fat REAL, carbs REAL)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS progress 
                      (id SERIAL PRIMARY KEY, user_id BIGINT, date DATE, weight REAL, waist REAL, bench REAL)''')
    conn.commit()
    cursor.close()
    conn.close()

def get_config(user_id):
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute("SELECT cal, prot, fat, carb, trip_mode, train_mode, target_waist, last_bench FROM user_settings WHERE user_id=%s", (user_id,))
    row = cursor.fetchone()
    if not row:
        cursor.execute("INSERT INTO user_settings (user_id, cal, prot, fat, carb) VALUES (%s, 2600, 170, 75, 300)", (user_id,))
        conn.commit()
        return (2600, 170, 75, 300, False, False, 87.0, 0.0)
    return row

# ===================== УВЕДОМЛЕНИЯ =====================
async def check_reminders():
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT user_id FROM user_settings")
    users = cursor.fetchall()
    
    now_hour = datetime.now().hour
    if 9 <= now_hour <= 22:
        for (uid,) in users:
            cursor.execute("SELECT COUNT(*) FROM meals WHERE user_id=%s AND date=%s", (uid, date.today()))
            count = cursor.fetchone()[0]
            if count == 0:
                try:
                    await bot.send_message(uid, "👋 Боец, сегодня еще нет записей о еде! Чтобы пожать 100 кг, нужно топливо. Запиши, что съел! 🍗")
                except: pass
    cursor.close()
    conn.close()

# ===================== ХЕНДЛЕРЫ =====================

@dp.message(Command("start"))
async def cmd_start(message: Message):
    init_db()
    await message.answer(
        f"👋 Привет, {message.from_user.first_name}!\n\n"
        "Я помогу тебе считать калории, чтобы ты пожал **100 кг** и не раздулся в талии.\n\n"
        "📥 Просто кидай фото еды или пиши текст.\n"
        "📈 Замеры: `/log Вес Талия Жим`", 
        reply_markup=get_main_kb()
    )

@dp.message(F.text == "📊 Статистика")
async def show_stats(message: Message):
    uid = message.from_user.id
    conf = get_config(uid)
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute("SELECT SUM(calories), SUM(protein), SUM(fat), SUM(carbs) FROM meals WHERE user_id=%s AND date=%s", (uid, date.today()))
    e = cursor.fetchone()
    e = [v if v else 0 for v in e]
    
    def pb(cur, goal):
        perc = min(int((cur/goal)*10), 10) if goal > 0 else 0
        return "🟩" * perc + "⬜" * (10 - perc)

    await message.answer(f"📊 **Сегодня:**\n\n🔥 Ккал: {e[0]:.0f}/{conf[0]}\n{pb(e[0], conf[0])}\n\n🥩 Б: {e[1]:.0f}г | 🥑 Ж: {e[2]:.0f}г | 🌾 У: {e[3]:.0f}г")

@dp.message(F.text == "💪 Мой Жим")
async def show_bench(message: Message):
    conf = get_config(message.from_user.id)
    await message.answer(f"🏋️‍♂️ Текущий жим: {conf[7]} кг\n🏁 До цели 100 кг: {max(0, 100-conf[7])} кг")

@dp.message(Command("log"))
async def log_data(message: Message):
    try:
        _, w, t, b = message.text.split()
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO progress (user_id, date, weight, waist, bench) VALUES (%s,%s,%s,%s,%s)",
                       (message.from_user.id, date.today(), float(w), float(t), float(b)))
        cursor.execute("UPDATE user_settings SET last_bench=%s WHERE user_id=%s", (float(b), message.from_user.id))
        conn.commit()
        await message.answer(f"✅ Записано! Жим: {b}кг. Идем к сотке!")
    except:
        await message.answer("Ошибка! Пиши: `/log 95 90 85` (Вес Талия Жим)")

@dp.message(F.photo | F.text)
async def handle_meal(message: Message):
    if message.text and message.text.startswith('/'): return
    uid = message.from_user.id
    conf = get_config(uid)
    msg_wait = await message.answer("🔍 Анализирую...")
    
    contents = []
    if message.photo:
        file = await bot.get_file(message.photo[-1].file_id)
        file_bytes = await bot.download_file(file.file_path)
        contents.append({"mime_type": "image/jpeg", "data": file_bytes.read()})
    
    prompt = f"Ты диетолог. Цель: жим 100кг (сейчас {conf[7]}). Оцени еду. Верни JSON: {{\"total\": {{\"calories\": X, \"protein\": X, \"fat\": X, \"carbs\": X}}, \"name\": \"название\", \"comment\": \"совет\"}}"
    contents.append(message.text or "Оцени еду")
    contents.append(prompt)

    try:
        response = model.generate_content(contents)
        data = json.loads(re.sub(r'```json\s*|```', '', response.text).strip())
        res = data["total"]
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO meals (user_id, date, meal_text, calories, protein, fat, carbs) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                       (uid, date.today(), data.get("name"), res["calories"], res["protein"], res["fat"], res["carbs"]))
        conn.commit()
        await msg_wait.edit_text(f"✅ **{data.get('name')}**\n🔥 {res['calories']:.0f} ккал\n💡 {data.get('comment')}")
    except:
        await msg_wait.edit_text("❌ Опиши еду текстом.")

async def main():
    init_db()
    scheduler.add_job(check_reminders, 'interval', hours=4)
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
