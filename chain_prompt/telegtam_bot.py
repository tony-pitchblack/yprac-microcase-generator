# bot.py
import os
import json
import asyncio
from pathlib import Path
from dotenv import load_dotenv
import httpx

# вместо requests.post
from mock_backend import gen_microcases, check_solution, review_solution

from telegram import Update
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
#   "microcases": [ { "id": "...", "title": "...", "description": "..." }, ... ],
#   "current": 0,
#   "solved": [false, ...],
#   "awaiting_review": false
# }

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

# --------------------
# Вспомогательные отправки пользователю
# --------------------
async def send_microcase_message(update: Update, microcase: dict):
    txt_parts = []
    mc_id = microcase.get("id") or microcase.get("mc_id") or "<unknown-id>"
    txt_parts.append(f"📌 Микро-кейс: {microcase.get('title', mc_id)}")
    txt_parts.append(f"🆔 ID: `{mc_id}`")
    desc = microcase.get("description") or microcase.get("prompt") or ""
    if desc:
        txt_parts.append("\nОписание:\n" + desc)
    instructions = microcase.get("instructions")
    if instructions:
        txt_parts.append("\nИнструкции:\n" + instructions)
    txt_parts.append("\n➡️ Отправь решение (прямым текстом в чат).")
    message_text = "\n\n".join(txt_parts)
    await update.message.reply_markdown_v2(message_text)

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
        await update.message.reply_text("Отправляю ссылку на backend для генерации микро-кейсов...")
        status, data = await post_json("/gen-microcases/", {"url": text, "user_id": user_id})
        if status != 200:
            await update.message.reply_text(f"Ошибка от backend: HTTP {status}. Подробнее: {data}")
            return

        microcases = data.get("microcases") or data.get("cases") or []
        if not microcases:
            await update.message.reply_text("Backend вернул пустой список микро-кейсов.")
            return

        # создаём сессию
        sessions[user_id] = {
            "microcases": microcases,
            "current": 0,
            "solved": [False] * len(microcases),
            "awaiting_review": False,
        }
        save_sessions(sessions)

        # отправляем первый микро-кейс
        mc = microcases[0]
        await update.message.reply_text(f"Получено {len(microcases)} микро-кейса(ов). Отправляю первый:")
        await send_microcase_message(update, mc)
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
    mc_id = mc.get("id") or mc.get("mc_id") or f"idx_{current_index}"
    solution = text  # берем весь текст как решение

    await update.message.reply_text("Отправляю решение на проверку (автотесты)...")
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

    print("Бот запущен...")
    app.run_polling()

if __name__ == "__main__":
    main()