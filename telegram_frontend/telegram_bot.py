# bot.py
import os
import json
import asyncio
from pathlib import Path
from dotenv import load_dotenv
import httpx
from typing import Dict, List, Optional
import re

# вместо requests.post
from mock_backend import gen_microcases, check_solution, review_solution

from telegram import Update, Bot
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# --------------------
# Конфиг
# --------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден в .env")

DATA_DIR = Path("data/backend")
DATA_DIR.mkdir(parents=True, exist_ok=True)
SESSIONS_FILE = DATA_DIR / "sessions.json"

# --------------------
# Сессии: простая локальная персистенция
# --------------------
def load_sessions():
    if SESSIONS_FILE.exists():
        try:
            return json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def save_sessions(sessions):
    SESSIONS_FILE.write_text(json.dumps(sessions, ensure_ascii=False, indent=2), encoding="utf-8")

# структура сессии по user_id (строка)
# {
#   "session_id": "owner-repo-123-abc123",
#   "microcases": [ { "microcase_id": "...", "file_path": "...", "comment": "...", "solution": "..." }, ... ],
#   "current": 0,
#   "solved": [false, ...],
#   "awaiting_review": false,
#   "streaming": false,
#   "generation_complete": false
# }

# Глобальный словарь для хранения активных SSE соединений
active_sse_tasks: Dict[str, asyncio.Task] = {}

# --------------------
# HTTP helper
# --------------------
async def post_json(path: str, payload: dict, timeout=15):
    url = f"{BACKEND_URL}{path}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload)
        # Попробуем парсить JSON везде, где возможно
        try:
            data = resp.json()
        except Exception:
            data = {"_raw_text": resp.text}
        return resp.status_code, data

async def listen_sse_stream(session_id: str, user_id: str, bot: Bot):
    """Listen to SSE stream and update user session with incoming microcases."""
    url = f"{BACKEND_URL}/stream-microcases/{session_id}"
    sessions = load_sessions()
    
    try:
        async with httpx.AsyncClient(timeout=600) as client:  # 10 minutes timeout
            async with client.stream('GET', url, headers={'Accept': 'text/event-stream'}) as response:
                if response.status_code != 200:
                    await bot.send_message(
                        chat_id=int(user_id), 
                        text=f"❌ Ошибка подключения к потоку: HTTP {response.status_code}"
                    )
                    return
                
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    
                    # Parse SSE format
                    if line.startswith('event: '):
                        event_type = line[7:]
                    elif line.startswith('data: '):
                        try:
                            data = json.loads(line[5:])
                            await handle_sse_event(event_type, data, user_id, bot)
                        except json.JSONDecodeError:
                            continue
                        
    except Exception as e:
        sessions = load_sessions()
        if user_id in sessions:
            sessions[user_id]['streaming'] = False
            save_sessions(sessions)
        
        await bot.send_message(
            chat_id=int(user_id), 
            text=f"❌ Ошибка при получении микрокейсов: {str(e)}"
        )

async def handle_sse_event(event_type: str, data: dict, user_id: str, bot: Bot):
    """Handle different types of SSE events."""
    sessions = load_sessions()
    
    if user_id not in sessions:
        return
    
    session = sessions[user_id]
    
    try:
        if event_type == 'progress':
            message = data.get('message', 'Обработка...')
            step = data.get('step', '')
            
            # Send progress updates to user
            await bot.send_message(
                chat_id=int(user_id), 
                text=f"🔄 {message}"
            )
            
        elif event_type == 'microcase':
            # New microcase received
            microcase = {
                'microcase_id': data.get('microcase_id'),
                'file_path': data.get('file_path'),
                'line_number': data.get('line_number'),
                'comment': data.get('comment'),
                'solution': data.get('solution')
            }
            
            session['microcases'].append(microcase)
            session['solved'].append(False)
            
            await bot.send_message(
                chat_id=int(user_id), 
                text=f"✅ Получен новый микрокейс! Всего: {len(session['microcases'])}"
            )
            
            # If this is the first microcase and user is not currently solving one
            if len(session['microcases']) == 1 and session['current'] == 0:
                await send_microcase_message_by_bot(bot, int(user_id), microcase)
                
        elif event_type == 'complete':
            message = data.get('message', 'Генерация завершена')
            total_accepted = data.get('total_accepted', 0)
            
            session['streaming'] = False
            session['generation_complete'] = True
            
            if total_accepted == 0:
                await bot.send_message(
                    chat_id=int(user_id), 
                    text="📝 Генерация завершена, но микрокейсы не были созданы. Попробуйте другую ссылку."
                )
                # Clear session
                sessions.pop(user_id, None)
            else:
                await bot.send_message(
                    chat_id=int(user_id), 
                    text=f"🎉 {message}\n\nВсего микрокейсов: {total_accepted}"
                )
                
                # If no microcases have been sent yet, send the first one
                if session['current'] == 0 and session['microcases']:
                    await send_microcase_message_by_bot(bot, int(user_id), session['microcases'][0])
            
        elif event_type == 'error':
            error_message = data.get('message', 'Неизвестная ошибка')
            session['streaming'] = False
            
            await bot.send_message(
                chat_id=int(user_id), 
                text=f"❌ Ошибка генерации: {error_message}"
            )
            
            # Clear session on error
            sessions.pop(user_id, None)
            
    except Exception as e:
        print(f"Error handling SSE event: {e}")
    
    finally:
        save_sessions(sessions)


