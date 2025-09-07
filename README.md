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