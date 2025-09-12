import os
import json
import sys
import time
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, Any, Optional
from langchain.prompts import PromptTemplate
from langchain_core.output_parsers import StrOutputParser
from pytasksyn.utils.logging_utils import get_logger


class TutorStage:
    def __init__(self, config: Dict[str, Any], session_dir: Path, tutor_llm):
        self.config = config
        self.session_dir = session_dir
        self.tutor_llm = tutor_llm
        self.parser = StrOutputParser()
    
    def run(self, expert_results: Dict[int, Dict]) -> Dict[int, Dict]:
        """Run the tutor stage to validate microcases using expert outputs"""
        logger = get_logger()
        logger.processing("Starting tutor stage (tutor)")
        
        # Load deduplicated CSV (for fallback microcase text and metadata)
        id_to_row: Dict[int, Dict[str, Any]] = {}
        try:
            dedup_csv = self.session_dir / "preprocess" / "code_review_deduplicated.csv"
            if dedup_csv.exists():
                import csv as _csv
                with open(dedup_csv, 'r', encoding='utf-8') as f:
                    reader = _csv.DictReader(f)
                    for row in reader:
                        try:
                            cid = int(row.get('comment_id', '0'))
                        except Exception:
                            continue
                        id_to_row[cid] = row
        except Exception:
            id_to_row = {}

        results = {}
        successful_validations = 0
        
        for comment_id, expert_result in expert_results.items():
            if not expert_result.get('success'):
                logger.info(f"Tutor: skipping comment {comment_id} (expert stage failed)")
                continue
            
            logger.info(f"Tutor: validating comment {comment_id}")
            result = self._validate_microcase(comment_id, expert_result, id_to_row.get(comment_id))
            results[comment_id] = result
            
            if result['accepted']:
                successful_validations += 1
                logger.success(f"Tutor: accepted (score: {result['score']:.2f})")
            else:
                logger.warning(f"Tutor: rejected (score: {result['score']:.2f})")
        
        total_validated = len([r for r in expert_results.values() if r['success']])
        logger.stage_complete("tutor", {"accepted": f"{successful_validations}/{total_validated}"})
        
        return results
    
    def _validate_microcase(self, comment_id: int, expert_result: Dict, dedup_row: Optional[Dict]) -> Dict:
        """Validate a microcase by generating tutor solution and review"""
        comment_dir = self.session_dir / f"comment_{comment_id}"
        expert_attempt_dir = Path(expert_result['successful_attempt_dir'])
        
        result = {
            'comment_id': comment_id,
            'accepted': False,
            'attempts': 0,
            'score': 0.0,
            'review': '',
            'duration': {'total': 0, 'avg': 0, 'attempts': []},
            'successful_attempt_dir': None
        }
        
        logger = get_logger()
        # Load microcase description (prefer CSV row if available, fallback to generated microcase.txt)
        microcase: str = ""
        if dedup_row is not None:
            microcase = (dedup_row.get('microcase')
                         or dedup_row.get('comment')
                         or "").strip()
        if not microcase:
            try:
                microcase = (expert_attempt_dir / "microcase.txt").read_text(encoding='utf-8')
            except Exception:
                microcase = ""
        if not microcase:
            logger.error(f"Tutor: microcase text not found for comment {comment_id}")
            return result

        # Load embedded source file (file with embedded comments)
        embedded_source_text: Optional[str] = None
        try:
            embedded_path = self.session_dir / "embedded_source" / expert_result['source_file_path']
            if embedded_path.exists():
                embedded_source_text = embedded_path.read_text(encoding='utf-8')
        except Exception:
            embedded_source_text = None
        
        try:
            max_attempts = int((self.config.get('tutor') or {}).get('max_solution_attempts', 3))
            # Enforce minimum 0.5 as per requirements
            acceptance_threshold = max(0.5, float((self.config.get('tutor') or {}).get('acceptance_threshold', 0.5)))
        except Exception as e:
            logger.error(f"Tutor: missing tutor config (max_solution_attempts/acceptance_threshold): {e}")
            return result
        
        for attempt in range(max_attempts):
            start_time = time.time()
            
            attempt_dir = comment_dir / "tutor_output" / f"attempt_{attempt}"
            attempt_dir.mkdir(parents=True, exist_ok=True)
            
            # Create attempt logger similar to expert stage
            attempt_logger = logger.create_attempt_logger(attempt_dir, attempt)
            logger.attempt_log(attempt + 1, max_attempts, f"Validating comment {comment_id}")
            
            success = self._generate_tutor_validation(microcase, embedded_source_text, expert_attempt_dir, attempt_dir, result, attempt_logger)
            
            end_time = time.time()
            duration = int(end_time - start_time)
            result['duration']['attempts'].append(duration)
            result['attempts'] = attempt + 1
            
            if success and result['score'] >= acceptance_threshold:
                result['accepted'] = True
                result['successful_attempt_dir'] = str(attempt_dir)
                break
        
        # Calculate duration stats
        result['duration']['total'] = sum(result['duration']['attempts'])
        if result['duration']['attempts']:
            result['duration']['avg'] = result['duration']['total'] // len(result['duration']['attempts'])
        
        return result
    
    def _generate_tutor_validation(self, microcase: str, embedded_source: Optional[str], expert_attempt_dir: Path, 
                                 attempt_dir: Path, result: Dict, attempt_logger) -> bool:
        """Generate tutor solution and review for one validation attempt"""
        logger = get_logger()
        try:
            # Generate tutor solution
            attempt_logger.log_generation("tutor solution", "starting")
            tutor_solution = self._generate_tutor_solution(microcase, embedded_source)
            if not tutor_solution:
                attempt_logger.log_generation("tutor solution", "failed", "Empty response from LLM")
                logger.warning("Tutor: empty solution generated")
                return False
            
            # Save tutor solution
            solution_file = attempt_dir / "solution_tutor.py"
            with open(solution_file, 'w', encoding='utf-8') as f:
                f.write(tutor_solution)
            
            # Verify tutor solution passes expert tests
            passed, test_stdout, test_stderr = self._verify_tutor_solution_detailed(expert_attempt_dir, solution_file, attempt_dir)
            attempt_logger.log_test_run("tutor_vs_expert_tests", test_stdout, test_stderr, 0 if passed else 1)
            if not passed:
                logger.warning("Tutor: solution failed expert tests")
                return False
            
            # Generate educational review
            attempt_logger.log_generation("tutor review", "starting")
            review_data = self._generate_educational_review(microcase, attempt_dir)
            if not review_data:
                attempt_logger.log_generation("tutor review", "failed", "Parsing or generation error")
                return False
            
            # Save review
            review_file = attempt_dir / "tutor_review.json"
            with open(review_file, 'w', encoding='utf-8') as f:
                json.dump(review_data, f, indent=2)
            
            # Update result
            result['score'] = review_data['score']
            result['review'] = review_data['review']
            attempt_logger.log_generation("tutor review", "success")
            
            return True
            
        except Exception as e:
            logger.error(f"Tutor: validation attempt failed: {e}")
            return False
    
    def _generate_tutor_solution(self, microcase: str, embedded_source: Optional[str]) -> str:
        """Generate tutor's solution to verify microcase is solvable"""
        prompt_template = """As an educational tutor, solve this programming microcase to verify it's solvable and educational.

Microcase:
{microcase}

Embedded source with review comments (for understanding only, do not copy domain specifics):
{embedded_source}

Provide a complete, correct Python implementation that would pass the given tests.
Only output valid Python code, no explanations."""
        
        prompt = PromptTemplate(
            template=prompt_template,
            input_variables=["microcase", "embedded_source"]
        )
        
        chain = prompt | self.tutor_llm | self.parser
        response = chain.invoke({"microcase": microcase, "embedded_source": (embedded_source or "")})
        
        return response.strip()
    
    def _verify_tutor_solution(self, expert_attempt_dir: Path, tutor_solution_file: Path, attempt_dir: Path) -> bool:
        """Verify tutor solution passes the expert's test suite"""
        logger = get_logger()
        try:
            expert_tests_dir = expert_attempt_dir / "tests"
            if not tutor_solution_file.exists() or not expert_tests_dir.exists():
                return False

            # Make tutor solution available under expected name for tests
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)
                alias_path = temp_path / "solution_expert.py"
                alias_path.write_text(tutor_solution_file.read_text(encoding='utf-8'), encoding='utf-8')

                env = os.environ.copy()
                env["PYTHONPATH"] = f"{str(temp_path)}{os.pathsep}{env.get('PYTHONPATH', '')}"

                result = subprocess.run([
                    sys.executable, "-m", "pytest", "-q", "tests/"
                ], cwd=expert_attempt_dir, env=env, capture_output=True, text=True)

                if result.returncode != 0:
                    logger.test_result(False, "tutor_solution_vs_expert_tests")
                    # Write detailed logs next to attempt_dir for easier debugging
                    try:
                        (attempt_dir / "test_stdout.txt").write_text(result.stdout or "", encoding='utf-8')
                        (attempt_dir / "test_stderr.txt").write_text(result.stderr or "", encoding='utf-8')
                    except Exception:
                        pass
                else:
                    logger.test_result(True, "tutor_solution_vs_expert_tests")

                return result.returncode == 0
                
        except Exception as e:
            logger.error(f"Tutor: error verifying solution: {e}")
            return False

    def _verify_tutor_solution_detailed(self, expert_attempt_dir: Path, tutor_solution_file: Path, attempt_dir: Path) -> tuple[bool, str, str]:
        """Verify tutor solution with detailed stdout/stderr for logging"""
        logger = get_logger()
        try:
            expert_tests_dir = expert_attempt_dir / "tests"
            if not tutor_solution_file.exists() or not expert_tests_dir.exists():
                return False, "", "Solution or tests directory not found"

            with tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)
                alias_path = temp_path / "solution_expert.py"
                alias_path.write_text(tutor_solution_file.read_text(encoding='utf-8'), encoding='utf-8')

                env = os.environ.copy()
                env["PYTHONPATH"] = f"{str(temp_path)}{os.pathsep}{env.get('PYTHONPATH', '')}"

                result = subprocess.run([
                    sys.executable, "-m", "pytest", "-v", "tests/"
                ], cwd=expert_attempt_dir, env=env, capture_output=True, text=True)

                success = result.returncode == 0
                if not success:
                    try:
                        (attempt_dir / "test_stdout.txt").write_text(result.stdout or "", encoding='utf-8')
                        (attempt_dir / "test_stderr.txt").write_text(result.stderr or "", encoding='utf-8')
                    except Exception:
                        pass
                logger.test_result(success, "tutor_solution_vs_expert_tests")
                return success, result.stdout, result.stderr
        except Exception as e:
            logger.error(f"Tutor: error verifying solution (detailed): {e}")
            return False, "", f"Error verifying solution: {e}"
    
    def _generate_educational_review(self, microcase: str, attempt_dir: Path) -> Optional[Dict]:
        """Generate educational review and scoring of the microcase"""
        prompt_template = """As an educational tutor, evaluate this microcase for learning effectiveness.

Microcase:
{microcase}

Rate this microcase on a scale of 0.0 to 1.0 based on how well it helps students learn from the original programming mistake.

Consider these criteria:
- Does it illustrate the general programming principle behind the mistake?
- Does it clearly show why the original approach was problematic?
- Is the microcase educational and appropriately challenging?
- Is it focused and solvable within reasonable scope?
- Does it provide good learning value for students?

Respond with valid JSON containing exactly two keys:
- 'score': a float between 0.0 and 1.0
- 'review': a string explaining your reasoning for the score

JSON Response:"""
        
        prompt = PromptTemplate(
            template=prompt_template,
            input_variables=["microcase"]
        )
        
        chain = prompt | self.tutor_llm | self.parser
        response = chain.invoke({"microcase": microcase})
        
        logger = get_logger()
        try:
            # Try to parse as JSON
            parsed = self._robust_parse_review_json(response)
            if parsed is None:
                raise ValueError("Failed to parse JSON after cleanup")
            return parsed
            
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            logger.warning(f"Tutor: failed to parse review JSON: {e}")
            try:
                (attempt_dir / "tutor_review_raw.txt").write_text(response, encoding='utf-8')
            except Exception:
                pass
            
            # Fallback: extract score from text if possible
            try:
                # Simple heuristic parsing
                lines = response.lower().split('\\n')
                score = 0.0
                
                for line in lines:
                    if 'score' in line:
                        # Try to extract number from line
                        import re
                        numbers = re.findall(r'\\d+\\.\\d+|\\d+', line)
                        if numbers:
                            potential_score = float(numbers[0])
                            if 0.0 <= potential_score <= 1.0:
                                score = potential_score
                                break
                            elif potential_score > 1.0 and potential_score <= 10.0:
                                # Maybe it's on a 1-10 scale
                                score = potential_score / 10.0
                                break
                
                return {
                    'score': score,
                    'review': f"Fallback parsing. Raw response: {response}"
                }
                
            except Exception:
                # Ultimate fallback
                return {
                    'score': 0.0,
                    'review': f"Failed to parse review. Raw response: {response}"
                }

    def _robust_parse_review_json(self, response_text: str) -> Optional[Dict[str, Any]]:
        """Attempt to robustly parse LLM JSON with common cleanup steps."""
        text = (response_text or "").strip()
        if not text:
            return None
        import re
        # Remove markdown code fences
        text = re.sub(r"^```[a-zA-Z]*\n|```$", "", text.strip())
        text = text.replace("```", "").strip()

        # If the text contains extra prose, extract first balanced JSON object
        def extract_first_json_object(s: str) -> Optional[str]:
            start = -1
            depth = 0
            for i, ch in enumerate(s):
                if ch == '{':
                    if depth == 0:
                        start = i
                    depth += 1
                elif ch == '}':
                    if depth > 0:
                        depth -= 1
                        if depth == 0 and start != -1:
                            return s[start:i+1]
            return None

        candidates = []
        # 1) direct
        candidates.append(text)
        # 2) first balanced object
        balanced = extract_first_json_object(text)
        if balanced:
            candidates.append(balanced)
        # 3) naive substring between first { and last }
        if '{' in text and '}' in text:
            try:
                first = text.index('{')
                last = text.rindex('}')
                candidates.append(text[first:last+1])
            except Exception:
                pass

        # Cleanup single quotes to double quotes cautiously if no double quotes present
        def normalize_quotes(s: str) -> str:
            if '"' not in s and "'" in s:
                return s.replace("'", '"')
            return s

        # Try parse candidates
        for cand in candidates:
            try:
                obj = json.loads(cand)
                parsed = self._normalize_review_obj(obj)
                if parsed is not None:
                    return parsed
            except Exception:
                try:
                    cand2 = normalize_quotes(cand)
                    obj = json.loads(cand2)
                    parsed = self._normalize_review_obj(obj)
                    if parsed is not None:
                        return parsed
                except Exception:
                    continue
        return None

    def _normalize_review_obj(self, obj: Any) -> Optional[Dict[str, Any]]:
        """Validate and normalize parsed JSON to required schema."""
        if not isinstance(obj, dict):
            return None
        score_val = obj.get('score')
        if score_val is None:
            # Try alternative key names
            score_val = obj.get('rating') or obj.get('grade') or obj.get('score_value')
        review_val = obj.get('review')
        if review_val is None:
            review_val = obj.get('feedback') or obj.get('fedback') or obj.get('comment') or obj.get('reason') or obj.get('explanation')
        if score_val is None or review_val is None:
            return None
        try:
            score = float(score_val)
        except Exception:
            # Extract first number if embedded like "0.8/1"
            import re
            m = re.search(r"\d+\.\d+|\d+", str(score_val))
            if not m:
                return None
            score = float(m.group(0))
        # Normalize to 0..1 if looks like 0..100 scale
        if score > 1.0 and score <= 100.0:
            score = score / 100.0
        if not (0.0 <= score <= 1.0):
            return None
        return {'score': score, 'review': str(review_val)}