# --------------------
# Вспомогательные отправки пользователю
# --------------------
async def send_microcase_message(update: Update, microcase: dict):
    await send_microcase_message_by_bot(update.get_bot(), update.effective_chat.id, microcase)

async def send_microcase_message_by_bot(bot: Bot, chat_id: int, microcase: dict):
    txt_parts = []
    mc_id = microcase.get("microcase_id") or microcase.get("id") or microcase.get("mc_id") or "<unknown-id>"
    file_path = microcase.get("file_path", "")
    line_number = microcase.get("line_number", "")
    comment = microcase.get("comment", "")
    
    txt_parts.append(f"📌 **Микрокейс #{mc_id}**")
    
    if file_path:
        txt_parts.append(f"📄 Файл: `{file_path}:{line_number}`")
    
    if comment:
        txt_parts.append(f"💬 Комментарий:\n{comment}")
    else:
        # Fallback to old format
        desc = microcase.get("description") or microcase.get("prompt") or ""
        if desc:
            txt_parts.append(f"📝 Описание:\n{desc}")
    
    instructions = microcase.get("instructions")
    if instructions:
        txt_parts.append(f"📋 Инструкции:\n{instructions}")
    
    txt_parts.append("➡️ **Отправьте ваше решение текстом в чат**")
    
    message_text = "\n\n".join(txt_parts)
    
    try:
        await bot.send_message(chat_id=chat_id, text=message_text, parse_mode='Markdown')
    except Exception:
        # Fallback without markdown if parsing fails
        await bot.send_message(chat_id=chat_id, text=message_text)

