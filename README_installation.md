# yprac-microcase-generator

### Установка

```bash
./setup.sh
```

Используется micromamba для пакетов (env `ymg`).

### 1) Создайте файл .env в корне

Минимально необходимое:

```bash
# Яндекс GPT (по умолчанию используется в backend)
YANDEX_API_KEY=... 
YANDEX_FOLDER_ID=...

# Телеграм-бот (для фронтенда)
BOT_TOKEN=...

# Необязательно
GITHUB_TOKEN=...        # повысит лимиты GitHub API
BACKEND_URL=http://localhost:8000  # для бота; по умолчанию так
NGROK_AUTHTOKEN=...     # если хотите туннель через ngrok
```

### 2) Поднять backend (FastAPI)

```bash
micromamba activate ymg
python pytasksyn-backend/main.py
```

Опционально с ngrok-туннелем:

```bash
micromamba activate ymg
python pytasksyn-backend/main.py --ngrok
```

### 3) Поднять фронт (Telegram-бот)

```bash
micromamba activate ymg
python telegram_frontend/telegram_bot.py
```

Далее отправьте боту ссылку на GitHub PR (например, https://github.com/owner/repo/pull/123) — бот запросит backend и пришлёт микрокейсы.
