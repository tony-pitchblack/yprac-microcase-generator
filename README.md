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