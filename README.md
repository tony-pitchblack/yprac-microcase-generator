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
- Skip any steps that are already completed

### Manual Setup

Alternatively, you can set up manually:

```bash
git submodule update --init --recursive
micromamba env create -f environment.yml -y
micromamba activate ymg
```