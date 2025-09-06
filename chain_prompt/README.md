# Chain Prompt - Micro-case Generator

This module generates micro-cases from code review comments using LLM chains.

## Overview

The script analyzes code review comments and:
1. Extracts errors and categorizes them by topic (SQL, Pandas, Python, etc.)
2. Generates educational micro-cases based on the identified errors

## Setup

### Prerequisites

1. Activate the micromamba environment:
   ```bash
   micromamba activate ymg
   ```

2. Configure your `.env` file in the project root with the appropriate API keys and settings.

### Environment Variables

#### OpenAI Configuration
```bash
OPENAI_API_KEY=your_openai_api_key_here
OPENAI_BASE_URL=https://api.openai.com/v1  # Optional: custom endpoint
OPENAI_MODEL=openai/gpt-5-nano  # Optional: model selection (default: openai/gpt-5-nano)
```

#### Yandex Configuration
```bash
YANDEX_API_KEY=your_yandex_api_key_here
YANDEX_FOLDER_ID=your_yandex_folder_id_here  # Required
YANDEX_MODEL=yandexgpt-lite  # Optional: model selection (default: yandexgpt-lite)
```

### Required Files

Ensure these files exist in the `chain_prompt/` directory:

- `review.txt` - Contains the code review comments to analyze
- `extract_prompt.txt` - Template for error extraction
- `case_prompt.txt` - Template for micro-case generation

## Usage

### Basic Usage

```bash
# Use Yandex provider (default)
./main.py

# Use OpenAI provider
./main.py --provider openai

# Use Yandex provider explicitly
./main.py --provider yandex
```

### Command Line Arguments

- `--provider` - Choose LLM provider (`yandex` or `openai`)
  - Default: `yandex`

## Output

The script will output:

1. **Errors** - JSON-formatted list of identified errors with topics
2. **Micro-cases** - Generated educational cases based on the errors

Example output:
```
Using OpenAI model: openai/gpt-5-nano
Ошибки:
[
  {
    "ошибка": "Неправильное использование GROUP BY",
    "тема": "SQL"
  }
]

Микро-кейсы:
[Generated micro-case content based on the errors]
```

## File Structure

```
chain_prompt/
├── main.py              # Main script
├── README.md            # This documentation
├── review.txt           # Input: code review comments
├── extract_prompt.txt   # Template: error extraction prompt
└── case_prompt.txt      # Template: micro-case generation prompt
```

## Model Configuration

You can specify different models using environment variables:

### OpenAI Models
- `openai/gpt-5-nano` (default)
- `gpt-4`
- `gpt-4-turbo`
- Any other OpenAI model

### Yandex Models  
- `yandexgpt-lite` (default)
- `yandexgpt`
- Other Yandex GPT models

Set the model in your `.env` file:
```bash
OPENAI_MODEL=gpt-4
YANDEX_MODEL=yandexgpt
```

## Troubleshooting

### Common Issues

1. **Missing API Keys**: Ensure all required environment variables are set in `.env`
2. **File Not Found**: Check that `review.txt`, `extract_prompt.txt`, and `case_prompt.txt` exist
3. **Model Not Found**: Verify the model name is correct for your chosen provider
4. **API Endpoint Issues**: For custom OpenAI endpoints, ensure `OPENAI_BASE_URL` is correctly configured

### Error Messages

- `"Не найдены YANDEX_API_KEY или YANDEX_FOLDER_ID в .env файле"` - Missing Yandex credentials
- `"Не найден OPENAI_API_KEY в .env файле"` - Missing OpenAI API key
- `404 endpoint not found` - Incorrect `OPENAI_BASE_URL` configuration

## Architecture

The script uses modern LangChain patterns:
- `PromptTemplate` for prompt management
- `RunnableSequence` (prompt | llm | parser) for chain construction
- `StrOutputParser` for output processing

This replaces the deprecated `LLMChain` and `SequentialChain` patterns from earlier LangChain versions.