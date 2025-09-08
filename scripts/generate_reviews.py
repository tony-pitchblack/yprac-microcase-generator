#!/usr/bin/env python3
import csv
import os
import re
import sys
from pathlib import Path
from typing import Iterable, List, Tuple


Review = Tuple[str, int, str]


def iter_text_files(root: Path) -> Iterable[Path]:
    exts = {".py", ".txt", ".ini", ".cfg", ".yaml", ".yml", ".json", ".md", ".html", ".css", ".js"}
    for dirpath, dirnames, filenames in os.walk(root):
        # Skip typical vendor/build directories
        dirnames[:] = [d for d in dirnames if d not in {".git", "node_modules", "venv", ".venv", "__pycache__", "dist", "build"}]
        for fname in filenames:
            p = Path(dirpath) / fname
            if p.suffix.lower() in exts:
                yield p


def review_python(file_path: Path, rel_to_root: Path) -> List[Review]:
    reviews: List[Review] = []
    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return reviews

    for idx, line in enumerate(content, start=1):
        if re.search(r"except\s*:\s*$", line):
            reviews.append((str(file_path.relative_to(rel_to_root)), idx, "Слишком общий except — перехватывайте конкретные исключения и обрабатывайте их адресно."))
        if re.search(r"def\s+\w+\s*\(.*=\s*\[|\{\|\}\]|\)\s*:", line):
            # Fallback simplistic detection for mutable defaults on same line
            if re.search(r"def\s+\w+\s*\([^\)]*(=\s*(\[|\{|\})[^\)]*)\)\s*:", line):
                reviews.append((str(file_path.relative_to(rel_to_root)), idx, "Изменяемый объект в значении по умолчанию параметра — используйте None и инициализируйте внутри функции."))
        if re.search(r"\beval\s*\(", line) or re.search(r"\bexec\s*\(", line):
            reviews.append((str(file_path.relative_to(rel_to_root)), idx, "Использование eval/exec небезопасно — избегайте динамического выполнения кода."))
        if re.search(r"\brequests\.(get|post|put|delete|patch|head)\s*\(", line) and "timeout=" not in line:
            reviews.append((str(file_path.relative_to(rel_to_root)), idx, "HTTP-запрос без timeout — добавьте timeout, чтобы избежать зависаний."))
        if re.search(r"\bopen\s*\(", line) and "encoding=" not in line and re.search(r"['\"](r|w|a|r\+|w\+|a\+)['\"]", line):
            reviews.append((str(file_path.relative_to(rel_to_root)), idx, "open(...) без encoding — укажите кодировку (например, encoding='utf-8')."))
        if re.search(r"\b(TODO|FIXME)\b", line, flags=re.IGNORECASE):
            reviews.append((str(file_path.relative_to(rel_to_root)), idx, "Есть TODO/FIXME — оформите задачу и доведите до завершения перед сдачей."))

    # File-level checks
    # Skip EOF newline/style checks

    # Django settings typical pitfalls
    if file_path.name == "settings.py":
        for idx, line in enumerate(content, start=1):
            if re.search(r"^\s*DEBUG\s*=\s*True\b", line):
                reviews.append((str(file_path.relative_to(rel_to_root)), idx, "DEBUG=True — выключите в продакшене и берите значение из переменных окружения."))
            if re.search(r"^\s*ALLOWED_HOSTS\s*=\s*\[\s*\]", line):
                reviews.append((str(file_path.relative_to(rel_to_root)), idx, "ALLOWED_HOSTS пуст — укажите домены/хосты или подставляйте из переменных окружения."))
            if "sqlite3" in line:
                reviews.append((str(file_path.relative_to(rel_to_root)), idx, "В настройках используется sqlite3 — для продакшена применяйте Postgres/MySQL."))

    # requirements.txt (basic pinning)
    if file_path.name == "requirements.txt":
        for idx, line in enumerate(content, start=1):
            striped = line.strip()
            if not striped or striped.startswith("#"):
                continue
            if ("==" not in striped) and ("@" not in striped) and ("~=" not in striped) and (">=" not in striped and "<=" not in striped):
                reviews.append((str(file_path.relative_to(rel_to_root)), idx, "Зависимость без зафиксированной версии — зафиксируйте диапазон или точную версию для воспроизводимости."))
    # DRF permissions heuristic for views
    try:
        full_text = "\n".join(content)
    except Exception:
        full_text = ""
    if file_path.name in {"views.py", "viewsets.py"}:
        if re.search(r"\b(APIView|ViewSet|ModelViewSet)\b", full_text) and "permission_classes" not in full_text:
            reviews.append((str(file_path.relative_to(rel_to_root)), 1, "В DRF представлениях явно задайте permission_classes (например, IsAuthenticated)."))
    return reviews


