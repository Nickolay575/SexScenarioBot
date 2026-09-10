import asyncio
import logging
import random
import string
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, 
    ReplyKeyboardMarkup, 
    KeyboardButton, 
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery
)
from aiogram.enums import ParseMode
from dotenv import load_dotenv
import os
from openai import AsyncOpenAI
import aiosqlite

load_dotenv()

# ================== НАСТРОЙКИ ==================
BOT_TOKEN = os.getenv("BOT_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден в .env")

# ================== ЛОГИРОВАНИЕ ==================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ================== БОТ И ДИСПЕТЧЕР ==================
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ================== КЛИЕНТ DEEPSEEK ==================
client = AsyncOpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url=DEEPSEEK_BASE_URL
)

# ================== ВРЕМЕННОЕ ХРАНИЛИЩЕ ДЛЯ РЕГЕНЕРАЦИИ ==================
# Ключ — min(user_id, partner_id), значение — данные последнего сценария
last_generation_cache = {}

# ================== СОСТОЯНИЯ ==================
class Form(StatesGroup):
    waiting_partner = State()
    waiting_preferences = State()
    waiting_place = State()
    waiting_time = State()
    waiting_role = State()
    waiting_style = State()          # при первом заполнении
    waiting_new_style = State()      # при смене стиля после генерации


# ================== БАЗА ДАННЫХ ==================
DB_PATH = "bot_data.db"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pairs (
                user_id INTEGER PRIMARY KEY,
                partner_id INTEGER,
                preferences TEXT,
                place TEXT,
                time_pref TEXT,
                role TEXT,
                style TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS invites (
                code TEXT PRIMARY KEY,
                creator_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()


async def create_invite(user_id: int) -> str:
    code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO invites (code, creator_id) VALUES (?, ?)", (code, user_id))
        await db.commit()
    return code


async def get_invite_creator(code: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT creator_id FROM invites WHERE code = ?", (code.upper(),)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def link_pair(user1: int, user2: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO pairs (user_id, partner_id) VALUES (?, ?)", (user1, user2))
        await db.execute("INSERT OR REPLACE INTO pairs (user_id, partner_id) VALUES (?, ?)", (user2, user1))
        await db.commit()


async def get_partner(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT partner_id FROM pairs WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def save_user_data(user_id: int, field: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE pairs SET {field} = ? WHERE user_id = ?", (value, user_id))
        await db.commit()


async def get_user_data(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT preferences, place, time_pref, role, style FROM pairs WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return {
                    "preferences": row[0],
                    "place": row[1],
                    "time_pref": row[2],
                    "role": row[3],
                    "style": row[4]
                }
            return None


async def clear_sensitive_data(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE pairs SET preferences = NULL, place = NULL, time_pref = NULL, role = NULL, style = NULL WHERE user_id = ?",
            (user_id,)
        )
        await db.commit()


# ================== ГЕНЕРАЦИЯ СЦЕНАРИЯ ==================
STYLE_PROMPTS = {
    "Нежный / Романтичный": "Стиль: нежный, чувственный, романтичный. Много ласки, поцелуев, эмоций, медленного развития. Без грубости.",
    "Грязный / Пошлый": "Стиль: грязный, пошлый, прямой. Много грязных словечек, откровенных описаний, без стеснения.",
    "Доминирование / Властный": "Стиль: властный, с элементами доминирования и подчинения. Чёткие команды, контроль, напряжение.",
    "Игривый / С юмором": "Стиль: игривый, с лёгким юмором и флиртом. Не слишком серьёзный, с элементами поддразнивания.",
    "Жёсткий / Интенсивный": "Стиль: жёсткий, интенсивный, страстный. Много энергии, силы, сильных ощущений (без реальной боли и вреда).",
    "Классический": "Стиль: классический сбалансированный. Нормальная смесь страсти, диалогов и атмосферы."
}


async def generate_scenario(
    pref1: str, 
    pref2: str, 
    place: str, 
    time_pref: str, 
    role1: str, 
    role2: str,
    style: str = "Классический"
) -> str:
    
    style_instruction = STYLE_PROMPTS.get(style, STYLE_PROMPTS["Классический"])
    
    prompt = f"""Ты — опытный сценарист эротических ролевых игр для пар 18+.

Два партнёра хотят провести сегодня сексуальную ролевую игру.

Данные партнёра 1:
- Предпочтения: {pref1}
- Желаемая роль: {role1}

Данные партнёра 2:
- Предпочтения: {pref2}
- Желаемая роль: {role2}

Место: {place}
Время / длительность: {time_pref}

{style_instruction}

Напиши подробный, возбуждающий, реалистичный и согласованный сценарий ролевой игры на русском языке.
Сценарий должен:
- Учитывать пожелания ОБОИХ партнёров
- Быть конкретным (с действиями, диалогами, атмосферой)
- Длиться примерно указанное время
- Быть безопасным и взаимно приятным
- Не содержать ничего, что прямо противоречит указанным предпочтениям

Структура ответа:
1. Краткое название сценария
2. Атмосфера и подготовка
3. Пошаговый сценарий с диалогами
4. Возможные варианты развития / чем можно закончить

Пиши живо, чувственно и без воды."""

    try:
        response = await client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": "Ты талантливый сценарист эротических ролевых игр для взрослых пар. Пишешь только на русском. Всегда учитывай выбранный стиль."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.9,
            max_tokens=2200
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"Ошибка генерации: {e}")
        return "❌ Не удалось сгенерировать сценарий. Попробуй позже или проверь API-ключ DeepSeek."


# ================== КЛАВИАТУРЫ ==================
def main_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔗 Создать пару"), KeyboardButton(text="📩 Присоединиться по коду")],
            [KeyboardButton(text="✍️ Начать новый сценарий")],
            [KeyboardButton(text="ℹ️ Помощь")]
        ],
        resize_keyboard=True
    )


def role_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Доминирую"), KeyboardButton(text="Подчиняюсь")],
            [KeyboardButton(text="По ситуации / равные")]
        ],
        resize_keyboard=True
    )


