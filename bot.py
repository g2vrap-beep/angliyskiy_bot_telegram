"""
EnglishBot - Telegram-бот для изучения английского языка
Один файл для удобства
"""
import os
import re
import json
import base64
import asyncio
import logging
import time
from datetime import datetime, timedelta, date
from uuid import UUID
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
import uuid

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, CallbackQuery, Update, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
from sqlalchemy import Column, BigInteger, String, Boolean, Integer, Text, DateTime, Date, ForeignKey, ARRAY, select, update, func, and_, or_
from sqlalchemy.dialects.postgresql import UUID as PGUUID, JSONB
from openai import AsyncOpenAI, RateLimitError, APIConnectionError, APIStatusError
from apscheduler.asyncio import AsyncIOScheduler

# ============= КОНФИГУРАЦИЯ =============
load_dotenv()

class Settings:
    BOT_TOKEN = os.getenv("BOT_TOKEN", "")
    OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
    OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
    DATABASE_URL = os.getenv("DATABASE_URL", "")
    ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
    WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
    WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
    TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "30"))
    SUBSCRIPTION_PRICE = int(os.getenv("SUBSCRIPTION_PRICE", "490"))
    PROVIDER_TOKEN = os.getenv("PROVIDER_TOKEN", "")

settings = Settings()

# ============= ЛОГИРОВАНИЕ =============
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# ============= БАЗА ДАННЫХ =============
Base = declarative_base()

engine = create_async_engine(settings.DATABASE_URL, echo=False, pool_pre_ping=True, pool_size=10, max_overflow=20)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# ============= МОДЕЛИ =============
class User(Base):
    __tablename__ = "users"
    id = Column(BigInteger, primary_key=True)
    username = Column(String(64), nullable=True)
    full_name = Column(String(128), nullable=True)
    language_code = Column(String(8), nullable=True)
    level = Column(String(4), default="B1")
    focus_areas = Column(ARRAY(Text), default=["vocabulary"])
    notify_times = Column(ARRAY(Text), default=["09:00"])
    timezone = Column(String(64), default="UTC")
    streak = Column(Integer, default=0)
    total_lessons = Column(Integer, default=0)
    last_lesson_date = Column(Date, nullable=True)
    trial_started = Column(DateTime, nullable=True)
    subscription_end = Column(DateTime, nullable=True)
    is_subscribed = Column(Boolean, default=False)
    is_blocked = Column(Boolean, default=False)
    xp_total = Column(Integer, default=0)
    xp_level = Column(Integer, default=1)
    longest_streak = Column(Integer, default=0)
    lessons_perfect = Column(Integer, default=0)
    ielts_lessons = Column(Integer, default=0)
    gifted_days_total = Column(Integer, default=0)
    gifted_by = Column(BigInteger, nullable=True)
    gifted_at = Column(DateTime, nullable=True)
    bot_mode = Column(String(16), default="casual")
    ielts_target_band = Column(String(4), nullable=True)
    ielts_type = Column(String(16), nullable=True)
    voice_count_today = Column(Integer, default=0)
    photo_count_today = Column(Integer, default=0)
    media_reset_at = Column(Date, nullable=True)
    voice_responses = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def has_active_access(self):
        if self.is_subscribed: return True
        if self.subscription_end and self.subscription_end > datetime.utcnow(): return True
        if self.trial_started:
            return (self.trial_started + timedelta(days=30)) > datetime.utcnow()
        return False
    
    def get_xp_for_next_level(self):
        level_xp = [0, 100, 300, 700, 1500, 3000, 6000]
        return level_xp[min(self.xp_level, len(level_xp)-1)]
    
    def get_level_name(self):
        names = {1: "🌱 Росток", 2: "📖 Ученик", 3: "⭐ Студент", 4: "🎓 Знаток", 5: "🏆 Мастер", 6: "🔥 Эксперт", 7: "💎 Легенда"}
        return names.get(self.xp_level, "💎 Легенда")

class Lesson(Base):
    __tablename__ = "lessons"
    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False)
    lesson_type = Column(String(32))
    content = Column(JSONB)
    user_answer = Column(Text)
    is_correct = Column(Boolean)
    input_type = Column(String(16), default="text")
    voice_transcript = Column(Text, nullable=True)
    started_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

class Notification(Base):
    __tablename__ = "notifications"
    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False)
    scheduled_at = Column(DateTime)
    sent_at = Column(DateTime, nullable=True)
    ignored = Column(Boolean, default=False)
    reminder_count = Column(Integer, default=0)

class Payment(Base):
    __tablename__ = "payments"
    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False)
    telegram_payment_id = Column(String(256), nullable=True)
    amount = Column(Integer)
    currency = Column(String(8))
    status = Column(String(32))
    created_at = Column(DateTime, default=datetime.utcnow)

class Badge(Base):
    __tablename__ = "badges"
    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False)
    badge_id = Column(String(64))
    earned_at = Column(DateTime, default=datetime.utcnow)

class Gift(Base):
    __tablename__ = "gifts"
    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False)
    admin_id = Column(BigInteger)
    days = Column(Integer)
    note = Column(String(256), nullable=True)
    expires_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)

