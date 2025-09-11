#!/usr/bin/env python3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, Union
from contextlib import contextmanager

class PipelineLogger:
    """Unified logging utility for PyTaskSyn pipeline with emoji support and file output"""
    
    EMOJI_MAP = {
        'info': '📍',
        'success': '✅',
        'warning': '⚠️',
        'error': '❌',
        'processing': '🔄',
        'test_pass': '✅',
        'test_fail': '❌',
        'stage_start': '🚀',
        'stage_complete': '🏁',
        'attempt': '🔄',
        'generation': '🤖',
        'validation': '🔍',
        'summary': '📊'
    }
    
    def __init__(self, session_dir: Optional[Path] = None, console_output: bool = True):
        self.session_dir = session_dir
        self.console_output = console_output
        self.log_files: Dict[str, Path] = {}
        
        if session_dir:
            self.main_log_path = session_dir / "pipeline.log"
            self.log_files['main'] = self.main_log_path
            
            # Ensure session directory exists
            session_dir.mkdir(parents=True, exist_ok=True)
    
    def _get_timestamp(self) -> str:
        """Get formatted timestamp"""
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    def _format_message(self, level: str, message: str, use_emoji: bool = True) -> str:
        """Format log message with timestamp, emoji and level"""
        timestamp = self._get_timestamp()
        emoji = self.EMOJI_MAP.get(level, '') if use_emoji else ''
        emoji_part = f"{emoji} " if emoji else ""
        return f"[{timestamp}] {emoji_part}{message}"
    
    def _write_to_file(self, log_type: str, message: str):
        """Write message to log file"""
        if self.session_dir and log_type in self.log_files:
            try:
                with open(self.log_files[log_type], 'a', encoding='utf-8') as f:
                    f.write(message + '\n')
            except Exception as e:
                if self.console_output:
                    print(f"Warning: Failed to write to log file: {e}")
    
    def log(self, level: str, message: str, log_type: str = 'main', console_emoji: bool = True):
        """Generic log method"""
        formatted_msg = self._format_message(level, message, use_emoji=console_emoji)
        
        if self.console_output:
            print(formatted_msg)
        
        # Write to file without emoji for cleaner file logs
        file_msg = self._format_message(level, message, use_emoji=False)
        self._write_to_file(log_type, file_msg)
    
    def info(self, message: str, log_type: str = 'main'):
        """Log info message"""
        self.log('info', message, log_type)
    
    def success(self, message: str, log_type: str = 'main'):
        """Log success message"""
        self.log('success', message, log_type)
    
    def warning(self, message: str, log_type: str = 'main'):
        """Log warning message"""
        self.log('warning', message, log_type)
    
    def error(self, message: str, log_type: str = 'main'):
        """Log error message"""
        self.log('error', message, log_type)
    
    def processing(self, message: str, log_type: str = 'main'):
        """Log processing message"""
        self.log('processing', message, log_type)
    
    def stage_start(self, stage_name: str):
        """Log stage start"""
        self.log('stage_start', f"{stage_name.upper()} STAGE", 'main')
    
    def stage_complete(self, stage_name: str, stats: Optional[Dict] = None):
        """Log stage completion with optional stats"""
        msg = f"{stage_name.upper()} STAGE COMPLETED"
        if stats:
            stats_str = ", ".join([f"{k}: {v}" for k, v in stats.items()])
            msg += f" ({stats_str})"
        self.log('stage_complete', msg, 'main')
    
    def test_result(self, passed: bool, test_name: str = "", details: str = "", log_type: str = 'main'):
        """Log test result"""
        level = 'test_pass' if passed else 'test_fail'
        status = "PASSED" if passed else "FAILED"
        msg = f"Test {status}"
        if test_name:
            msg += f": {test_name}"
        if details:
            msg += f" - {details}"
        self.log(level, msg, log_type)
    
    def attempt_log(self, attempt_num: int, max_attempts: int, message: str, log_type: str = 'main'):
        """Log attempt information"""
        self.log('attempt', f"Attempt {attempt_num}/{max_attempts}: {message}", log_type)
    
    def generation_log(self, what: str, status: str = "", log_type: str = 'main'):
        """Log generation activities"""
        msg = f"Generating {what}"
        if status:
            msg += f" - {status}"
        self.log('generation', msg, log_type)
    
    def validation_log(self, what: str, result: str, log_type: str = 'main'):
        """Log validation activities"""
        self.log('validation', f"Validating {what}: {result}", log_type)
    
    def summary(self, message: str, log_type: str = 'main'):
        """Log summary information"""
        self.log('summary', message, log_type)
    
    def create_attempt_logger(self, attempt_dir: Path, attempt_num: int) -> 'AttemptLogger':
        """Create a logger for a specific attempt"""
        return AttemptLogger(self, attempt_dir, attempt_num)
    
    def setup_log_file(self, log_type: str, file_path: Path):
        """Setup additional log file"""
        self.log_files[log_type] = file_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
    
    @contextmanager
    def timed_operation(self, operation_name: str, log_type: str = 'main'):
        """Context manager for timing operations"""
        start_time = time.time()
        self.processing(f"Starting {operation_name}", log_type)
        try:
            yield
            duration = time.time() - start_time
            self.success(f"Completed {operation_name} in {duration:.2f}s", log_type)
        except Exception as e:
            duration = time.time() - start_time
            self.error(f"Failed {operation_name} after {duration:.2f}s: {str(e)}", log_type)
            raise