def style_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Нежный / Романтичный"), KeyboardButton(text="Грязный / Пошлый")],
            [KeyboardButton(text="Доминирование / Властный"), KeyboardButton(text="Игривый / С юмором")],
            [KeyboardButton(text="Жёсткий / Интенсивный"), KeyboardButton(text="Классический")]
        ],
        resize_keyboard=True
    )


def after_scenario_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔄 Другой вариант", callback_data="regenerate"),
            InlineKeyboardButton(text="🎨 Другой стиль", callback_data="change_style")
        ]
    ])


# ================== ХЕНДЛЕРЫ ==================
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет! Я бот для создания сексуальных сценариев для пары 18+.\n\n"
        "Как это работает:\n"
        "1. Один создаёт пару → получает код\n"
        "2. Второй вводит код → вы связаны\n"
        "3. Оба пишут свои сегодняшние предпочтения, место, время и стиль\n"
        "4. Я придумываю сценарий под вас обоих\n\n"
        "После сценария можно попросить другой вариант или сменить стиль.\n\n"
        "Выбери действие:",
        reply_markup=main_kb()
    )


@dp.message(F.text == "ℹ️ Помощь")
async def help_handler(message: Message):
    await message.answer(
        "📌 Как пользоваться:\n\n"
        "1. Нажми «Создать пару» — получишь 6-значный код\n"
        "2. Отправь код партнёру\n"
        "3. Партнёр нажимает «Присоединиться по коду» и вводит его\n"
        "4. После связывания оба нажимают «Начать новый сценарий»\n"
        "5. Указываете предпочтения → место → время → роль → стиль\n"
        "6. Когда оба заполнили — я генерирую сценарий\n\n"
        "После получения сценария можно нажать:\n"
        "• 🔄 Другой вариант — новый сценарий в том же стиле\n"
        "• 🎨 Другой стиль — выбрать другой характер сценария\n\n"
        "Данные предпочтений удаляются после генерации."
    )


@dp.message(F.text == "🔗 Создать пару")
async def create_pair(message: Message, state: FSMContext):
    code = await create_invite(message.from_user.id)
    await message.answer(
        f"Твой код приглашения:\n\n<code>{code}</code>\n\n"
        "Отправь его партнёру. Когда он введёт код — вы будете связаны.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_kb()
    )


@dp.message(F.text == "📩 Присоединиться по коду")
async def join_pair(message: Message, state: FSMContext):
    await state.set_state(Form.waiting_partner)
    await message.answer("Введи 6-значный код, который тебе дал партнёр:", reply_markup=ReplyKeyboardRemove())


