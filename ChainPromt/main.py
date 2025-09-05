import os
from dotenv import load_dotenv
from langchain.prompts import PromptTemplate
from langchain_community.llms.yandex import YandexGPT
from langchain.chains import LLMChain
from langchain.chains import SequentialChain

os.chdir(os.path.dirname(os.path.abspath(__file__)))

# === Настройки ===
load_dotenv()
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY")
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID")

if not YANDEX_API_KEY or not YANDEX_FOLDER_ID:
    raise ValueError("Не найдены YANDEX_API_KEY или YANDEX_FOLDER_ID в .env файле")

llm = YandexGPT(api_key=YANDEX_API_KEY, folder_id=YANDEX_FOLDER_ID)

# === Читаем текст комментариев (review) ===
with open("review.txt", "r", encoding="utf-8") as f:
    review_text = f.read()

# === Читаем промпты из файлов ===
def load_prompt(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

extract_template = load_prompt("extract_prompt.txt")
case_template = load_prompt("case_prompt.txt")

# === Цепочки ===
extract_prompt = PromptTemplate(
    input_variables=["review"], template=extract_template
)
error_chain = LLMChain(llm=llm, prompt=extract_prompt, output_key="errors")

case_prompt = PromptTemplate(
    input_variables=["errors"], template=case_template
)
# Измените output_key, чтобы избежать перезаписи
case_chain = LLMChain(llm=llm, prompt=case_prompt, output_key="cases")

# === Общая последовательная цепочка ===
pipeline = SequentialChain(
    chains=[error_chain, case_chain],
    input_variables=["review"],
    output_variables=["errors", "cases"],
    verbose=True
)



# === Запуск ===
result = pipeline({"review": review_text})

print("Ошибки:\n", result["errors"])
print("\nМикро-кейсы:\n", result["cases"])