class AttemptLogger:
    """Logger for individual attempts with detailed logging to attempt-specific files"""
    
    def __init__(self, parent_logger: PipelineLogger, attempt_dir: Path, attempt_num: int):
        self.parent = parent_logger
        self.attempt_dir = attempt_dir
        self.attempt_num = attempt_num
        
        # Create attempt-specific log files
        attempt_dir.mkdir(parents=True, exist_ok=True)
        self.attempt_log = attempt_dir / f"attempt_{attempt_num}.log"
        self.test_log = attempt_dir / f"test_results_{attempt_num}.log"
        self.generation_log = attempt_dir / f"generation_{attempt_num}.log"
        
        # Setup log files in parent
        self.parent.setup_log_file(f'attempt_{attempt_num}', self.attempt_log)
        self.parent.setup_log_file(f'test_{attempt_num}', self.test_log)
        self.parent.setup_log_file(f'generation_{attempt_num}', self.generation_log)
    
    def log_generation(self, component: str, status: str, details: str = ""):
        """Log generation details to both console (brief) and file (detailed)"""
        # Brief console log
        self.parent.generation_log(f"{component} ({status})", log_type='main')
        
        # Detailed file log
        detailed_msg = f"Generated {component}: {status}"
        if details:
            detailed_msg += f"\nDetails: {details}"
        self.parent.log('generation', detailed_msg, f'generation_{self.attempt_num}', console_emoji=False)
    
    def log_test_run(self, test_file: str, stdout: str, stderr: str, returncode: int):
        """Log detailed test execution results to file only"""
        passed = returncode == 0
        
        # Brief console log
        self.parent.test_result(passed, test_file)
        
        # Detailed file log
        detailed_log = f"""
Test Execution: {test_file}
Return Code: {returncode}
Status: {'PASSED' if passed else 'FAILED'}

=== STDOUT ===
{stdout}

=== STDERR ===
{stderr}

=== END TEST LOG ===
"""
        # Write directly to test log file
        with open(self.test_log, 'a', encoding='utf-8') as f:
            f.write(f"[{self.parent._get_timestamp()}] {detailed_log}\n")
    
    def log_validation(self, what: str, passed: bool, details: str = ""):
        """Log validation with details"""
        result = "PASSED" if passed else "FAILED" 
        self.parent.validation_log(what, result)
        
        if details:
            detailed_msg = f"Validation {what}: {result}\nDetails: {details}"
            self.parent.log('validation', detailed_msg, f'attempt_{self.attempt_num}', console_emoji=False)
    
    def error(self, message: str):
        """Log attempt-specific error"""
        self.parent.error(f"Attempt {self.attempt_num}: {message}", f'attempt_{self.attempt_num}')
    
    def info(self, message: str):
        """Log attempt-specific info"""
        self.parent.info(f"Attempt {self.attempt_num}: {message}", f'attempt_{self.attempt_num}')


# Global logger instance - will be initialized by main.py
logger: Optional[PipelineLogger] = None

def init_logger(session_dir: Optional[Path] = None, console_output: bool = True) -> PipelineLogger:
    """Initialize global logger"""
    global logger
    logger = PipelineLogger(session_dir, console_output)
    return logger

def get_logger() -> PipelineLogger:
    """Get global logger instance"""
    global logger
    if logger is None:
        # Fallback to console-only logger
        logger = PipelineLogger(console_output=True)
    return logger