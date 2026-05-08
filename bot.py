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
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Message, CallbackQuery, Update, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
from sqlalchemy import Column, BigInteger, String, Boolean, Integer, Text, DateTime, Date, ForeignKey, ARRAY, select, update, func, and_, or_
from sqlalchemy.dialects.postgresql import UUID as PGUUID, JSONB
from openai import AsyncOpenAI, RateLimitError, APIConnectionError, APIStatusError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz

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

def _fix_db_url(url: str) -> str:
    """Ensure async-compatible driver in DATABASE_URL."""
    if not url or not url.strip():
        raise ValueError(
            "\n\n[CONFIG ERROR] DATABASE_URL не задан!\n"
            "Добавь в .env файл:\n"
            "DATABASE_URL=postgresql+asyncpg://user:password@host:5432/dbname\n"
        )
    url = url.strip()
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgresql://") and "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    elif not url.startswith("postgresql"):
        raise ValueError(
            f"\n\n[CONFIG ERROR] Неверный формат DATABASE_URL: '{url[:60]}'\n"
            "Ожидается: postgresql+asyncpg://user:password@host:5432/dbname\n"
        )
    logging.getLogger(__name__).info(f"DB driver: {url.split('://')[0]}")
    return url

engine = create_async_engine(_fix_db_url(settings.DATABASE_URL), echo=False, pool_pre_ping=True, pool_size=10, max_overflow=20)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# ============= МОДЕЛИ =============
class User(Base):
    __tablename__ = "eng_users"
    id = Column(BigInteger, primary_key=True)
    username = Column(String(64), nullable=True)
    full_name = Column(String(128), nullable=True)
    display_name = Column(String(128), nullable=True)       # [NEW] имя из онбординга
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
    learning_style = Column(String(32), nullable=True)      # [NEW] стиль восприятия
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
        names = {1: "🌱 Росток", 2: "📖 Ученик", 3: "⭐ Студент", 4: "🎓 Знаток",
                 5: "🏆 Мастер", 6: "🔥 Эксперт", 7: "💎 Легенда"}
        return names.get(self.xp_level, "💎 Легенда")

    def get_display_name(self):
        return self.display_name or self.full_name or self.username or "друг"


class Lesson(Base):
    __tablename__ = "eng_lessons"
    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(BigInteger, ForeignKey("eng_users.id"), nullable=False)
    lesson_type = Column(String(32))
    content = Column(JSONB)
    user_answer = Column(Text)
    is_correct = Column(Boolean)
    input_type = Column(String(16), default="text")
    voice_transcript = Column(Text, nullable=True)
    started_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

class Notification(Base):
    __tablename__ = "eng_notifications"
    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(BigInteger, ForeignKey("eng_users.id"), nullable=False)
    scheduled_at = Column(DateTime)
    sent_at = Column(DateTime, nullable=True)
    ignored = Column(Boolean, default=False)
    reminder_count = Column(Integer, default=0)

class Payment(Base):
    __tablename__ = "eng_payments"
    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(BigInteger, ForeignKey("eng_users.id"), nullable=False)
    telegram_payment_id = Column(String(256), nullable=True)
    amount = Column(Integer)
    currency = Column(String(8))
    status = Column(String(32))
    created_at = Column(DateTime, default=datetime.utcnow)

class Badge(Base):
    __tablename__ = "eng_badges"
    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(BigInteger, ForeignKey("eng_users.id"), nullable=False)
    badge_id = Column(String(64))
    earned_at = Column(DateTime, default=datetime.utcnow)

class Gift(Base):
    __tablename__ = "eng_gifts"
    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(BigInteger, ForeignKey("eng_users.id"), nullable=False)
    admin_id = Column(BigInteger)
    days = Column(Integer)
    note = Column(String(256), nullable=True)
    expires_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)

class UserMemory(Base):
    """Слабые места, пройденные темы и паттерны ошибок пользователя."""
    __tablename__ = "eng_memory"
    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(BigInteger, ForeignKey("eng_users.id"), nullable=False)
    key = Column(String(128))      # e.g. "weak:past_perfect", "done:vocabulary:run"
    value = Column(Text)           # описание на русском
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class ChatMessage(Base):
    """История диалога с Алексом — контекст для следующего ответа."""
    __tablename__ = "eng_chat"
    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(BigInteger, ForeignKey("eng_users.id"), nullable=False)
    role = Column(String(16))      # "user" | "assistant"
    content = Column(Text)
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
        user = User(id=user_id, username=username, full_name=full_name,
                    language_code=language_code, trial_started=datetime.utcnow())
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
        result = await db.execute(
            select(Badge).where(and_(Badge.user_id == user_id, Badge.badge_id == badge_id))
        )
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
        result = await db.execute(
            update(Lesson).where(Lesson.id == lesson_id).values(**kwargs).returning(Lesson)
        )
        await db.commit()
        return result.scalar_one_or_none()

async def get_total_users_count():
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(func.count(User.id)))
        return result.scalar() or 0

async def get_active_users_count():
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(func.count(User.id)).where(
                or_(User.is_subscribed == True, User.subscription_end > datetime.utcnow())
            )
        )
        return result.scalar() or 0

async def count_user_lessons(user_id: int, lesson_type: str):
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(func.count(Lesson.id)).where(
                and_(Lesson.user_id == user_id, Lesson.lesson_type == lesson_type)
            )
        )
        return result.scalar() or 0

async def get_user_gifts(user_id: int):
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Gift).where(Gift.user_id == user_id).order_by(Gift.created_at.desc())
        )
        return list(result.scalars().all())

async def create_gift(user_id: int, admin_id: int, days: int, expires_at: datetime, note=None):
    async with AsyncSessionLocal() as db:
        gift = Gift(user_id=user_id, admin_id=admin_id, days=days, expires_at=expires_at, note=note)
        db.add(gift)
        await db.commit()

# ---- Память пользователя ----

async def get_user_memory(user_id: int) -> list[dict]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(UserMemory)
            .where(UserMemory.user_id == user_id)
            .order_by(UserMemory.updated_at.desc())
        )
        rows = result.scalars().all()
        return [{"key": r.key, "value": r.value} for r in rows]

async def upsert_memory(user_id: int, key: str, value: str):
    """Создаёт или обновляет запись памяти по ключу."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(UserMemory).where(and_(UserMemory.user_id == user_id, UserMemory.key == key))
        )
        row = result.scalar_one_or_none()
        if row:
            row.value = value
            row.updated_at = datetime.utcnow()
        else:
            db.add(UserMemory(user_id=user_id, key=key, value=value))
        await db.commit()

# ---- История чата ----

async def get_chat_history(user_id: int, limit: int = 12) -> list[dict]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ChatMessage)
            .where(ChatMessage.user_id == user_id)
            .order_by(ChatMessage.created_at.desc())
            .limit(limit)
        )
        rows = list(reversed(result.scalars().all()))
        return [{"role": r.role, "content": r.content} for r in rows]

async def add_chat_message(user_id: int, role: str, content: str):
    """Сохраняет сообщение и оставляет только последние 30."""
    async with AsyncSessionLocal() as db:
        db.add(ChatMessage(user_id=user_id, role=role, content=content))
        await db.commit()
        # Чистим старые сообщения — оставляем 30
        result = await db.execute(
            select(ChatMessage.id)
            .where(ChatMessage.user_id == user_id)
            .order_by(ChatMessage.created_at.desc())
            .offset(30)
        )
        old_ids = result.scalars().all()
        if old_ids:
            await db.execute(
                update(ChatMessage).where(ChatMessage.id.in_(old_ids))
            )
            for oid in old_ids:
                old = await db.get(ChatMessage, oid)
                if old:
                    await db.delete(old)
            await db.commit()

# ============= OPENROUTER =============
openrouter = AsyncOpenAI(api_key=settings.OPENROUTER_API_KEY, base_url=settings.OPENROUTER_BASE_URL)

# ---- Личность Алекса ----

ALEX_PERSONA = """
Ты Алекс — живой, тёплый учитель английского языка. Ты работаешь с этим учеником персонально.

ТВОЙ ХАРАКТЕР:
- Общаешься по-русски, но иногда естественно вставляешь английские слова/фразы — как бы показывая пример
- Тёплый, немного с юмором, но без заигрывания
- Искренне радуешься успехам: "О, это уже уровень B2, молодец!"
- Когда ученик ошибается — объясняешь ПОЧЕМУ, даёшь правило и аналогичный пример
- Иногда рассказываешь короткие истории: "У меня был студент, который тоже путал это..."
- Помнишь прогресс и слабые места ученика, упоминаешь их к месту
- Можешь сам задать вопрос о жизни, целях, интересах — используй это для практики

