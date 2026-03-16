import asyncio
import sqlite3
import json
import os
from datetime import datetime, date
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, BufferedInputFile
from aiogram.fsm.storage.memory import MemoryStorage
import google.generativeai as genai   # временно оставляем, позже обновим

# ===================== НАСТРОЙКИ ИЗ RAILWAY =====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not BOT_TOKEN or not GEMINI_API_KEY:
    raise ValueError("❌ BOT_TOKEN или GEMINI_API_KEY не найдены в Variables!")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-2.5-flash')

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ===================== БАЗА ДАННЫХ =====================
conn = sqlite3.connect('calories.db', check_same_thread=False)
cursor = conn.cursor()
cursor.execute('''CREATE TABLE IF NOT EXISTS goals 
                  (user_id INTEGER PRIMARY KEY, calories INTEGER, protein INTEGER, fat INTEGER, carbs INTEGER)''')
cursor.execute('''CREATE TABLE IF NOT EXISTS meals 
                  (id INTEGER PRIMARY KEY, user_id INTEGER, date TEXT, meal_text TEXT, 
                   calories REAL, protein REAL, fat REAL, carbs REAL)''')
conn.commit()

DEFAULT_GOAL = (2600, 170, 75, 300)

def get_goal(user_id):
    cursor.execute("SELECT * FROM goals WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    if not row:
        cursor.execute("REPLACE INTO goals VALUES (?,?,?,?,?)", (user_id, *DEFAULT_GOAL))
        conn.commit()
        return DEFAULT_GOAL
    return row[1:]

# ===================== ПРОМПТЫ =====================
ANALYSIS_PROMPT = """
Ты — узкоспециализированный ИИ-диетолог для цели 100 кг жим лёжа и талия 87 см.
Анализируй фото или описание еды. Если вес не указан — оцени по ладони (\~150г).
Учитывай "Командировка" — добавь +10-15% жиров.
Верни ТОЛЬКО JSON:
{"total": {"calories": X, "protein": X, "fat": X, "carbs": X}}
"""

RECOMMEND_PROMPT = """
Сегодня: {time} | Тренировка: {training} | Командировка: {trip}
Набрано: {eaten_cal} ккал | Б:{eaten_p} | Ж:{eaten_f} | У:{eaten_c}
Осталось: {remain_cal} ккал | Б:{remain_p} | Ж:{remain_f} | У:{remain_c}

Предложи 2–3 варианта. Если >19:00 и белок <160г — только чистый белок.
Ответ — ТОЛЬКО таблица markdown:

| Вариант | Продукт/Блюдо | Кол-во | Ккал | Б | Ж | У |
|---------|---------------|--------|------|---|---|---|
"""

# ===================== ФУНКЦИИ =====================
def get_today_stats(user_id):
    today = date.today().isoformat()
    cursor.execute("SELECT SUM(calories), SUM(protein), SUM(fat), SUM(carbs) FROM meals WHERE user_id=? AND date=?", (user_id, today))
    row = cursor.fetchone()
    return row if row and row[0] else (0.0, 0.0, 0.0, 0.0)

# ===================== ХЕНДЛЕРЫ =====================
@dp.message(Command("start"))
async def start(message: Message):
    await message.answer(
        "✅ Бот запущен под цель 100 кг жим + талия 87 см\n"
        "Норма: 2600 ккал | Б 170 г | Ж 75 г | У 300 г\n\n"
        "Кидай фото еды или описание.\nКоманды: /goal /today /endday"
    )

@dp.message(Command("goal"))
async def set_goal(message: Message):
    try:
        _, c, p, f, u = message.text.split()
        cursor.execute("REPLACE INTO goals VALUES (?,?,?,?,?)", (message.from_user.id, int(c), int(p), int(f), int(u)))
        conn.commit()
        await message.answer(f"✅ Норма обновлена: {c} ккал | Б:{p} | Ж:{f} | У:{u}")
    except:
        await message.answer("Формат: /goal 2600 170 75 300")

@dp.message(F.photo | F.text)
async def handle_food(message: Message):
    user_id = message.from_user.id
    goal = get_goal(user_id)
    now = datetime.now()
    text = message.text or ""

    training = any(w in text.lower() for w in ["тренировка", "зал", "gym"])
    trip = "командировка" in text.lower()

    # Анализ через Gemini
    if message.photo:
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        bytes_data = await bot.download_file(file.file_path)
        contents = [genai.upload_file(BufferedInputFile(bytes_data.read(), "meal.jpg")), ANALYSIS_PROMPT]
    else:
        contents = [text + f"\nВремя: {now.strftime('%H:%M')}", ANALYSIS_PROMPT]

    try:
        response = model.generate_content(contents)
        data = json.loads(response.text.strip().strip('`json\n'))
        total = data["total"]

        # Сохраняем приём пищи
        cursor.execute(
            "INSERT INTO meals (user_id, date, meal_text, calories, protein, fat, carbs) VALUES (?,?,?,?,?,?,?)",
            (user_id, date.today().isoformat(), text or "Фото", 
             total["calories"], total["protein"], total["fat"], total["carbs"])
        )
        conn.commit()

        eaten = get_today_stats(user_id)
        remain = [goal[i] - eaten[i] for i in range(4)]

        status = f"""
📊 **Статус после приёма**

| Нутриент | Набрано | Осталось | % от нормы |
|----------|---------|----------|------------|
| Ккал     | {eaten[0]:.0f} | {remain[0]:.0f} | {eaten[0]/goal[0]*100:.0f}% |
| Белки    | {eaten[1]:.1f} | {remain[1]:.1f} | {eaten[1]/goal[1]*100:.0f}% |
| Жиры     | {eaten[2]:.1f} | {remain[2]:.1f} | {eaten[2]/goal[2]*100:.0f}% |
| Углеводы | {eaten[3]:.1f} | {remain[3]:.1f} | {eaten[3]/goal[3]*100:.0f}% |
"""
        await message.answer(status.strip())

        # Рекомендации
        rec_prompt = RECOMMEND_PROMPT.format(
            time=now.strftime("%H:%M"), training="да" if training else "нет",
            trip="да" if trip else "нет",
            eaten_cal=eaten[0], eaten_p=eaten[1], eaten_f=eaten[2], eaten_c=eaten[3],
            remain_cal=remain[0], remain_p=remain[1], remain_f=remain[2], remain_c=remain[3]
        )
        rec = model.generate_content(rec_prompt)
        await message.answer(rec.text)

        if now.hour >= 19 and eaten[1] < 160:
            await message.answer("🕘 После 19:00 — только чистый белок. Добей до 160 г.")

    except Exception as e:
        await message.answer(f"❌ Не удалось распознать еду.\nПопробуй описать подробнее или пришли другое фото.")

# ===================== ЗАПУСК =====================
async def main():
    print("🚀 Бот успешно запущен под цель 100 кг жим лёжа + талия 87 см")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
