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


class TutorStage:
    def __init__(self, config: Dict[str, Any], session_dir: Path, tutor_llm):
        self.config = config
        self.session_dir = session_dir
        self.tutor_llm = tutor_llm
        self.parser = StrOutputParser()
    
    def run(self, expert_results: Dict[int, Dict]) -> Dict[int, Dict]:
        """Run the tutor stage to validate microcases"""
        print("Starting tutor stage...")
        
        results = {}
        successful_validations = 0
        
        for comment_id, expert_result in expert_results.items():
            if not expert_result['success']:
                print(f"  Skipping comment {comment_id} (expert stage failed)")
                continue
            
            print(f"  Validating comment {comment_id}")
            result = self._validate_microcase(comment_id, expert_result)
            results[comment_id] = result
            
            if result['accepted']:
                successful_validations += 1
                print(f"    ✓ Microcase accepted (score: {result['score']:.2f})")
            else:
                print(f"    ✗ Microcase rejected (score: {result['score']:.2f})")
        
        total_validated = len([r for r in expert_results.values() if r['success']])
        print(f"Tutor stage completed: {successful_validations}/{total_validated} microcases accepted")
        
        return results
    
    def _validate_microcase(self, comment_id: int, expert_result: Dict) -> Dict:
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
        
        # Load microcase description
        microcase_file = expert_attempt_dir / "microcase.txt"
        with open(microcase_file, 'r', encoding='utf-8') as f:
            microcase = f.read()
        
        max_attempts = self.config['tutor']['max_solution_attempts']
        acceptance_threshold = self.config['tutor']['acceptance_threshold']
        
        for attempt in range(max_attempts):
            start_time = time.time()
            
            attempt_dir = comment_dir / "tutor_output" / f"attempt_{attempt}"
            attempt_dir.mkdir(parents=True, exist_ok=True)
            
            success = self._generate_tutor_validation(microcase, expert_attempt_dir, attempt_dir, result)
            
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
    
    def _generate_tutor_validation(self, microcase: str, expert_attempt_dir: Path, 
                                 attempt_dir: Path, result: Dict) -> bool:
        """Generate tutor solution and review for one validation attempt"""
        try:
            # Generate tutor solution
            tutor_solution = self._generate_tutor_solution(microcase)
            if not tutor_solution:
                return False
            
            # Save tutor solution
            solution_file = attempt_dir / "solution_tutor.py"
            with open(solution_file, 'w', encoding='utf-8') as f:
                f.write(tutor_solution)
            
            # Verify tutor solution passes expert tests
            if not self._verify_tutor_solution(expert_attempt_dir, solution_file):
                print(f"      Tutor solution failed expert tests")
                return False
            
            # Generate educational review
            review_data = self._generate_educational_review(microcase)
            if not review_data:
                return False
            
            # Save review
            review_file = attempt_dir / "tutor_review.json"
            with open(review_file, 'w', encoding='utf-8') as f:
                json.dump(review_data, f, indent=2)
            
            # Update result
            result['score'] = review_data['score']
            result['review'] = review_data['review']
            
            return True
            
        except Exception as e:
            print(f"      Tutor validation attempt failed: {e}")
            return False
    
    def _generate_tutor_solution(self, microcase: str) -> str:
        """Generate tutor's solution to verify microcase is solvable"""
        prompt_template = """As an educational tutor, solve this programming microcase to verify it's solvable and educational.

Microcase:
{microcase}

Provide a complete, well-structured Python solution that demonstrates best practices.
Focus on clarity and educational value:"""
        
        prompt = PromptTemplate(
            template=prompt_template,
            input_variables=["microcase"]
        )
        
        chain = prompt | self.tutor_llm | self.parser
        response = chain.invoke({"microcase": microcase})
        
        return response.strip()
    
    def _verify_tutor_solution(self, expert_attempt_dir: Path, tutor_solution_file: Path) -> bool:
        """Verify tutor solution passes the expert's test suite"""
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
                    sys.executable, "-m", "pytest", "-v", "tests/"
                ], cwd=expert_attempt_dir, env=env, capture_output=True, text=True)

                return result.returncode == 0
                
        except Exception as e:
            print(f"      Error verifying tutor solution: {e}")
            return False
    
    def _generate_educational_review(self, microcase: str) -> Optional[Dict]:
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
        
        try:
            # Try to parse as JSON
            review_data = json.loads(response.strip())
            
            # Validate structure
            if 'score' not in review_data or 'review' not in review_data:
                raise ValueError("Missing required keys in review data")
            
            # Ensure score is a valid float between 0 and 1
            score = float(review_data['score'])
            if not (0.0 <= score <= 1.0):
                raise ValueError(f"Score {score} is not between 0.0 and 1.0")
            
            review_data['score'] = score
            return review_data
            
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            print(f"      Failed to parse tutor review JSON: {e}")
            print(f"      Raw response: {response}")
            
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