BADGE_DEFINITIONS = {
    "first_lesson": {"icon": "🎉", "name": "Первый шаг", "description": "Завершить 1-й урок"},
    "streak_3": {"icon": "🔥", "name": "Три в ряд", "description": "Серия 3 дня"},
    "streak_7": {"icon": "🔥🔥", "name": "Неделя!", "description": "Серия 7 дней"},
    "streak_30": {"icon": "🏆", "name": "Месяц без остановки", "description": "Серия 30 дней"},
    "lessons_10": {"icon": "📚", "name": "Десятка", "description": "10 уроков"},
    "lessons_50": {"icon": "🎓", "name": "Полсотни", "description": "50 уроков"},
    "lessons_100": {"icon": "⭐", "name": "Сотня", "description": "100 уроков"},
    "perfect_5": {"icon": "✨", "name": "Перфекционист", "description": "5 уроков без ошибок"},
    "ielts_first": {"icon": "📋", "name": "IELTS боец", "description": "Первый IELTS-урок"},
    "level_up_5": {"icon": "🏅", "name": "Знаток", "description": "Уровень 5"},
}

# ============= CRUD =============
async def get_user(user_id: int):
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()

async def get_or_create_user(user_id: int, username=None, full_name=None, language_code=None):
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if user: return user, False
        user = User(id=user_id, username=username, full_name=full_name, language_code=language_code, trial_started=datetime.utcnow())
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user, True

async def update_user(user_id: int, **kwargs):
    async with AsyncSessionLocal() as db:
        await db.execute(update(User).where(User.id == user_id).values(**kwargs))
        await db.commit()

async def get_all_users(limit=100):
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).order_by(User.created_at.desc()).limit(limit))
        return list(result.scalars().all())

async def get_user_badges(user_id: int):
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Badge).where(Badge.user_id == user_id))
        return list(result.scalars().all())

async def award_badge(user_id: int, badge_id: str):
    async with AsyncSessionLocal() as db:
        badge = Badge(user_id=user_id, badge_id=badge_id)
        db.add(badge)
        await db.commit()

async def has_badge(user_id: int, badge_id: str) -> bool:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Badge).where(and_(Badge.user_id == user_id, Badge.badge_id == badge_id)))
        return result.scalar_one_or_none() is not None

async def create_lesson(user_id: int, lesson_type: str, content: dict):
    async with AsyncSessionLocal() as db:
        lesson = Lesson(user_id=user_id, lesson_type=lesson_type, content=content)
        db.add(lesson)
        await db.commit()
        await db.refresh(lesson)
        return lesson

async def update_lesson(lesson_id: UUID, **kwargs):
    async with AsyncSessionLocal() as db:
        result = await db.execute(update(Lesson).where(Lesson.id == lesson_id).values(**kwargs).returning(Lesson))
        await db.commit()
        return result.scalar_one_or_none()

async def get_total_users_count():
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(func.count(User.id)))
        return result.scalar() or 0

async def get_active_users_count():
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(func.count(User.id)).where(or_(User.is_subscribed == True, User.subscription_end > datetime.utcnow())))
        return result.scalar() or 0

async def count_user_lessons(user_id: int, lesson_type: str):
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(func.count(Lesson.id)).where(and_(Lesson.user_id == user_id, Lesson.lesson_type == lesson_type)))
        return result.scalar() or 0

async def get_user_gifts(user_id: int):
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Gift).where(Gift.user_id == user_id).order_by(Gift.created_at.desc()))
        return list(result.scalars().all())

async def create_gift(user_id: int, admin_id: int, days: int, expires_at: datetime, note=None):
    async with AsyncSessionLocal() as db:
        gift = Gift(user_id=user_id, admin_id=admin_id, days=days, expires_at=expires_at, note=note)
        db.add(gift)
        await db.commit()

# ============= OPENROUTER =============
openrouter = AsyncOpenAI(api_key=settings.OPENROUTER_API_KEY, base_url=settings.OPENROUTER_BASE_URL)

SYSTEM_PROMPTS = {
    "casual": """You are Alex, a friendly English-Russian teacher. Student: level {level}, areas: {focus_areas}. Rules: explanations in Russian, exercises in English. Keep lessons 5-10 min. Respond in JSON.""",
    "intensive": """You are Coach, a demanding English coach. Student: level {level}. Rules: strict feedback in Russian, English exercises only. Respond in JSON.""",
    "ielts": """You are an IELTS examiner. Student: level {level}, target band {ielts_target_band}. Rules: feedback in Russian, tasks in English. Respond in JSON."""
}

async def generate_with_retry(prompt: str, system: str, **kwargs) -> dict:
    for attempt in range(3):
        try:
            resp = await openrouter.chat.completions.create(
                model="google/gemini-2.5-flash",
                messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0.7, max_tokens=1500, **kwargs
            )
            return json.loads(resp.choices[0].message.content)
        except (RateLimitError, json.JSONDecodeError, APIConnectionError) as e:
            if attempt < 2: await asyncio.sleep(5 * (attempt + 1))
            else: return None

