#!/usr/bin/env python3
import os
import json
import asyncio
from pathlib import Path
import shutil
from dotenv import load_dotenv
import httpx
from typing import Dict, List, Optional
import re
import hashlib

# HTTP backend is used via post_json helper below

from telegram import Update, Bot, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler,
)

# --------------------
# Конфиг
# --------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден в .env")

BASE_TMP = Path("tmp/telegram_frontend")
BASE_TMP.mkdir(parents=True, exist_ok=True)
USERS_INDEX = BASE_TMP / "users_index.json"

# --------------------
# Сессии: локальная персистенция на пользователя/сессию (tmp/telegram_frontend)
# --------------------
def _load_index():
    try:
        if USERS_INDEX.exists():
            return json.loads(USERS_INDEX.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}

def _save_index(index: dict):
    USERS_INDEX.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

def _session_dir(session_id: str) -> Path:
    d = BASE_TMP / f"session_{session_id}"
    d.mkdir(parents=True, exist_ok=True)
    return d

def load_user_session(user_id: str) -> dict:
    index = _load_index()
    session_id = index.get(user_id)
    if not session_id:
        return {}
    fp = _session_dir(session_id) / "session.json"
    try:
        if fp.exists():
            return json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}

def save_user_session(user_id: str, session: dict):
    session_id = session.get("session_id")
    if not session_id:
        return
    index = _load_index()
    index[user_id] = session_id
    _save_index(index)
    (_session_dir(session_id) / "session.json").write_text(
        json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8"
    )

def delete_user_session(user_id: str):
    index = _load_index()
    session_id = index.pop(user_id, None)
    _save_index(index)
    # Drop solved tracking for this user
    try:
        solved_cases.pop(user_id, None)
    except Exception:
        pass
    # Keep session files for debugging; uncomment to remove
    # if session_id:
    #     shutil.rmtree(_session_dir(session_id), ignore_errors=True)

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