ЧТО НЕЛЬЗЯ:
- Роботизированные ответы ("Incorrect. The answer is...")
- Просто похвалить без объяснения ошибки
- Игнорировать ошибки в английском тексте ученика — всегда мягко поправляй

ПРОФИЛЬ УЧЕНИКА:
Имя: {name}
Уровень: {level} | Серия: {streak} дней | Уроков всего: {total_lessons}
Слабые места: {weak_spots}
Недавние темы: {recent_topics}
Дата сегодня: {today}

ФОРМАТ ОТВЕТА: обычный текст, без JSON. Максимум 3-4 абзаца. Можно использовать эмодзи умеренно.
"""

ALEX_ERROR_ANALYSIS = """
Ты Алекс, учитель английского. Ученик только что ошибся в задании.

Задание: {task}
Ответ ученика: {user_answer}
Правильный ответ: {correct_answer}
Тип урока: {lesson_type}

Напиши разбор ошибки в формате JSON:
{{
  "explanation": "объяснение на русском — почему это неправильно, какое правило нарушено (2-3 предложения)",
  "example_correct": "пример правильного использования на английском",
  "example_wrong": "типичная ошибка (похожая на допущенную)",
  "weak_spot": "короткое название темы для запоминания (10-15 слов макс, по-русски)",
  "encouragement": "одна ободряющая фраза от Алекса"
}}
"""

async def chat_with_alex(user_id: int, user_text: str) -> str:
    """Свободный диалог с Алексом — отвечает как живой учитель."""
    user = await get_user(user_id)
    if not user:
        return "Привет! Напиши /start чтобы начать 😊"

    memory = await get_user_memory(user_id)
    history = await get_chat_history(user_id, limit=12)

    weak_spots  = [m["value"] for m in memory if m["key"].startswith("weak:")]
    recent_done = [m["value"] for m in memory if m["key"].startswith("done:")]

    system = ALEX_PERSONA.format(
        name=user.get_display_name(),
        level=user.level,
        streak=user.streak,
        total_lessons=user.total_lessons,
        weak_spots=", ".join(weak_spots[-5:]) or "пока не определены",
        recent_topics=", ".join(recent_done[-5:]) or "уроков ещё не было",
        today=date.today().strftime("%d %B %Y"),
    )

    messages = [{"role": m["role"], "content": m["content"]} for m in history]
    messages.append({"role": "user", "content": user_text})

    try:
        resp = await openrouter.chat.completions.create(
            model="google/gemini-2.5-flash",
            messages=[{"role": "system", "content": system}] + messages,
            temperature=0.85,
            max_tokens=700,
        )
        reply = resp.choices[0].message.content.strip()
    except Exception as e:
        logger.warning(f"Alex chat error: {e}")
        reply = "Упс, что-то пошло не так 😅 Попробуй ещё раз!"

    await add_chat_message(user_id, "user", user_text)
    await add_chat_message(user_id, "assistant", reply)
    return reply

async def analyze_error_and_update_memory(
    user_id: int,
    lesson_type: str,
    task: str,
    user_answer: str,
    correct_answer: str,
) -> dict | None:
    """Разбирает ошибку через AI и сохраняет слабое место в память."""
    prompt = ALEX_ERROR_ANALYSIS.format(
        task=task,
        user_answer=user_answer,
        correct_answer=correct_answer,
        lesson_type=lesson_type,
    )
    system = "You are an English teacher. Respond only with valid JSON, no markdown."
    result = await generate_with_retry(prompt, system)
    if result and result.get("weak_spot"):
        key = f"weak:{lesson_type}:{result['weak_spot'][:40]}"
        await upsert_memory(user_id, key, result["weak_spot"])
    return result

async def mark_topic_done(user_id: int, lesson_type: str, topic: str):
    """Запоминает пройденную тему."""
    key = f"done:{lesson_type}:{topic[:40]}"
    await upsert_memory(user_id, key, f"{topic} ({lesson_type})")

LEARNING_STYLE_DESCRIPTIONS = {
    "visual":       "prefers reading texts and written explanations",
    "auditory":     "prefers listening, speaking aloud and repeating",
    "kinesthetic":  "prefers writing exercises and hands-on practice",
    "game":         "prefers games, quizzes and interactive formats",
}

SYSTEM_PROMPTS = {
    "casual": (
        "You are Alex, a friendly English-Russian teacher. "
        "Student: level {level}, areas: {focus_areas}, learning style: {learning_style}. "
        "Tailor your lesson format to the student's learning style. "
        "Rules: explanations in Russian, exercises in English. Keep lessons 5-10 min. Respond in JSON."
    ),
    "intensive": (
        "You are Coach, a demanding English coach. "
        "Student: level {level}, learning style: {learning_style}. "
        "Rules: strict, concise feedback in Russian, English exercises only. Respond in JSON."
    ),
    "ielts": (
        "You are an IELTS examiner. "
        "Student: level {level}, target band {ielts_target_band}, learning style: {learning_style}. "
        "Rules: feedback in Russian, tasks in English. Respond in JSON."
    ),
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

async def get_user_weak_spots(user_id: int) -> str:
    """Возвращает строку со слабыми местами для промпта."""
    memory = await get_user_memory(user_id)
    weak = [m["value"] for m in memory if m["key"].startswith("weak:")]
    return ", ".join(weak[-3:]) if weak else ""

async def generate_lesson(lesson_type: str, level: str, focus_areas: list,
                          total_lessons=0, streak=0, bot_mode="casual",
                          learning_style="visual", weak_spots="", **kwargs):
    style_desc = LEARNING_STYLE_DESCRIPTIONS.get(learning_style, learning_style)
    system = SYSTEM_PROMPTS.get(bot_mode, SYSTEM_PROMPTS["casual"]).format(
        level=level,
        focus_areas=", ".join(focus_areas),
        learning_style=style_desc,
        ielts_target_band=kwargs.get("ielts_target_band", "6.0"),
    )
    if weak_spots:
        system += f"\n\nStudent's WEAK SPOTS (prioritize these in the lesson if relevant): {weak_spots}"
    prompts = {
        "vocabulary": (
            f'Generate vocabulary lesson for {level}. '
            f'Return JSON: {{"word":"word","transcription":"IPA","translation":"ru","definition":"def",'
            f'"example_sentence":"example","quiz":{{"question":"What does it mean?",'
            f'"options":["translation","w1","w2","w3"],"correct_index":0}}}}'
        ),
        "grammar": (
            f'Generate grammar lesson for {level}. '
            f'Return JSON: {{"topic":"topic","explanation":"explanation in Russian",'
            f'"exercises":[{{"question":"q","options":["a","b","c","d"],"correct_index":1,"explanation":"why"}}]}} '
            f'Include 3 exercises.'
        ),
        "speaking": (
            f'Generate speaking practice for {level}. '
            f'Return JSON: {{"situation":"situation in Russian","task":"task in English","hint_words":["w1","w2"]}}'
        ),
        "reading": (
            f'Generate reading for {level}. '
            f'Return JSON: {{"text":"3-5 sentences","questions":[{{"question":"q","options":["a","b","c","d"],"correct_index":0}}],'
            f'"new_words":[{{"word":"w","translation":"t"}}]}}'
        ),
    }
    return await generate_with_retry(prompts.get(lesson_type, prompts["vocabulary"]), system)

async def evaluate_answer(user_answer: str, task: str, level: str) -> dict:
    prompt = (
        f"Evaluate: task='{task}', answer='{user_answer}'. "
        f"Return JSON: {{\"is_correct\":true/false,\"score\":1-10,"
        f"\"praise\":\"ru text\",\"tip\":\"ru tip if wrong\"}}"
    )
    return await generate_with_retry(
        prompt,
        SYSTEM_PROMPTS["casual"].format(level=level, focus_areas="", learning_style="")
    )

async def transcribe_audio(file_bytes: bytes, filename: str = "voice.ogg") -> str:
    try:
        resp = await openrouter.audio.transcriptions.create(
            file=(filename, file_bytes), model="openai/whisper-1",
            language="en", response_format="json"
        )
        return resp.text
    except: return None

async def analyze_image(image_base64: str, prompt: str, level: str) -> dict:
    system = SYSTEM_PROMPTS["casual"].format(level=level, focus_areas="", learning_style="")
    user_prompt = (
        f"{prompt}. Return JSON: {{\"detected_content\":\"ru\",\"english_lesson\":\"ru\","
        f"\"new_words\":[{{\"word\":\"w\",\"translation\":\"t\"}}],\"task\":\"exercise\"}}"
    )
    try:
        resp = await openrouter.chat.completions.create(
            model="google/gemini-2.5-flash",
            messages=[{
                "role": "system", "content": system
            }, {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}},
                ]
            }],
            response_format={"type": "json_object"}, max_tokens=1000
        )
        return json.loads(resp.choices[0].message.content)
    except: return None

# [NEW] Генерация персонального плана
async def generate_personal_plan(level: str, focus_areas: list, bot_mode: str, learning_style: str) -> dict:
    mode_names = {"casual": "дружелюбный", "intensive": "интенсивный", "ielts": "подготовка к IELTS"}
    style_names = {
        "visual":      "чтение текстов и объяснений",
        "auditory":    "прослушивание и повторение вслух",
        "kinesthetic": "письменные упражнения",
        "game":        "игры и квизы",
    }
    focus_ru = {
        "vocabulary": "словарный запас", "grammar": "грамматика",
        "speaking": "разговорная речь", "reading": "чтение",
    }
    focus_str = ", ".join(focus_ru.get(a, a) for a in focus_areas)
    prompt = (
        f"Create a short personalized English learning plan in Russian (2–3 paragraphs). "
        f"Student profile: current level={level}, wants to improve: {focus_str}, "
        f"mode={mode_names.get(bot_mode, bot_mode)}, preferred style={style_names.get(learning_style, learning_style)}. "
        f"Be specific: mention what topics, what exercises, how often. "
        f'Return JSON: {{"plan":"2-3 paragraphs in Russian","target_level":"next English level e.g. B2","duration":"e.g. 2–3 месяца"}}'
    )
    return await generate_with_retry(
        prompt,
        "You are an expert English language learning coach. Write only in Russian. Return only JSON."
    )

def determine_level(correct: int) -> str:
    return {0: "A1", 1: "A1", 2: "A2", 3: "B1", 4: "B2"}.get(correct, "C1")

# ============= ДАННЫЕ ТЕСТА =============
# [FIX] correct_index теперь реальные правильные ответы
LEVEL_TEST_QUESTIONS = [
    {
        "question": "He ___ to school yesterday.",
        "options": ["goes", "went", "going", "go"],
        "correct_index": 1,   # "went" — Past Simple
        "explanation": "Глагол в прошедшем времени (Past Simple): went.",
    },
    {
        "question": "Как правильно сказать «Я люблю читать»?",
        "options": ["I love read", "I love reading", "I loves reading", "I loving read"],
        "correct_index": 1,   # "I love reading" — Gerund after love
        "explanation": "После глагола love используется герундий (reading).",
    },
    {
        "question": "The weather is ___ today.",
        "options": ["good", "well", "better", "best"],
        "correct_index": 0,   # "good" — adjective after linking verb
        "explanation": "После глагола-связки is нужно прилагательное good, не наречие well.",
    },
    {
        "question": "Which sentence is grammatically correct?",
        "options": [
            "Although tired, but he continued",
            "Although he was tired, he continued",
            "He was tired although continued",
            "Although but he was tired he continued",
        ],
        "correct_index": 1,   # стандартное although + clause
        "explanation": "Союз although не используется вместе с but в одном предложении.",
    },
    {
        "question": "'Would you mind opening the window?' — лучший ответ:",
        "options": ["Yes, please", "No, not at all", "I'm not minding", "That's right"],
        "correct_index": 1,   # "No, not at all" — соглашение на просьбу
        "explanation": "'No, not at all' = «конечно, без проблем». 'Yes' здесь означало бы отказ.",
    },
]

# ============= КЛАВИАТУРЫ =============

# --- Онбординг ---

def get_name_keyboard(tg_name: str) -> InlineKeyboardMarkup:
    """Кнопка быстрого использования имени из Telegram."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"👤 Использовать «{tg_name}»", callback_data="use_tg_name")],
    ])