async def generate_lesson(lesson_type: str, level: str, focus_areas: list, total_lessons=0, streak=0, bot_mode="casual", **kwargs):
    system = SYSTEM_PROMPTS.get(bot_mode, SYSTEM_PROMPTS["casual"]).format(level=level, focus_areas=", ".join(focus_areas), **kwargs)
    prompts = {
        "vocabulary": f'Generate vocabulary lesson for {level}. Return JSON: {{"word": "word", "transcription": "IPA", "translation": "ру", "definition": "def", "example_sentence": "example", "quiz": {{"question": "What does it mean?", "options": ["translation", "w1", "w2", "w3"], "correct_index": 0}}}}',
        "grammar": f'Generate grammar lesson for {level}. Return JSON: {{"topic": "topic", "explanation": "explanation in Russian", "exercises": [{{"question": "q", "options": ["a","b","c","d"], "correct_index": 1, "explanation": "why"}}]}} Include 3 exercises.',
        "speaking": f'Generate speaking practice for {level}. Return JSON: {{"situation": "situation in Russian", "task": "task in English", "hint_words": ["w1", "w2"]}}',
        "reading": f'Generate reading for {level}. Return JSON: {{"text": "3-5 sentences", "questions": [{{"question": "q", "options": ["a","b","c","d"], "correct_index": 0}}], "new_words": [{{"word": "w", "translation": "t"}}]}}',
    }
    return await generate_with_retry(prompts.get(lesson_type, prompts["vocabulary"]), system)

async def evaluate_answer(user_answer: str, task: str, level: str) -> dict:
    prompt = f"Evaluate: task='{task}', answer='{user_answer}'. Return JSON: {{'is_correct': true/false, 'score': 1-10, 'praise': 'ru text', 'tip': 'ru tip if wrong'}}"
    return await generate_with_retry(prompt, SYSTEM_PROMPTS["casual"].format(level=level, focus_areas=""))

async def transcribe_audio(file_bytes: bytes, filename: str = "voice.ogg") -> str:
    try:
        resp = await openrouter.audio.transcriptions.create(file=(filename, file_bytes), model="openai/whisper-1", language="en", response_format="json")
        return resp.text
    except: return None

async def analyze_image(image_base64: str, prompt: str, level: str) -> dict:
    system = SYSTEM_PROMPTS["casual"].format(level=level, focus_areas="")
    user_prompt = f"{prompt}. Return JSON: {{'detected_content': 'ru', 'english_lesson': 'ru', 'new_words': [{{'word':'w','translation':'t'}}], 'task': 'exercise'}}"
    try:
        resp = await openrouter.chat.completions.create(
            model="google/gemini-2.5-flash",
            messages=[{"role": "system", "content": system}, {"role": "user", "content": [{"type": "text", "text": user_prompt}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}}]}],
            response_format={"type": "json_object"}, max_tokens=1000
        )
        return json.loads(resp.choices[0].message.content)
    except: return None

def determine_level(correct: int) -> str:
    return {0: "A1", 1: "A1", 2: "A2", 3: "B1", 4: "B2"}.get(correct, "C1")

# ============= КЛАВИАТУРЫ =============
def get_level_keyboard(): return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🟢 Начинающий (A1–A2)", callback_data="level_beginner")], [InlineKeyboardButton(text="🔵 Средний (B1–B2)", callback_data="level_intermediate")], [InlineKeyboardButton(text="🟣 Продвинутый (C1–C2)", callback_data="level_advanced")], [InlineKeyboardButton(text="❓ Не знаю", callback_data="level_dont_know")]])

def get_focus_areas_keyboard(selected: list) -> InlineKeyboardMarkup:
    areas = [("vocabulary", "📝 Словарный запас"), ("grammar", "📖 Грамматика"), ("speaking", "🗣 Разговорная речь"), ("reading", "📚 Чтение")]
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"{'✅' if a in selected else '   '} {n}", callback_data=f"area_{a}")] for a, n in areas] + [[InlineKeyboardButton(text="Готово ➡️", callback_data="areas_done")]])

def get_bot_mode_keyboard(): return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="👋 Дружелюбный", callback_data="mode_casual")], [InlineKeyboardButton(text="💪 Интенсивный", callback_data="mode_intensive")], [InlineKeyboardButton(text="📋 IELTS", callback_data="mode_ielts")]])

def get_timezone_keyboard(): return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🇷🇺 Москва (UTC+3)", callback_data="tz_moscow")], [InlineKeyboardButton(text="🇷🇺 Екатеринбург (UTC+5)", callback_data="tz_ekb")], [InlineKeyboardButton(text="🇺🇿 Ташкент (UTC+5)", callback_data="tz_tashkent")], [InlineKeyboardButton(text="🌍 Другой", callback_data="tz_other")]])

def get_main_menu_keyboard(): return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📚 Урок", callback_data="start_lesson"), InlineKeyboardButton(text="📊 Статистика", callback_data="show_stats")], [InlineKeyboardButton(text="⏰ Расписание", callback_data="show_schedule"), InlineKeyboardButton(text="💳 Подписка", callback_data="show_subscription")]])

def get_lesson_type_keyboard(): return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📝 Словарный", callback_data="lesson_vocabulary")], [InlineKeyboardButton(text="📖 Грамматика", callback_data="lesson_grammar")], [InlineKeyboardButton(text="🗣 Разговорная", callback_data="lesson_speaking")], [InlineKeyboardButton(text="📚 Чтение", callback_data="lesson_reading")]])

def get_lesson_answer_keyboard(options: list): return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=opt, callback_data=f"answer_{i}")] for i, opt in enumerate(options)])

def get_lesson_continue_keyboard(): return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➡️ Дальше", callback_data="lesson_continue")], [InlineKeyboardButton(text="❌ Завершить", callback_data="lesson_end")]])

