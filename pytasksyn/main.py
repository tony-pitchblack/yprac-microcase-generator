#!/usr/bin/env python3
import os
import sys
import argparse
import yaml
import json
import shutil
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from langchain.prompts import PromptTemplate
from langchain_community.llms.yandex import YandexGPT
from langchain_openai import ChatOpenAI
from langchain_core.output_parsers import StrOutputParser

# Import stages
from pytasksyn.stages.preprocessing import PreprocessingStage
from pytasksyn.stages.expert import ExpertStage
from pytasksyn.stages.tutor import TutorStage
from pytasksyn.stages.student import StudentStage

# Import logging utilities
from pytasksyn.utils.logging_utils import init_logger, get_logger

def parse_args():
    parser = argparse.ArgumentParser(description='PyTaskSyn — генерация микро-кейсов из обзоров кода')
    
    # Model configuration
    parser.add_argument('--preprocessor-provider', choices=['yandex', 'openai'], help='Провайдер LLM для этапа препроцессинга')
    parser.add_argument('--preprocessor-model', help='Название модели для этапа препроцессинга')
    parser.add_argument('--expert-provider', choices=['yandex', 'openai'], help='Провайдер LLM для экспертного этапа')
    parser.add_argument('--expert-model', help='Название модели для экспертного этапа')
    parser.add_argument('--tutor-provider', choices=['yandex', 'openai'], help='Провайдер LLM для этапа наставника')
    parser.add_argument('--tutor-model', help='Название модели для этапа наставника')
    parser.add_argument('--student-provider', choices=['yandex', 'openai'], help='Провайдер LLM для этапа студента')
    parser.add_argument('--student-model', help='Название модели для этапа студента')
    
    # Paths
    parser.add_argument('--student-project', help='Путь к корню учебного проекта')
    parser.add_argument('--code-review-file', help='Путь к CSV с комментариями code review')
    
    # Stage configuration
    parser.add_argument('--skip-val-stage', help='Пропустить этапы валидации: "t" (наставник), "s" (студент), "st"/"ts" (оба)')
    # New explicit enable flags: by default tutor/student are DISABLED (isolated).
    parser.add_argument('--enable-tutor', action='store_true', help='Включить этап наставника (по умолчанию выключен)')
    parser.add_argument('--enable-student', action='store_true', help='Включить этап студента (по умолчанию выключен)')
    
    # Expert settings
    parser.add_argument('--expert-max-attempts', type=int, help='Максимум попыток для экспертного этапа')
    parser.add_argument('--expert-context-max-symbols', type=int, help='Максимум символов в контексте эксперта')
    parser.add_argument('--expert-context-comment-margin', type=int, help='Количество строк выше/ниже комментария для контекста')
    parser.add_argument('--expert-context-add-rest', action='store_true', help='Добавлять файлы без комментариев в контекст')
    
    # Tutor settings
    parser.add_argument('--tutor-max-solution-attempts', type=int, help='Максимум попыток решения для этапа наставника')
    parser.add_argument('--tutor-acceptance-threshold', type=float, help='Минимальная оценка для принятия микро-кейса')
    
    # Student settings
    parser.add_argument('--num-students', type=int, help='Количество симулированных студентов')
    parser.add_argument('--student-comprehension-threshold', type=float, help='Минимальная доля успешных, чтобы принять микро-кейс')
    
    return parser.parse_args()

def load_config(args=None):
    """Load and merge configuration from config.yml and CLI args"""
    script_dir = Path(__file__).parent.resolve()
    root_dir = script_dir.parent
    
    # Always use pytasksyn/config.yml as the primary config
    config_path = script_dir / "config.yml"
    if not config_path.exists():
        raise ValueError(f"Файл конфигурации не найден: {config_path}")
    
    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)
    
    # Parse args if not provided
    if args is None:
        args = parse_args()
    
    # Apply CLI args to config
    config = apply_cli_overrides(config, args)
    
    # Ensure we have stage flags and set defaults: by default tutor/student stages are disabled (isolated)
    config.setdefault('stages', {})
    # If config already defines enable_tutor/enable_student, keep them unless CLI explicitly changed.
    config['stages'].setdefault('enable_tutor', False)
    config['stages'].setdefault('enable_student', False)
    
    # Validate required fields
    validate_config(config)
    
    return config, args

def merge_configs(base, override):
    """Recursively merge two configuration dictionaries"""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = merge_configs(result[key], value)
        else:
            result[key] = value
    return result