@dp.message(Form.waiting_partner)
async def process_code(message: Message, state: FSMContext):
    code = message.text.strip().upper()
    creator_id = await get_invite_creator(code)

    if not creator_id:
        await message.answer("Код не найден. Попробуй ещё раз или попроси новый.")
        return

    if creator_id == message.from_user.id:
        await message.answer("Нельзя присоединиться к своему же коду 😄")
        return

    await link_pair(creator_id, message.from_user.id)
    await state.clear()

    await message.answer("✅ Вы успешно связаны в пару!", reply_markup=main_kb())
    try:
        await bot.send_message(creator_id, f"✅ Партнёр присоединился! Теперь можно начинать сценарий.")
    except Exception:
        pass


@dp.message(F.text == "✍️ Начать новый сценарий")
async def start_scenario(message: Message, state: FSMContext):
    partner = await get_partner(message.from_user.id)
    if not partner:
        await message.answer("Сначала создай пару или присоединись по коду.", reply_markup=main_kb())
        return

    await state.set_state(Form.waiting_preferences)
    await message.answer(
        "Опиши свои сегодняшние сексуальные предпочтения / настроение / кинки / табу.\n"
        "Можно в свободной форме, чем подробнее — тем лучше сценарий.",
        reply_markup=ReplyKeyboardRemove()
    )


@dp.message(Form.waiting_preferences)
async def process_preferences(message: Message, state: FSMContext):
    await save_user_data(message.from_user.id, "preferences", message.text)
    await state.set_state(Form.waiting_place)
    await message.answer("Где будете играть? (дом, отель, машина, улица, другое...)")


@dp.message(Form.waiting_place)
async def process_place(message: Message, state: FSMContext):
    await save_user_data(message.from_user.id, "place", message.text)
    await state.set_state(Form.waiting_time)
    await message.answer("Сколько примерно времени хотите уделить? (например: 20-30 минут, час, весь вечер)")


@dp.message(Form.waiting_time)
async def process_time(message: Message, state: FSMContext):
    await save_user_data(message.from_user.id, "time_pref", message.text)
    await state.set_state(Form.waiting_role)
    await message.answer("Какую роль сегодня предпочитаешь?", reply_markup=role_kb())


@dp.message(Form.waiting_role)
async def process_role(message: Message, state: FSMContext):
    await save_user_data(message.from_user.id, "role", message.text)
    await state.set_state(Form.waiting_style)
    await message.answer(
        "Выбери стиль сценария:",
        reply_markup=style_kb()
    )


@dp.message(Form.waiting_style)
async def process_style(message: Message, state: FSMContext):
    style = message.text
    if style not in STYLE_PROMPTS:
        await message.answer("Выбери стиль из кнопок ниже:", reply_markup=style_kb())
        return

    await save_user_data(message.from_user.id, "style", style)
    await state.clear()

    partner_id = await get_partner(message.from_user.id)
    my_data = await get_user_data(message.from_user.id)
    partner_data = await get_user_data(partner_id)

    await message.answer("Данные сохранены ✅", reply_markup=main_kb())

    # Проверяем, заполнил ли партнёр полностью
    if (partner_data and partner_data.get("preferences") and partner_data.get("place") 
        and partner_data.get("style")):
        
        await message.answer("Оба заполнили данные. Генерирую сценарий... 🔥")
        try:
            await bot.send_message(partner_id, "Оба заполнили данные. Генерирую сценарий... 🔥")
        except Exception:
            pass

        # Берём стиль того, кто заполнил последним (или средний)
        final_style = my_data.get("style") or partner_data.get("style") or "Классический"

        scenario = await generate_scenario(
            pref1=my_data["preferences"],
            pref2=partner_data["preferences"],
            place=my_data["place"] or partner_data["place"],
            time_pref=my_data["time_pref"] or partner_data["time_pref"],
            role1=my_data["role"] or "по ситуации",
            role2=partner_data["role"] or "по ситуации",
            style=final_style
        )

        # Сохраняем в кэш для возможности регенерации
        cache_key = min(message.from_user.id, partner_id)
        last_generation_cache[cache_key] = {
            "pref1": my_data["preferences"],
            "pref2": partner_data["preferences"],
            "place": my_data["place"] or partner_data["place"],
            "time_pref": my_data["time_pref"] or partner_data["time_pref"],
            "role1": my_data["role"] or "по ситуации",
            "role2": partner_data["role"] or "по ситуации",
            "style": final_style,
            "user1": message.from_user.id,
            "user2": partner_id
        }

        text = f"🔥 <b>Ваш сценарий на сегодня</b>\n<i>Стиль: {final_style}</i>\n\n{scenario}"
        
        await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=after_scenario_kb())
        try:
            await bot.send_message(partner_id, text, parse_mode=ParseMode.HTML, reply_markup=after_scenario_kb())
        except Exception:
            pass

        # Чистим чувствительные данные из БД
        await clear_sensitive_data(message.from_user.id)
        await clear_sensitive_data(partner_id)
    else:
        await message.answer(
            "Ждём, пока партнёр тоже заполнит свои данные (включая стиль).\n"
            "Как только он закончит — сценарий придёт обоим автоматически."
        )