def get_quiz_keyboard(options: list): return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=opt, callback_data=f"quiz_{i}")] for i, opt in enumerate(options)])

def get_back_keyboard(): return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]])

def get_onboarding_keyboard(): return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📚 Начать первый урок", callback_data="start_lesson")]])

# ============= FSM =============
class OnboardingStates(StatesGroup):
    waiting_level_choice = State()
    waiting_test_answer = State()
    waiting_focus_areas = State()
    waiting_bot_mode = State()
    waiting_notify_time = State()
    waiting_timezone = State()

class LessonStates(StatesGroup):
    in_lesson = State()
    waiting_answer = State()

# ============= RATE LIMIT =============
class RateLimitMiddleware:
    def __init__(self):
        self.counters = defaultdict(dict)
    
    def check(self, user_id: int, limit_type="default") -> bool:
        limit, period = {"default": (1, 2), "lesson": (1, 10), "voice": (1, 15), "photo": (1, 15)}.get(limit_type, (1, 2))
        key = f"{limit_type}_{user_id}"
        now = time.time()
        last = self.counters[user_id].get(key)
        if not last or now - last[1] >= period:
            self.counters[user_id][key] = (1, now)
            return True
        if last[0] >= limit: return False
        self.counters[user_id][key] = (last[0] + 1, last[1])
        return True

rate_limiter = RateLimitMiddleware()

# ============= ROUTER =============
router = Router()

# /start
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    user, created = await get_or_create_user(message.from_user.id, message.from_user.username, message.from_user.full_name, message.from_user.language_code)
    if user.level and user.focus_areas and user.trial_started:
        status = "🟢 Активна" if user.has_active_access() else "🔴 Истекла"
        await message.answer(f"👋 Привет, {user.full_name or user.username}!\n\n📊 Уровень: {user.level}\n⏰ Расписание: {', '.join(user.notify_times)}\n💳 Подписка: {status}\n\nЧто хочешь сделать?", reply_markup=get_main_menu_keyboard())
    else:
        await state.set_state(OnboardingStates.waiting_level_choice)
        await message.answer("👋 Привет! Я Алекс — твой учитель английского!\n\nДавай начнём — это займёт 1 минуту.", reply_markup=get_onboarding_keyboard())
        await message.answer("Какой у тебя уровень английского?", reply_markup=get_level_keyboard())

@router.callback_query(F.data == "onboarding_done", state=OnboardingStates.waiting_level_choice)
async def onboarding_level(callback: CallbackQuery, state: FSMContext):
    level_map = {"level_beginner": "A1", "level_elementary": "A2", "level_intermediate": "B1", "level_upper": "B2", "level_advanced": "C1", "level_proficient": "C2"}
    if callback.data == "level_dont_know":
        await callback.answer()
        await callback.message.edit_text("Не проблема! Пройдём короткий тест — 5 вопросов 🎯")
        await state.set_state(OnboardingStates.waiting_test_answer)
        await state.update_data(test_correct=0, test_question_num=0)
        await send_test_question(callback.message, state, 0)
        return
    if callback.data in level_map:
        await state.update_data(selected_level=level_map[callback.data])
        await callback.answer()
        await callback.message.edit_text("Отлично! Твой уровень выбран ✅")
        await state.set_state(OnboardingStates.waiting_focus_areas)
        await callback.message.answer("Что хочешь прокачать? (можно несколько)", reply_markup=get_focus_areas_keyboard([]))

@router.callback_query(F.data.startswith("area_"), state=OnboardingStates.waiting_focus_areas)
async def toggle_area(callback: CallbackQuery, state: FSMContext):
    area = callback.data.replace("area_", "")
    data = await state.get_data()
    selected = data.get("selected_areas", [])
    if area in selected: selected.remove(area)
    else: selected.append(area)
    await state.update_data(selected_areas=selected)
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=get_focus_areas_keyboard(selected))

