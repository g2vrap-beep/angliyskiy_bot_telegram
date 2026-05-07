# EnglishBot

Telegram-бот для изучения английского языка с AI.

## Возможности

- 📚 Уроки английского (словарный запас, грамматика, разговорная речь, чтение)
- 🎯 AI-оценка ответов через OpenRouter (Gemini 2.5 Flash)
- 🎤 Голосовые сообщения (STT + TTS)
- 📷 Анализ фото
- ⏰ Настраиваемое расписание уведомлений
- 📊 Статистика и геймификация (XP, уровни, бейджи)
- 💳 Подписка через Telegram Payments
- 🏆 Режим IELTS подготовки

## Установка

```bash
# Клонирование
git clone https://github.com/yourusername/english_bot_telegram.git
cd english_bot_telegram

# Виртуальное окружение
python -m venv venv
source venv/bin/activate  # Linux/Mac
venv\Scripts\activate     # Windows

# Зависимости
pip install -r requirements.txt

# Настройка
cp .env.example .env
# Заполните .env своими токенами

# Запуск
python -m bot.main
```

## Переменные окружения

| Переменная | Описание |
|---|---|
| `BOT_TOKEN` | Токен Telegram бота от @BotFather |
| `OPENROUTER_API_KEY` | API ключ OpenRouter |
| `DATABASE_URL` | URL PostgreSQL базы данных |
| `ADMIN_IDS` | Telegram ID администраторов (через запятую) |
| `WEBHOOK_URL` | URL для webhook (для прода) |
| `WEBHOOK_SECRET` | Секретный токен для webhook |
| `TRIAL_DAYS` | Дней бесплатного триала |
| `SUBSCRIPTION_PRICE` | Цена подписки в рублях |
| `PROVIDER_TOKEN` | Провайдер токен для Telegram Payments |

## Деплой на Railway

```bash
# Установить Railway CLI
npm install -g @railway/cli

# Войти
railway login

# Подключиться к проекту
railway link

# Задеплоить
railway up

# Запустить миграции (если есть)
railway run alembic upgrade head
```

## Команды бота

- `/start` — начать/перезапустить бота
- `/lesson` — начать урок
- `/stats` — статистика
- `/schedule` — настройка расписания
- `/help` — справка

## Админ-команды

- `/admin_stats` — общая статистика
- `/admin_users` — список пользователей
- `/admin_broadcast` — рассылка
- `/admin_user {id}` — информация о пользователе
- `/admin_ban/unban` — бан/разбан
- `/admin_gift {id} [дней]` — подарить доступ