def apply_cli_overrides(config, args):
    """Apply CLI arguments to config"""
    # Model settings
    if args.preprocessor_provider:
        config['models']['preprocessor']['provider'] = args.preprocessor_provider
    if args.preprocessor_model:
        config['models']['preprocessor']['model_name'] = args.preprocessor_model
    if args.expert_provider:
        config['models']['expert']['provider'] = args.expert_provider
    if args.expert_model:
        config['models']['expert']['model_name'] = args.expert_model
    if args.tutor_provider:
        config['models']['tutor']['provider'] = args.tutor_provider
    if args.tutor_model:
        config['models']['tutor']['model_name'] = args.tutor_model
    if args.student_provider:
        config['models']['student']['provider'] = args.student_provider
    if args.student_model:
        config['models']['student']['model_name'] = args.student_model
    
    # Paths
    if args.student_project:
        config['paths']['student_project'] = args.student_project
    if args.code_review_file:
        config['paths']['code_review_file'] = args.code_review_file
    
    # Stages: parse skip flags
    skip_stages = []
    if args.skip_val_stage:
        if 't' in args.skip_val_stage:
            skip_stages.append('t')
        if 's' in args.skip_val_stage:
            skip_stages.append('s')
        config.setdefault('stages', {})
        config['stages']['skip_validation'] = skip_stages
    
    # Important: by default we keep tutor/student disabled (isolated).
    # Enable only if CLI flag provided and not explicitly skipped.
    config.setdefault('stages', {})
    # If user passed --enable-tutor, enable unless explicitly skipped by skip_val_stage
    if getattr(args, 'enable_tutor', False):
        if 't' not in skip_stages:
            config['stages']['enable_tutor'] = True
    # If user passed --enable-student, enable unless explicitly skipped by skip_val_stage
    if getattr(args, 'enable_student', False):
        if 's' not in skip_stages:
            config['stages']['enable_student'] = True
    
    # Expert settings
    if args.expert_max_attempts is not None:
        config['expert']['max_attempts'] = args.expert_max_attempts
    if args.expert_context_max_symbols is not None:
        config['expert']['context_max_symbols'] = args.expert_context_max_symbols
    if args.expert_context_comment_margin is not None:
        config['expert']['context_comment_margin'] = args.expert_context_comment_margin
    if args.expert_context_add_rest:
        config['expert']['context_add_rest'] = True
    
    # Tutor settings
    if args.tutor_max_solution_attempts is not None:
        config['tutor']['max_solution_attempts'] = args.tutor_max_solution_attempts
    if args.tutor_acceptance_threshold is not None:
        config['tutor']['acceptance_threshold'] = args.tutor_acceptance_threshold
    
    # Student settings
    if args.num_students is not None:
        config['student']['num_students'] = args.num_students
    if args.student_comprehension_threshold is not None:
        config['student']['comprehension_threshold'] = args.student_comprehension_threshold
    
    return config

def validate_config(config):
    """Validate that required configuration fields are present"""
    required_fields = [
        ('paths', 'student_project'),
        ('paths', 'code_review_file')
    ]
    
    for field_path in required_fields:
        current = config
        for key in field_path:
            if key not in current:
                raise ValueError(f"Требуемое поле конфигурации {'.'.join(field_path)} отсутствует")
            current = current[key]
        if not current:
            raise ValueError(f"Требуемое поле конфигурации {'.'.join(field_path)} пустое")

def create_llm(model_config):
    """Create LLM instance based on configuration"""
    script_dir = Path(__file__).parent
    root_dir = script_dir.parent
    load_dotenv(root_dir / ".env")
    
    provider = model_config['provider']
    model_name = model_config['model_name']
    
    if provider == 'yandex':
        api_key = os.getenv("YANDEX_API_KEY")
        folder_id = os.getenv("YANDEX_FOLDER_ID")
        
        if not api_key or not folder_id:
            raise ValueError("YANDEX_API_KEY или YANDEX_FOLDER_ID не найдены в файле .env")
        
        return YandexGPT(
            api_key=api_key,
            folder_id=folder_id,
            model_name=model_name
        )
    
    elif provider == 'openai':
        api_key = os.getenv("OPENAI_API_KEY")
        base_url = os.getenv("OPENAI_BASE_URL")
        
        if not api_key:
            raise ValueError("OPENAI_API_KEY не найден в файле .env")
        
        kwargs = {
            "api_key": api_key,
            "model": model_name
        }
        if base_url:
            kwargs["base_url"] = base_url
        
        return ChatOpenAI(**kwargs)
    
    else:
        raise ValueError(f"Неподдерживаемый провайдер: {provider}")