@router.callback_query(F.data == "areas_done", state=OnboardingStates.waiting_focus_areas)
async def areas_done(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = data.get("selected_areas", [])
    if not selected:
        await callback.answer("Выбери хотя бы одно!", show_alert=True)
        return
    await state.update_data(selected_areas=selected)
    await callback.answer()
    await state.set_state(OnboardingStates.waiting_bot_mode)
    await callback.message.edit_text("Выбери режим обучения:", reply_markup=get_bot_mode_keyboard())

@router.callback_query(F.data.startswith("mode_"), state=OnboardingStates.waiting_bot_mode)
async def choose_mode(callback: CallbackQuery, state: FSMContext):
    mode = callback.data.replace("mode_", "")
    await state.update_data(bot_mode=mode)
    await callback.answer()
    await state.set_state(OnboardingStates.waiting_notify_time)
    await callback.message.edit_text("В какое время присылать напоминания?\nВведи в формате ЧЧ:ММ, например: 09:00\nМожно несколько — каждое с новой строки")

@router.message(state=OnboardingStates.waiting_notify_time)
async def set_notify_time(message: Message, state: FSMContext):
    times = [l.strip() for l in message.text.strip().split("\n") if re.match(r"^([01]\d|2[0-3]):([0-5]\d)$", l.strip())]
    if not times:
        await message.answer("❌ Неверный формат. Введи время ЧЧ:ММ")
        return
    await state.update_data(notify_times=times)
    await state.set_state(OnboardingStates.waiting_timezone)
    await message.answer("Выбери часовой пояс:", reply_markup=get_timezone_keyboard())

@router.callback_query(F.data.startswith("tz_"), state=OnboardingStates.waiting_timezone)
async def choose_tz(callback: CallbackQuery, state: FSMContext):
    tz_map = {"tz_moscow": "Europe/Moscow", "tz_ekb": "Asia/Yekaterinburg", "tz_tashkent": "Asia/Tashkent"}
    if callback.data == "tz_other":
        await callback.answer()
        await callback.message.edit_text("Введи UTC-смещение (например: +4):")
        return
    if callback.data in tz_map:
        await state.update_data(timezone=tz_map[callback.data])
        await finish_onboarding(callback.message, state)

@router.message(state=OnboardingStates.waiting_timezone)
async def custom_tz(message: Message, state: FSMContext):
    tz = message.text.strip()
    if re.match(r"^[+-]?\d{1,2}$", tz):
        if not tz.startswith(("+", "-")): tz = "+" + tz
        await state.update_data(timezone=f"UTC{tz}")
        await finish_onboarding(message, state)
    else:
        await message.answer("❌ Неверный формат")

async def send_test_question(message: Message, state: FSMContext, num: int):
    fallback_q = [("He ___ to school yesterday.", ["goes", "went", "going", "go"]), ("'Я люблю читать'", ["I love read", "I love reading", "I loves reading", "I loving read"]), ("The weather is ___ today.", ["good", "well", "better", "best"]), ("Which is correct?", ["Although tired, he continued", "Although he was tired, he continued", "Although tired, but he continued", "He was tired although"]), ("'Would you mind opening the window?'", ["Yes, please", "No, not at all", "I'm not minding", "That's right"])]
    q_data = {"question": fallback_q[num][0], "options": fallback_q[num][1], "correct_index": 0}
    await state.update_data(current_question=q_data, test_question_num=num)
    await message.answer(f"❓ Вопрос {num+1}/5:\n\n{fallback_q[num][0]}", reply_markup=get_quiz_keyboard(fallback_q[num][1]))

@router.callback_query(F.data.startswith("quiz_"), state=OnboardingStates.waiting_test_answer)
async def test_answer(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    num = data.get("test_question_num", 0)
    correct = data.get("test_correct", 0)
    answer_idx = int(callback.data.replace("quiz_", ""))
    is_correct = answer_idx == 0
    if is_correct: correct += 1
    await callback.answer("✅" if is_correct else "❌")
    next_num = num + 1
    if next_num < 5:
        await state.update_data(test_correct=correct, test_question_num=next_num)
        await send_test_question(callback.message, state, next_num)
    else:
        level = determine_level(correct)
        await state.update_data(selected_level=level)
        level_names = {"A1": "Начинающий", "A2": "Элементарный", "B1": "Средний", "B2": "Выше среднего", "C1": "Продвинутый"}
        await callback.message.answer(f"🎉 Тест завершён!\n\nТвой уровень: {level_names.get(level, level)} ({level})\n\nЭто значит что ты уже знаешь базу, но есть куда расти!")
        await state.set_state(OnboardingStates.waiting_focus_areas)
        await callback.message.answer("Что хочешь прокачать? (можно несколько)", reply_markup=get_focus_areas_keyboard([]))

async def finish_onboarding(message: Message, state: FSMContext):
    data = await state.get_data()
    await update_user(message.from_user.id, level=data.get("selected_level", "B1"), focus_areas=data.get("selected_areas", ["vocabulary"]), bot_mode=data.get("bot_mode", "casual"), notify_times=data.get("notify_times", ["09:00"]), timezone=data.get("timezone", "UTC"))
    await state.clear()
    await message.answer("🚀 Всё готово!\n\nТвой пробный период активирован — 30 дней бесплатно.\n\nНачнём прямо сейчас?", reply_markup=get_onboarding_keyboard())

@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer("📚 <b>Что я умею:</b>\n\n/start — начать\n/lesson — урок\n/stats — статистика\n/schedule — расписание\n/help — эта справка", parse_mode="HTML")

# /lesson
@router.message(Command("lesson"))
async def cmd_lesson(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if not user: await message.answer("Сначала /start"); return
    if not user.has_active_access(): await message.answer("⏰ Подписка требуется"); return
    await state.set_state(LessonStates.in_lesson)
    await message.answer("Какой тип урока?", reply_markup=get_lesson_type_keyboard())

@router.callback_query(F.data == "start_lesson")
async def start_lesson(callback: CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
    if not user or not user.has_active_access(): await callback.answer("⏰ Подписка требуется!", show_alert=True); return
    await state.set_state(LessonStates.in_lesson)
    await callback.message.edit_text("Какой тип урока?", reply_markup=get_lesson_type_keyboard())

@router.callback_query(F.data.startswith("lesson_"), state=LessonStates.in_lesson)
async def handle_lesson_type(callback: CallbackQuery, state: FSMContext):
    lesson_type = callback.data.replace("lesson_", "")
    user = await get_user(callback.from_user.id)
    await callback.answer("Генерирую... ⏳")
    content = await generate_lesson(lesson_type, user.level, user.focus_areas, user.total_lessons, user.streak, user.bot_mode)
    if not content:
        await callback.message.answer("😔 Ошибка генерации. Попробуй ещё раз.")
        await state.clear()
        return
    lesson = await create_lesson(callback.from_user.id, lesson_type, content)
    await state.update_data(lesson_id=str(lesson.id), lesson_type=lesson_type, content=content)
    await state.set_state(LessonStates.waiting_answer)
    if lesson_type == "vocabulary":
        word = content.get("word", ""); trans = content.get("transcription", ""); translation = content.get("translation", ""); definition = content.get("definition", ""); example = content.get("example_sentence", "")
        await callback.message.answer(f"📝 <b>Словарный урок</b>\n\nWord: <b>{word}</b> {trans}\nTranslation: {translation}\n\n<i>Definition:</i> {definition}\n\n<i>Example:</i> {example}", parse_mode="HTML")
        quiz = content.get("quiz", {})
        if quiz:
            await state.update_data(quiz_correct_index=quiz.get("correct_index", 0))
            await callback.message.answer(f"❓ {quiz.get('question', 'What does it mean?')}", reply_markup=get_lesson_answer_keyboard(quiz.get("options", [])))
    elif lesson_type == "grammar":
        topic = content.get("topic", ""); explanation = content.get("explanation", ""); example = content.get("example_correct", "")
        await callback.message.answer(f"📖 <b>Грамматика: {topic}</b>\n\n{explanation}\n\n<i>Example:</i> {example}", parse_mode="HTML")
        exercises = content.get("exercises", [])
        if exercises:
            await state.update_data(current_exercise=0, exercises=exercises)
            ex = exercises[0]
            await callback.message.answer(f"❓ {ex.get('question', '')}", reply_markup=get_lesson_answer_keyboard(ex.get("options", [])))
    elif lesson_type == "speaking":
        situation = content.get("situation", ""); task = content.get("task", ""); hints = content.get("hint_words", [])
        await callback.message.answer(f"🗣 <b>Разговорная речь</b>\n\nSituation: {situation}\n\n<b>Task:</b> {task}\n\nHint: {', '.join(hints)}", parse_mode="HTML")
        await callback.message.answer("Напиши свой ответ на английском:")
    elif lesson_type == "reading":
        text = content.get("text", "")
        await callback.message.answer(f"📚 <b>Чтение</b>\n\n{text}\n\nAnswer the questions:", parse_mode="HTML")
        questions = content.get("questions", [])
        if questions:
            await state.update_data(current_question=0, questions=questions)
            q = questions[0]
            await callback.message.answer(f"❓ {q.get('question', '')}", reply_markup=get_lesson_answer_keyboard(q.get("options", [])))

@router.callback_query(F.data.startswith("answer_"), state=LessonStates.waiting_answer)
async def handle_answer(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    answer_idx = int(callback.data.replace("answer_", ""))
    lesson_type = data.get("lesson_type", "")
    lesson_id = UUID(data.get("lesson_id", ""))
    content = data.get("content", {})
    is_correct = False
    feedback = ""
    if lesson_type == "vocabulary":
        correct_idx = content.get("quiz", {}).get("correct_index", 0)
        is_correct = answer_idx == correct_idx
        feedback = "✅ Правильно!" if is_correct else f"❌ Неправильно. Правильный: {content['quiz']['options'][correct_idx]}"
    elif lesson_type == "grammar":
        exercises = data.get("exercises", [])
        current = data.get("current_exercise", 0)
        ex = exercises[current] if current < len(exercises) else {}
        correct_idx = ex.get("correct_index", 0)
        is_correct = answer_idx == correct_idx
        feedback = f"✅ Правильно! {ex.get('explanation', '')}" if is_correct else f"❌ Неправильно. {ex.get('explanation', '')}"
        next_ex = current + 1
        if next_ex < len(exercises):
            await state.update_data(current_exercise=next_ex)
            ex = exercises[next_ex]
            await callback.answer(feedback)
            await callback.message.answer(f"❓ {ex.get('question', '')}", reply_markup=get_lesson_answer_keyboard(ex.get("options", [])))
            return
    elif lesson_type == "reading":
        questions = data.get("questions", [])
        current = data.get("current_question", 0)
        q = questions[current] if current < len(questions) else {}
        correct_idx = q.get("correct_index", 0)
        is_correct = answer_idx == correct_idx
        feedback = "✅ Правильно!" if is_correct else f"❌ Неправильно"
        next_q = current + 1
        if next_q < len(questions):
            await state.update_data(current_question=next_q)
            q = questions[next_q]
            await callback.answer(feedback)
            await callback.message.answer(f"❓ {q.get('question', '')}", reply_markup=get_lesson_answer_keyboard(q.get("options", [])))
            return
    await callback.answer(feedback)
    await callback.message.answer("🎉 Урок завершён!", reply_markup=get_lesson_continue_keyboard())
    if is_correct: await update_user_after_lesson(callback.from_user.id, lesson_type)

@router.message(state=LessonStates.waiting_answer)
async def handle_free_answer(message: Message, state: FSMContext):
    data = await state.get_data()
    lesson_type = data.get("lesson_type", "")
    content = data.get("content", {})
    task = content.get("task", "")
    user = await get_user(message.from_user.id)
    result = await evaluate_answer(message.text, task, user.level if user else "B1")
    if result:
        await message.answer(f"📝 {result.get('praise', 'Хорошо!')}")
        if result.get("tip"): await message.answer(f"💡 {result['tip']}")
    else:
        await message.answer("📝 Спасибо за ответ!")
    await message.answer("Продолжим?", reply_markup=get_lesson_continue_keyboard())

@router.callback_query(F.data == "lesson_continue", state=LessonStates.waiting_answer)
async def continue_lesson(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Выбери урок:")
    await callback.message.answer(reply_markup=get_lesson_type_keyboard())

@router.callback_query(F.data == "lesson_end")
async def end_lesson(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("✅ Хорошая работа! Возвращайся завтра 📚")

async def update_user_after_lesson(user_id: int, lesson_type: str):
    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        if not user: return
        user.total_lessons += 1
        user.streak += 1
        user.xp_total += 10
        if user.streak > user.longest_streak: user.longest_streak = user.streak
        new_level = 1
        for i, xp in enumerate([0, 100, 300, 700, 1500, 3000, 6000]):
            if user.xp_total < xp: break
            new_level = i + 1
        user.xp_level = new_level
        await db.commit()
        # Check badges
        checks = [("first_lesson", user.total_lessons >= 1), ("streak_3", user.streak >= 3), ("streak_7", user.streak >= 7), ("lessons_10", user.total_lessons >= 10)]
        for badge_id, cond in checks:
            if cond and not await has_badge(user_id, badge_id):
                await award_badge(user_id, badge_id)

# Voice
@router.message(F.voice)
async def handle_voice(message: Message, bot: Bot):
    user = await get_user(message.from_user.id)
    if not user or not user.has_active_access(): return
    await message.answer("🎤 Распознаю...")
    try:
        file = await bot.get_file(message.voice.file_id)
        file_bytes = await bot.download_file(file.file_path)
        transcript = await transcribe_audio(file_bytes.getvalue())
        if transcript:
            await message.answer(f"📝 Ты сказал: {transcript}")
            await message.answer("Отличное произношение! 🎯")
        else:
            await message.answer("😔 Не удалось распознать. Попробуй написать текстом.")
    except: await message.answer("😔 Ошибка")

# Photo
@router.message(F.photo)
async def handle_photo(message: Message, bot: Bot):
    user = await get_user(message.from_user.id)
    if not user or not user.has_active_access(): return
    await message.answer("📷 Анализирую...")
    try:
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        file_bytes = await bot.download_file(file.file_path)
        img_b64 = base64.b64encode(file_bytes.getvalue()).decode()
        result = await analyze_image(img_b64, message.caption or "Что на этом изображении?", user.level)
        if result:
            text = f"🔍 {result.get('detected_content', '')}\n\n📚 {result.get('english_lesson', '')}"
            if result.get("task"): text += f"\n\n❓ {result['task']}"
            if result.get("new_words"): text += "\n\n📝 " + "\n".join([f"• {w['word']} — {w['translation']}" for w in result["new_words"]])
            await message.answer(text)
        else:
            await message.answer("😔 Не удалось проанализировать")
    except: await message.answer("😔 Ошибка")

# /stats
@router.message(Command("stats"))
async def cmd_stats(message: Message):
    user = await get_user(message.from_user.id)
    if not user: await message.answer("Сначала /start"); return
    badges = await get_user_badges(message.from_user.id)
    text = f"📊 <b>Твоя статистика</b>\n\n{user.get_level_name()} | Уровень {user.xp_level}\n⭐ XP: {user.xp_total} / {user.get_xp_for_next_level()}\n\n📚 Уроков: {user.total_lessons}\n🔥 Серия: {user.streak} дней | Рекорд: {user.longest_streak} дней\n✨ Идеальных: {user.lessons_perfect}\n\n🏅 Бейджей: {len(badges)}"
    await message.answer(text, parse_mode="HTML")

@router.callback_query(F.data == "show_stats")
async def show_stats(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    badges = await get_user_badges(callback.from_user.id)
    text = f"📊 <b>Статистика</b>\n\n{user.get_level_name()} | XP: {user.xp_total}\n📚 Уроков: {user.total_lessons}\n🔥 Серия: {user.streak} дней\n🏅 Бейджей: {len(badges)}"
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=get_back_keyboard())

# /schedule
@router.message(Command("schedule"))
async def cmd_schedule(message: Message):
    user = await get_user(message.from_user.id)
    if not user: await message.answer("Сначала /start"); return
    times = ", ".join(user.notify_times) if user.notify_times else "Не настроено"
    await message.answer(f"⏰ <b>Расписание</b>\n\nВремя: {times}\nЧасовой пояс: {user.timezone}\n\nВведи новое время в формате ЧЧ:ММ:", parse_mode="HTML")

@router.message(F.text.regexp(r"^\d{2}:\d{2}"))
async def set_schedule(message: Message):
    times = [l.strip() for l in message.text.strip().split("\n") if re.match(r"^([01]\d|2[0-3]):([0-5]\d)$", l.strip())]
    if not times:
        await message.answer("❌ Неверный формат"); return
    await update_user(message.from_user.id, notify_times=times)
    await message.answer(f"✅ Расписание обновлено: {', '.join(times)}")

# Subscription
@router.callback_query(F.data == "show_subscription")
async def show_subscription(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    has_access = user.has_active_access() if user else False
    if has_access:
        end = user.subscription_end or (user.trial_started + timedelta(days=30))
        days = (end - datetime.utcnow()).days
        text = f"💳 <b>Подписка</b>\n\n✅ Активна\n📅 До: {end.strftime('%d.%m.%Y')}\n⏰ {days} дней"
    else:
        text = f"💳 <b>Подписка</b>\n\n🔴 Неактивна\n\n💰 {settings.SUBSCRIPTION_PRICE}₽/месяц"
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=get_back_keyboard())

# Admin
def is_admin(user_id: int) -> bool:
    return user_id in settings.ADMIN_IDS

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id): return
    await message.answer("🔧 <b>Админ-панель</b>\n\n/admin_stats — статистика\n/admin_users — пользователи\n/admin_gift ID [дни] — подарить\n/admin_ban ID — забанить\n/admin_unban ID — разбанить", parse_mode="HTML")

@router.message(Command("admin_stats"))
async def admin_stats(message: Message):
    if not is_admin(message.from_user.id): return
    total = await get_total_users_count()
    active = await get_active_users_count()
    await message.answer(f"📊 Статистика:\n\nВсего: {total}\nАктивных: {active}")

@router.message(Command("admin_user"))
async def admin_user(message: Message):
    if not is_admin(message.from_user.id): return
    try:
        uid = int(message.text.split()[1])
        user = await get_user(uid)
        if not user: await message.answer("Не найден"); return
        await message.answer(f"👤 {user.full_name or 'N/A'}\n@{user.username or 'N/A'}\nID: {user.id}\nУровень: {user.level}\nУроков: {user.total_lessons}\nXP: {user.xp_total}\nПодписка: {'Активна' if user.has_active_access() else 'Нет'}")
    except: await message.answer("Использование: /admin_user ID")

@router.message(Command("admin_gift"))
async def admin_gift(message: Message, bot: Bot):
    if not is_admin(message.from_user.id): return
    args = message.text.split()
    try:
        target_id = int(args[1])
        days = int(args[2]) if len(args) > 2 else 30
        user = await get_user(target_id)
        if not user: await message.answer("Не найден"); return
        now = datetime.utcnow()
        current_end = user.subscription_end or now
        if current_end < now: current_end = now
        new_end = current_end + timedelta(days=days)
        await update_user(target_id, subscription_end=new_end, gifted_days_total=user.gifted_days_total + days, gifted_by=message.from_user.id, gifted_at=now)
        await create_gift(target_id, message.from_user.id, days, new_end)
        try: await bot.send_message(target_id, f"🎁 Тебе подарено {days} дней!\n\nДоступ до {new_end.strftime('%d.%m.%Y')}\n\nУчи английский! 🚀")
        except: pass
        await message.answer(f"✅ {target_id} подарено {days} дней до {new_end.strftime('%d.%m.%Y')}")
    except: await message.answer("Использование: /admin_gift ID [дни]")

@router.message(Command("admin_ban"))
async def admin_ban(message: Message):
    if not is_admin(message.from_user.id): return
    try:
        uid = int(message.text.split()[1])
        await update_user(uid, is_blocked=True)
        await message.answer(f"✅ {uid} заблокирован")
    except: await message.answer("Использование: /admin_ban ID")

@router.message(Command("admin_unban"))
async def admin_unban(message: Message):
    if not is_admin(message.from_user.id): return
    try:
        uid = int(message.text.split()[1])
        await update_user(uid, is_blocked=False)
        await message.answer(f"✅ {uid} разблокирован")
    except: await message.answer("Использование: /admin_unban ID")

@router.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: CallbackQuery):
    await callback.message.edit_text("Главное меню:", reply_markup=get_main_menu_keyboard())

# ============= MAIN =============
dp = Dispatcher()
dp.include_router(router)

scheduler = AsyncIOScheduler()

async def send_daily(bot: Bot):
    users = await get_all_users(500)
    now = datetime.utcnow().strftime("%H:%M")
    for user in users:
        if not user.has_active_access(): continue
        if now in (user.notify_times or []):
            try:
                await bot.send_message(user.id, "☀️ Доброе утро!\n\nГотов к уроку? 📚", reply_markup=get_lesson_type_keyboard())
                await asyncio.sleep(0.5)
            except: pass

def setup_scheduler(bot: Bot):
    scheduler.add_job(send_daily, "cron", minute="*", args=[bot], id="daily")
    scheduler.start()

async def main():
    if not settings.BOT_TOKEN or not settings.DATABASE_URL or not settings.OPENROUTER_API_KEY:
        logger.error("Missing config! Set BOT_TOKEN, DATABASE_URL, OPENROUTER_API_KEY")
        return
    
    # Создать таблицы в БД
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database tables created")
    except Exception as e:
        logger.error(f"Failed to create tables: {e}")
    
    bot = Bot(token=settings.BOT_TOKEN, parse_mode=ParseMode.HTML)
    setup_scheduler(bot)
    logger.info("Starting EnglishBot...")
    if settings.WEBHOOK_URL:
        await bot.set_webhook(f"{settings.WEBHOOK_URL}/webhook", secret_token=settings.WEBHOOK_SECRET)
        from aiohttp import web
        app = web.Application()
        async def health(req): return web.json_response({"status": "ok"})
        async def wh(req):
            try:
                update = Update.model_validate(await req.json())
                await dp.feed_update(bot, update)
            except: pass
            return web.Response()
        app.router.add_get("/health", health)
        app.router.add_post("/webhook", wh)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", 8000)
        await site.start()
        await asyncio.Event().wait()
    else:
        await bot.delete_webhook()
        await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