# Tracking solved microcases per user_id
solved_cases: Dict[str, set] = {}

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
# Cache helpers
# --------------------
def _hash_pr_url(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()

def _cached_root_for_url(url: str) -> Path:
    pr_hash = _hash_pr_url(url)
    return Path("tmp") / "pytasksyn-backend" / "microcase_storage" / pr_hash

def load_cached_microcases(pr_url: str) -> List[dict]:
    root = _cached_root_for_url(pr_url)
    if not root.exists():
        return []
    microcases: List[dict] = []
    try:
        for d in sorted(root.glob("microcase_*")):
            mc_json = d / "microcase.json"
            if not mc_json.exists():
                continue
            try:
                meta = json.loads(mc_json.read_text(encoding="utf-8"))
                microcases.append({
                    'microcase_id': int(meta.get('microcase_id')),
                    'file_path': meta.get('file_path'),
                    'line_number': meta.get('line_number'),
                    'microcase': meta.get('microcase_text') or "",
                    'review_comment': "",
                    'solution': ""
                })
            except Exception:
                continue
    except Exception:
        return []
    # stable order by microcase_id
    microcases.sort(key=lambda x: x.get('microcase_id') or 0)
    return microcases

async def listen_sse_stream(session_id: str, user_id: str, bot: Bot):
    """Listen to SSE stream and update user session with incoming microcases."""
    url = f"{BACKEND_URL}/stream-microcases/{session_id}"
    # Note: session state is updated in handle_sse_event
    
    try:
        async with httpx.AsyncClient(timeout=600) as client:  # 10 minutes timeout
            async with client.stream('GET', url, headers={'Accept': 'text/event-stream'}) as response:
                if response.status_code != 200:
                    await bot.send_message(
                        chat_id=int(user_id), 
                        text=f"❌ Ошибка подключения к потоку: HTTP {response.status_code}"
                    )
                    return
                
                event_type = 'message'
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    if line.startswith(':'):
                        continue
                    
                    # Parse SSE format
                    if line.startswith('event: '):
                        event_type = line[7:]
                    elif line.startswith('data: '):
                        try:
                            data = json.loads(line[6:])
                            await handle_sse_event(event_type, data, user_id, bot)
                        except json.JSONDecodeError:
                            continue
                        
    except Exception as e:
        sess = load_user_session(user_id)
        if sess:
            sess['streaming'] = False
            save_user_session(user_id, sess)
        
        await bot.send_message(
            chat_id=int(user_id), 
            text=f"❌ Ошибка при получении микрокейсов: {str(e)}"
        )

async def handle_sse_event(event_type: str, data: dict, user_id: str, bot: Bot):
    """Handle different types of SSE events."""
    session = load_user_session(user_id)
    if not session:
        return
    
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
            # Ensure we always have a visible id for the UI
            mc_visible_id = data.get('microcase_id') or (len(session['microcases']) + 1)
            microcase = {
                'microcase_id': int(mc_visible_id),
                'file_path': data.get('file_path'),
                'line_number': data.get('line_number'),
                'microcase': data.get('microcase') or data.get('comment'),
                'review_comment': data.get('review_comment') or "",
                'solution': data.get('solution')
            }
            
            session['microcases'].append(microcase)
            session['solved'].append(False)
            
            # Do not notify or display list per microcase; wait until completion
                
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
                    text=f"⚙️ {message}.\n\nКоличество микрокейсов: {total_accepted}"
                )
                
                # Show selection list now that generation is complete
                await show_cases_list(bot, int(user_id), session)
            
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
        save_user_session(user_id, session)


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
    body = microcase.get("microcase", "")
    
    txt_parts.append(f"📌 **Микрокейс #{mc_id}**")
    
    if file_path:
        txt_parts.append(f"📄 Файл: `{file_path}:{line_number}`")
    
    if body:
        txt_parts.append(f"📝 Микрокейс:\n{body}")
    else:
        # Fallback to old format
        desc = microcase.get("description") or microcase.get("prompt") or ""
        if desc:
            txt_parts.append(f"📝 Описание:\n{desc}")
    
    instructions = microcase.get("instructions")
    if instructions:
        txt_parts.append(f"📋 Инструкции:\n{instructions}")
    
    txt_parts.append("➡️ Отправьте валидный Python-код решением (только код, без пояснений)")
    
    message_text = "\n\n".join(txt_parts)
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(text="⬅️ К списку", callback_data="back_to_list")]])
    
    try:
        await bot.send_message(chat_id=chat_id, text=message_text, parse_mode='Markdown', reply_markup=keyboard)
    except Exception:
        # Fallback without markdown if parsing fails
        await bot.send_message(chat_id=chat_id, text=message_text, reply_markup=keyboard)

async def show_cases_list(bot: Bot, chat_id: int, session: dict):
    buttons = []
    for i, mc in enumerate(session.get('microcases', [])):
        visible_no = i + 1
        fp = mc.get('file_path', '')
        ln = mc.get('line_number', '')
        label = f"#{visible_no} — {fp}:{ln}"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"choose_mc_idx:{i}")])
    if not buttons:
        return
    await bot.send_message(
        chat_id=chat_id,
        text="Выберите микрокейс для решения:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def handle_choose_mc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    user_id = str(query.from_user.id)
    session = load_user_session(user_id)
    if not session:
        await query.edit_message_text("Сессия не найдена. Пришлите ссылку, чтобы начать.")
        return
    data = query.data or ""
    microcases = session.get('microcases', [])
    idx = None

    if data.startswith("choose_mc_idx:"):
        try:
            idx = int(data.split(":", 1)[1])
        except ValueError:
            idx = None
    elif data.startswith("choose_mc:"):
        mc_id = data.split(":", 1)[1]
        for i, mc in enumerate(microcases):
            if str(mc.get('microcase_id')) == str(mc_id):
                idx = i
                break
    else:
        return
    if idx is None:
        await query.edit_message_text("Микрокейс не найден.")
        return
    session['current'] = idx
    save_user_session(user_id, session)
    mc = microcases[idx]
    # Edit the list message into a microcase card (avoid extra message)
    txt_parts = []
    mc_id = mc.get("microcase_id") or mc.get("id") or mc.get("mc_id") or "<unknown-id>"
    file_path = mc.get("file_path", "")
    line_number = mc.get("line_number", "")
    body = mc.get("microcase", "")

    txt_parts.append(f"📌 **Микрокейс #{mc_id}**")
    if file_path:
        txt_parts.append(f"📄 Файл: `{file_path}:{line_number}`")
    if body:
        txt_parts.append(f"📝 Микрокейс:\n{body}")
    else:
        desc = mc.get("description") or mc.get("prompt") or ""
        if desc:
            txt_parts.append(f"📝 Описание:\n{desc}")
    instructions = mc.get("instructions")
    if instructions:
        txt_parts.append(f"📋 Инструкции:\n{instructions}")
    txt_parts.append("➡️ Отправьте валидный Python-код решением (только код, без пояснений)")

    message_text = "\n\n".join(txt_parts)
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(text="⬅️ К списку", callback_data="back_to_list")]])
    try:
        await query.edit_message_text(text=message_text, parse_mode='Markdown', reply_markup=keyboard)
    except Exception:
        await query.edit_message_text(text=message_text, reply_markup=keyboard)

