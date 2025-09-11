# mock_backend.py
import random

def gen_microcases(repo_url: str):
    """Имитация генерации микро-кейсов по ссылке на репозиторий"""
    return {
        "status": "ok",
        "microcases": [
            {"id": 1, "task": "Напиши функцию, которая возвращает сумму списка чисел."},
            {"id": 2, "task": "Реализуй функцию, которая проверяет, является ли строка палиндромом."}
        ]
    }


def check_solution(microcase_id: int, solution: str):
    """Имитация проверки решения"""
    # случайный результат для теста
    passed = random.choice([True, False])

    if passed:
        return {
            "status": "passed",
            "message": f"✅ Тесты для задачи {microcase_id} пройдены!"
        }
    else:
        return {
            "status": "failed",
            "message": f"❌ Тесты для задачи {microcase_id} не пройдены.",
            "input": "[1, 2, 3]",
            "expected": "6",
            "got": "5"
        }


def review_solution(text: str):
    """Имитация ревью с помощью GPT"""
    return {
        "status": "ok",
        "review": (
            "📝 Ты написал решение корректно, но можно улучшить читаемость.\n"
            "Хорошо, что ты использовал list comprehension.\n"
            "Следи за именами переменных, чтобы они были более понятными."
        )
    }