def review_html(file_path: Path, rel_to_root: Path) -> List[Review]:
    reviews: List[Review] = []
    try:
        text = file_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return reviews
    lines = text.splitlines()
    # lang attribute
    if re.search(r"<html(?![^>]*\blang=)", text, flags=re.IGNORECASE):
        reviews.append((str(file_path.relative_to(rel_to_root)), 1, "Добавьте атрибут lang на тег <html> — это улучшит доступность и SEO."))
    # Accessibility/content/security checks
    for idx, line in enumerate(lines, start=1):
        if re.search(r"<img(?![^>]*\balt=)[^>]*>", line, flags=re.IGNORECASE):
            reviews.append((str(file_path.relative_to(rel_to_root)), idx, "У тега <img> отсутствует alt — добавьте краткое текстовое описание изображения."))
        if re.search(r"<a[^>]*target=\"_blank\"", line, flags=re.IGNORECASE) and not re.search(r"rel=\"(noopener|noreferrer)", line, flags=re.IGNORECASE):
            reviews.append((str(file_path.relative_to(rel_to_root)), idx, "Ссылка с target=_blank без rel=noopener — добавьте rel=\"noopener\" или rel=\"noreferrer\"."))
    return reviews


def review_css(file_path: Path, rel_to_root: Path) -> List[Review]:
    # No style checks for CSS per requirements
    return []


def review_js(file_path: Path, rel_to_root: Path) -> List[Review]:
    # No style checks for JS per requirements
    return []


def review_generic(file_path: Path, rel_to_root: Path) -> List[Review]:
    # Placeholder for simple text checks (e.g., secrets)
    reviews: List[Review] = []
    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return reviews
    for idx, line in enumerate(content, start=1):
        if re.search(r"(AKIA|ASIA)[0-9A-Z]{16}", line):
            reviews.append((str(file_path.relative_to(rel_to_root)), idx, "Похоже на AWS-ключ — вынесите секреты в переменные окружения/хранилище секретов и отзовите ключ."))
        if re.search(r"SECRET_KEY\s*=\s*['\"]\w+['\"]", line):
            reviews.append((str(file_path.relative_to(rel_to_root)), idx, "Секретный ключ закоммичен в репозиторий — загрузите из env и перегенерируйте ключ."))
        if re.search(r"\b(API|ACCESS|REFRESH)?_?TOKEN\s*=\s*['\"][^'\"]+['\"]", line, flags=re.IGNORECASE):
            reviews.append((str(file_path.relative_to(rel_to_root)), idx, "Найден токен в коде — удалите из репозитория и храните в переменных окружения."))
        if re.search(r"\bPASSWORD\s*=\s*['\"][^'\"]+['\"]", line, flags=re.IGNORECASE):
            reviews.append((str(file_path.relative_to(rel_to_root)), idx, "Найден пароль в коде — вынесите в переменные окружения/секреты и смените пароль."))
    return reviews


def review_file(file_path: Path, project_root: Path) -> List[Review]:
    suffix = file_path.suffix.lower()
    if suffix == ".py" or file_path.name in {"requirements.txt", "settings.py"}:
        rev = review_python(file_path, project_root)
    elif suffix == ".html":
        rev = review_html(file_path, project_root)
    elif suffix == ".css":
        rev = review_css(file_path, project_root)
    elif suffix == ".js":
        rev = review_js(file_path, project_root)
    else:
        rev = review_generic(file_path, project_root)
    return rev


def detect_projects(root: Path) -> List[Path]:
    projects: List[Path] = []
    if not root.exists():
        return projects
    # Heuristic: treat immediate children of backend/ as separate projects
    backend = root / "backend"
    if backend.exists():
        for child in backend.iterdir():
            if child.is_dir():
                projects.append(child)
    # Frontend: find nested student project folders containing index.html
    frontend = root / "frontend"
    if frontend.exists():
        for dirpath, dirnames, filenames in os.walk(frontend):
            if "index.html" in filenames:
                projects.append(Path(dirpath))
    return projects


def write_csv(reviews: List[Review], out_csv: Path) -> None:
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["file_path", "line_number", "comment"])
        for file_path, line_number, comment in reviews:
            writer.writerow([file_path, line_number, comment])


def main() -> int:
    if len(sys.argv) < 3:
        print("Usage: generate_reviews.py <input_root> <output_dir>")
        return 2
    input_root = Path(sys.argv[1]).resolve()
    output_dir = Path(sys.argv[2]).resolve()

    projects = detect_projects(input_root)
    if not projects:
        print(f"No projects found under {input_root}")
        return 0

    for project_root in projects:
        project_name = project_root.name
        all_reviews: List[Review] = []
        for path in iter_text_files(project_root):
            all_reviews.extend(review_file(path, project_root))
        # Ensure at least one row with general note if nothing found
        if not all_reviews:
            all_reviews.append(("", 1, "Автоматических замечаний не найдено."))
        out_csv = output_dir / f"{project_name}.csv"
        write_csv(all_reviews, out_csv)
        print(f"Written {out_csv} ({len(all_reviews)} reviews)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


