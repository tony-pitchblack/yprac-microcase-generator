#!/usr/bin/env python3
import os
import argparse
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
        OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")  # Default model
        
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
    model_name = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")
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

extract_template = load_prompt("extract_prompt.txt")
case_template = load_prompt("case_prompt.txt")

# === Цепочки ===
extract_prompt = PromptTemplate(
    input_variables=["review"], template=extract_template
)

case_prompt = PromptTemplate(
    input_variables=["errors"], template=case_template
)

# === Общая последовательная цепочка ===
parser = StrOutputParser()

# Создаем цепочку для извлечения ошибок
extract_chain = extract_prompt | llm | parser

# Создаем цепочку для генерации кейсов
case_chain = case_prompt | llm | parser

# Функция для выполнения полного pipeline
def run_pipeline(review_text):
    # Извлекаем ошибки
    errors = extract_chain.invoke({"review": review_text})
    
    # Генерируем кейсы на основе ошибок
    cases = case_chain.invoke({"errors": errors})
    
    return {"errors": errors, "cases": cases}



# === Запуск ===
result = run_pipeline(review_text)

print("Ошибки:\n", result["errors"])
print("\nМикро-кейсы:\n", result["cases"])