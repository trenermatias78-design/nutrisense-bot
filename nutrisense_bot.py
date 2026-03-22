# NutriSense Bot
# @NutriSenseUABot - personal nutritionist
# aiogram 3.x, SQLite, Monobank jar payment
# Admin ID: 342045533

import asyncio
import logging
import os
import math
import json
import aiosqlite
import aiohttp
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup,
    InlineKeyboardButton, LabeledPrice, PreCheckoutQuery,
    ContentType
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("NutriSense")

BOT_TOKEN = "8740306918:AAEDMvoW4ZJ5lSvrkd8NGmFaoavhXcc5EaA"
MONO_TOKEN = os.getenv("MONO_TOKEN", "")
ADMIN_ID = 342045533
CHANNEL_ID = os.getenv("CHANNEL_ID", "@matmatias")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
MONO_JAR_URL = "https://send.monobank.ua/jar/8hUo6jMR5M"
DB_PATH = "nutrisense.db"
PLANS = {
    "start": {
        "name": "Start",
        "price_uah": 199,
        "price_kopiy": 19900,
        "days": 30,
        "emoji": "🌱",
        "features": [
            "📊 Точний розрахунок КБЖУ",
            "🍽 Індивідуальний план харчування",
            "🧠 Тест харчової поведінки",
            "📚 28 тижнів освітнього контенту",
            "📋 Рекомендації по продуктах",
        ]
    },
    "premium": {
        "name": "Premium",
        "price_uah": 349,
        "price_kopiy": 34900,
        "days": 30,
        "emoji": "🌿",
        "features": [
            "✅ Все з Start",
            "📸 Аналіз фото тарілки",
            "📅 Щоденний трекер їжі",
            "📈 Щотижневий звіт прогресу",
            "💬 Пріоритетна підтримка",
        ]
    },
    "vip": {
        "name": "VIP",
        "price_uah": 549,
        "price_kopiy": 54900,
        "days": 30,
        "emoji": "👑",
        "features": [
            "✅ Все з Premium",
            "🤖 ШІ-нутриціолог необмежено",
            "🎯 Персональний план на місяць",
            "⚡ Відповідь протягом 1 години",
            "🏆 VIP-чат з нутриціологом",
        ]
    }
}

ACTIVITY_LEVELS = {
    "sedentary":   (1.2,   "Малорухливий (офіс, без спорту)"),
    "light":       (1.375, "Легка активність (1-3 тренування/тиж)"),
    "moderate":    (1.55,  "Помірна активність (3-5 тренувань/тиж)"),
    "active":      (1.725, "Висока активність (6-7 тренувань/тиж)"),
    "very_active": (1.9,   "Дуже висока (спортсмен/фізична робота)"),
}

GOALS = {
    "lose":     ("Схуднення (-300 ккал)",  -300),
    "maintain": ("Підтримка ваги (0 ккал)",   0),
    "gain":     ("Набір маси (+300 ккал)", +300),
}
class KBJUState(StatesGroup):
    waiting_gender   = State()
    waiting_age      = State()
    waiting_weight   = State()
    waiting_height   = State()
    waiting_activity = State()
    waiting_goal     = State()

class BehaviorTestState(StatesGroup):
    waiting_answer = State()

class AdminBroadcastState(StatesGroup):
    waiting_text = State()