def get_level_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 Начинающий (A1–A2)", callback_data="level_beginner")],
        [InlineKeyboardButton(text="🔵 Средний (B1–B2)",    callback_data="level_intermediate")],
        [InlineKeyboardButton(text="🟣 Продвинутый (C1–C2)", callback_data="level_advanced")],
        [InlineKeyboardButton(text="❓ Не знаю — пройти тест", callback_data="level_dont_know")],
        [InlineKeyboardButton(text="🔙 Назад",              callback_data="back_to_name")],
    ])

def get_focus_areas_keyboard(selected: list) -> InlineKeyboardMarkup:
    areas = [
        ("vocabulary", "📝 Словарный запас"),
        ("grammar",    "📖 Грамматика"),
        ("speaking",   "🗣 Разговорная речь"),
        ("reading",    "📚 Чтение"),
    ]
    rows = [
        [InlineKeyboardButton(
            text=f"{'✅' if a in selected else '   '} {n}",
            callback_data=f"area_{a}"
        )]
        for a, n in areas
    ]
    rows.append([InlineKeyboardButton(text="Готово ➡️", callback_data="areas_done")])
    rows.append([InlineKeyboardButton(text="🔙 Назад",  callback_data="back_to_level")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def get_learning_style_keyboard() -> InlineKeyboardMarkup:  # [NEW]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👁 Читать тексты и объяснения",  callback_data="style_visual")],
        [InlineKeyboardButton(text="👂 Слушать и повторять вслух",   callback_data="style_auditory")],
        [InlineKeyboardButton(text="✍️ Писать и делать упражнения",  callback_data="style_kinesthetic")],
        [InlineKeyboardButton(text="🎮 Через игры и квизы",          callback_data="style_game")],
        [InlineKeyboardButton(text="🔙 Назад",                       callback_data="back_to_focus_areas")],
    ])

def get_bot_mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👋 Дружелюбный",  callback_data="mode_casual")],
        [InlineKeyboardButton(text="💪 Интенсивный",  callback_data="mode_intensive")],
        [InlineKeyboardButton(text="📋 IELTS",        callback_data="mode_ielts")],
        [InlineKeyboardButton(text="🔙 Назад",        callback_data="back_to_learning_style")],
    ])

def get_timezone_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇷🇺 Москва (UTC+3)",         callback_data="tz_moscow")],
        [InlineKeyboardButton(text="🇷🇺 Екатеринбург (UTC+5)",   callback_data="tz_ekb")],
        [InlineKeyboardButton(text="🇺🇿 Ташкент (UTC+5)",        callback_data="tz_tashkent")],
        [InlineKeyboardButton(text="🌍 Другой",                  callback_data="tz_other")],
        [InlineKeyboardButton(text="🔙 Назад",                   callback_data="back_to_notify_time")],
    ])

def get_plan_confirm_keyboard() -> InlineKeyboardMarkup:  # [NEW]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, поехали!",        callback_data="plan_confirm")],
        [InlineKeyboardButton(text="✏️ Изменить настройки", callback_data="plan_change")],
    ])

# --- Уроки ---

def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📚 Урок",        callback_data="start_lesson"),
         InlineKeyboardButton(text="📊 Статистика",  callback_data="show_stats")],
        [InlineKeyboardButton(text="⏰ Расписание",  callback_data="show_schedule"),
         InlineKeyboardButton(text="💳 Подписка",    callback_data="show_subscription")],
    ])

def get_lesson_type_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Словарный",   callback_data="lesson_vocabulary")],
        [InlineKeyboardButton(text="📖 Грамматика",  callback_data="lesson_grammar")],
        [InlineKeyboardButton(text="🗣 Разговорная", callback_data="lesson_speaking")],
        [InlineKeyboardButton(text="📚 Чтение",      callback_data="lesson_reading")],
        [InlineKeyboardButton(text="🔙 В меню",      callback_data="back_to_menu")],
    ])

def get_lesson_answer_keyboard(options: list) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=opt, callback_data=f"answer_{i}")]
            for i, opt in enumerate(options)
        ]
    )

def get_lesson_continue_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➡️ Следующий урок", callback_data="lesson_continue")],
        [InlineKeyboardButton(text="🏠 В меню",         callback_data="back_to_menu")],
    ])

