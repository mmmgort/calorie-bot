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
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        # Таблица настроек
        cursor.execute('''CREATE TABLE IF NOT EXISTS user_settings 
                          (user_id BIGINT PRIMARY KEY, cal INT DEFAULT 2600, prot INT DEFAULT 170, 
                           fat INT DEFAULT 75, carb INT DEFAULT 300, 
                           target_waist REAL DEFAULT 87.0, last_bench REAL DEFAULT 0.0)''')
        # Таблица еды
        cursor.execute('''CREATE TABLE IF NOT EXISTS meals 
                          (id SERIAL PRIMARY KEY, user_id BIGINT, date DATE, meal_text TEXT, 
                           calories REAL, protein REAL, fat REAL, carbs REAL)''')
        # Таблица прогресса
        cursor.execute('''CREATE TABLE IF NOT EXISTS progress 
                          (id SERIAL PRIMARY KEY, user_id BIGINT, date DATE, weight REAL, waist REAL, bench REAL DEFAULT 0.0)''')
        
        # Проверка и добавление колонки bench, если таблица уже была создана без неё
        cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_name='progress' AND column_name='bench'")
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE progress ADD COLUMN bench REAL DEFAULT 0.0")
            print("Колонка 'bench' успешно добавлена в таблицу 'progress'.")
            
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"ОШИБКА ИНИЦИАЛИЗАЦИИ БД: {e}")

def get_config(user_id):
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute("SELECT cal, prot, fat, carb, target_waist, last_bench FROM user_settings WHERE user_id=%s", (user_id,))
    row = cursor.fetchone()
    if not row:
        cursor.execute("INSERT INTO user_settings (user_id) VALUES (%s)", (user_id,))
        conn.commit()
        row = (2600, 170, 75, 300, 87.0, 0.0)
    cursor.close()
    conn.close()
    return row

# ===================== ХЕНДЛЕРЫ =====================

@dp.message(Command("start"))
async def cmd_start(message: Message):
    init_db()
    kb = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="💪 Мой Жим")],
        [KeyboardButton(text="⚙️ Настройки")]
    ], resize_keyboard=True)
    await message.answer(
        f"👋 Привет, {message.from_user.first_name}!\n"
        f"Я все так же считаю твою еду и ничего не забываю. Твои данные в безопасности в базе данных PostgreSQL.\n\n"
        f"📈 Замеры: `/log Вес Талия Жим`", 
        reply_markup=kb
    )

@dp.message(Command("log"))
async def log_data(message: Message):
    nums = re.findall(r"\d+\.?\d*", message.text)
    if len(nums) < 3:
        return await message.answer("⚠️ Пиши так: `/log 76 87 100` (Вес, Талия, Жим)")

    try:
        w, t, b = float(nums[0]), float(nums[1]), float(nums[2])
        uid = message.from_user.id
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO progress (user_id, date, weight, waist, bench) VALUES (%s,%s,%s,%s,%s)", (uid, date.today(), w, t, b))
        cursor.execute("UPDATE user_settings SET last_bench=%s WHERE user_id=%s", (b, uid))
        conn.commit()
        cursor.close()
        conn.close()
        await message.answer(f"✅ Данные приняты!\n⚖️ Вес: {w}кг | 📏 Талия: {t}см | 💪 Жим: {b}кг")
    except Exception as e:
        print(f"ОШИБКА СОХРАНЕНИЯ ЗАМЕРА: {e}")
        await message.answer(f"❌ Ошибка записи. Проверь логи Railway.")

@dp.message(F.photo | F.text)
async def handle_meal(message: Message):
    if message.text and message.text.startswith('/'): return
    uid = message.from_user.id
    conf = get_config(uid)
    msg_wait = await message.answer("🔍 Считаю...")
    
    contents = []
    if message.photo:
        file = await bot.get_file(message.photo[-1].file_id)
        file_bytes = await bot.download_file(file.file_path)
        contents.append({"mime_type": "image/jpeg", "data": file_bytes.read()})
    
    prompt = f"Ты диетолог. Цель: жим 100кг. Верни ТОЛЬКО JSON: {{\"total\": {{\"calories\": X, \"protein\": X, \"fat\": X, \"carbs\": X}}, \"name\": \"название\", \"comment\": \"совет\"}}"
    contents.append(message.text or "Еда на фото")
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
        cursor.close()
        conn.close()
        await msg_wait.edit_text(f"✅ **{data.get('name')}**\n🔥 {res['calories']:.0f} ккал\n💡 {data.get('comment')}")
    except Exception as e:
        print(f"ОШИБКА Gemini/DB: {e}")
        await msg_wait.edit_text("❌ Опиши еду текстом подробнее.")

async def main():
    init_db()
    # КРИТИЧЕСКИ ВАЖНО: сбрасываем старые соединения
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