# ================== ОБРАБОТКА КНОПОК ПОСЛЕ СЦЕНАРИЯ ==================
@dp.callback_query(F.data == "regenerate")
async def regenerate_scenario(callback: CallbackQuery):
    user_id = callback.from_user.id
    partner_id = await get_partner(user_id)
    
    if not partner_id:
        await callback.answer("Пара не найдена", show_alert=True)
        return

    cache_key = min(user_id, partner_id)
    data = last_generation_cache.get(cache_key)
    
    if not data:
        await callback.answer("Данные для повторной генерации устарели. Начните новый сценарий.", show_alert=True)
        return

    await callback.answer("Генерирую другой вариант...")
    
    scenario = await generate_scenario(
        pref1=data["pref1"],
        pref2=data["pref2"],
        place=data["place"],
        time_pref=data["time_pref"],
        role1=data["role1"],
        role2=data["role2"],
        style=data["style"]
    )

    text = f"🔄 <b>Новый вариант сценария</b>\n<i>Стиль: {data['style']}</i>\n\n{scenario}"
    
    await callback.message.answer(text, parse_mode=ParseMode.HTML, reply_markup=after_scenario_kb())
    
    # Отправляем и партнёру
    other_id = data["user2"] if user_id == data["user1"] else data["user1"]
    try:
        await bot.send_message(other_id, text, parse_mode=ParseMode.HTML, reply_markup=after_scenario_kb())
    except Exception:
        pass


@dp.callback_query(F.data == "change_style")
async def change_style_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(Form.waiting_new_style)
    await callback.message.answer(
        "Выбери новый стиль для сценария:",
        reply_markup=style_kb()
    )


@dp.message(Form.waiting_new_style)
async def process_new_style(message: Message, state: FSMContext):
    """Обработка смены стиля после уже сгенерированного сценария"""
    style = message.text
    if style not in STYLE_PROMPTS:
        await message.answer("Выбери стиль из кнопок:", reply_markup=style_kb())
        return

    user_id = message.from_user.id
    partner_id = await get_partner(user_id)
    
    if not partner_id:
        await message.answer("Пара не найдена", reply_markup=main_kb())
        await state.clear()
        return

    cache_key = min(user_id, partner_id)
    data = last_generation_cache.get(cache_key)
    
    if not data:
        await message.answer(
            "Данные для генерации устарели. Начните новый сценарий через меню.",
            reply_markup=main_kb()
        )
        await state.clear()
        return

    await state.clear()
    await message.answer(f"Генерирую сценарий в стиле «{style}»...", reply_markup=main_kb())

    # Обновляем стиль в кэше
    data["style"] = style
    last_generation_cache[cache_key] = data

    scenario = await generate_scenario(
        pref1=data["pref1"],
        pref2=data["pref2"],
        place=data["place"],
        time_pref=data["time_pref"],
        role1=data["role1"],
        role2=data["role2"],
        style=style
    )

    text = f"🎨 <b>Сценарий в новом стиле</b>\n<i>Стиль: {style}</i>\n\n{scenario}"
    
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=after_scenario_kb())
    
    other_id = data["user2"] if user_id == data["user1"] else data["user1"]
    try:
        await bot.send_message(other_id, text, parse_mode=ParseMode.HTML, reply_markup=after_scenario_kb())
    except Exception:
        pass


# ================== ЗАПУСК ==================
async def main():
    await init_db()
    logger.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