def get_quiz_keyboard(options: list, show_back: bool = False) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=opt, callback_data=f"quiz_{i}")] for i, opt in enumerate(options)]
    if show_back:
        rows.append([InlineKeyboardButton(text="🔙 Предыдущий вопрос", callback_data="quiz_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def get_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ])

def get_onboarding_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📚 Начать первый урок", callback_data="start_lesson")]
    ])

# ============= FSM =============
class OnboardingStates(StatesGroup):
    waiting_name           = State()   # [NEW] шаг 1
    waiting_level_choice   = State()   # шаг 2
    waiting_test_answer    = State()   # шаг 2b (тест)
    waiting_focus_areas    = State()   # шаг 3
    waiting_learning_style = State()   # [NEW] шаг 4
    waiting_bot_mode       = State()   # шаг 5
    waiting_notify_time    = State()   # шаг 6
    waiting_timezone       = State()   # шаг 7
    waiting_plan_confirm   = State()   # [NEW] шаг 8

class LessonStates(StatesGroup):
    in_lesson      = State()
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

# ============================================================
#  /start
# ============================================================
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    user, created = await get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name,
        message.from_user.language_code,
    )

    # Уже зарегистрирован — показываем меню
    if not created and user.level and user.focus_areas and user.bot_mode:
        status = "🟢 Активна" if user.has_active_access() else "🔴 Истекла"
        name = user.get_display_name()
        await message.answer(
            f"👋 Привет, {name}!\n\n"
            f"📊 Уровень: {user.level}\n"
            f"⏰ Расписание: {', '.join(user.notify_times)}\n"
            f"💳 Подписка: {status}\n\n"
            f"Что хочешь сделать?",
            reply_markup=get_main_menu_keyboard()
        )
        return

    # Новый пользователь — онбординг: шаг 1, имя
    await state.set_state(OnboardingStates.waiting_name)
    tg_name = message.from_user.full_name or message.from_user.username or ""
    await message.answer(
        "👋 Привет! Я Алекс — твой персональный учитель английского!\n\n"
        "Давай познакомимся — как тебя зовут?",
        reply_markup=get_name_keyboard(tg_name) if tg_name else None,
    )