async def start_generation_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: str, pr_url: str):
    session = load_user_session(user_id)
    # Check if user already has active streaming session
    if session and session.get('streaming', False):
        await update.message.reply_text("🔄 У вас уже идет генерация микрокейсов. Дождитесь завершения.")
        return

    status, data = await post_json("/gen-microcases/", {"url": pr_url, "user_id": user_id})
    if status != 202:
        await update.message.reply_text(f"❌ Ошибка от backend: HTTP {status}. Подробнее: {data}")
        return

    session_id = data.get("session_id")
    if not session_id:
        await update.message.reply_text("❌ Backend не вернул session_id")
        return

    session = {
        "session_id": session_id,
        "microcases": [],
        "current": 0,
        "solved": [],
        "awaiting_review": False,
        "streaming": True,
        "generation_complete": False,
        "pr_url": pr_url
    }
    save_user_session(user_id, session)
    try:
        solved_cases[user_id] = set()
    except Exception:
        pass
    await update.message.reply_text(
        f"🚀 Началась генерация микрокейсов по PR `" + pr_url + "` — ожидайте.",
        parse_mode='Markdown'
    )
    bot = context.bot
    task = asyncio.create_task(listen_sse_stream(session_id, user_id, bot))
    active_sse_tasks[user_id] = task

