# PyTaskSyn

PyTaskSyn generates educational microcases from code review comments, following the methodology described in the PyTaskSyn paper. The system uses a multi-stage pipeline to create, validate, and test programming exercises derived from real code review feedback.

## Project Structure

```
pytasksyn/
├── main.py                 # Main application entry point
├── config_default.yml      # Default configuration values
├── config.yml              # Local configuration (gitignored)
├── README.md               # This file
├── stages/                 # Processing stages
│   ├── __init__.py
│   ├── preprocessing.py    # Stage 1: Comment deduplication
│   ├── expert.py          # Stage 2: Microcase generation
│   ├── tutor.py           # Stage 3: Educational validation
│   └── student.py         # Stage 4: Comprehension testing
└── utils/
    ├── __init__.py
    └── embed_comments.py   # Helper to embed comments in source files
```

## Pipeline Stages

### 1. Preprocessing Stage
- Loads code review CSV file with columns: `file_path`, `line_number`, `comment`
- Adds `comment_id` column (enumerated from 0)
- Uses LLM to deduplicate similar comments per file
- Outputs: `preprocess/code_review_deduplicated.csv`

### 2. Expert Stage
- Embeds review comments into source files using template format
- Applies context limits and margin settings
- For each comment, generates:
  - `microcase.txt` - Task description
  - `tests/test_*.py` - pytest-compatible test suite
  - `solution_expert.py` - Reference solution
- Verifies expert solution passes all tests
- Retries up to `--expert-max-attempts` times

### 3. Tutor Stage (Optional)
- Takes only `microcase.txt` as input
- Generates `solution_tutor.py` and verifies it passes expert tests
- Creates `tutor_review.json` with educational assessment:
  - `score`: Float 0.0-1.0 rating educational value
  - `review`: String explanation of rating
- Accepts microcases with score ≥ `--tutor-acceptance-threshold`

### 4. Student Stage (Optional)
- Simulates `--num-students` student attempts at solving microcase
- Each student generates `student_{id}_solution.py`
- Tests all solutions against expert test suite
- Calculates pass ratio and accepts if ≥ `--student-comprehension-threshold`

## Configuration

### Default Configuration (`config_default.yml`)
Contains all default settings including model configurations, paths, and stage parameters.

### Local Configuration (`config.yml`)
Override any default settings. This file is gitignored to keep local settings private.

### Configuration Priority
1. CLI arguments (highest priority)
2. Local config file (`config.yml`)
3. Default config file (`config_default.yml`)

### Required Configuration
- `paths.student_project`: Path to student project root directory
- `paths.code_review_file`: Path to CSV file with code review comments

## Usage

### Basic Usage
```bash
cd pytasksyn
python main.py --student-project /path/to/student/project --code-review-file /path/to/reviews.csv
```

### Skip Validation Stages
```bash
# Skip tutor validation
python main.py --skip-val-stage t --student-project /path/to/project --code-review-file reviews.csv

# Skip student validation  
python main.py --skip-val-stage s --student-project /path/to/project --code-review-file reviews.csv

# Skip both tutor and student validation
python main.py --skip-val-stage ts --student-project /path/to/project --code-review-file reviews.csv
```

### Configure Models
```bash
python main.py \\
  --expert-provider openai --expert-model gpt-4 \\
  --tutor-provider openai --tutor-model gpt-4 \\
  --student-provider openai --student-model gpt-3.5-turbo \\
  --student-project /path/to/project \\
  --code-review-file reviews.csv
```

### Expert Context Settings
```bash
python main.py \\
  --expert-context-max-symbols 10000 \\
  --expert-context-comment-margin 30 \\
  --expert-context-add-rest \\
  --student-project /path/to/project \\
  --code-review-file reviews.csv
```

### Tutor and Student Settings
```bash
python main.py \\
  --tutor-acceptance-threshold 0.7 \\
  --num-students 50 \\
  --student-comprehension-threshold 0.6 \\
  --student-project /path/to/project \\
  --code-review-file reviews.csv
```

## Input Format

### Code Review CSV
Required columns:
- `file_path`: Relative path to source file from project root
- `line_number`: Line number where comment applies
- `comment`: Review comment text

Example:
```csv
file_path,line_number,comment
src/main.py,42,"Variable naming should be more descriptive"
src/utils.py,15,"Missing error handling for edge cases"
src/main.py,18,"This loop can be optimized using list comprehension"
```

## Output Structure

Results are saved in `data/pytasksyn/session_YYYYMMDD_HHMMSS/`:

```
session_20241201_143022/
├── config_used.yml                    # Configuration used for this session
├── preprocess/
│   └── code_review_deduplicated.csv   # Deduplicated comments
├── embedded_source/                   # Source files with embedded comments
│   └── src/
│       ├── main.py
│       └── utils.py
├── comment_0/                         # Results for comment ID 0
│   ├── expert_output/
│   │   └── attempt_0/
│   │       ├── microcase.txt
│   │       ├── tests/
│   │       │   └── test_microcase.py
│   │       └── solution_expert.py
│   ├── tutor_output/
│   │   └── attempt_0/
│   │       ├── solution_tutor.py
│   │       └── tutor_review.json
│   └── student_output/
│       ├── student_0_solution.py
│       ├── student_1_solution.py
│       └── ...
├── comment_1/
│   └── ...
└── script_report.json                # Final summary report
```

## Report Format

The `script_report.json` contains a list of results for each comment:

```json
[
  {
    "comment_id": 0,
    "source_file_path": "src/main.py",
    "source_line_number": 42,
    "accepted": true,
    "pass_ratio": 0.85,
    "tutor_review": "Good microcase that demonstrates variable naming principles...",
    "tutor_score": 0.8,
    "attempts_tutor": 1,
    "attempts_expert": 2,
    "stage_duration": {
      "expert": {"total": 45, "avg": 22, "attempts": [30, 15]},
      "tutor": {"total": 20, "avg": 20, "attempts": [20]},
      "student": {"total": 180, "avg": 9, "attempts": [8, 12, 7, ...]}
    },
    "students_failed": [2, 7, 13],
    "students_passed": [0, 1, 3, 4, 5, 6, 8, 9, 10, 11, 12, 14, 15, 16, 17, 18, 19]
  }
]
```

## Environment Setup

Ensure you have the required API keys in your `.env` file in the project root:

```bash
# For OpenAI models (including third-party compatible APIs)
OPENAI_API_KEY=your_openai_api_key
OPENAI_BASE_URL=https://api.openai.com/v1  # Optional for custom endpoints

# For Yandex models
YANDEX_API_KEY=your_yandex_api_key
YANDEX_FOLDER_ID=your_yandex_folder_id
```

## Dependencies

- langchain
- langchain-openai  
- langchain-community
- python-dotenv
- PyYAML
- pytest (for running generated tests)

## Helper Scripts

### embed_comments.py
Standalone utility to embed code review comments into source files:

```bash
python utils/embed_comments.py \\
  --review-file reviews.csv \\
  --project-root /path/to/project \\
  --output-dir /path/to/embedded/files
```

Comment embedding template:
```
###### LINE {line_number} ################
{comment_body}
#####################################
```