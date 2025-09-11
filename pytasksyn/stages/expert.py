import os
import csv
import sys
import time
import shutil
import subprocess
import tempfile
import ast
from pathlib import Path
from typing import Dict, Any
from langchain.prompts import PromptTemplate
from langchain_core.output_parsers import StrOutputParser

from pytasksyn.utils.logging_utils import get_logger


class ExpertStage:
    def __init__(self, config: Dict[str, Any], session_dir: Path, expert_llm):
        self.config = config
        self.session_dir = session_dir
        self.expert_llm = expert_llm
        self.parser = StrOutputParser()
    
    def run(self, deduplicated_review_file: Path) -> Dict[int, Dict]:
        """Run the expert stage to generate microcases for each comment"""
        logger = get_logger()
        logger.processing("Starting expert stage")
        
        # Load deduplicated comments
        comments = self._load_comments(deduplicated_review_file)
        logger.info(f"Processing {len(comments)} deduplicated comments")
        
        # Embed comments into source files first
        self._embed_comments(deduplicated_review_file)
        
        # Process each comment
        results = {}
        for comment in comments:
            comment_id = int(comment['comment_id'])
            logger.processing(f"Processing comment {comment_id}: {comment['file_path']}:{comment['line_number']}")
            
            result = self._process_comment(comment)
            results[comment_id] = result
            
            if result['success']:
                logger.success(f"Generated microcase for comment {comment_id} after {result['attempts']} attempts")
            else:
                logger.error(f"Failed to generate valid microcase for comment {comment_id} after {result['attempts']} attempts")
        
        # Print summary
        successful = sum(1 for r in results.values() if r['success'])
        total = len(results)
        logger.stage_complete("expert", {"successful": successful, "total": total})
        
        return results
    
    def _load_comments(self, review_file: Path):
        """Load comments from CSV file"""
        comments = []
        with open(review_file, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            comments = list(reader)
        return comments
    
    def _embed_comments(self, review_file: Path):
        """Run embed_comments.py to create embedded source files"""
        logger = get_logger()
        logger.processing("Embedding comments into source files")
        
        # Paths
        project_root = Path(self.config['paths']['student_project'])
        embedded_dir = self.session_dir / "embedded_source"
        embed_script = Path(__file__).parent.parent / "utils" / "embed_comments.py"
        
        try:
            # Run embed_comments.py
            result = subprocess.run([
                sys.executable, str(embed_script),
                "--review-file", str(review_file),
                "--project-root", str(project_root),
                "--output-dir", str(embedded_dir)
            ], capture_output=True, text=True, check=True)
            
            logger.success(f"Embedded source files created in: {embedded_dir}")
            
        except subprocess.CalledProcessError as e:
            logger.warning(f"Failed to embed comments: {e}")
            # Log detailed error to file
            logger.log('error', f"Embed comments failed - stdout: {e.stdout}, stderr: {e.stderr}", 'main', console_emoji=False)
            logger.log('debug', f"Embed script command: {[sys.executable, str(embed_script), '--review-file', str(review_file), '--project-root', str(project_root), '--output-dir', str(embedded_dir)]}", 'main', console_emoji=False)
            # Continue without embedded files
    
    def _process_comment(self, comment: Dict) -> Dict:
        """Process a single comment through the expert stage"""
        logger = get_logger()
        comment_id = int(comment['comment_id'])
        comment_dir = self.session_dir / f"comment_{comment_id}"
        comment_dir.mkdir(exist_ok=True)
        
        result = {
            'comment_id': comment_id,
            'source_file_path': comment['file_path'],
            'source_line_number': int(comment['line_number']),
            'success': False,
            'attempts': 0,
            'duration': {'total': 0, 'avg': 0, 'attempts': []},
            'successful_attempt_dir': None
        }
        
        max_attempts = self.config['expert']['max_attempts']
        
        for attempt in range(max_attempts):
            start_time = time.time()
            
            attempt_dir = comment_dir / "expert_output" / f"attempt_{attempt}"
            attempt_dir.mkdir(parents=True, exist_ok=True)
            
            # Create attempt logger for detailed logging
            attempt_logger = logger.create_attempt_logger(attempt_dir, attempt)
            
            logger.attempt_log(attempt + 1, max_attempts, f"Processing comment {comment_id}")
            
            success = self._generate_microcase_attempt(comment, attempt_dir, attempt_logger)
            
            end_time = time.time()
            duration = int(end_time - start_time)
            result['duration']['attempts'].append(duration)
            result['attempts'] = attempt + 1
            
            if success:
                result['success'] = True
                result['successful_attempt_dir'] = str(attempt_dir)
                logger.success(f"Comment {comment_id} succeeded on attempt {attempt + 1}")
                break
            else:
                logger.warning(f"Comment {comment_id} attempt {attempt + 1} failed")
        
        # Calculate duration stats
        result['duration']['total'] = sum(result['duration']['attempts'])
        if result['duration']['attempts']:
            result['duration']['avg'] = result['duration']['total'] // len(result['duration']['attempts'])
        
        return result
    
    def _generate_microcase_attempt(self, comment: Dict, attempt_dir: Path, attempt_logger) -> bool:
        """Generate microcase, tests, and solution for one attempt"""
        try:
            # Load source context
            source_context = self._load_source_context(comment)
            
            # Generate microcase description
            attempt_logger.log_generation("microcase description", "starting")
            microcase = self._generate_microcase_description(comment, source_context)
            if not microcase:
                attempt_logger.log_generation("microcase description", "failed", "Empty response from LLM")
                return False
            
            attempt_logger.log_generation("microcase description", "success")
            
            # Save microcase
            with open(attempt_dir / "microcase.txt", 'w', encoding='utf-8') as f:
                f.write(microcase)
            
            # Generate test suite
            attempt_logger.log_generation("test suite", "starting")
            tests = self._generate_test_suite(microcase)
            if not tests:
                attempt_logger.log_generation("test suite", "failed", "Empty response from LLM")
                return False
            
            attempt_logger.log_generation("test suite", "success")
            
            # Save tests
            tests_dir = attempt_dir / "tests"
            tests_dir.mkdir(exist_ok=True)
            with open(tests_dir / "test_microcase.py", 'w', encoding='utf-8') as f:
                f.write(tests)
            
            # Multiple solution generation attempts
            solution_max_attempts = self.config.get('expert', {}).get('max_solution_attempts', 3)

            last_solution_text = None
            for sol_try in range(solution_max_attempts):
                attempt_logger.log_generation("expert solution", f"attempt {sol_try + 1}/{solution_max_attempts}")
                solution = self._generate_expert_solution(microcase, tests)
                if not solution:
                    attempt_logger.log_generation("expert solution", f"attempt {sol_try + 1} failed", "Empty response from LLM")
                    continue

                last_solution_text = solution
                # Save (overwrite) solution file for this try
                solution_path = attempt_dir / "solution_expert.py"
                with open(solution_path, 'w', encoding='utf-8') as f:
                    f.write(solution)

                # Verify solution passes tests
                passed, test_stdout, test_stderr = self._verify_solution_detailed(attempt_dir, "solution_expert.py")
                attempt_logger.log_test_run("solution_expert.py", test_stdout, test_stderr, 0 if passed else 1)
                
                if passed:
                    attempt_logger.log_generation("expert solution", f"success on attempt {sol_try + 1}")
                    return True
                else:
                    attempt_logger.log_generation("expert solution", f"attempt {sol_try + 1} failed tests")

            # If we reach here — all solution generation attempts failed
            attempt_logger.log_generation("expert solution", "all attempts failed")
            # Keep the last solution saved for inspection
            if last_solution_text:
                (attempt_dir / "failed_solution_last.py").write_text(last_solution_text, encoding='utf-8')
            return False
            
        except Exception as e:
            attempt_logger.error(f"Expert attempt failed: {e}")
            return False

    
    def _load_source_context(self, comment: Dict) -> str:
        """Load source context with embedded comments and apply limits"""
        # Try to load embedded file first
        embedded_dir = self.session_dir / "embedded_source"
        embedded_file = embedded_dir / comment['file_path']
        
        content = None
        if embedded_file.exists():
            try:
                with open(embedded_file, 'r', encoding='utf-8') as f:
                    content = f.read()
            except Exception as e:
                print(f"      Warning: Could not read embedded file: {e}")
        
        # Fallback to original file
        if content is None:
            original_file = Path(self.config['paths']['student_project']) / comment['file_path']
            try:
                with open(original_file, 'r', encoding='utf-8') as f:
                    content = f.read()
            except Exception as e:
                logger = get_logger()
                logger.warning(f"Could not read original file {original_file}: {e}")
                # Create minimal content with enough lines to handle the comment
                target_line = int(comment['line_number'])
                lines = [f"// File: {comment['file_path']}"]
                lines.extend([f"// Line {i} - could not load original content" for i in range(2, target_line + 5)])
                content = "\\n".join(lines)
        
        # Apply context limits
        return self._apply_context_limits(content, comment)
    
    def _apply_context_limits(self, content: str, comment: Dict) -> str:
        """Apply context limits based on configuration"""
        max_symbols = self.config['expert']['context_max_symbols']
        comment_margin = self.config['expert']['context_comment_margin']
        
        # If content is within limits, return as-is
        if len(content) <= max_symbols:
            return content
        
        lines = content.split('\\n')
        comment_line = int(comment['line_number']) - 1  # Convert to 0-based
        
        # If comment_margin is specified and not None, extract margin around comment
        if comment_margin is not None:
            start_line = max(0, comment_line - comment_margin)
            end_line = min(len(lines), comment_line + comment_margin + 1)
            limited_content = '\\n'.join(lines[start_line:end_line])
            
            if len(limited_content) <= max_symbols:
                return limited_content
        
        # Fallback: truncate to max_symbols
        return content[:max_symbols] + "\\n\\n... [Content truncated due to size limits] ..."
    
    def _generate_microcase_description(self, comment: Dict, source_context: str) -> str:
        """Generate microcase description using LLM"""
        prompt_template = """        
        На основе комментария к коду и окружающего его контекста создай учебный микро-кейс по программированию, который поможет студенту понять и исправить ошибку.
Данные:
1. Файл: {file_path}
2. Строка: {line_number}
3. Комментарий: {comment}
4. Контекст кода: {source_context}

Требования к микро-кейсу:

1. Показывает именно ту ошибку, о которой говорит комментарий.
2. Объясняет основной принцип программирования, связанный с этой ошибкой.
3. Решается в ограниченном объёме и логично.
4. Может быть реализован в одном Python-файле.
5. Содержит понятное описание задачи и требования.
6. Не включай примеры кода или тесты — только описание задачи.


Оформление к микро-кейсу:
Название ошибки, на основе которого выделен микро кейсы должно писаться с верху и быть выделен жирным цветом.
Далее должно быть описание микро кейсы.

Описание микро-кейса:
        """
        
        prompt = PromptTemplate(
            template=prompt_template,
            input_variables=["file_path", "line_number", "comment", "source_context"]
        )
        
        chain = prompt | self.expert_llm | self.parser
        response = chain.invoke({
            "file_path": comment['file_path'],
            "line_number": comment['line_number'],
            "comment": comment['comment'],
            "source_context": source_context
        })
        
        return response.strip()
    
    def _generate_test_suite(self, microcase: str) -> str:
        """Generate pytest test suite for the microcase"""
        prompt_template = """На основе этого микро-кейса создайте тестовый набор с использованием pytest с корректным Python-кодом.

Микро-кейс:
{microcase}

Требования:

- Пишите ТОЛЬКО корректный Python-код, без объяснений
- Начинайте с необходимых импортов (pytest и модули стандартной библиотеки)
- Импортируйте функции из solution_expert с помощью: from solution_expert import function_name
- Создавайте тестовые функции, проверяющие правильность работы решения
- Используйте понятные имена тестовых функций, начинающиеся с test_
- Включайте assertions для проверки ожидаемого поведения
- НЕ определяйте тестируемые функции — только тестируйте их

Пример формата:
```
import pytest
from solution_expert import my_function

def test_basic_functionality():
    result = my_function(input_value)
    assert result == expected_value

def test_edge_cases():
    # реализация теста
    assert True
```

Предоставьте полный корректный Python-код тестов, импортирующий функции из solution_expert."""
        
        prompt = PromptTemplate(
            template=prompt_template,
            input_variables=["microcase"]
        )
        
        chain = prompt | self.expert_llm | self.parser
        response = chain.invoke({"microcase": microcase})
        
        return self._clean_test_code(response)
    
    def _generate_expert_solution(self, microcase: str, tests: str) -> str:
        """Generate expert solution for the microcase"""
        prompt_template = """На основе этого микро-кейса и набора тестов создайте эталонное решение.

Микро-кейс:
{microcase}

Набор тестов:
{tests}

Требования:

- Пишите ТОЛЬКО корректный Python-код, без объяснений и markdown
- Включите все необходимые импорты в начале
- Создавайте функции/классы по необходимости для решения микро-кейса
- Убедитесь, что код проходит все предоставленные тесты
- Следуйте лучшим практикам Python
- НЕ включайте тестовые функции в решение
- Включайте только функции реализации, которые будут импортироваться тестами

Предоставьте полный корректный Python-код решения (только реализация, без тестов):"""
        
        prompt = PromptTemplate(
            template=prompt_template,
            input_variables=["microcase", "tests"]
        )
        
        chain = prompt | self.expert_llm | self.parser
        response = chain.invoke({
            "microcase": microcase,
            "tests": tests
        })
        
        return self._clean_solution_code(response)
    
    def _clean_test_code(self, code_text: str) -> str:
        """Clean LLM output for test files"""
        return self._clean_python_code(code_text, keep_tests=True)
    
    def _clean_solution_code(self, code_text: str) -> str:
        """Clean LLM output for solution files"""
        return self._clean_python_code(code_text, keep_tests=False)
    
    def _clean_python_code(self, code_text: str, keep_tests: bool = True) -> str:
        """Clean LLM output to extract only valid Python code"""
        lines = code_text.strip().split('\n')
        cleaned_lines = []
        in_code_block = False
        skip_test_functions = False
        
        for line in lines:
            # Skip markdown code block markers
            if line.strip().startswith('```'):
                in_code_block = not in_code_block
                continue
            
            # Skip test functions in solution files (but keep them in test files)
            if line.strip().startswith('def test_') and not keep_tests:
                skip_test_functions = True
                continue
            
            # Reset skip flag when we encounter a new function that's not a test
            if line.strip().startswith('def ') and not line.strip().startswith('def test_'):
                skip_test_functions = False
            
            # Skip lines inside test functions when not keeping tests
            if skip_test_functions and not keep_tests and (line.startswith('    ') or line.startswith('\t') or line.strip() == ''):
                continue
            
            # If we're in a code block or line looks like Python code, keep it
            if in_code_block or line.strip().startswith(('import ', 'from ', 'def ', 'class ', 'if ', 'for ', 'while ', 'try:', 'except:', 'with ', '@')) or line.strip() == '' or line.startswith('    ') or line.startswith('\t'):
                if keep_tests or not skip_test_functions:
                    cleaned_lines.append(line)
            # Keep lines that look like Python statements
            elif any(keyword in line for keyword in ['=', 'return ', 'assert ', 'print(', 'raise ']):
                if keep_tests or not skip_test_functions:
                    cleaned_lines.append(line)
            # Keep comments
            elif line.strip().startswith('#'):
                if keep_tests or not skip_test_functions:
                    cleaned_lines.append(line)
        
        cleaned_code = '\n'.join(cleaned_lines).strip()
        
        # Validate Python syntax
        if cleaned_code:
            try:
                ast.parse(cleaned_code)
            except SyntaxError as e:
                print(f"      Warning: Generated code has syntax error: {e}")
                # Return a minimal valid Python file if parsing fails completely
                if not cleaned_code:
                    cleaned_code = "# Empty implementation\npass"
        
        return cleaned_code
    
    def _verify_solution(self, attempt_dir: Path, solution_filename: str) -> bool:
        """Verify that the solution passes all tests"""
        passed, _, _ = self._verify_solution_detailed(attempt_dir, solution_filename)
        return passed
    
    def _verify_solution_detailed(self, attempt_dir: Path, solution_filename: str) -> tuple[bool, str, str]:
        """Verify that the solution passes all tests and return detailed output"""
        try:
            # Ensure pytest can import solution module from attempt_dir
            solution_file = attempt_dir / solution_filename
            tests_dir = attempt_dir / "tests"
            
            if not solution_file.exists() or not tests_dir.exists():
                return False, "", "Solution or tests directory not found"

            env = os.environ.copy()
            # Prepend attempt_dir to PYTHONPATH to import solution module
            env["PYTHONPATH"] = f"{str(attempt_dir)}{os.pathsep}{env.get('PYTHONPATH', '')}"

            # Run pytest directly against tests in attempt_dir
            result = subprocess.run([
                sys.executable, "-m", "pytest", "-v", "tests/"
            ], cwd=attempt_dir, env=env, capture_output=True, text=True)

            success = result.returncode == 0
            return success, result.stdout, result.stderr
                
        except Exception as e:
            return False, "", f"Error verifying solution: {e}"