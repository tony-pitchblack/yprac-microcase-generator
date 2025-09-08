#!/usr/bin/env python3
import os
import json
import argparse
from datetime import datetime
from dotenv import load_dotenv
from langchain.prompts import PromptTemplate
from langchain_community.llms.yandex import YandexGPT
from langchain_openai import ChatOpenAI
from langchain_core.output_parsers import StrOutputParser

# === Настройки ===
def parse_args():
    parser = argparse.ArgumentParser(description='Generate micro-cases from code reviews')
    parser.add_argument('--provider', choices=['yandex', 'openai'], default='openai', 
                       help='LLM provider to use (default: yandex)')
    return parser.parse_args()

def create_llm(provider):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    root_dir = os.path.dirname(script_dir)
    load_dotenv(os.path.join(root_dir, ".env"))
    
    if provider == 'yandex':
        YANDEX_API_KEY = os.getenv("YANDEX_API_KEY")
        YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID")
        YANDEX_MODEL = os.getenv("YANDEX_MODEL", "yandexgpt-lite")  # Default model
        
        if not YANDEX_API_KEY or not YANDEX_FOLDER_ID:
            raise ValueError("Не найдены YANDEX_API_KEY или YANDEX_FOLDER_ID в .env файле")
        
        kwargs = {
            "api_key": YANDEX_API_KEY,
            "folder_id": YANDEX_FOLDER_ID,
            "model_name": YANDEX_MODEL
        }
        
        return YandexGPT(**kwargs)
    
    elif provider == 'openai':
        OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
        OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL")
        OPENAI_MODEL = os.getenv("OPENAI_MODEL", "openai/gpt-oss-120b")  # Default model
        
        if not OPENAI_API_KEY:
            raise ValueError("Не найден OPENAI_API_KEY в .env файле")
        
        kwargs = {
            "api_key": OPENAI_API_KEY,
            "model": OPENAI_MODEL
        }
        if OPENAI_BASE_URL:
            kwargs["base_url"] = OPENAI_BASE_URL
        
        return ChatOpenAI(**kwargs)

args = parse_args()
llm = create_llm(args.provider)

# Print model information
if args.provider == 'openai':
    model_name = os.getenv("OPENAI_MODEL", "openai/gpt-5-nano")
    print(f"Using OpenAI model: {model_name}")
elif args.provider == 'yandex':
    model_name = os.getenv("YANDEX_MODEL", "yandexgpt-lite")
    print(f"Using Yandex model: {model_name}")

# === Читаем текст комментариев (review) ===
script_dir = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(script_dir, "review.txt"), "r", encoding="utf-8") as f:
    review_text = f.read()

# === Читаем промпты из файлов ===
def load_prompt(path: str) -> str:
    with open(os.path.join(script_dir, path), "r", encoding="utf-8") as f:
        return f.read()

# === Функция для логирования ===
def setup_logging_dir():
    session_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    root_dir = os.path.dirname(script_dir)
    log_dir = os.path.join(root_dir, "data", "chain_prompt", "logs", f"session_{session_timestamp}")
    os.makedirs(log_dir, exist_ok=True)
    return log_dir

def save_response_to_log(log_dir, prompt_filename, response):
    log_file = os.path.join(log_dir, f"{prompt_filename}_response.txt")
    with open(log_file, "w", encoding="utf-8") as f:
        f.write(response)

# === Читае промпта ===
language_prompt = load_prompt("language_detection.txt")
extract_template = load_prompt("extract_prompt.txt")
case_template = load_prompt("case_prompt.txt")

# === Цепочки ===
language_detection_prompt = PromptTemplate(
    input_variables=["code"], 
    template=language_prompt
)

extract_prompt = PromptTemplate(
    input_variables=["review"], template=extract_template
)

case_prompt = PromptTemplate(
    input_variables=["errors"], template=case_template
)

# === Общая последовательная цепочка ===
parser = StrOutputParser()

# цепочка для определения языка
language_chain = language_detection_prompt | llm | parser

# Создаем цепочку для извлечения ошибок
extract_chain = extract_prompt | llm | parser

# Создаем цепочку для генерации кейсов
case_chain = case_prompt | llm | parser

# Функция для выполнения полного pipeline
def run_pipeline(review_text, log_dir):
    # 1. Определяем язык и его тип (exec / non-exec)
    language_info_raw = language_chain.invoke({"code": review_text})
    save_response_to_log(log_dir, "language_detection", language_info_raw)

    # Пытаемся распарсить JSON
    try:
        language_info = json.loads(language_info_raw)
    except json.JSONDecodeError:
        language_info = {"language": "unknown", "type": "unknown"}
    
    # 2. Если язык non-executable → логируем и останавливаемся
    if language_info.get("type") == "non-executable":
        note = f"Обнаружен язык {language_info.get('language', 'unknown')} (non-executable). Кейсы не генерируются."
        save_response_to_log(log_dir, "pipeline_note", note)
        print(note)
        return {"language": language_info, "errors": None, "cases": None}

    # 3. Если язык executable → идём дальше
    errors = extract_chain.invoke({"review": review_text})
    save_response_to_log(log_dir, "extract_prompt", errors)
    
    cases = case_chain.invoke({"errors": errors})
    save_response_to_log(log_dir, "case_prompt", cases)

    return {"language": language_info, "errors": errors, "cases": cases}

# === Запуск ===
log_dir = setup_logging_dir()
result = run_pipeline(review_text, log_dir)

print("Ошибки:\n", result["errors"])
print("\nМикро-кейсы:\n", result["cases"])
print(f"\nЛоги сохранены в: {log_dir}")