async def handle_back_to_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    user_id = str(query.from_user.id)
    session = load_user_session(user_id)
    if not session:
        await query.edit_message_text("Сессия не найдена. Пришлите ссылку, чтобы начать.")
        return
    # Edit current message in-place to show the list (no extra messages)
    buttons = []
    for i, mc in enumerate(session.get('microcases', [])):
        visible_no = i + 1
        fp = mc.get('file_path', '')
        ln = mc.get('line_number', '')
        label = f"#{visible_no} — {fp}:{ln}"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"choose_mc_idx:{i}")])
    if not buttons:
        await query.edit_message_text("Нет доступных микрокейсов.")
        return
    await query.edit_message_text(
        text="Выберите микрокейс для решения:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def handle_use_cached_or_regen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    user_id = str(query.from_user.id)
    session = load_user_session(user_id) or {}
    pending_pr = session.get("pending_pr_url")
    if not pending_pr:
        await query.edit_message_text("Ссылка не найдена. Пришлите ссылку снова.")
        return
    if query.data == "use_cached":
        cached = load_cached_microcases(pending_pr)
        if not cached:
            await query.edit_message_text("Сохранённые микрокейсы не найдены. Запускаю генерацию заново...")
            await start_generation_flow(update, context, user_id, pending_pr)
            return
        session.update({
            "microcases": cached,
            "current": 0,
            "solved": [False for _ in cached],
            "awaiting_review": False,
            "streaming": False,
            "generation_complete": True,
            "pr_url": pending_pr
        })
        save_user_session(user_id, session)
        await query.edit_message_text(f"Загружено из кеша: {len(cached)} микрокейсов.")
        await show_cases_list(context.bot, int(user_id), session)
    elif query.data == "regen":
        await query.edit_message_text("Запускаю генерацию заново...")
        await start_generation_flow(update, context, user_id, pending_pr)

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
    session = load_user_session(user_id)

    # если это ссылка (репозиторий)
    if text.startswith("http"):
        # If cached microcases exist for this URL, offer choice
        cached = load_cached_microcases(text)
        if cached:
            # save pending pr url in session
            session = {
                "session_id": None,
                "microcases": [],
                "current": 0,
                "solved": [],
                "awaiting_review": False,
                "streaming": False,
                "generation_complete": False,
                "pending_pr_url": text
            }
            save_user_session(user_id, session)
            buttons = [
                [InlineKeyboardButton(text="🗂 Использовать сохранённые", callback_data="use_cached")],
                [InlineKeyboardButton(text="🔁 Сгенерировать заново", callback_data="regen")]
            ]
            await update.message.reply_text(
                "Для этой ссылки уже есть сохранённые микрокейсы. Что сделать?",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
            return
        # otherwise proceed to generation
        await start_generation_flow(update, context, user_id, text)
        return

    # не ссылка: смотрим — есть ли активная сессия и ожидается ли ответ на микро-кейс
    if not session:
        await update.message.reply_text("Я не вижу активной сессии. Пришли ссылку на репозиторий (http...) чтобы начать.")
        return
    
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
        delete_user_session(user_id)
        await update.message.reply_text("Сессия завершена. Если хочешь — пришли новую ссылку на репозиторий.")
        return

    # иначе — это решение на текущий микро-кейс
    current_index = session.get("current", 0)
    microcases = session.get("microcases", [])
    if current_index >= len(microcases):
        await update.message.reply_text("Все микро-кейсы уже обработаны. Напиши ревью (почему ты так решил).")
        session["awaiting_review"] = True
        save_user_session(user_id, session)
        return

    mc = microcases[current_index]
    mc_id = mc.get("microcase_id") or mc.get("id") or mc.get("mc_id") or f"idx_{current_index}"
    solution = text  # берем весь текст как решение

    await update.message.reply_text("⚙️ Отправляю решение на проверку...")
    payload = {"user_id": user_id, "microcase_id": mc_id, "solution": solution}
    # pass pr_url when session was started from URL to allow cached check
    pr_url = session.get("pr_url")
    if pr_url:
        payload["pr_url"] = pr_url
    status, data = await post_json("/check-microcase/", payload)

    if status != 200:
        await update.message.reply_text(f"Ошибка от backend: HTTP {status}. Подробнее: {data}")
        return

    # ожидаем структуру: {"status":"passed"} или {"status":"failed", "input":..., "expected":..., "actual":...}
    result_status = data.get("status") or data.get("result") or ""
    if result_status == "passed" or result_status == "ok":
        session["solved"][current_index] = True
        session["current"] = current_index + 1
        save_user_session(user_id, session)
        # Track solved microcase for user
        try:
            solved_cases.setdefault(user_id, set()).add(str(mc_id))
        except Exception:
            pass
        await update.message.reply_text("✅ Автотесты пройдены! Переходим к следующему микро-кейсу.")
        # если есть следующий — отправляем
        if session["current"] < len(microcases):
            next_mc = microcases[session["current"]]
            await send_microcase_message(update, next_mc)
        else:
            # все решены
            session["awaiting_review"] = True
            save_user_session(user_id, session)
            await update.message.reply_text("Все микрокейсы пройдены!")
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
    session = load_user_session(user_id)
    if not session:
        await update.message.reply_text("Нет активной сессии. Пришли ссылку на репозиторий (http...) чтобы начать.")
        return

    doc = update.message.document
    if not doc:
        await update.message.reply_text("Не могу прочитать файл.")
        return

    # скачиваем файл во временный путь (в каталоге tmp/telegram_frontend/tmp)
    tmp_dir = BASE_TMP / "tmp"
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
    app.add_handler(CallbackQueryHandler(handle_choose_mc, pattern="^choose_mc"))
    app.add_handler(CallbackQueryHandler(handle_back_to_list, pattern="^back_to_list$"))
    app.add_handler(CallbackQueryHandler(handle_use_cached_or_regen, pattern="^(use_cached|regen)$"))
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