class AdminPaymentState(StatesGroup):
    waiting_user_id = State()
    waiting_plan    = State()

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                username    TEXT,
                full_name   TEXT,
                plan        TEXT DEFAULT 'free',
                plan_until  TEXT,
                joined_at   TEXT DEFAULT CURRENT_TIMESTAMP,
                week        INTEGER DEFAULT 1,
                last_post   TEXT,
                gender      TEXT,
                age         INTEGER,
                weight      REAL,
                height      REAL,
                activity    REAL,
                goal        TEXT,
                calories    INTEGER,
                protein     REAL,
                fat         REAL,
                carbs       REAL
            );
            CREATE TABLE IF NOT EXISTS payments (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER,
                plan        TEXT,
                amount_uah  INTEGER,
                invoice_id  TEXT,
                status      TEXT DEFAULT 'pending',
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
                paid_at     TEXT
            );
            CREATE TABLE IF NOT EXISTS tracker (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER,
                date        TEXT,
                meal        TEXT,
                note        TEXT,
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await db.commit()
    log.info("DB initialized")

async def get_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

async def upsert_user(user_id: int, **kwargs):
    async with aiosqlite.connect(DB_PATH) as db:
        existing = await get_user(user_id)
        if not existing:
            await db.execute(
                "INSERT INTO users (user_id) VALUES (?)", (user_id,)
            )
        if kwargs:
            sets = ", ".join(f"{k} = ?" for k in kwargs)
            vals = list(kwargs.values()) + [user_id]
            await db.execute(
                f"UPDATE users SET {sets} WHERE user_id = ?", vals
            )
        await db.commit()

async def get_all_users():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users") as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

async def get_stats():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as c:
            total = (await c.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM users WHERE plan != 'free'"
        ) as c:
            paid = (await c.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM payments WHERE status = 'paid'"
        ) as c:
            payments = (await c.fetchone())[0]
        async with db.execute(
            "SELECT SUM(amount_uah) FROM payments WHERE status = 'paid'"
        ) as c:
            revenue = (await c.fetchone())[0] or 0
    return {"total": total, "paid": paid,
            "payments": payments, "revenue": revenue}

async def save_payment(user_id: int, plan: str, amount: int, invoice_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO payments (user_id, plan, amount_uah, invoice_id)
               VALUES (?, ?, ?, ?)""",
            (user_id, plan, amount, invoice_id)
        )
        await db.commit()

def has_access(user: dict, required: str) -> bool:
    order = {"free": 0, "start": 1, "premium": 2, "vip": 3}
    if not user:
        return False
    plan = user.get("plan", "free")
    until_str = user.get("plan_until")
    if plan != "free" and until_str:
        until = datetime.strptime(until_str, "%Y-%m-%d %H:%M:%S")
        if datetime.now() > until:
            return False
    return order.get(plan, 0) >= order.get(required, 0)

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID
def calculate_kbju(gender, age, weight, height, activity, goal):
    if gender == "female":
        bmr = 10 * weight + 6.25 * height - 5 * age - 161
    else:
        bmr = 10 * weight + 6.25 * height - 5 * age + 5
    tdee = bmr * activity
    goal_delta = GOALS[goal][1]
    calories = round(tdee + goal_delta)
    protein = round(weight * 1.7)
    fat = round(calories * 0.28 / 9)
    carbs = round((calories - protein * 4 - fat * 9) / 4)
    return {
        "bmr": round(bmr), "tdee": round(tdee),
        "calories": calories, "protein": protein,
        "fat": fat, "carbs": carbs,
    }

def build_meal_plan(calories, protein, fat, carbs):
    dist = {
        "🌅 Сніданок":  (0.30, 0.25, 0.35, 0.25),
        "🍱 Перекус 1": (0.10, 0.15, 0.10, 0.10),
        "☀️ Обід":      (0.35, 0.35, 0.30, 0.40),
        "🍎 Перекус 2": (0.10, 0.15, 0.10, 0.10),
        "🌙 Вечеря":    (0.15, 0.10, 0.15, 0.15),
    }
    lines = ["<b>📋 Розподіл по прийомах їжі:</b>\n"]
    for meal, (kc, pr, ft, cb) in dist.items():
        lines.append(
            f"{meal}\n"
            f"  Калорії: {round(calories*kc)} ккал\n"
            f"  Білок:   {round(protein*pr)} г\n"
            f"  Жири:    {round(fat*ft)} г\n"
            f"  Вуглев.: {round(carbs*cb)} г\n"
        )
    return "\n".join(lines)

def kb_main_menu(plan="free"):
    b = InlineKeyboardBuilder()
    b.button(text="📊 Розрахувати КБЖУ", callback_data="kbju_start")
    b.button(text="🍽 Мій план харчування", callback_data="my_plan")
    b.button(text="🧠 Тест поведінки", callback_data="behavior_test")
    b.button(text="📚 Контент тижня", callback_data="weekly_content")
    if plan in ("premium", "vip"):
        b.button(text="📸 Аналіз тарілки", callback_data="plate_analysis")
    if plan == "vip":
        b.button(text="🤖 ШІ-нутриціолог", callback_data="ai_nutritionist")
    b.button(text="📅 Трекер їжі", callback_data="tracker")
    b.button(text="💎 Тарифи", callback_data="plans")
    b.button(text="👤 Мій профіль", callback_data="my_profile")
    b.adjust(2, 2, 2, 2, 1)
    return b.as_markup()

def kb_plans():
    b = InlineKeyboardBuilder()
    b.button(text="🌱 Start — 199 грн/міс", callback_data="buy_start")
    b.button(text="🌿 Premium — 349 грн/міс", callback_data="buy_premium")
    b.button(text="👑 VIP — 549 грн/міс", callback_data="buy_vip")
    b.button(text="◀️ Назад", callback_data="main_menu")
    b.adjust(1)
    return b.as_markup()

def kb_confirm_buy(plan):
    b = InlineKeyboardBuilder()
    b.button(text="💳 Оплатити", callback_data=f"pay_card_{plan}")
    b.button(text="◀️ Назад", callback_data="plans")
    b.adjust(1)
    return b.as_markup()

def kb_activity():
    b = InlineKeyboardBuilder()
    for key, (_, label) in ACTIVITY_LEVELS.items():
        b.button(text=label, callback_data=f"activity_{key}")
    b.adjust(1)
    return b.as_markup()

def kb_goal():
    b = InlineKeyboardBuilder()
    for key, (label, _) in GOALS.items():
        b.button(text=label, callback_data=f"goal_{key}")
    b.adjust(1)
    return b.as_markup()

def kb_gender():
    b = InlineKeyboardBuilder()
    b.button(text="👩 Жінка", callback_data="gender_female")
    b.button(text="👨 Чоловік", callback_data="gender_male")
    b.adjust(2)
    return b.as_markup()

def kb_back_main():
    b = InlineKeyboardBuilder()
    b.button(text="🏠 Головне меню", callback_data="main_menu")
    b.adjust(1)
    return b.as_markup()

def kb_admin():
    b = InlineKeyboardBuilder()
    b.button(text="📊 Статистика", callback_data="adm_stats")
    b.button(text="👥 Всі юзери", callback_data="adm_users")
    b.button(text="📢 Розсилка", callback_data="adm_broadcast")
    b.button(text="📤 Контент всім", callback_data="adm_send_content")
    b.adjust(2)
    return b.as_markup()

def kb_behavior_test(q_num):
    options = {
        0: [("👍 Так, часто", "bt_yes"),
            ("🤔 Іноді", "bt_sometimes"),
            ("👎 Рідко", "bt_no")],
        1: [("🟢 Наростає поступово", "bt_gradual"),
            ("🔴 Виникає раптово", "bt_sudden"),
            ("⚪ По-різному", "bt_varies")],
        2: [("😔 Від стресу", "bt_stress"),
            ("😴 Від нудьги", "bt_bored"),
            ("😋 Від запаху/вигляду", "bt_sensory")],
        3: [("✅ Так, легко", "bt_easy"),
            ("⚠️ Важко, але можу", "bt_hard"),
            ("❌ Не можу зупинитись", "bt_cant")],
        4: [("😔 Часто соромлюсь", "bt_shame"),
            ("😌 Іноді", "bt_sometimes2"),
            ("😊 Рідко", "bt_never")],
    }
    b = InlineKeyboardBuilder()
    for text, cb in options.get(q_num, []):
        b.button(text=text, callback_data=cb)
    b.adjust(1)
    return b.as_markup()

BEHAVIOR_QUESTIONS = [
    "1️⃣ Чи буває що їси без фізичного голоду\n(від нудьги, стресу, емоцій)?",
    "2️⃣ Як зазвичай з'являється голод?",
    "3️⃣ Що найчастіше провокує їсти не в свій час?",
    "4️⃣ Чи вдається зупинитись після першого шматочка?",
    "5️⃣ Чи виникає сором або провина після їжі?",
]

def analyze_behavior(answers):
    score = 0
    if "bt_yes" in answers: score += 2
    if "bt_sometimes" in answers: score += 1
    if "bt_sudden" in answers: score += 2
    if "bt_stress" in answers: score += 2
    if "bt_bored" in answers: score += 1
    if "bt_cant" in answers: score += 3
    if "bt_shame" in answers: score += 2
    if score <= 3:
        return (
            "🟢 <b>Гомеостатичний тип</b>\n\n"
            "Харчування за фізичним голодом.\n"
            "Насичення відчувається добре.\n\n"
            "✅ Продовжуй слухати тіло\n"
            "✅ Стеж за балансом макронутрієнтів\n"
            "✅ Перевір КБЖУ /kbju"
        )
    elif score <= 7:
        return (
            "🟡 <b>Гедонічний тип</b>\n\n"
            "Іноді їжа від емоцій або задоволення.\n\n"
            "🔸 Визначай рівень голоду (шкала 1-10)\n"
            "🔸 Знайди альтернативи для емоцій\n"
            "🔸 Регулярні прийоми їжі зменшать тягу"
        )
    else:
        return (
            "🔴 <b>Дисрегульований тип</b>\n\n"
            "Харчування часто некероване.\n\n"
            "🔴 Регулярні прийоми кожні 3-4 год\n"
            "🔴 Достатня калорійність\n"
            "🔴 Якісний сон 7-9 годин\n"
            "🔴 Зниження стресу"
        )
router = Router()

@router.message(CommandStart())
async def cmd_start(msg: Message):
    await upsert_user(
        msg.from_user.id,
        username=msg.from_user.username or "",
        full_name=msg.from_user.full_name or "",
    )
    user = await get_user(msg.from_user.id)
    plan = user.get("plan", "free") if user else "free"
    text = (
        "🌿 <b>Вітаю в NutriSense!</b>\n\n"
        "Я твій особистий нутриціолог у Telegram.\n\n"
        "Що я вмію:\n"
        "📊 Точно розраховую КБЖУ за Міффліним\n"
        "🍽 Складаю план харчування по прийомах\n"
        "🧠 Визначаю тип харчової поведінки\n"
        "📚 Надсилаю контент 28 тижнів\n"
        "📸 Аналізую фото тарілки (Premium)\n"
        "🤖 Відповідаю на питання (VIP)\n\n"
        "<b>Перший розрахунок КБЖУ — безкоштовно!</b>"
    )
    await msg.answer(text, reply_markup=kb_main_menu(plan))

@router.message(Command("menu"))
async def cmd_menu(msg: Message):
    user = await get_user(msg.from_user.id)
    plan = user.get("plan", "free") if user else "free"
    await msg.answer("🏠 Головне меню:", reply_markup=kb_main_menu(plan))

@router.message(Command("admin"))
async def cmd_admin(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    await msg.answer("🔐 <b>Адмін панель NutriSense</b>", reply_markup=kb_admin())

@router.message(Command("kbju"))
async def cmd_kbju(msg: Message, state: FSMContext):
    await state.set_state(KBJUState.waiting_gender)
    await msg.answer(
        "📊 <b>Розрахунок КБЖУ</b>\n\nОбери стать:",
        reply_markup=kb_gender()
    )

@router.callback_query(F.data == "kbju_start")
async def cb_kbju_start(cq: CallbackQuery, state: FSMContext):
    await state.set_state(KBJUState.waiting_gender)
    await cq.message.edit_text(
        "📊 <b>Розрахунок КБЖУ</b>\n\nОбери стать:",
        reply_markup=kb_gender()
    )
    await cq.answer()

@router.callback_query(F.data.startswith("gender_"), KBJUState.waiting_gender)
async def kbju_gender(cq: CallbackQuery, state: FSMContext):
    gender = cq.data.split("_")[1]
    await state.update_data(gender=gender)
    await state.set_state(KBJUState.waiting_age)
    await cq.message.edit_text("Введи свій <b>вік</b> (наприклад: 27):")
    await cq.answer()

@router.message(KBJUState.waiting_age)
async def kbju_age(msg: Message, state: FSMContext):
    try:
        age = int(msg.text.strip())
        if not 10 <= age <= 100:
            raise ValueError
        await state.update_data(age=age)
        await state.set_state(KBJUState.waiting_weight)
        await msg.answer("Введи свою <b>вагу</b> (кг, наприклад: 65):")
    except ValueError:
        await msg.answer("⚠️ Введи коректний вік (10-100):")

@router.message(KBJUState.waiting_weight)
async def kbju_weight(msg: Message, state: FSMContext):
    try:
        weight = float(msg.text.strip().replace(",", "."))
        if not 30 <= weight <= 300:
            raise ValueError
        await state.update_data(weight=weight)
        await state.set_state(KBJUState.waiting_height)
        await msg.answer("Введи свій <b>зріст</b> (см, наприклад: 168):")
    except ValueError:
        await msg.answer("⚠️ Введи коректну вагу (30-300 кг):")

@router.message(KBJUState.waiting_height)
async def kbju_height(msg: Message, state: FSMContext):
    try:
        height = float(msg.text.strip().replace(",", "."))
        if not 100 <= height <= 250:
            raise ValueError
        await state.update_data(height=height)
        await state.set_state(KBJUState.waiting_activity)
        await msg.answer(
            "Обери рівень <b>активності</b>:",
            reply_markup=kb_activity()
        )
    except ValueError:
        await msg.answer("⚠️ Введи коректний зріст (100-250 см):")

@router.callback_query(F.data.startswith("activity_"), KBJUState.waiting_activity)
async def kbju_activity(cq: CallbackQuery, state: FSMContext):
    key = cq.data.replace("activity_", "")
    coef, label = ACTIVITY_LEVELS[key]
    await state.update_data(activity=coef, activity_label=label)
    await state.set_state(KBJUState.waiting_goal)
    await cq.message.edit_text("Яка твоя <b>ціль</b>?", reply_markup=kb_goal())
    await cq.answer()

@router.callback_query(F.data.startswith("goal_"), KBJUState.waiting_goal)
async def kbju_goal(cq: CallbackQuery, state: FSMContext):
    goal = cq.data.replace("goal_", "")
    data = await state.get_data()
    await state.clear()
    result = calculate_kbju(
        data["gender"], data["age"], data["weight"],
        data["height"], data["activity"], goal
    )
    await upsert_user(
        cq.from_user.id,
        gender=data["gender"], age=data["age"],
        weight=data["weight"], height=data["height"],
        activity=data["activity"], goal=goal,
        calories=result["calories"], protein=result["protein"],
        fat=result["fat"], carbs=result["carbs"],
    )
    meal_plan = build_meal_plan(
        result["calories"], result["protein"],
        result["fat"], result["carbs"]
    )
    goal_label = GOALS[goal][0]
    gender_label = "Жінка" if data["gender"] == "female" else "Чоловік"
    text = (
        f"✅ <b>Твій розрахунок КБЖУ</b>\n\n"
        f"👤 {gender_label}, {data['age']} р., "
        f"{data['weight']} кг, {data['height']} см\n"
        f"🎯 {goal_label}\n\n"
        f"🔥 <b>BMR:</b> {result['bmr']} ккал\n"
        f"⚡ <b>TDEE:</b> {result['tdee']} ккал\n"
        f"🎯 <b>Норма:</b> {result['calories']} ккал\n\n"
        f"🥩 Білок: {result['protein']} г\n"
        f"🧈 Жири: {result['fat']} г\n"
        f"🌾 Вуглеводи: {result['carbs']} г\n\n"
        f"{meal_plan}"
    )
    user = await get_user(cq.from_user.id)
    plan = user.get("plan", "free") if user else "free"
    await cq.message.edit_text(text, reply_markup=kb_main_menu(plan))
    await cq.answer("✅ Готово!")
@router.callback_query(F.data == "main_menu")
async def cb_main_menu(cq: CallbackQuery):
    user = await get_user(cq.from_user.id)
    plan = user.get("plan", "free") if user else "free"
    await cq.message.edit_text(
        "🏠 <b>Головне меню NutriSense</b>",
        reply_markup=kb_main_menu(plan)
    )
    await cq.answer()

@router.callback_query(F.data == "my_plan")
async def cb_my_plan(cq: CallbackQuery):
    user = await get_user(cq.from_user.id)
    if not user or not user.get("calories"):
        await cq.answer("⚠️ Спочатку розрахуй КБЖУ!", show_alert=True)
        return
    meal = build_meal_plan(
        user["calories"], user["protein"],
        user["fat"], user["carbs"]
    )
    goal_label = GOALS.get(user["goal"], ("Не вказано",))[0]
    text = (
        f"🍽 <b>Твій план харчування</b>\n\n"
        f"🎯 Ціль: {goal_label}\n"
        f"🔥 Норма: {user['calories']} ккал/день\n"
        f"🥩 Б: {user['protein']}г  "
        f"🧈 Ж: {user['fat']}г  "
        f"🌾 В: {user['carbs']}г\n\n"
        f"{meal}\n"
        f"<b>📌 Рекомендовані продукти:</b>\n\n"
        f"🌾 Крупи: гречка, овес, рис, булгур\n"
        f"🥩 Білок: курка, індичка, тріска, яйця\n"
        f"🫒 Жири: авокадо, оливкова олія, горіхи\n"
        f"🥗 Овочі: броколі, шпинат, кабачок\n"
        f"🍎 Фрукти: ягоди, ківі, яблуко\n"
        f"🦠 Пробіотики: йогурт, кефір, квашена капуста"
    )
    await cq.message.edit_text(text, reply_markup=kb_back_main())
    await cq.answer()

@router.callback_query(F.data == "plans")
async def cb_plans(cq: CallbackQuery):
    text = "💎 <b>Тарифи NutriSense</b>\n\n"
    for key, plan in PLANS.items():
        text += f"{plan['emoji']} <b>{plan['name']}</b> — {plan['price_uah']} грн/міс\n"
        for f in plan["features"]:
            text += f"  {f}\n"
        text += "\n"
    await cq.message.edit_text(text, reply_markup=kb_plans())
    await cq.answer()

@router.callback_query(F.data.startswith("buy_"))
async def cb_buy(cq: CallbackQuery):
    plan = cq.data.replace("buy_", "")
    if plan not in PLANS:
        await cq.answer("⚠️ Помилка", show_alert=True)
        return
    p = PLANS[plan]
    text = (
        f"{p['emoji']} <b>{p['name']}</b>\n\n"
        f"💰 Вартість: <b>{p['price_uah']} грн/місяць</b>\n"
        f"📅 Доступ: 30 днів\n\n"
        f"<b>Включає:</b>\n"
        + "\n".join(p["features"])
        + "\n\nОплата через Monobank 💳"
    )
    await cq.message.edit_text(text, reply_markup=kb_confirm_buy(plan))
    await cq.answer()

@router.callback_query(F.data.startswith("pay_card_"))
async def cb_pay_card(cq: CallbackQuery):
    plan = cq.data.replace("pay_card_", "")
    if plan not in PLANS:
        await cq.answer("⚠️ Помилка", show_alert=True)
        return
    p = PLANS[plan]
    user = cq.from_user
    await save_payment(
        user_id=user.id,
        plan=plan,
        amount=p["price_uah"],
        invoice_id=f"jar_{user.id}_{plan}_{int(datetime.now().timestamp())}"
    )
    try:
        await cq.bot.send_message(
            ADMIN_ID,
            f"💰 <b>Новий запит на оплату!</b>\n\n"
            f"👤 {user.full_name} (@{user.username or '-'})\n"
            f"🆔 ID: {user.id}\n"
            f"💎 Тариф: {p['name']}\n"
            f"💵 Сума: {p['price_uah']} грн"
        )
    except Exception as e:
        log.warning(f"Cannot notify admin: {e}")
    b = InlineKeyboardBuilder()
    b.button(text="💳 Перейти до оплати", url=MONO_JAR_URL)
    b.button(text="✅ Я оплатив(ла)", callback_data=f"paid_notify_{plan}")
    b.button(text="◀️ Назад", callback_data="plans")
    b.adjust(1)
    await cq.message.edit_text(
        f"💳 <b>Оплата {p['name']}</b>\n\n"
        f"💵 Сума: <b>{p['price_uah']} грн</b>\n\n"
        f"1️⃣ Натисни <b>Перейти до оплати</b>\n"
        f"2️⃣ Переведи рівно <b>{p['price_uah']} грн</b>\n"
        f"3️⃣ Натисни <b>Я оплатив(ла)</b>\n"
        f"4️⃣ Підписка активується за кілька хвилин ⚡",
        reply_markup=b.as_markup()
    )
    await cq.answer()

@router.callback_query(F.data.startswith("paid_notify_"))
async def cb_paid_notify(cq: CallbackQuery):
    plan = cq.data.replace("paid_notify_", "")
    user = cq.from_user
    p = PLANS.get(plan, {})
    b = InlineKeyboardBuilder()
    b.button(
        text=f"✅ Активувати {p.get('name', plan)}",
        callback_data=f"adm_oneclick_{user.id}_{plan}"
    )
    b.button(text="❌ Відхилити", callback_data=f"adm_reject_{user.id}")
    b.adjust(1)
    try:
        await cq.bot.send_message(
            ADMIN_ID,
            f"🔔 <b>Клієнт каже що оплатив!</b>\n\n"
            f"👤 {user.full_name} (@{user.username or '-'})\n"
            f"💎 Тариф: {p.get('name', plan)}\n"
            f"💵 Сума: {p.get('price_uah', '?')} грн\n\n"
            f"Перевір банку і натисни 👇",
            reply_markup=b.as_markup()
        )
    except Exception as e:
        log.warning(f"Cannot notify admin: {e}")
    await cq.message.edit_text(
        "✅ <b>Дякуємо!</b>\n\n"
        "Отримали сповіщення і перевіряємо оплату.\n"
        "Підписка активується за кілька хвилин 🌿",
        reply_markup=kb_back_main()
    )
    await cq.answer("✅ Сповіщення надіслано!")
@router.callback_query(F.data.startswith("adm_oneclick_"))
async def adm_oneclick_activate(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        return
    parts = cq.data.split("_")
    user_id = int(parts[2])
    plan = parts[3]
    plan_until = (
        datetime.now() + timedelta(days=PLANS[plan]["days"])
    ).strftime("%Y-%m-%d %H:%M:%S")
    await upsert_user(user_id, plan=plan, plan_until=plan_until)
    try:
        await cq.bot.send_message(
            user_id,
            f"🎉 <b>Підписку активовано!</b>\n\n"
            f"Тариф: {PLANS[plan]['name']}\n"
            f"Діє до: {plan_until[:10]}\n\n"
            f"Дякуємо! 🌿\n/menu — головне меню"
        )
    except Exception as e:
        log.warning(f"Cannot notify user {user_id}: {e}")
    await cq.message.edit_text(
        f"✅ Активовано!\n"
        f"Юзер {user_id} — {PLANS[plan]['name']}\n"
        f"До: {plan_until[:10]}"
    )
    await cq.answer("✅ Підписку активовано!")

@router.callback_query(F.data.startswith("adm_reject_"))
async def adm_reject_payment(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        return
    user_id = int(cq.data.split("_")[2])
    try:
        await cq.bot.send_message(
            user_id,
            "⚠️ <b>Оплату не знайдено.</b>\n\n"
            "Перевір чи правильно переведена сума "
            "або напиши адміністратору."
        )
    except Exception:
        pass
    await cq.message.edit_text(f"❌ Оплату юзера {user_id} відхилено.")
    await cq.answer("❌ Відхилено")

@router.callback_query(F.data == "behavior_test")
async def cb_behavior_test(cq: CallbackQuery, state: FSMContext):
    await state.set_state(BehaviorTestState.waiting_answer)
    await state.update_data(answers=[], q_num=0)
    await cq.message.edit_text(
        "🧠 <b>Тест харчової поведінки</b>\n\n"
        "5 питань визначать твій тип.\n\n"
        f"{BEHAVIOR_QUESTIONS[0]}",
        reply_markup=kb_behavior_test(0)
    )
    await cq.answer()

@router.callback_query(BehaviorTestState.waiting_answer, F.data.startswith("bt_"))
async def cb_behavior_answer(cq: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    answers = data.get("answers", [])
    q_num = data.get("q_num", 0)
    answers.append(cq.data)
    q_num += 1
    if q_num >= len(BEHAVIOR_QUESTIONS):
        await state.clear()
        result_text = analyze_behavior(answers)
        b = InlineKeyboardBuilder()
        b.button(text="🏠 Головне меню", callback_data="main_menu")
        await cq.message.edit_text(
            f"🧠 <b>Результат тесту</b>\n\n{result_text}",
            reply_markup=b.as_markup()
        )
    else:
        await state.update_data(answers=answers, q_num=q_num)
        await cq.message.edit_text(
            f"<b>Питання {q_num+1}/5</b>\n\n{BEHAVIOR_QUESTIONS[q_num]}",
            reply_markup=kb_behavior_test(q_num)
        )
    await cq.answer()

@router.callback_query(F.data == "tracker")
async def cb_tracker(cq: CallbackQuery):
    user = await get_user(cq.from_user.id)
    if not has_access(user, "premium"):
        await cq.answer("🔒 Трекер доступний з Premium", show_alert=True)
        return
    today = datetime.now().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM tracker WHERE user_id = ? AND date = ?",
            (cq.from_user.id, today)
        ) as cur:
            entries = await cur.fetchall()
    text = f"📅 <b>Трекер їжі — {today}</b>\n\n"
    if entries:
        for e in entries:
            text += f"• {e['meal']}: {e['note']}\n"
    else:
        text += "Записів немає.\nФормат: Сніданок: вівсянка, яйця"
    b = InlineKeyboardBuilder()
    b.button(text="🏠 Головне меню", callback_data="main_menu")
    await cq.message.edit_text(text, reply_markup=b.as_markup())
    await cq.answer()

@router.callback_query(F.data == "plate_analysis")
async def cb_plate_analysis(cq: CallbackQuery):
    user = await get_user(cq.from_user.id)
    if not has_access(user, "premium"):
        await cq.answer("🔒 Доступний з Premium", show_alert=True)
        return
    await cq.message.edit_text(
        "📸 <b>Аналіз тарілки</b>\n\n"
        "Надішли фото своєї тарілки 👇",
        reply_markup=kb_back_main()
    )
    await cq.answer()

@router.message(F.content_type == ContentType.PHOTO)
async def handle_photo(msg: Message):
    user = await get_user(msg.from_user.id)
    if not has_access(user, "premium"):
        await msg.answer("🔒 Аналіз фото доступний з Premium. /plans")
        return
    await msg.answer(
        "🔍 <b>Аналіз тарілки:</b>\n\n"
        "✅ Є джерело білку\n"
        "✅ Є овочі\n"
        "⚠️ Додай більше різнокольорових овочів\n"
        "⚠️ Додай джерело корисних жирів\n\n"
        "💡 Прагни до правила тарілки:\n"
        "½ овочі · ¼ білок · ¼ крупи",
        reply_markup=kb_back_main()
    )

@router.callback_query(F.data == "ai_nutritionist")
async def cb_ai_nutritionist(cq: CallbackQuery):
    user = await get_user(cq.from_user.id)
    if not has_access(user, "vip"):
        await cq.answer("🔒 Доступний з VIP", show_alert=True)
        return
    await cq.message.edit_text(
        "🤖 <b>ШІ-нутриціолог</b>\n\n"
        "Постав будь-яке питання про харчування 👇",
        reply_markup=kb_back_main()
    )
    await cq.answer()

@router.callback_query(F.data == "my_profile")
async def cb_my_profile(cq: CallbackQuery):
    user = await get_user(cq.from_user.id)
    if not user:
        await cq.answer("⚠️ Профіль не знайдено", show_alert=True)
        return
    plan_name = PLANS.get(user["plan"], {}).get("name", "Free") if user["plan"] != "free" else "Free"
    until_str = user.get("plan_until", "")
    if until_str:
        until = datetime.strptime(until_str, "%Y-%m-%d %H:%M:%S")
        days_left = max(0, (until - datetime.now()).days)
        until_label = f"{until.strftime('%d.%m.%Y')} (ще {days_left} дн.)"
    else:
        until_label = "—"
    text = (
        f"👤 <b>Мій профіль</b>\n\n"
        f"💎 Тариф: {plan_name}\n"
        f"📅 Діє до: {until_label}\n"
        f"📚 Тиждень: {user.get('week', 1)}/28\n\n"
    )
    if user.get("calories"):
        goal_label = GOALS.get(user["goal"], ("—",))[0]
        text += (
            f"<b>📊 Мій КБЖУ:</b>\n"
            f"🔥 {user['calories']} ккал/день\n"
            f"🥩 Білок: {user['protein']}г\n"
            f"🧈 Жири: {user['fat']}г\n"
            f"🌾 Вуглеводи: {user['carbs']}г\n"
            f"🎯 Ціль: {goal_label}\n"
        )
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Перерахувати КБЖУ", callback_data="kbju_start")
    b.button(text="💎 Підписка", callback_data="plans")
    b.button(text="🏠 Головне меню", callback_data="main_menu")
    b.adjust(1)
    await cq.message.edit_text(text, reply_markup=b.as_markup())
    await cq.answer()
@router.callback_query(F.data == "adm_stats")
async def adm_stats(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        return
    stats = await get_stats()
    text = (
        f"📊 <b>Статистика NutriSense</b>\n\n"
        f"👥 Всього юзерів: {stats['total']}\n"
        f"💎 Платних: {stats['paid']}\n"
        f"🆓 Безкоштовних: {stats['total'] - stats['paid']}\n"
        f"✅ Успішних оплат: {stats['payments']}\n"
        f"💰 Дохід: {stats['revenue']} грн\n"
        f"📅 {datetime.now().strftime('%d.%m.%Y %H:%M')}"
    )
    b = InlineKeyboardBuilder()
    b.button(text="◀️ Назад", callback_data="adm_back")
    await cq.message.edit_text(text, reply_markup=b.as_markup())
    await cq.answer()

@router.callback_query(F.data == "adm_users")
async def adm_users(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        return
    users = await get_all_users()
    text = f"👥 <b>Користувачі ({len(users)}):</b>\n\n"
    for u in users[:20]:
        plan = u.get("plan", "free")
        name = u.get("full_name") or u.get("username") or str(u["user_id"])
        text += f"• {name} | {plan} | тиж.{u.get('week', 1)}\n"
    if len(users) > 20:
        text += f"\n...і ще {len(users)-20}"
    b = InlineKeyboardBuilder()
    b.button(text="◀️ Назад", callback_data="adm_back")
    await cq.message.edit_text(text, reply_markup=b.as_markup())
    await cq.answer()

@router.callback_query(F.data == "adm_broadcast")
async def adm_broadcast(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        return
    await state.set_state(AdminBroadcastState.waiting_text)
    await cq.message.edit_text(
        "📢 <b>Розсилка</b>\n\nНадішли текст для всіх юзерів:"
    )
    await cq.answer()

@router.message(AdminBroadcastState.waiting_text)
async def adm_do_broadcast(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    await state.clear()
    users = await get_all_users()
    sent = failed = 0
    for u in users:
        try:
            await msg.bot.send_message(u["user_id"], msg.text)
            sent += 1
        except Exception:
            failed += 1
    await msg.answer(
        f"✅ Розсилка завершена\n"
        f"📤 Відправлено: {sent}\n"
        f"❌ Помилок: {failed}",
        reply_markup=kb_admin()
    )

@router.callback_query(F.data == "adm_send_content")
async def adm_send_content(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        return
    await cq.answer("⏳ Відправляю...")
    users = await get_all_users()
    sent = 0
    for u in users:
        if not has_access(u, "start"):
            continue
        week_num = u.get("week", 1) or 1
        week_data = WEEKLY_CONTENT.get(week_num)
        if not week_data:
            continue
        posts = week_data["posts"]
        if not posts:
            continue
        post = posts[0]
        try:
            b = InlineKeyboardBuilder()
            b.button(text="📚 Всі матеріали тижня", callback_data="weekly_content")
            await cq.bot.send_message(
                u["user_id"], post["text"],
                reply_markup=b.as_markup()
            )
            next_week = min(week_num + 1, 28)
            await upsert_user(u["user_id"], week=next_week)
            sent += 1
        except Exception:
            pass
    await cq.message.edit_text(
        f"✅ Контент відправлено {sent} юзерам",
        reply_markup=kb_admin()
    )

@router.callback_query(F.data == "adm_back")
async def adm_back(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        return
    await cq.message.edit_text(
        "🔐 <b>Адмін панель NutriSense</b>",
        reply_markup=kb_admin()
    )
    await cq.answer()

@router.callback_query(F.data == "weekly_content")
async def cb_weekly_content(cq: CallbackQuery):
    user = await get_user(cq.from_user.id)
    if not has_access(user, "start"):
        await cq.answer("🔒 Контент доступний з Start", show_alert=True)
        return
    week_num = user.get("week", 1) or 1
    week_data = WEEKLY_CONTENT.get(week_num, WEEKLY_CONTENT[1])
    posts = week_data["posts"]
    text = (
        f"📚 <b>Тиждень {week_num}: {week_data['theme']}</b>\n\n"
        f"Матеріалів: {len(posts)}\n\n"
    )
    for p in posts:
        text += f"📌 День {p['day']}: {p['title']}\n"
    text += f"\nПрогрес: тиждень {week_num} з 28"
    b = InlineKeyboardBuilder()
    for i, p in enumerate(posts):
        b.button(
            text=f"📖 {p['title'][:30]}",
            callback_data=f"read_post_{week_num}_{i}"
        )
    b.button(text="◀️ Назад", callback_data="main_menu")
    b.adjust(1)
    await cq.message.edit_text(text, reply_markup=b.as_markup())
    await cq.answer()

@router.callback_query(F.data.startswith("read_post_"))
async def cb_read_post(cq: CallbackQuery):
    parts = cq.data.split("_")
    week_num, post_idx = int(parts[2]), int(parts[3])
    week_data = WEEKLY_CONTENT.get(week_num)
    if not week_data or post_idx >= len(week_data["posts"]):
        await cq.answer("⚠️ Не знайдено", show_alert=True)
        return
    post = week_data["posts"][post_idx]
    b = InlineKeyboardBuilder()
    if post_idx > 0:
        b.button(text="◀️ Попередній", callback_data=f"read_post_{week_num}_{post_idx-1}")
    if post_idx < len(week_data["posts"]) - 1:
        b.button(text="Наступний ▶️", callback_data=f"read_post_{week_num}_{post_idx+1}")
    b.button(text="📚 До тижня", callback_data="weekly_content")
    b.adjust(2, 1)
    await cq.message.edit_text(post["text"], reply_markup=b.as_markup())
    await cq.answer()
WEEKLY_CONTENT = {
    1: {
        "theme": "🌱 Основи усвідомленого харчування",
        "posts": [
            {"day": 1, "rubric": "знання", "title": "Що таке усвідомлене харчування?",
             "text": "🧠 <b>Тиждень 1 · День 1</b>\n\n<b>Усвідомлене харчування — це не дієта.</b>\n\nЦе вміння чути своє тіло:\n→ їсти, коли є фізичний голод\n→ зупинятися при насиченні\n→ обирати їжу, яка дає енергію\n\nФормула здорової харчової поведінки:\nЇм за фізіологічним голодом\nСмачну та поживну їжу\nЗупиняюся за насиченням\nЗабуваю про їжу до наступного голоду 🌿"},
            {"day": 2, "rubric": "поведінка", "title": "Шкала голоду-ситості",
             "text": "🌡 <b>Тиждень 1 · День 2</b>\n\n<b>Шкала голоду-ситості (1-10)</b>\n\n1 — Дуже сильний голод\n2 — Комфортний голод, треба їсти\n3 — Легкий голод\n4 — Нейтральний стан ← <b>починай їсти</b>\n5 — Легка ситість\n6 — Комфортна ситість ← <b>зупиняйся</b>\n7 — Насичення\n8 — Трішки переїв\n9 — Дуже переїв\n10 — Крайня ситість\n\n<b>Практика:</b> перевіряй рівень перед кожним прийомом їжі 🌿"},
            {"day": 4, "rubric": "тарілка", "title": "Ідеальна тарілка",
             "text": "🍽 <b>Тиждень 1 · День 4</b>\n\n<b>Формула збалансованої тарілки:</b>\n\n🥗 1/2 тарілки — різнокольорові овочі\n🍗 1/4 тарілки — білок\n🌾 1/4 тарілки — складні вуглеводи\n🫒 невелика кількість — корисні жири\n\nЦя формула працює для кожного прийому їжі 🌿"},
            {"day": 6, "rubric": "знання", "title": "Чому заборони не працюють",
             "text": "🚫 <b>Тиждень 1 · День 6</b>\n\n<b>Немає поганої їжі.</b>\n\nКоли забороняємо щось:\n❌ мозок фіксується на забороненому\n❌ зростає тяга і напруга\n❌ зрив — і відчуття провини\n\nКоли їжа дозволена:\n✅ зникають зриви\n✅ баланс важливіший за ідеальність\n✅ повертається задоволення від їжі 🌿"},
        ]
    },
    2: {
        "theme": "🥗 Макронутрієнти: Білок",
        "posts": [
            {"day": 1, "rubric": "знання", "title": "Навіщо нам білок?",
             "text": "💪 <b>Тиждень 2 · День 1</b>\n\n<b>Білок — будівельний матеріал тіла.</b>\n\nДефіцит білка:\n→ випадіння волосся та ламкість нігтів\n→ збої в гормональній системі\n→ ризик набряків\n→ зниження імунітету\n\n<b>Норма:</b> 1.4-2.0 г на кг ваги\nРозрахуй точно через /kbju 📊"},
            {"day": 3, "rubric": "тарілка", "title": "Жирний vs нежирний білок",
             "text": "🐟 <b>Тиждень 2 · День 3</b>\n\n<b>ЖИРНИЙ БІЛОК</b>\n• Лосось, форель, скумбрія\n• Цілі яйця\n• Червоне м'ясо\n• Домашній сир 9%\n\n<b>НЕЖИРНИЙ БІЛОК</b>\n• Куряче філе, індичка\n• Яєчний білок\n• Тріска, хек, судак\n• Креветки, кальмари\n• Знежирений сир 2%\n\nНа сніданку — жирний, на вечерю — нежирний 🌿"},
            {"day": 5, "rubric": "рецепт", "title": "Сніданок з білком за 10 хв",
             "text": "🥘 <b>Тиждень 2 · День 5</b>\n\n<b>Омлет з овочами</b>\n\n• 3 яйця\n• 50г шпинату\n• 1/2 помідора\n• 30г твердого сиру\n• 1 ч.л. оливкової олії\n\nКБЖУ на порцію:\nКалорії: 320 ккал\nБілок: 24г · Жири: 22г · Вуглеводи: 4г\n\nІдеальний старт дня 💚"},
        ]
    },
    3: {"theme": "🧈 Макронутрієнти: Жири",
        "posts": [
            {"day": 1, "rubric": "знання", "title": "Жири — не ворог",
             "text": "🥑 <b>Тиждень 3 · День 1</b>\n\n<b>Жири — основа гормонального здоров'я.</b>\n\nДефіцит жирів:\n→ гормональний збій\n→ суха шкіра, ламке волосся\n→ вітаміни A, D, E, K не засвоюються\n→ мозок на 60% з жирів\n\nПравило: жири потрібні, але правильні 🌿"},
            {"day": 3, "rubric": "тарілка", "title": "Насичені / Ненасичені / Омега-3",
             "text": "🫒 <b>Тиждень 3 · День 3</b>\n\n<b>НАСИЧЕНІ</b> (обмежити)\nВершкове масло, сало, жирне м'ясо\n\n<b>НЕНАСИЧЕНІ</b> ✅\nОливкова олія, авокадо, горіхи\n\n<b>ОМЕГА-3</b> ✅✅\nЛосось, скумбрія, насіння льону, волоські горіхи\n\nОмега-3 — 2-3 рази на тиждень 💚"},
        ]
    },
    4: {"theme": "🌾 Макронутрієнти: Вуглеводи",
        "posts": [
            {"day": 1, "rubric": "знання", "title": "Вуглеводи — паливо для мозку",
             "text": "⚡ <b>Тиждень 4 · День 1</b>\n\nВуглеводи — головне паливо для мозку та м'язів.\n\nПри нестачі:\n→ туман у голові\n→ дратівливість, апатія\n→ порушення нервової системи\n\nЗолоте правило: їж переважно складні вуглеводи — крупи, хліб, овочі 🌿"},
            {"day": 3, "rubric": "тарілка", "title": "Прості vs складні вуглеводи",
             "text": "🌾 <b>Тиждень 4 · День 3</b>\n\n<b>ПРОСТІ</b> (обмежити)\nФрукти, цукор, мед, солодощі\n\n<b>СКЛАДНІ</b> ✅ (основа)\n• Крупи: гречка, овес, рис, булгур\n• ЦЗ хліб\n• Бобові: сочевиця, нут\n• Батат, картопля\n\nЧим більше клітковини — тим довше ситість 💚"},
        ]
    },
    5: {"theme": "🧠 Харчова поведінка",
        "posts": [
            {"day": 1, "rubric": "поведінка", "title": "3 типи голоду",
             "text": "🧠 <b>Тиждень 5 · День 1</b>\n\n<b>Гомеостатичний голод</b> 🟢\nНаростає поступово. Що робити: їсти\n\n<b>Гедонічний голод</b> 🟡\nВиникає без фізичного голоду.\nЩо робити: пауза, відволіктись, і якщо хочеш — їсти\n\n<b>Дисрегульований голод</b> 🔴\nГолод без насичення.\nЩо робити: регулярні прийоми, достатня калорійність, сон 🌿"},
        ]
    },
    6: {"theme": "😴 Сон, стрес і харчування",
        "posts": [
            {"day": 1, "rubric": "знання", "title": "Як сон впливає на вагу",
             "text": "😴 <b>Тиждень 6 · День 1</b>\n\nПри нестачі сну:\n→ Зростає грелін (гормон голоду)\n→ Знижується лептин (насичення)\n→ Тягне на солодке і жирне\n\nЩо робити:\n✅ 7-9 годин сну\n✅ Стабільний час сну\n✅ Вечеря не перед сном\n✅ Мінімум екранів ввечері 🌿"},
        ]
    },
    7: {"theme": "🦠 Пробіотики і мікробіом",
        "posts": [
            {"day": 1, "rubric": "знання", "title": "Мікробіом керує голодом",
             "text": "🦠 <b>Тиждень 7 · День 1</b>\n\n90% серотоніну синтезується в кишківнику.\n\nЗдорові бактерії впливають на:\n🧠 Настрій\n🛡 Імунітет\n🍽 Апетит і тягу\n\nЩо підтримує мікробіом:\n✅ Йогурт, кефір, квашена капуста\n✅ Клітковина щодня\n✅ Різноманітне харчування 💚"},
        ]
    },
    8: {"theme": "🍽 Сніданок", "posts": [
        {"day": 1, "rubric": "тарілка", "title": "Формула ідеального сніданку",
         "text": "🌅 <b>Тиждень 8 · День 1</b>\n\n1/4 — білок\n1/8 — вуглеводи\n1/4 — насичені жири\n1/6 — ненасичені жири\n1/2 — овочі та ягоди\n\n5 варіантів:\n1. Овсянка + ягоди + яйце\n2. Омлет + авокадо + хліб\n3. Йогурт + фрукти + горіхи\n4. Яйця + лосось + огірок\n5. Гречка + яйце + салат 🌿"},
    ]},
    9: {"theme": "☀️ Обід", "posts": [
        {"day": 1, "rubric": "тарілка", "title": "Формула обіду",
         "text": "☀️ <b>Тиждень 9 · День 1</b>\n\n1/4 — білок\n1/4 — вуглеводи (найбільше за день)\n невелика кількість жирів\n1/2 — овочі різнокольорові 💚"},
    ]},
    10: {"theme": "🌙 Вечеря", "posts": [
        {"day": 1, "rubric": "тарілка", "title": "Формула вечері",
         "text": "🌙 <b>Тиждень 10 · День 1</b>\n\n1/3 — нежирний білок\n1/6 — складні вуглеводи\n мінімум жирів\n1/2 — овочі\n\nТипові помилки:\n❌ Тільки жирний білок\n❌ Немає круп\n❌ Мало овочів 🌿"},
    ]},
    11: {"theme": "💧 Гідратація", "posts": [
        {"day": 1, "rubric": "знання", "title": "Вода та обмін речовин",
         "text": "💧 <b>Тиждень 11 · День 1</b>\n\nНорма: 30-35 мл на кг ваги\n\nОзнаки нестачі:\n→ Втома і туман у голові\n→ Хибне відчуття голоду\n→ Запори\n\nЛайфхак: випий склянку води перед їжею 💚"},
    ]},
    12: {"theme": "🏋️ Харчування і тренування", "posts": [
        {"day": 1, "rubric": "знання", "title": "До та після тренування",
         "text": "🏋️ <b>Тиждень 12 · День 1</b>\n\nДо (за 1-2 год):\n🌾 Складні вуглеводи + білок\n\nПісля (30-60 хв):\n🥩 Білок + прості вуглеводи\n\nВуглеводи після тренування відновлюють глікоген 💪"},
    ]},
    13: {"theme": "🥗 Клітковина", "posts": [
        {"day": 1, "rubric": "знання", "title": "Норма клітковини",
         "text": "🌿 <b>Тиждень 13 · День 1</b>\n\nНорма: 25-35г на день\n\nДжерела:\n🥦 Овочі і зелень\n🍎 Фрукти з шкіркою\n🌾 Цільнозернові крупи\n🫘 Бобові\n\nКлітковина годує бактерії і дає ситість 💚"},
    ]},
    14: {"theme": "🍫 Солодке без шкоди", "posts": [
        {"day": 1, "rubric": "поведінка", "title": "Десерт без провини",
         "text": "🍫 <b>Тиждень 14 · День 1</b>\n\nСолодке можна. Питання як і коли.\n\nНайкращий час:\n✅ Після сніданку або обіду\n✅ Після повноцінного прийому їжі\n✅ При фізичному голоді\n\nЗаборона → зрив → провина\nДозвіл → задоволення → контроль 💚"},
    ]},
    15: {"theme": "⚖️ Управління вагою", "posts": [
        {"day": 1, "rubric": "знання", "title": "Дефіцит калорій правильно",
         "text": "⚖️ <b>Тиждень 15 · День 1</b>\n\nБезпечний дефіцит: 300-500 ккал\n\nПравила:\n✅ Не менше 1200-1500 ккал\n✅ Достатньо білку 1.6-2г/кг\n✅ Темп: 0.5-1 кг на тиждень\n\nРозрахуй свою норму: /kbju 📊"},
    ]},
    16: {"theme": "⚖️ Гормони та харчування", "posts": [
        {"day": 1, "rubric": "знання", "title": "Як харчування впливає на гормони",
         "text": "⚖️ <b>Тиждень 16 · День 1</b>\n\nІнсулін — регулює цукор.\nЛептин — сигнал ситості.\nКортизол — гормон стресу.\n\nЯк підтримати баланс:\n✅ Збалансована тарілка\n✅ Регулярні прийоми\n✅ Якісний сон\n✅ Зниження стресу 🌿"},
    ]},
    17: {"theme": "☀️ Вітамін D", "posts": [
        {"day": 1, "rubric": "знання", "title": "Вітамін D: дефіцит у більшості",
         "text": "☀️ <b>Тиждень 17 · День 1</b>\n\nДефіцит D:\n→ Втома, депресія\n→ Слабкий імунітет\n→ Ламкі кістки\n\nДжерела:\n☀️ Сонце\n🐟 Жирна риба\n🥚 Жовтки\n💊 Добавки D3\n\nОптимальний рівень: 50-80 нг/мл 💚"},
    ]},
    18: {"theme": "💊 Залізо", "posts": [
        {"day": 1, "rubric": "знання", "title": "Залізодефіцит: як виявити",
         "text": "🩸 <b>Тиждень 18 · День 1</b>\n\nОзнаки дефіциту:\n→ Хронічна втома\n→ Блідість\n→ Випадіння волосся\n\nДжерела:\n🥩 Червоне м'ясо, печінка\n🌿 Шпинат, гречка, сочевиця\n\nЇж з вітаміном С — засвоєння +50% 🌿"},
    ]},
    19: {"theme": "😤 Стрес і кортизол", "posts": [
        {"day": 1, "rubric": "поведінка", "title": "Кортизол і вага",
         "text": "😤 <b>Тиждень 19 · День 1</b>\n\nКортизол підвищує апетит і тягу до солодкого.\n\nЩо знижує кортизол:\n✅ Сон 7-9 годин\n✅ Фізична активність\n✅ Медитація\n✅ Природа і прогулянки 🌿"},
    ]},
    20: {"theme": "🍱 Meal Prep", "posts": [
        {"day": 1, "rubric": "практика", "title": "Готовлю раз на тиждень",
         "text": "📦 <b>Тиждень 20 · День 1</b>\n\nMeal Prep за 2 години:\n🌾 Відвари 3 крупи\n🥩 Запечи курку і рибу\n🥗 Наріж овочі по контейнерах\n🫘 Відвари сочевицю\n\nРезультат: 5 днів без думок що готувати 💚"},
    ]},
    21: {"theme": "🎯 Харчові цілі", "posts": [
        {"day": 1, "rubric": "поведінка", "title": "SMART-цілі",
         "text": "🎯 <b>Тиждень 21 · День 1</b>\n\nПравильна ціль:\n✅ Конкретна\n✅ Вимірювана\n✅ Досяжна\n✅ Важлива особисто\n✅ З терміном\n\nПриклад:\nНе: хочу харчуватися здоровіше\nТак: їстиму 5 порцій овочів щодня 4 тижні 🌿"},
    ]},
    22: {"theme": "🧪 Аналізи", "posts": [
        {"day": 1, "rubric": "знання", "title": "Які аналізи здати",
         "text": "🧪 <b>Тиждень 22 · День 1</b>\n\nБазові аналізи:\n🩸 Загальний аналіз крові\n⚗️ Глюкоза, холестерин\n🦋 ТТГ (щитовидна залоза)\n☀️ Вітамін D, B12\n🥗 Магній, цинк\n\nЗдавай раз на 6 місяців 💚"},
    ]},
    23: {"theme": "🌍 Їжа поза домом", "posts": [
        {"day": 1, "rubric": "практика", "title": "Правильно їсти в кафе",
         "text": "🍽 <b>Тиждень 23 · День 1</b>\n\nЯк обирати в кафе:\n✅ Шукай білок в меню\n✅ Проси соус окремо\n✅ Замінюй картоплю фрі на овочі\n✅ Їж повільно\n\nВ дорозі:\n🥜 Горіхи, сир, яйця, фрукти 🌿"},
    ]},
    24: {"theme": "📱 Трекінг їжі", "posts": [
        {"day": 1, "rubric": "поведінка", "title": "Рахувати калорії?",
         "text": "📱 <b>Тиждень 24 · День 1</b>\n\nКоли корисно:\n✅ Хочеш зрозуміти раціон\n✅ Немає результату\n\nКоли шкідливо:\n❌ Викликає тривогу\n❌ Відмовляєшся їсти через ліміт\n\nАльтернатива: метод тарілки + відчуття голоду 💚"},
    ]},
    25: {"theme": "🏃 Харчування для енергії", "posts": [
        {"day": 1, "rubric": "знання", "title": "Чому немає енергії?",
         "text": "⚡ <b>Тиждень 25 · День 1</b>\n\nПричини втоми після їжі:\n→ Переїдання\n→ Надлишок простих вуглеводів\n→ Мало білку\n\nЩо дає енергію:\n✅ Збалансована тарілка\n✅ Регулярні прийоми\n✅ 1.5-2л води 🌿"},
    ]},
    26: {"theme": "🌟 Підтримка ваги", "posts": [
        {"day": 1, "rubric": "поведінка", "title": "Як не зірватися",
         "text": "🌟 <b>Тиждень 26 · День 1</b>\n\nДосягти легше ніж утримати.\n\nЯк підтримати:\n✅ Нова ціль\n✅ Система звичок\n✅ Регулярна перевірка\n✅ Повернення до базового протоколу 💚"},
    ]},
    27: {"theme": "🎁 Харчування на святах", "posts": [
        {"day": 1, "rubric": "поведінка", "title": "Свята без зривів",
         "text": "🎉 <b>Тиждень 27 · День 1</b>\n\n✅ Не голодуй до святкового столу\n✅ Починай з овочів\n✅ Їж повільно\n✅ Дозволяй улюблене усвідомлено\n✅ Не компенсуй голодуванням наступного дня\n✅ Повернись до режиму вранці 🌿"},
    ]},
    28: {"theme": "🏆 Підсумок", "posts": [
        {"day": 1, "rubric": "знання", "title": "28 тижнів: ти зробив це!",
         "text": "🏆 <b>Тиждень 28 · Фінал</b>\n\nТи пройшов весь курс NutriSense!\n\n✅ Навчився розрізняти голод\n✅ Зрозумів роль кожного нутрієнту\n✅ Позбувся заборон\n✅ Отримав інструменти на все життя\n\n<b>Харчування — це не дієта.\nЦе твій спосіб жити.</b>\n\nNutriSense завжди поруч 🌿💚"},
    ]},
}
async def schedule_weekly_content(bot: Bot):
    while True:
        now = datetime.now()
        days_until_monday = (7 - now.weekday()) % 7 or 7
        next_run = now.replace(hour=10, minute=0, second=0, microsecond=0)
        next_run += timedelta(days=days_until_monday)
        wait_seconds = (next_run - now).total_seconds()
        log.info(f"Next content: {next_run.strftime('%d.%m.%Y %H:%M')}")
        await asyncio.sleep(wait_seconds)
        users = await get_all_users()
        sent = 0
        for u in users:
            if not has_access(u, "start"):
                continue
            week_num = u.get("week", 1) or 1
            week_data = WEEKLY_CONTENT.get(week_num)
            if not week_data:
                continue
            posts = week_data["posts"]
            if not posts:
                continue
            post = posts[0]
            try:
                b = InlineKeyboardBuilder()
                b.button(
                    text="📚 Всі матеріали тижня",
                    callback_data="weekly_content"
                )
                await bot.send_message(
                    u["user_id"],
                    f"📚 <b>Новий тиждень!</b>\n\n"
                    f"<b>Тиждень {week_num}: {week_data['theme']}</b>\n\n"
                    + post["text"],
                    reply_markup=b.as_markup()
                )
                next_week = min(week_num + 1, 28)
                await upsert_user(u["user_id"], week=next_week)
                sent += 1
            except Exception as e:
                log.warning(f"Cannot send to {u['user_id']}: {e}")
        log.info(f"Content sent: {sent} users")


async def main():
    await init_db()
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    asyncio.create_task(schedule_weekly_content(bot))
    log.info("NutriSense Bot started")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
