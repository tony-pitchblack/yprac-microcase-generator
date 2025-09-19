# [AIPH 2025] Генерация микрокейсов по ревью кода
В рамках хакатона [AIPH 2025](https://aiproducthack.com/) решался кейс от Яндекс.Практикума по генерации небольших заданий по комментариям ревьюера для улучшения усваиваемости материала курсов учениками.
<img width="690" height="304" alt="Снимок экрана 2025-09-19 в 10 48 44" src="https://github.com/user-attachments/assets/eb1108b5-95f8-46e9-be2d-30b8ec729d94" />

## Общий подход
Реализовывался пайплан по статье [Synthesizing High-Quality Programming Tasks
with LLM-based Expert and Student Agents](https://arxiv.org/pdf/2504.07655).

<img width="547" height="240" alt="Снимок экрана 2025-09-19 в 10 42 13" src="https://github.com/user-attachments/assets/c6910974-511c-41cf-9da4-f97bc196dd7b" />

Презентация решения доступна по [ссылке](https://docs.google.com/presentation/d/1bAYlRHtAMhULJf-EfORmKhAgraGQLNVY0RvRErsilsA/edit?slide=id.p#slide=id.p)

В текущей версии реализован этап "Эксперт" из статьи, который включает:
- генерацию микрокейсов и автотестов по ошибкам студента;
- проверку решений струдента с помощью автотестов.

## Интерфейс приложения
Интерфейс пользователя реализован с помощью Телеграм-бота.

1. Пользователь отправляет ссылку на pull-ревест с комментариями реаьюера к его коду:

<img width="568" height="181" alt="Снимок экрана 2025-09-19 в 10 53 52" src="https://github.com/user-attachments/assets/4c5667c5-e5cc-47d1-8143-f7fd98d11c77" />

2. На бекенде генерируется микрокейсы и автотесты по каждому комментарию.
3. Пользователь выбирает микрокейс для решения и отправляет решение:

<img width="507" height="585" alt="Снимок экрана 2025-09-19 в 10 58 11" src="https://github.com/user-attachments/assets/aa5704d6-4afe-4273-931f-5aa7d01ed3dc" />

4. Решение пользователя **не** проходит автотесты:

<img width="561" height="136" alt="Снимок экрана 2025-09-19 в 10 54 11" src="https://github.com/user-attachments/assets/b8e2b291-fac2-49b9-9443-332e8e8ac90f" />

5. Решение пользователя проходит автотесты:

<img width="566" height="192" alt="Снимок экрана 2025-09-19 в 10 54 38" src="https://github.com/user-attachments/assets/436624cc-621b-4cec-851f-52a36d932fe7" />

## Установка

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