# ============================================================
#  ШАГ 1: Имя
# ============================================================
@router.message(StateFilter(OnboardingStates.waiting_name))
async def handle_name_input(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name or len(name) > 50:
        await message.answer("Имя должно быть не длиннее 50 символов. Попробуй ещё раз 👇")
        return
    await state.update_data(display_name=name)
    await state.set_state(OnboardingStates.waiting_level_choice)
    await message.answer(
        f"Приятно познакомиться, {name}! 🙌\n\n<b>Шаг 2 из 7.</b> Какой у тебя уровень английского?",
        parse_mode="HTML",
        reply_markup=get_level_keyboard(),
    )

@router.callback_query(F.data == "use_tg_name", StateFilter(OnboardingStates.waiting_name))
async def use_tg_name(callback: CallbackQuery, state: FSMContext):
    name = callback.from_user.full_name or callback.from_user.username or "друг"
    await state.update_data(display_name=name)
    await callback.answer()
    await state.set_state(OnboardingStates.waiting_level_choice)
    await callback.message.edit_text(
        f"Приятно познакомиться, {name}! 🙌\n\n<b>Шаг 2 из 7.</b> Какой у тебя уровень английского?",
        parse_mode="HTML",
        reply_markup=get_level_keyboard(),
    )

# ============================================================
#  ШАГ 2: Уровень
# ============================================================
@router.callback_query(F.data.startswith("level_"), StateFilter(OnboardingStates.waiting_level_choice))
async def onboarding_level(callback: CallbackQuery, state: FSMContext):
    level_map = {
        "level_beginner":     "A1",
        "level_intermediate": "B1",
        "level_advanced":     "C1",
    }
    if callback.data == "level_dont_know":
        await callback.answer()
        await state.set_state(OnboardingStates.waiting_test_answer)
        await state.update_data(test_correct=0, test_question_num=0, test_answers=[])
        await callback.message.edit_text("Не проблема! Пройдём короткий тест — 5 вопросов 🎯")
        await send_test_question(callback.message, state, 0)
        return
    if callback.data in level_map:
        await state.update_data(selected_level=level_map[callback.data])
        await callback.answer()
        await state.set_state(OnboardingStates.waiting_focus_areas)
        await callback.message.edit_text(
            "Отлично! Уровень выбран ✅\n\n<b>Шаг 3 из 7.</b> Что хочешь прокачать? (можно несколько)",
            parse_mode="HTML",
            reply_markup=get_focus_areas_keyboard([]),
        )

# ============================================================
#  ШАГ 2b: Тест на уровень
# ============================================================
async def send_test_question(message: Message, state: FSMContext, num: int):
    q = LEVEL_TEST_QUESTIONS[num]
    await state.update_data(test_question_num=num)
    kb = get_quiz_keyboard(q["options"], show_back=(num > 0))
    await message.answer(
        f"❓ <b>Вопрос {num + 1}/5</b>\n\n{q['question']}",
        parse_mode="HTML",
        reply_markup=kb,
    )

@router.callback_query(F.data.startswith("quiz_"), StateFilter(OnboardingStates.waiting_test_answer))
async def test_answer(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    num = data.get("test_question_num", 0)

    # [NEW] Кнопка назад в тесте
    if callback.data == "quiz_back":
        if num > 0:
            await callback.answer()
            await send_test_question(callback.message, state, num - 1)
        return

    answer_idx = int(callback.data.replace("quiz_", ""))
    q = LEVEL_TEST_QUESTIONS[num]
    is_correct = answer_idx == q["correct_index"]

    # Копим ответы для подробного итога
    answers = data.get("test_answers", [])
    answers = answers[:num]  # обрезаем если шли назад
    answers.append({"is_correct": is_correct, "explanation": q["explanation"] if not is_correct else None})
    correct_total = sum(1 for a in answers if a["is_correct"])

    await callback.answer("✅ Правильно!" if is_correct else "❌ Неверно")
    await state.update_data(test_answers=answers)

    next_num = num + 1
    if next_num < 5:
        await state.update_data(test_question_num=next_num)
        await send_test_question(callback.message, state, next_num)
    else:
        # Финал теста — подробный результат
        level = determine_level(correct_total)
        level_names = {
            "A1": "🟢 Начинающий",
            "A2": "🟡 Элементарный",
            "B1": "🔵 Средний",
            "B2": "🟣 Выше среднего",
            "C1": "🏆 Продвинутый",
        }
        await state.update_data(selected_level=level)

        # Список ошибок
        errors = [
            f"• {LEVEL_TEST_QUESTIONS[i]['question']} → {answers[i]['explanation']}"
            for i in range(5)
            if not answers[i]["is_correct"] and answers[i].get("explanation")
        ]
        error_text = "\n" + "\n".join(errors) if errors else ""

        await callback.message.answer(
            f"🎉 <b>Тест завершён!</b>\n\n"
            f"Правильных ответов: <b>{correct_total}/5</b>\n"
            f"Твой уровень: <b>{level_names.get(level, level)} ({level})</b>\n"
            f"{error_text}\n\n"
            f"<b>Шаг 3 из 7.</b> Что хочешь прокачать?",
            parse_mode="HTML",
            reply_markup=get_focus_areas_keyboard([]),
        )
        await state.set_state(OnboardingStates.waiting_focus_areas)

# ============================================================
#  ШАГ 3: Зоны фокуса
# ============================================================
@router.callback_query(F.data.startswith("area_"), StateFilter(OnboardingStates.waiting_focus_areas))
async def toggle_area(callback: CallbackQuery, state: FSMContext):
    area = callback.data.replace("area_", "")
    data = await state.get_data()
    selected = data.get("selected_areas", [])
    if area in selected:
        selected.remove(area)
    else:
        selected.append(area)
    await state.update_data(selected_areas=selected)
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=get_focus_areas_keyboard(selected))

@router.callback_query(F.data == "areas_done", StateFilter(OnboardingStates.waiting_focus_areas))
async def areas_done(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = data.get("selected_areas", [])
    if not selected:
        await callback.answer("Выбери хотя бы одну зону!", show_alert=True)
        return
    await state.update_data(selected_areas=selected)
    await callback.answer()
    await state.set_state(OnboardingStates.waiting_learning_style)
    await callback.message.edit_text(
        "<b>Шаг 4 из 7.</b> Как тебе легче учить новое? 🧠",
        parse_mode="HTML",
        reply_markup=get_learning_style_keyboard(),
    )

# ============================================================
#  ШАГ 4: Стиль восприятия [NEW]
# ============================================================
STYLE_LABELS = {
    "style_visual":      "visual",
    "style_auditory":    "auditory",
    "style_kinesthetic": "kinesthetic",
    "style_game":        "game",
}
STYLE_NAMES_RU = {
    "visual":      "👁 Тексты и объяснения",
    "auditory":    "👂 Слушать и повторять",
    "kinesthetic": "✍️ Упражнения и письмо",
    "game":        "🎮 Игры и квизы",
}

@router.callback_query(F.data.startswith("style_"), StateFilter(OnboardingStates.waiting_learning_style))
async def choose_learning_style(callback: CallbackQuery, state: FSMContext):
    style = STYLE_LABELS.get(callback.data, "visual")
    await state.update_data(learning_style=style)
    await callback.answer()
    await state.set_state(OnboardingStates.waiting_bot_mode)
    await callback.message.edit_text(
        f"Стиль: <b>{STYLE_NAMES_RU[style]}</b> ✅\n\n"
        f"<b>Шаг 5 из 7.</b> Выбери режим обучения:",
        parse_mode="HTML",
        reply_markup=get_bot_mode_keyboard(),
    )

# ============================================================
#  ШАГ 5: Режим обучения
# ============================================================
@router.callback_query(F.data.startswith("mode_"), StateFilter(OnboardingStates.waiting_bot_mode))
async def choose_mode(callback: CallbackQuery, state: FSMContext):
    mode = callback.data.replace("mode_", "")
    await state.update_data(bot_mode=mode)
    await callback.answer()
    await state.set_state(OnboardingStates.waiting_notify_time)
    await callback.message.edit_text(
        "<b>Шаг 6 из 7.</b> В какое время присылать напоминания?\n\n"
        "Введи в формате ЧЧ:ММ, например: <code>09:00</code>\n"
        "Можно несколько — каждое с новой строки",
        parse_mode="HTML",
    )

# ============================================================
#  ШАГ 6: Время напоминаний
# ============================================================
@router.message(StateFilter(OnboardingStates.waiting_notify_time))
async def set_notify_time(message: Message, state: FSMContext):
    times = [
        l.strip() for l in message.text.strip().split("\n")
        if re.match(r"^([01]\d|2[0-3]):([0-5]\d)$", l.strip())
    ]
    if not times:
        await message.answer("❌ Неверный формат. Введи время в формате ЧЧ:ММ, например: <code>09:00</code>", parse_mode="HTML")
        return
    await state.update_data(notify_times=times)
    await state.set_state(OnboardingStates.waiting_timezone)
    await message.answer(
        f"⏰ Напоминания: <b>{', '.join(times)}</b>\n\n"
        f"<b>Шаг 7 из 7.</b> Выбери часовой пояс:",
        parse_mode="HTML",
        reply_markup=get_timezone_keyboard(),
    )

# ============================================================
#  ШАГ 7: Часовой пояс
# ============================================================
@router.callback_query(F.data.startswith("tz_"), StateFilter(OnboardingStates.waiting_timezone))
async def choose_tz(callback: CallbackQuery, state: FSMContext):
    tz_map = {
        "tz_moscow":   "Europe/Moscow",
        "tz_ekb":      "Asia/Yekaterinburg",
        "tz_tashkent": "Asia/Tashkent",
    }
    if callback.data == "tz_other":
        await callback.answer()
        await callback.message.edit_text("Введи UTC-смещение (например: <code>+4</code> или <code>-5</code>):", parse_mode="HTML")
        return
    if callback.data in tz_map:
        await state.update_data(timezone=tz_map[callback.data])
        await callback.answer()
        await show_plan_step(callback.message, state, callback.from_user.id)

@router.message(StateFilter(OnboardingStates.waiting_timezone))
async def custom_tz(message: Message, state: FSMContext):
    tz = message.text.strip()
    if re.match(r"^[+-]?\d{1,2}$", tz):
        if not tz.startswith(("+", "-")):
            tz = "+" + tz
        await state.update_data(timezone=f"UTC{tz}")
        await show_plan_step(message, state, message.from_user.id)
    else:
        await message.answer("❌ Неверный формат. Введи число, например: <code>+3</code>", parse_mode="HTML")

# ============================================================
#  ШАГ 8: Персональный план [NEW]
# ============================================================
async def show_plan_step(message: Message, state: FSMContext, user_id: int):
    """Сохраняем данные → генерируем план → показываем подтверждение."""
    data = await state.get_data()

    display_name   = data.get("display_name", "")
    level          = data.get("selected_level", "B1")
    focus_areas    = data.get("selected_areas", ["vocabulary"])
    bot_mode       = data.get("bot_mode", "casual")
    notify_times   = data.get("notify_times", ["09:00"])
    timezone       = data.get("timezone", "UTC")
    learning_style = data.get("learning_style", "visual")

    # Сохраняем всё в базу
    await update_user(
        user_id,
        display_name=display_name,
        level=level,
        focus_areas=focus_areas,
        bot_mode=bot_mode,
        notify_times=notify_times,
        timezone=timezone,
        learning_style=learning_style,
    )

    await state.set_state(OnboardingStates.waiting_plan_confirm)

    # Сообщаем что генерируем
    wait_msg = await message.answer("⏳ Генерирую твой персональный план...")

    plan = await generate_personal_plan(level, focus_areas, bot_mode, learning_style)

    name = display_name or "друг"
    if plan:
        plan_text = plan.get("plan", "")
        target    = plan.get("target_level", "")
        duration  = plan.get("duration", "")
        caption = (
            f"🎯 <b>Персональный план для {name}</b>\n\n"
            f"{plan_text}\n\n"
            f"📈 Цель: <b>{target}</b>   ⏱ Срок: <b>{duration}</b>"
        )
    else:
        caption = (
            f"🎯 <b>Всё готово, {name}!</b>\n\n"
            f"Начнём с уровня <b>{level}</b> и будем прокачивать то, что тебе нужно.\n"
            f"Уроки подобраны под твой стиль обучения."
        )

    try:
        await wait_msg.delete()
    except Exception:
        pass

    await message.answer(
        caption + "\n\n<i>Этот план тебе подходит?</i>",
        parse_mode="HTML",
        reply_markup=get_plan_confirm_keyboard(),
    )

@router.callback_query(F.data == "plan_confirm", StateFilter(OnboardingStates.waiting_plan_confirm))
async def plan_confirmed(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    user = await get_user(callback.from_user.id)
    name = user.get_display_name() if user else "друг"
    await callback.message.edit_text(
        f"🚀 <b>Поехали, {name}!</b>\n\n"
        f"Пробный период активирован — <b>30 дней бесплатно</b>.\n\n"
        f"Выбери, с чего начнём:",
        parse_mode="HTML",
        reply_markup=get_main_menu_keyboard(),
    )

@router.callback_query(F.data == "plan_change", StateFilter(OnboardingStates.waiting_plan_confirm))
async def plan_change(callback: CallbackQuery, state: FSMContext):
    """Пересматриваем настройки — возвращаем на первый шаг онбординга."""
    await callback.answer()
    await state.clear()
    tg_name = callback.from_user.full_name or callback.from_user.username or ""
    await state.set_state(OnboardingStates.waiting_name)
    await callback.message.edit_text(
        "Хорошо, настроим заново! 👋\n\nКак тебя зовут?",
        reply_markup=get_name_keyboard(tg_name) if tg_name else None,
    )

# ============================================================
#  Кнопки «Назад» по онбордингу
# ============================================================
ONBOARDING_STATES = (
    OnboardingStates.waiting_name,
    OnboardingStates.waiting_level_choice,
    OnboardingStates.waiting_test_answer,
    OnboardingStates.waiting_focus_areas,
    OnboardingStates.waiting_learning_style,
    OnboardingStates.waiting_bot_mode,
    OnboardingStates.waiting_notify_time,
    OnboardingStates.waiting_timezone,
    OnboardingStates.waiting_plan_confirm,
)

@router.callback_query(F.data == "back_to_name", StateFilter(*ONBOARDING_STATES))
async def back_to_name(callback: CallbackQuery, state: FSMContext):
    tg_name = callback.from_user.full_name or callback.from_user.username or ""
    await state.set_state(OnboardingStates.waiting_name)
    await callback.answer()
    await callback.message.edit_text(
        "Как тебя зовут? 👋",
        reply_markup=get_name_keyboard(tg_name) if tg_name else None,
    )

@router.callback_query(F.data == "back_to_level", StateFilter(*ONBOARDING_STATES))
async def back_to_level(callback: CallbackQuery, state: FSMContext):
    await state.set_state(OnboardingStates.waiting_level_choice)
    await callback.answer()
    data = await state.get_data()
    name = data.get("display_name", "")
    await callback.message.edit_text(
        f"{'Привет, ' + name + '! ' if name else ''}<b>Шаг 2 из 7.</b> Какой у тебя уровень английского?",
        parse_mode="HTML",
        reply_markup=get_level_keyboard(),
    )

@router.callback_query(F.data == "back_to_focus_areas", StateFilter(*ONBOARDING_STATES))
async def back_to_focus_areas(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = data.get("selected_areas", [])
    await state.set_state(OnboardingStates.waiting_focus_areas)
    await callback.answer()
    await callback.message.edit_text(
        "<b>Шаг 3 из 7.</b> Что хочешь прокачать? (можно несколько)",
        parse_mode="HTML",
        reply_markup=get_focus_areas_keyboard(selected),
    )

@router.callback_query(F.data == "back_to_learning_style", StateFilter(*ONBOARDING_STATES))
async def back_to_learning_style_cb(callback: CallbackQuery, state: FSMContext):
    await state.set_state(OnboardingStates.waiting_learning_style)
    await callback.answer()
    await callback.message.edit_text(
        "<b>Шаг 4 из 7.</b> Как тебе легче учить новое? 🧠",
        parse_mode="HTML",
        reply_markup=get_learning_style_keyboard(),
    )

@router.callback_query(F.data == "back_to_mode", StateFilter(*ONBOARDING_STATES))
async def back_to_mode(callback: CallbackQuery, state: FSMContext):
    await state.set_state(OnboardingStates.waiting_bot_mode)
    await callback.answer()
    await callback.message.edit_text(
        "<b>Шаг 5 из 7.</b> Выбери режим обучения:",
        parse_mode="HTML",
        reply_markup=get_bot_mode_keyboard(),
    )

@router.callback_query(F.data == "back_to_notify_time", StateFilter(*ONBOARDING_STATES))
async def back_to_notify_time(callback: CallbackQuery, state: FSMContext):
    await state.set_state(OnboardingStates.waiting_notify_time)
    await callback.answer()
    await callback.message.edit_text(
        "<b>Шаг 6 из 7.</b> В какое время присылать напоминания?\n\n"
        "Введи в формате ЧЧ:ММ, например: <code>09:00</code>",
        parse_mode="HTML",
    )

# ============================================================
#  Уроки
# ============================================================
@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "📚 <b>Что я умею:</b>\n\n"
        "/start — начать / главное меню\n"
        "/lesson — урок\n"
        "/stats — статистика\n"
        "/schedule — расписание\n"
        "/help — эта справка",
        parse_mode="HTML",
    )

@router.message(Command("lesson"))
async def cmd_lesson(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала /start")
        return
    if not user.has_active_access():
        await message.answer("⏰ Требуется активная подписка")
        return
    await state.set_state(LessonStates.in_lesson)
    await message.answer("Какой тип урока?", reply_markup=get_lesson_type_keyboard())

@router.callback_query(F.data == "start_lesson")
async def start_lesson(callback: CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
    if not user or not user.has_active_access():
        await callback.answer("⏰ Подписка требуется!", show_alert=True)
        return
    await state.set_state(LessonStates.in_lesson)
    await callback.message.edit_text("Какой тип урока?", reply_markup=get_lesson_type_keyboard())

@router.callback_query(F.data.startswith("lesson_"), StateFilter(LessonStates.in_lesson))
async def handle_lesson_type(callback: CallbackQuery, state: FSMContext):
    lesson_type = callback.data.replace("lesson_", "")
    user = await get_user(callback.from_user.id)
    if not user:
        await callback.answer("Сначала /start", show_alert=True)
        return
    await callback.answer("Генерирую... ⏳")
    content = await generate_lesson(
        lesson_type, user.level, user.focus_areas,
        user.total_lessons, user.streak, user.bot_mode,
        user.learning_style or "visual",
        weak_spots=await get_user_weak_spots(callback.from_user.id),
    )
    if not content:
        await callback.message.answer("😔 Ошибка генерации. Попробуй ещё раз.", reply_markup=get_lesson_type_keyboard())
        await state.clear()
        return
    lesson = await create_lesson(callback.from_user.id, lesson_type, content)
    await state.update_data(lesson_id=str(lesson.id), lesson_type=lesson_type, content=content)
    await state.set_state(LessonStates.waiting_answer)

    if lesson_type == "vocabulary":
        word = content.get("word", "")
        trans = content.get("transcription", "")
        translation = content.get("translation", "")
        definition = content.get("definition", "")
        example = content.get("example_sentence", "")
        await callback.message.answer(
            f"📝 <b>Словарный урок</b>\n\n"
            f"Word: <b>{word}</b> {trans}\n"
            f"Translation: {translation}\n\n"
            f"<i>Definition:</i> {definition}\n\n"
            f"<i>Example:</i> {example}",
            parse_mode="HTML",
        )
        quiz = content.get("quiz", {})
        if quiz:
            await state.update_data(quiz_correct_index=quiz.get("correct_index", 0))
            await callback.message.answer(
                f"❓ {quiz.get('question', 'What does it mean?')}",
                reply_markup=get_lesson_answer_keyboard(quiz.get("options", [])),
            )

    elif lesson_type == "grammar":
        topic = content.get("topic", "")
        explanation = content.get("explanation", "")
        example = content.get("example_correct", "")
        await callback.message.answer(
            f"📖 <b>Грамматика: {topic}</b>\n\n{explanation}\n\n<i>Example:</i> {example}",
            parse_mode="HTML",
        )
        exercises = content.get("exercises", [])
        if exercises:
            await state.update_data(current_exercise=0, exercises=exercises)
            ex = exercises[0]
            await callback.message.answer(
                f"❓ {ex.get('question', '')}",
                reply_markup=get_lesson_answer_keyboard(ex.get("options", [])),
            )

    elif lesson_type == "speaking":
        situation = content.get("situation", "")
        task = content.get("task", "")
        hints = content.get("hint_words", [])
        await callback.message.answer(
            f"🗣 <b>Разговорная речь</b>\n\n"
            f"Situation: {situation}\n\n"
            f"<b>Task:</b> {task}\n\n"
            f"Hint: {', '.join(hints)}",
            parse_mode="HTML",
        )
        await callback.message.answer("Напиши свой ответ на английском:")

    elif lesson_type == "reading":
        text = content.get("text", "")
        await callback.message.answer(
            f"📚 <b>Чтение</b>\n\n{text}\n\nAnswer the questions:",
            parse_mode="HTML",
        )
        questions = content.get("questions", [])
        if questions:
            await state.update_data(current_question=0, questions=questions)
            q = questions[0]
            await callback.message.answer(
                f"❓ {q.get('question', '')}",
                reply_markup=get_lesson_answer_keyboard(q.get("options", [])),
            )

@router.callback_query(F.data.startswith("answer_"), StateFilter(LessonStates.waiting_answer))
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
        feedback = (
            f"✅ Правильно! {ex.get('explanation', '')}"
            if is_correct else
            f"❌ Неправильно. {ex.get('explanation', '')}"
        )
        next_ex = current + 1
        if next_ex < len(exercises):
            await state.update_data(current_exercise=next_ex)
            ex = exercises[next_ex]
            await callback.answer(feedback)
            await callback.message.answer(
                f"❓ {ex.get('question', '')}",
                reply_markup=get_lesson_answer_keyboard(ex.get("options", [])),
            )
            return

    elif lesson_type == "reading":
        questions = data.get("questions", [])
        current = data.get("current_question", 0)
        q = questions[current] if current < len(questions) else {}
        correct_idx = q.get("correct_index", 0)
        is_correct = answer_idx == correct_idx
        feedback = "✅ Правильно!" if is_correct else "❌ Неправильно"
        next_q = current + 1
        if next_q < len(questions):
            await state.update_data(current_question=next_q)
            q = questions[next_q]
            await callback.answer(feedback)
            await callback.message.answer(
                f"❓ {q.get('question', '')}",
                reply_markup=get_lesson_answer_keyboard(q.get("options", [])),
            )
            return

    await callback.answer(feedback)
    await update_lesson(lesson_id, completed_at=datetime.utcnow(), is_correct=is_correct, user_answer=str(answer_idx))
    await update_user_after_lesson(callback.from_user.id, lesson_type, is_perfect=is_correct)

    # Записываем пройденную тему
    topic = content.get("word") or content.get("topic") or lesson_type
    await mark_topic_done(callback.from_user.id, lesson_type, str(topic))

    if not is_correct:
        # Разбор ошибки через AI
        correct_text = ""
        task_text = ""
        if lesson_type == "vocabulary":
            correct_text = content.get("quiz", {}).get("options", [""])[content.get("quiz", {}).get("correct_index", 0)]
            task_text = content.get("quiz", {}).get("question", "")
        elif lesson_type in ("grammar", "reading"):
            items = data.get("exercises") or data.get("questions") or []
            cur = data.get("current_exercise") or data.get("current_question") or 0
            item = items[cur] if cur < len(items) else {}
            correct_text = item.get("options", [""])[item.get("correct_index", 0)]
            task_text = item.get("question", "")
        if task_text:
            error_analysis = await analyze_error_and_update_memory(
                callback.from_user.id, lesson_type,
                task_text, str(answer_idx), correct_text
            )
            if error_analysis:
                expl = error_analysis.get("explanation", "")
                ex_c = error_analysis.get("example_correct", "")
                ex_w = error_analysis.get("example_wrong", "")
                enc  = error_analysis.get("encouragement", "")
                analysis_text = f"📖 <b>Разбор ошибки:</b>\n{expl}"
                if ex_c:
                    analysis_text += f"\n\n✅ Правильно: <i>{ex_c}</i>"
                if ex_w:
                    analysis_text += f"\n❌ Ошибка: <i>{ex_w}</i>"
                if enc:
                    analysis_text += f"\n\n💬 {enc}"
                await callback.message.answer(analysis_text, parse_mode="HTML")

    await callback.message.answer("🎉 Урок завершён!", reply_markup=get_lesson_continue_keyboard())

@router.message(StateFilter(LessonStates.waiting_answer))
async def handle_free_answer(message: Message, state: FSMContext):
    data = await state.get_data()
    lesson_type = data.get("lesson_type", "")
    lesson_id_str = data.get("lesson_id", "")
    content = data.get("content", {})
    task = content.get("task", "")
    user = await get_user(message.from_user.id)
    result = await evaluate_answer(message.text, task, user.level if user else "B1")
    if result:
        await message.answer(f"📝 {result.get('praise', 'Хорошо!')}")
        if result.get("tip"):
            await message.answer(f"💡 {result['tip']}")
    else:
        await message.answer("📝 Спасибо за ответ!")
    # Сохраняем урок и обновляем статистику
    if lesson_id_str:
        try:
            is_perfect = bool(result and result.get("score", 0) >= 7)
            await update_lesson(
                UUID(lesson_id_str),
                completed_at=datetime.utcnow(),
                is_correct=is_perfect,
                user_answer=message.text[:500],
            )
        except Exception:
            is_perfect = False
    else:
        is_perfect = False
    await update_user_after_lesson(message.from_user.id, lesson_type, is_perfect=is_perfect)
    # Запоминаем пройденную тему
    topic = content.get("task", content.get("situation", lesson_type))
    await mark_topic_done(message.from_user.id, lesson_type, str(topic)[:40])
    await message.answer("Продолжим?", reply_markup=get_lesson_continue_keyboard())

@router.callback_query(F.data == "lesson_continue", StateFilter(LessonStates.waiting_answer))
async def continue_lesson(callback: CallbackQuery, state: FSMContext):
    await state.set_state(LessonStates.in_lesson)
    await callback.message.edit_text("Какой тип урока?", reply_markup=get_lesson_type_keyboard())

@router.callback_query(F.data == "lesson_end")
async def end_lesson(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("✅ Хорошая работа! Возвращайся скорее 📚")

async def update_user_after_lesson(user_id: int, lesson_type: str, is_perfect: bool = False):
    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        if not user: return
        today = date.today()
        # Streak: считаем по дням, не по урокам
        if user.last_lesson_date is None:
            user.streak = 1
        elif user.last_lesson_date == today:
            pass  # Уже занимался сегодня — streak не меняем
        elif user.last_lesson_date == today - timedelta(days=1):
            user.streak += 1  # Вчера занимался — продолжаем серию
        else:
            user.streak = 1  # Пропустил день — сбрасываем
        user.last_lesson_date = today
        user.total_lessons += 1
        user.xp_total += 10
        if user.streak > user.longest_streak:
            user.longest_streak = user.streak
        if is_perfect:
            user.lessons_perfect += 1
        if lesson_type == "ielts":
            user.ielts_lessons += 1
        # XP уровни
        new_level = 1
        for i, xp in enumerate([0, 100, 300, 700, 1500, 3000, 6000]):
            if user.xp_total < xp: break
            new_level = i + 1
        user.xp_level = new_level
        await db.commit()
        # Бейджи
        checks = [
            ("first_lesson", user.total_lessons >= 1),
            ("streak_3",     user.streak >= 3),
            ("streak_7",     user.streak >= 7),
            ("streak_30",    user.streak >= 30),
            ("lessons_10",   user.total_lessons >= 10),
            ("lessons_50",   user.total_lessons >= 50),
            ("lessons_100",  user.total_lessons >= 100),
            ("perfect_5",    user.lessons_perfect >= 5),
            ("ielts_first",  user.ielts_lessons >= 1),
            ("level_up_5",   user.xp_level >= 5),
        ]
        for badge_id, cond in checks:
            if cond and not await has_badge(user_id, badge_id):
                await award_badge(user_id, badge_id)

# ============================================================
#  Голос / Фото
# ============================================================
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
    except:
        await message.answer("😔 Ошибка")

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
            if result.get("task"):
                text += f"\n\n❓ {result['task']}"
            if result.get("new_words"):
                text += "\n\n📝 " + "\n".join([f"• {w['word']} — {w['translation']}" for w in result["new_words"]])
            await message.answer(text)
        else:
            await message.answer("😔 Не удалось проанализировать")
    except:
        await message.answer("😔 Ошибка")

# ============================================================
#  Свободный диалог с Алексом (вне уроков и онбординга)
# ============================================================
@router.message(F.text, StateFilter(None))
async def handle_free_chat(message: Message):
    """Любое текстовое сообщение вне FSM → живой диалог с Алексом."""
    if message.text.startswith("/"):
        return  # команды обрабатываются отдельно
    user = await get_user(message.from_user.id)
    if not user:
        await message.answer("Привет! Напиши /start чтобы начать 😊")
        return
    if not user.has_active_access():
        await message.answer(
            "⏳ Твой пробный период закончился.\n\n"
            "Оформи подписку чтобы продолжить учёбу с Алексом 👇",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💳 Подписка", callback_data="show_subscription")]
            ])
        )
        return
    await message.bot.send_chat_action(message.chat.id, "typing")
    reply = await chat_with_alex(message.from_user.id, message.text)
    await message.answer(reply, parse_mode="HTML")


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    user = await get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала /start")
        return
    badges = await get_user_badges(message.from_user.id)
    await message.answer(
        f"📊 <b>Твоя статистика</b>\n\n"
        f"{user.get_level_name()} | Уровень {user.xp_level}\n"
        f"⭐ XP: {user.xp_total} / {user.get_xp_for_next_level()}\n\n"
        f"📚 Уроков: {user.total_lessons}\n"
        f"🔥 Серия: {user.streak} дней | Рекорд: {user.longest_streak} дней\n"
        f"✨ Идеальных: {user.lessons_perfect}\n\n"
        f"🏅 Бейджей: {len(badges)}",
        parse_mode="HTML",
    )

@router.callback_query(F.data == "show_stats")
async def show_stats(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    if not user:
        await callback.answer("Сначала /start", show_alert=True)
        return
    badges = await get_user_badges(callback.from_user.id)
    text = (
        f"📊 <b>Статистика</b>\n\n"
        f"{user.get_level_name()} | XP: {user.xp_total}\n"
        f"📚 Уроков: {user.total_lessons}\n"
        f"🔥 Серия: {user.streak} дней\n"
        f"🏅 Бейджей: {len(badges)}"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=get_back_keyboard())

@router.message(Command("schedule"))
async def cmd_schedule(message: Message):
    user = await get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала /start")
        return
    times = ", ".join(user.notify_times) if user.notify_times else "Не настроено"
    await message.answer(
        f"⏰ <b>Расписание</b>\n\nВремя: {times}\nЧасовой пояс: {user.timezone}\n\n"
        f"Введи новое время в формате ЧЧ:ММ:",
        parse_mode="HTML",
    )

@router.message(F.text.regexp(r"^\d{2}:\d{2}"), StateFilter(None))
async def set_schedule(message: Message):
    times = [
        l.strip() for l in message.text.strip().split("\n")
        if re.match(r"^([01]\d|2[0-3]):([0-5]\d)$", l.strip())
    ]
    if not times:
        await message.answer("❌ Неверный формат")
        return
    await update_user(message.from_user.id, notify_times=times)
    await message.answer(f"✅ Расписание обновлено: {', '.join(times)}")

@router.callback_query(F.data == "show_schedule")
async def show_schedule(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    times = ", ".join(user.notify_times) if user and user.notify_times else "Не настроено"
    tz = user.timezone if user else "UTC"
    await callback.message.edit_text(
        f"⏰ <b>Расписание</b>\n\nВремя: {times}\nЧасовой пояс: {tz}",
        parse_mode="HTML",
        reply_markup=get_back_keyboard(),
    )

@router.callback_query(F.data == "show_subscription")
async def show_subscription(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    has_access = user.has_active_access() if user else False
    if has_access:
        if user.subscription_end and user.subscription_end > datetime.utcnow():
            end = user.subscription_end
        elif user.trial_started:
            end = user.trial_started + timedelta(days=30)
        else:
            end = datetime.utcnow()
        days = max((end - datetime.utcnow()).days, 0)
        text = f"💳 <b>Подписка</b>\n\n✅ Активна\n📅 До: {end.strftime('%d.%m.%Y')}\n⏰ {days} дней"
    else:
        text = f"💳 <b>Подписка</b>\n\n🔴 Неактивна\n\n💰 {settings.SUBSCRIPTION_PRICE}₽/месяц"
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=get_back_keyboard())

@router.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Главное меню:", reply_markup=get_main_menu_keyboard())

# ============================================================
#  Админка
# ============================================================
def is_admin(user_id: int) -> bool:
    return user_id in settings.ADMIN_IDS

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id): return
    await message.answer(
        "🔧 <b>Админ-панель</b>\n\n"
        "/admin_stats — статистика\n"
        "/admin_users — пользователи\n"
        "/admin_user ID — инфо о пользователе\n"
        "/admin_gift ID [дни] — подарить доступ\n"
        "/admin_reset ID — полный сброс (для тестов)\n"
        "/admin_ban ID — забанить\n"
        "/admin_unban ID — разбанить",
        parse_mode="HTML",
    )

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
        if not user:
            await message.answer("Не найден")
            return
        await message.answer(
            f"👤 {user.display_name or user.full_name or 'N/A'}\n"
            f"@{user.username or 'N/A'}\n"
            f"ID: {user.id}\n"
            f"Уровень: {user.level}\n"
            f"Стиль: {user.learning_style or 'не задан'}\n"
            f"Уроков: {user.total_lessons}\n"
            f"XP: {user.xp_total}\n"
            f"Подписка: {'Активна' if user.has_active_access() else 'Нет'}"
        )
    except:
        await message.answer("Использование: /admin_user ID")

@router.message(Command("admin_gift"))
async def admin_gift(message: Message, bot: Bot):
    if not is_admin(message.from_user.id): return
    args = message.text.split()
    try:
        target_id = int(args[1])
        days = int(args[2]) if len(args) > 2 else 30
        user = await get_user(target_id)
        if not user:
            await message.answer("Не найден")
            return
        now = datetime.utcnow()
        current_end = user.subscription_end or now
        if current_end < now: current_end = now
        new_end = current_end + timedelta(days=days)
        await update_user(
            target_id,
            subscription_end=new_end,
            gifted_days_total=user.gifted_days_total + days,
            gifted_by=message.from_user.id,
            gifted_at=now,
        )
        await create_gift(target_id, message.from_user.id, days, new_end)
        try:
            await bot.send_message(
                target_id,
                f"🎁 Тебе подарено {days} дней!\n\nДоступ до {new_end.strftime('%d.%m.%Y')}\n\nУчи английский! 🚀"
            )
        except: pass
        await message.answer(f"✅ {target_id} подарено {days} дней до {new_end.strftime('%d.%m.%Y')}")
    except:
        await message.answer("Использование: /admin_gift ID [дни]")

@router.message(Command("admin_ban"))
async def admin_ban(message: Message):
    if not is_admin(message.from_user.id): return
    try:
        uid = int(message.text.split()[1])
        await update_user(uid, is_blocked=True)
        await message.answer(f"✅ {uid} заблокирован")
    except:
        await message.answer("Использование: /admin_ban ID")

@router.message(Command("admin_unban"))
async def admin_unban(message: Message):
    if not is_admin(message.from_user.id): return
    try:
        uid = int(message.text.split()[1])
        await update_user(uid, is_blocked=False)
        await message.answer(f"✅ {uid} разблокирован")
    except:
        await message.answer("Использование: /admin_unban ID")

@router.message(Command("admin_reset"))
async def admin_reset(message: Message):
    """Полный сброс статистики пользователя для тестирования."""
    if not is_admin(message.from_user.id): return
    try:
        uid = int(message.text.split()[1])
        user = await get_user(uid)
        if not user:
            await message.answer("❌ Пользователь не найден")
            return
        # Сбрасываем все поля статистики
        await update_user(
            uid,
            streak=0,
            longest_streak=0,
            total_lessons=0,
            xp_total=0,
            xp_level=1,
            lessons_perfect=0,
            ielts_lessons=0,
            last_lesson_date=None,
            trial_started=datetime.utcnow(),
            subscription_end=None,
            is_subscribed=False,
            # Сбрасываем онбординг — бот пройдёт его заново
            level="B1",
            focus_areas=["vocabulary"],
            bot_mode="casual",
            learning_style=None,
            display_name=None,
            notify_times=["09:00"],
            timezone="UTC",
            voice_count_today=0,
            photo_count_today=0,
            media_reset_at=None,
        )
        # Удаляем бейджи, историю уроков, память и чат
        async with AsyncSessionLocal() as db:
            await db.execute(update(Badge).where(Badge.user_id == uid).values())
            # Удаляем бейджи
            result = await db.execute(select(Badge).where(Badge.user_id == uid))
            for b in result.scalars().all():
                await db.delete(b)
            # Удаляем историю уроков
            result = await db.execute(select(Lesson).where(Lesson.user_id == uid))
            for l in result.scalars().all():
                await db.delete(l)
            # Удаляем память
            result = await db.execute(select(UserMemory).where(UserMemory.user_id == uid))
            for m in result.scalars().all():
                await db.delete(m)
            # Удаляем историю чата
            result = await db.execute(select(ChatMessage).where(ChatMessage.user_id == uid))
            for c in result.scalars().all():
                await db.delete(c)
            await db.commit()
        await message.answer(
            f"✅ Пользователь <b>{uid}</b> полностью сброшен:\n\n"
            f"• Статистика → 0\n"
            f"• Бейджи → удалены\n"
            f"• История уроков → удалена\n"
            f"• Память (слабые места) → удалена\n"
            f"• История чата → удалена\n"
            f"• Онбординг → начнётся заново при /start\n"
            f"• Пробный период → перезапущен (30 дней)",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"admin_reset error: {e}")
        await message.answer("Использование: /admin_reset ID")

# ============================================================
#  MAIN
# ============================================================
dp = Dispatcher()
dp.include_router(router)

scheduler = AsyncIOScheduler()

async def send_daily(bot: Bot):
    import random
    users = await get_all_users(500)
    for user in users:
        if not user.has_active_access(): continue
        try:
            tz = pytz.timezone(user.timezone) if user.timezone else pytz.utc
        except pytz.exceptions.UnknownTimeZoneError:
            tz = pytz.utc
        now = datetime.now(tz).strftime("%H:%M")
        if now not in (user.notify_times or []):
            continue
        try:
            name = user.get_display_name()
            memory = await get_user_memory(user.id)
            weak = [m["value"] for m in memory if m["key"].startswith("weak:")]

            # Выбираем персонализированное сообщение
            if weak and random.random() > 0.4:
                # Напоминаем о слабом месте
                spot = random.choice(weak[-3:])
                text = (
                    f"👋 {name}, помнишь — у тебя было слабое место: <b>{spot}</b>?\n\n"
                    f"Сегодня хороший день это исправить! Я уже подготовил урок специально для тебя 💪"
                )
            elif user.streak >= 7:
                text = (
                    f"🔥 {name}, <b>{user.streak} дней</b> подряд — это серьёзно!\n\n"
                    f"Не останавливайся, продолжаем вместе 📚"
                )
            elif user.streak == 0:
                text = (
                    f"☀️ {name}, давно не виделись!\n\n"
                    f"Я уже скучал. Давай вернёмся в ритм — даже 10 минут в день делают разницу 🙌"
                )
            else:
                text = (
                    f"☀️ Привет, {name}!\n\n"
                    f"Готов к уроку сегодня? 📚 Серия <b>{user.streak}</b> дней — держим темп!"
                )

            await bot.send_message(
                user.id, text,
                parse_mode="HTML",
                reply_markup=get_lesson_type_keyboard(),
            )
            await asyncio.sleep(0.5)
        except Exception:
            pass

def setup_scheduler(bot: Bot):
    scheduler.add_job(send_daily, "cron", minute="*", args=[bot], id="send_daily", replace_existing=True)
    scheduler.start()

async def main():
    if not settings.BOT_TOKEN or not settings.DATABASE_URL or not settings.OPENROUTER_API_KEY:
        logger.error("Missing config! Set BOT_TOKEN, DATABASE_URL, OPENROUTER_API_KEY")
        return

    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database tables created / verified")
    except Exception as e:
        logger.error(f"Failed to create tables: {e}")

    bot = Bot(token=settings.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    setup_scheduler(bot)
    logger.info("Starting EnglishBot...")

    if settings.WEBHOOK_URL:
        await bot.set_webhook(f"{settings.WEBHOOK_URL}/webhook", secret_token=settings.WEBHOOK_SECRET)
        from aiohttp import web

        async def health(req): return web.json_response({"status": "ok"})

        async def wh(req):
            try:
                upd = Update.model_validate(await req.json())
                await dp.feed_update(bot, upd)
            except: pass
            return web.Response()

        app = web.Application()
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
