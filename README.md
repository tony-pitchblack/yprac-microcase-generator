# yprac-microcase-generator

## Setup

### Quick Setup

Run the automated setup script to install micromamba, create the environment, and download the dataset:

```bash
./setup.sh
```

The script will automatically:
- Install micromamba (if not already installed)
- Create the `ymg` environment from `environment.yml`
- Download the Hugging Face dataset via git submodules

### Data Processing

To unzip and process data files, run:

```bash
./scripts/unzip_data.sh
```

This script will:
- Process all files in the `data/` directory recursively
- Unzip any `.zip` files directly to `tmp/data_unzipped/`
- Copy non-zip files to the same directory
- Handle various character encodings for zip files

### Context Estimation

Use the context estimation script to calculate text metrics for LLM context cost estimation:

```bash
# Estimate context for current directory
./scripts/estimate_context.py .

# Show directories at depth level 1 only
./scripts/estimate_context.py . --depth 1

# Show directories at depth level 2 only
./scripts/estimate_context.py . --depth 2

# Estimate specific directory
./scripts/estimate_context.py /path/to/directory

# Analyze backend projects and save to file
micromamba activate ymg && ./scripts/estimate_context.py tmp/data_unzipped/backend/ --depth 1 >> data/estimate_context_backend.txt

# Analyze frontend projects at depth 3 and save to file
micromamba activate ymg && ./scripts/estimate_context.py tmp/data_unzipped/frontend/ --depth 3 >> data/estimate_context_frontend.txt
```

The script provides:
- Total lines, characters, and accurate token counts using OpenAI's tiktoken
- Characters per 1000 and tokens per 1000 (for cost estimation)
- Table format showing leaf directories at the specified depth level only
- Automatic text file detection (excludes binary files)
- File-level counting within each leaf directory (no recursive aggregation)