# --------------------
# Хэндлеры
# --------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Пришли ссылку на репозиторий (GitHub) — я пошлю её на backend и верну тебе микро-кейсы."
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Инструкции:\n"
        "- Пришли ссылку (http...) — начнём генерацию микро-кейсов.\n"
        "- После получения микро-кейса пришли решение простым текстом (код/ответ).\n"
        "- Когда все микро-кейсы пройдены — тебя попросят написать ревью/обоснование.\n"
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    user_id = str(update.effective_user.id)
    sessions = load_sessions()

    # если это ссылка (репозиторий)
    if text.startswith("http"):
        # Check if user already has active streaming session
        if user_id in sessions and sessions[user_id].get('streaming', False):
            await update.message.reply_text("🔄 У вас уже идет генерация микрокейсов. Дождитесь завершения.")
            return
            
        await update.message.reply_text("🚀 Отправляю ссылку на backend для генерации микрокейсов...")
        
        # Start microcase generation with new API (returns 202 with session_id)
        status, data = await post_json("/gen-microcases/", {"url": text, "user_id": user_id})
        if status != 202:
            await update.message.reply_text(f"❌ Ошибка от backend: HTTP {status}. Подробнее: {data}")
            return

        session_id = data.get("session_id")
        if not session_id:
            await update.message.reply_text("❌ Backend не вернул session_id")
            return

        # создаём сессию для streaming
        sessions[user_id] = {
            "session_id": session_id,
            "microcases": [],
            "current": 0,
            "solved": [],
            "awaiting_review": False,
            "streaming": True,
            "generation_complete": False
        }
        save_sessions(sessions)

        await update.message.reply_text(
            f"✅ Генерация запущена!\n"
            f"🆔 Session ID: `{session_id}`\n"
            f"🔄 Ожидайте, микрокейсы будут приходить по мере готовности..."
        )

        # Start SSE listener in background
        bot = context.bot
        task = asyncio.create_task(listen_sse_stream(session_id, user_id, bot))
        active_sse_tasks[user_id] = task
        
        return

    # не ссылка: смотрим — есть ли активная сессия и ожидается ли ответ на микро-кейс
    if user_id not in sessions:
        await update.message.reply_text("Я не вижу активной сессии. Пришли ссылку на репозиторий (http...) чтобы начать.")
        return

    session = sessions[user_id]
    # если ждём ревью
    if session.get("awaiting_review"):
        review_text = text
        await update.message.reply_text("Отправляю ревью на оценку...")
        status, data = await post_json("/evaluate-review/", {"user_id": user_id, "review": review_text})
        if status != 200:
            await update.message.reply_text(f"Ошибка от backend при оценке ревью: HTTP {status}. Подробнее: {data}")
            return
        # показать результат
        score = data.get("score")
        feedback = data.get("feedback") or data.get("comment") or data
        await update.message.reply_text(f"Оценка ревью: {score}\n\nКомментарий:\n{feedback}")
        # завершаем сессию
        sessions.pop(user_id, None)
        save_sessions(sessions)
        await update.message.reply_text("Сессия завершена. Если хочешь — пришли новую ссылку на репозиторий.")
        return

    # иначе — это решение на текущий микро-кейс
    current_index = session.get("current", 0)
    microcases = session.get("microcases", [])
    if current_index >= len(microcases):
        await update.message.reply_text("Все микро-кейсы уже обработаны. Напиши ревью (почему ты так решил).")
        session["awaiting_review"] = True
        save_sessions(sessions)
        return

    mc = microcases[current_index]
    mc_id = mc.get("microcase_id") or mc.get("id") or mc.get("mc_id") or f"idx_{current_index}"
    solution = text  # берем весь текст как решение

    await update.message.reply_text("⚙️ Отправляю решение на проверку...")
    payload = {"user_id": user_id, "microcase_id": mc_id, "solution": solution}
    status, data = await post_json("/check-microcase/", payload)

    if status != 200:
        await update.message.reply_text(f"Ошибка от backend: HTTP {status}. Подробнее: {data}")
        return

    # ожидаем структуру: {"status":"passed"} или {"status":"failed", "input":..., "expected":..., "actual":...}
    result_status = data.get("status") or data.get("result") or ""
    if result_status == "passed" or result_status == "ok":
        session["solved"][current_index] = True
        session["current"] = current_index + 1
        save_sessions(sessions)
        await update.message.reply_text("✅ Автотесты пройдены! Переходим к следующему микро-кейсу.")
        # если есть следующий — отправляем
        if session["current"] < len(microcases):
            next_mc = microcases[session["current"]]
            await send_microcase_message(update, next_mc)
        else:
            # все решены
            session["awaiting_review"] = True
            save_sessions(sessions)
            await update.message.reply_text(
                "🎉 Ты решил все микро-кейсы! Напиши, пожалуйста, краткое ревью/пояснение: "
                "почему ты так решил, что вынес из решения и т.п. Отправь текст в ответ."
            )
        return
    else:
        # не прошли: выводим информацию от backend если есть
        input_data = data.get("input") or data.get("given_input")
        expected = data.get("expected")
        actual = data.get("actual")
        msg = ["❌ Автотесты не пройдены."]
        if input_data is not None:
            msg.append(f"\nВходные данные:\n{input_data}")
        if expected is not None:
            msg.append(f"\nОжидаемый результат:\n{expected}")
        if actual is not None:
            msg.append(f"\nФактический результат:\n{actual}")
        # возможен дополнительный текст объяснения
        if data.get("explanation"):
            msg.append(f"\nКомментарий:\n{data.get('explanation')}")
        await update.message.reply_text("\n".join(msg))
        await update.message.reply_text("Попробуй исправить решение и пришли снова.")
        return

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # попытка поддержать отправку файла с кодом
    user_id = str(update.effective_user.id)
    sessions = load_sessions()
    if user_id not in sessions:
        await update.message.reply_text("Нет активной сессии. Пришли ссылку на репозиторий (http...) чтобы начать.")
        return

    doc = update.message.document
    if not doc:
        await update.message.reply_text("Не могу прочитать файл.")
        return

    # скачиваем файл во временный путь (в каталоге data/backend/tmp)
    tmp_dir = DATA_DIR / "tmp"
    tmp_dir.mkdir(exist_ok=True)
    file_path = tmp_dir / doc.file_name
    # загрузка файла
    file = await context.bot.get_file(doc.file_id)
    await file.download_to_drive(custom_path=str(file_path))

    # читаем содержимое (если текстовый)
    try:
        solution_text = file_path.read_text(encoding="utf-8")
    except Exception:
        await update.message.reply_text("Не удалось прочитать файл как текст. Отправь решение текстом в сообщении.")
        return

    # далее обрабатываем как текстовое решение — повторно вызываем код из handle_text:
    update.message.text = solution_text  # временно
    await handle_text(update, context)
    # удалять временный файл не обязательно, но можно
    try:
        file_path.unlink()
    except Exception:
        pass

async def cleanup_sse_tasks():
    """Clean up any active SSE tasks."""
    for user_id, task in active_sse_tasks.items():
        if not task.done():
            task.cancel()
            print(f"Cancelled SSE task for user {user_id}")
    active_sse_tasks.clear()

# --------------------
# main
# --------------------
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    # Документы (файлы с кодом)
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    # Текстовые сообщения: ссылки и решения
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("🤖 Telegram бот запущен с поддержкой SSE...")
    print(f"📡 Backend URL: {BACKEND_URL}")
    
    try:
        app.run_polling()
    except KeyboardInterrupt:
        print("\n🛑 Остановка бота...")
        # Clean up SSE tasks
        asyncio.run(cleanup_sse_tasks())
        print("✅ Бот остановлен")

if __name__ == "__main__":
    main()