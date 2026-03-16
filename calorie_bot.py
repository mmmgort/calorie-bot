import asyncio
import os
import json
import re
import psycopg2
from datetime import datetime, date, time
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncioScheduler # Для уведомлений
import google.generativeai as genai

# ===================== НАСТРОЙКИ =====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncioScheduler()

# Клавиатура основного меню
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
                      (id SERIAL PRIMARY KEY, user_id BIGINT, date DATE, time TIME, meal_text TEXT, 
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
    # Проверяем всех активных пользователей (упрощенно)
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT user_id FROM user_settings")
    users = cursor.fetchall()
    
    now_hour = datetime.now().hour
    if 9 <= now_hour <= 22: # Напоминаем только с 9 утра до 10 вечера
        for (uid,) in users:
            cursor.execute("SELECT MAX(id) FROM meals WHERE user_id=%s AND date=%s", (uid, date.today()))
            last_meal = cursor.fetchone()[0]
            if not last_meal:
                try:
                    await bot.send_message(uid, "👋 Привет! Сегодня еще не было записей о еде. Чтобы пожать 100 кг, нужно вовремя заправляться! Скинь фото обеда? 🥗")
                except: pass
    cursor.close()
    conn.close()

# ===================== ХЕНДЛЕРЫ =====================

@dp.message(Command("start"))
async def cmd_start(message: Message):
    init_db()
    await message.answer(
        f"👋 Привет, {message.from_user.first_name}!\n\n"
        "Я твой ИИ-напарник. Моя цель — помочь тебе **пожать 100 кг**, сохранив форму.\n\n"
        "📥 **Просто кидай мне фото еды или пиши текстом.**\n"
        "📈 **Замеры:** используй `/log вес талия жим`", 
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

    res = (f"📊 **Твой рацион сегодня:**\n\n"
           f"Ккал: {e[0]:.0f} / {conf[0]}\n{pb(e[0], conf[0])}\n\n"
           f"🥩 Белки: {e[1]:.0f}г / {conf[1]}г\n"
           f"🥑 Жиры: {e[2]:.0f}г / {conf[2]}г\n"
           f"🌾 Угли: {e[3]:.0f}г / {conf[3]}г\n\n"
           f"🎯 Осталось: **{max(0, conf[0]-e[0]):.0f} ккал**")
    await message.answer(res)

@dp.message(F.text == "💪 Мой Жим")
async def show_bench(message: Message):
    conf = get_config(message.from_user.id)
    bench = conf[7]
    to_goal = 100 - bench
    msg = (f"🏋️‍♂️ **Твой текущий жим:** {bench} кг\n"
           f"🏁 **До цели 100 кг:** {max(0, to_goal)} кг\n\n"
           f"Чтобы обновить, напиши: `/log вес талия жим` (напр: `/log 92 90 85`)")
    await message.answer(msg)

@dp.message(F.text == "⚙️ Настройки")
async def settings(message: Message):
    conf = get_config(message.from_user.id)
    msg = (f"⚙️ **Твои настройки:**\n\n"
           f"🏠 Цель по талии: {conf[6]} см\n"
           f"🚄 Командировка: {'✅' if conf[4] else '❌'}\n"
           f"🏋️‍♂️ Тренировка: {'✅' if conf[5] else '❌'}\n\n"
           f"Команды:\n/trip — переключить поездку\n/train — режим тренировки\n/set_goal — изменить КБЖУ")
    await message.answer(msg)

@dp.message(F.photo | F.text)
async def handle_meal(message: Message):
    if message.text and message.text.startswith('/'): return
    
    uid = message.from_user.id
    conf = get_config(uid)
    msg_wait = await message.answer("🔍 Изучаю твою тарелку...")
    
    contents = []
    if message.photo:
        file = await bot.get_file(message.photo[-1].file_id)
        file_bytes = await bot.download_file(file.file_path)
        contents.append({"mime_type": "image/jpeg", "data": file_bytes.read()})
    
    prompt = f"""
    Ты диетолог. Цель юзера: жим 100 кг. Сейчас жмет {conf[7]}кг. 
    Норма: {conf[0]}ккал (Б:{conf[1]}, Ж:{conf[2]}, У:{conf[3]}). 
    Режим тренировки: {conf[5]}. Оцени еду.
    Дай краткий совет для силы.
    Верни JSON: {{"total": {{"calories": X, "protein": X, "fat": X, "carbs": X}}, "name": "название", "comment": "совет"}}
    """
    contents.append(message.text or "Оцени еду")
    contents.append(prompt)

    try:
        response = model.generate_content(contents)
        data = json.loads(re.sub(r'```json\s*|```', '', response.text).strip())
        res = data["total"]
        
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO meals (user_id, date, time, meal_text, calories, protein, fat, carbs) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                       (uid, date.today(), datetime.now().time(), data.get("name"), res["calories"], res["protein"], res["fat"], res["carbs"]))
        conn.commit()
        
        await msg_wait.edit_text(f"✅ **{data.get('name')}**\n\n🔥 {res['calories']:.0f} ккал\n🥩 Б: {res['protein']:.1f}г | 🥑 Ж: {res['fat']:.1f}г | 🌾 У: {res['carbs']:.1f}г\n\n💡 {data.get('comment')}")
    except:
        await msg_wait.edit_text("❌ Не удалось разобрать. Опиши еду текстом более детально!")

# ===================== ЗАПУСК =====================
async def main():
    init_db()
    # Запускаем планировщик уведомлений (проверка каждые 4 часа)
    scheduler.add_job(check_reminders, 'interval', hours=4)
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