def setup_session_directory(config):
    """Create session directory and save config"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_name = f"{config['output']['session_prefix']}_{timestamp}"
    
    root_dir = Path(__file__).parent.parent
    session_dir = root_dir / config['output']['base_output_dir'] / session_name
    session_dir.mkdir(parents=True, exist_ok=True)
    
    # Save the actual config used for this run to session directory
    config_session_path = session_dir / "config_used.yml"
    with open(config_session_path, 'w') as f:
        yaml.dump(config, f, default_flow_style=False)
    
    return session_dir

def run_pipeline(config=None, session_dir=None):
    """Run the pipeline - can be called externally or from main()"""
    logger = get_logger()
    
    try:
        # Setup session directory if not provided
        if session_dir is None:
            session_dir = setup_session_directory(config)
        
        # Create LLM instances
        preprocessor_llm = create_llm(config['models']['preprocessor'])
        expert_llm = create_llm(config['models']['expert'])
        
        # Tutor/Student LLMs and stages: create only if explicitly enabled in config (default: disabled)
        tutor_llm = None
        student_llm = None
        if config.get('stages', {}).get('enable_tutor', False):
            tutor_llm = create_llm(config['models']['tutor'])
        if config.get('stages', {}).get('enable_student', False):
            student_llm = create_llm(config['models']['student'])
        
        logger.info(f"Каталог сессии: {session_dir}")
        logger.info(f"Модель препроцессинга: {config['models']['preprocessor']['provider']}/{config['models']['preprocessor']['model_name']}")
        logger.info(f"Модель эксперта: {config['models']['expert']['provider']}/{config['models']['expert']['model_name']}")
        if tutor_llm:
            logger.info(f"Модель наставника: {config['models']['tutor']['provider']}/{config['models']['tutor']['model_name']}")
        else:
            logger.info("Этап наставника ОТКЛЮЧЁН (используйте --enable-tutor, чтобы включить).")
        if student_llm:
            logger.info(f"Модель студента: {config['models']['student']['provider']}/{config['models']['student']['model_name']}")
        else:
            logger.info("Этап студента ОТКЛЮЧЁН (используйте --enable-student, чтобы включить).")
        
        # Initialize stages
        preprocessing_stage = PreprocessingStage(config, session_dir, preprocessor_llm)
        expert_stage = ExpertStage(config, session_dir, expert_llm)
        tutor_stage = TutorStage(config, session_dir, tutor_llm) if tutor_llm else None
        student_stage = StudentStage(config, session_dir, student_llm) if student_llm else None
        
        # Execute pipeline
        logger.stage_start("препроцессинг")
        deduplicated_review_file = preprocessing_stage.run()
        
        logger.stage_start("эксперт")
        expert_results = expert_stage.run(deduplicated_review_file)
        
        tutor_results = None
        if tutor_stage:
            logger.stage_start("наставник")
            tutor_results = tutor_stage.run(expert_results)
        
        student_results = None
        if student_stage:
            logger.stage_start("студент")
            student_results = student_stage.run(expert_results, tutor_results)
        
        # Generate final report
        logger.stage_start("генерация отчёта")
        generate_final_report(config, session_dir, expert_results, tutor_results, student_results)
        
        logger.success(f"Пайплайн завершён. Результаты сохранены в: {session_dir}")
        
        return {
            'session_dir': session_dir,
            'expert_results': expert_results,
            'tutor_results': tutor_results,
            'student_results': student_results
        }
        
    except Exception as e:
        logger.error(f"Сбой пайплайна: {e}")
        raise

def main():
    try:
        # Parse args once (we pass them into load_config to avoid double parsing)
        args = parse_args()

        # Load configuration
        config, _ = load_config(args)
        
        # Setup session directory
        session_dir = setup_session_directory(config)
        
        # Initialize logger with session directory
        init_logger(session_dir, console_output=True)
        
        # Run the pipeline
        run_pipeline(config, session_dir)
        
    except Exception as e:
        logger = get_logger()
        logger.error(f"Сбой пайплайна: {e}")
        sys.exit(1)

def generate_final_report(config, session_dir, expert_results, tutor_results, student_results):
    """Generate final script report"""
    logger = get_logger()
    report = []
    
    for comment_id, expert_result in expert_results.items():
        tutor_result = tutor_results.get(comment_id) if tutor_results else None
        student_result = student_results.get(comment_id) if student_results else None
        
        # Determine acceptance: base on expert success; tutor/student can veto
        accepted = bool(expert_result.get('success', False))
        if tutor_result and not tutor_result.get('accepted', True):
            accepted = False
        if student_result and not student_result.get('accepted', True):
            accepted = False
        
        report_entry = {
            "comment_id": comment_id,
            "source_file_path": expert_result['source_file_path'],
            "source_line_number": expert_result['source_line_number'],
            "accepted": accepted,
            "pass_ratio": student_result['pass_ratio'] if student_result else None,
            "tutor_review": tutor_result['review'] if tutor_result else None,
            "tutor_score": tutor_result['score'] if tutor_result else None,
            "attempts_tutor": tutor_result['attempts'] if tutor_result else 0,
            "attempts_expert": expert_result['attempts'],
            "stage_duration": {
                "expert": expert_result['duration'],
                "tutor": tutor_result['duration'] if tutor_result else {"total": 0, "avg": 0, "attempts": []},
                "student": student_result['duration'] if student_result else {"total": 0, "avg": 0, "attempts": []}
            },
            "students_failed": student_result.get('failed_students', []) if student_result else [],
            "students_passed": student_result.get('passed_students', []) if student_result else []
        }
        
        report.append(report_entry)
    
    # Save report
    report_path = session_dir / "script_report.json"
    with open(report_path, 'w') as f:
        json.dump(report, f, indent=2)
    
    # Print summary
    total_comments = len(report)
    accepted_comments = sum(1 for entry in report if entry['accepted'])
    logger.summary(f"Всего обработано комментариев: {total_comments}")
    logger.summary(f"Принято микро-кейсов: {accepted_comments}")
    logger.summary(f"Доля принятых: {accepted_comments/total_comments*100:.1f}%")

if __name__ == "__main__":
    main()
