import os
import sys
import time
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, Any, List, Optional
from langchain.prompts import PromptTemplate
from langchain_core.output_parsers import StrOutputParser


class StudentStage:
    def __init__(self, config: Dict[str, Any], session_dir: Path, student_llm):
        self.config = config
        self.session_dir = session_dir
        self.student_llm = student_llm
        self.parser = StrOutputParser()
    
    def run(self, expert_results: Dict[int, Dict], tutor_results: Optional[Dict[int, Dict]] = None) -> Dict[int, Dict]:
        """Run the student stage to test comprehension with simulated students"""
        print("Starting student stage...")
        
        results = {}
        total_passed = 0
        total_evaluated = 0
        
        for comment_id, expert_result in expert_results.items():
            if not expert_result['success']:
                print(f"  Skipping comment {comment_id} (expert stage failed)")
                continue
            
            # Check if tutor stage rejected this microcase
            if tutor_results and comment_id in tutor_results:
                if not tutor_results[comment_id]['accepted']:
                    print(f"  Skipping comment {comment_id} (tutor stage rejected)")
                    continue
            
            print(f"  Testing comprehension for comment {comment_id}")
            result = self._test_student_comprehension(comment_id, expert_result)
            results[comment_id] = result
            
            total_evaluated += 1
            if result['accepted']:
                total_passed += 1
                print(f"    ✓ Students passed (pass ratio: {result['pass_ratio']:.2f})")
            else:
                print(f"    ✗ Students failed (pass ratio: {result['pass_ratio']:.2f})")
        
        print(f"Student stage completed: {total_passed}/{total_evaluated} microcases passed student validation")
        
        return results
    
    def _test_student_comprehension(self, comment_id: int, expert_result: Dict) -> Dict:
        """Test student comprehension by generating multiple student solutions"""
        comment_dir = self.session_dir / f"comment_{comment_id}"
        expert_attempt_dir = Path(expert_result['successful_attempt_dir'])
        
        result = {
            'comment_id': comment_id,
            'accepted': False,
            'pass_ratio': 0.0,
            'passed_students': [],
            'failed_students': [],
            'duration': {'total': 0, 'avg': 0, 'attempts': []},
            'student_solutions_dir': None
        }
        
        # Load microcase description
        microcase_file = expert_attempt_dir / "microcase.txt"
        with open(microcase_file, 'r', encoding='utf-8') as f:
            microcase = f.read()
        
        # Create student output directory
        student_output_dir = comment_dir / "student_output"
        student_output_dir.mkdir(exist_ok=True)
        result['student_solutions_dir'] = str(student_output_dir)
        
        # Generate solutions from multiple simulated students
        num_students = self.config['student']['num_students']
        student_times = []
        
        print(f"    Generating solutions from {num_students} simulated students...")
        
        for student_id in range(num_students):
            start_time = time.time()
            
            success = self._generate_student_solution(
                microcase, student_id, student_output_dir, expert_attempt_dir
            )
            
            end_time = time.time()
            duration = int(end_time - start_time)
            student_times.append(duration)
            
            if success:
                result['passed_students'].append(student_id)
            else:
                result['failed_students'].append(student_id)
        
        # Calculate pass ratio
        result['pass_ratio'] = len(result['passed_students']) / num_students if num_students > 0 else 0.0
        
        # Check if pass ratio meets threshold
        threshold = self.config['student']['comprehension_threshold']
        result['accepted'] = result['pass_ratio'] >= threshold
        
        # Calculate duration stats
        result['duration']['attempts'] = student_times
        result['duration']['total'] = sum(student_times)
        result['duration']['avg'] = result['duration']['total'] // len(student_times) if student_times else 0
        
        print(f"    Student results: {len(result['passed_students'])}/{num_students} passed " +
              f"(ratio: {result['pass_ratio']:.2f}, threshold: {threshold:.2f})")
        
        return result
    
    def _generate_student_solution(self, microcase: str, student_id: int, 
                                 output_dir: Path, expert_attempt_dir: Path) -> bool:
        """Generate solution from a simulated student"""
        try:
            # Generate student solution with some variation in prompting
            student_solution = self._generate_student_code(microcase, student_id)
            if not student_solution:
                return False
            
            # Save student solution
            solution_file = output_dir / f"student_{student_id}_solution.py"
            with open(solution_file, 'w', encoding='utf-8') as f:
                f.write(student_solution)
            
            # Test solution against expert test suite
            return self._test_student_solution(solution_file, expert_attempt_dir)
            
        except Exception as e:
            print(f"      Student {student_id} generation failed: {e}")
            return False
    
    def _generate_student_code(self, microcase: str, student_id: int) -> str:
        """Generate student solution with variation based on student_id"""
        # Add variation to simulate different student approaches
        variations = [
            "As a programming student, solve this microcase step by step.",
            "As a student learning to code, provide your solution to this problem.",
            "Solve this programming exercise as a student would approach it.",
            "As a student, write code to solve this programming challenge.",
            "Provide a student-level solution to this coding problem."
        ]
        
        variation = variations[student_id % len(variations)]
        
        prompt_template = """{variation}

Microcase:
{microcase}

Write complete, working Python code that solves this problem:"""
        
        prompt = PromptTemplate(
            template=prompt_template,
            input_variables=["variation", "microcase"]
        )
        
        chain = prompt | self.student_llm | self.parser
        response = chain.invoke({
            "variation": variation,
            "microcase": microcase
        })
        
        return response.strip()
    
    def _test_student_solution(self, solution_file: Path, expert_attempt_dir: Path) -> bool:
        """Test student solution against expert test suite"""
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)
                
                # Copy student solution and expert tests
                expert_tests_dir = expert_attempt_dir / "tests"
                
                if not solution_file.exists() or not expert_tests_dir.exists():
                    return False
                
                # Copy files with appropriate naming
                solution_name = solution_file.name.replace("student_", "").replace("_solution", "")
                shutil.copy2(solution_file, temp_path / solution_name)
                shutil.copytree(expert_tests_dir, temp_path / "tests")
                
                # Run pytest with timeout to prevent hanging
                result = subprocess.run([
                    sys.executable, "-m", "pytest", "-v", "--tb=short", "tests/"
                ], cwd=temp_path, capture_output=True, text=True, timeout=30)
                
                return result.returncode == 0
                
        except subprocess.TimeoutExpired:
            print(f"      Student solution timed out during testing")
            return False
        except Exception as e:
            # Don't print detailed errors for student failures - it's expected
            return False