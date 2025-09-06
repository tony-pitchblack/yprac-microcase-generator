#!/usr/bin/env bash
set -euo pipefail

# load profile if it exists (may set PATH for micromamba)
[ -f "$HOME/.bash_profile" ] && source "$HOME/.bash_profile" || true

# 1) install micromamba if not found in system
if ! command -v micromamba >/dev/null 2>&1; then
  echo "micromamba not found — installing..."
  bash <(curl -L micro.mamba.pm/install.sh)
else
  echo "micromamba found: $(command -v micromamba)"
fi

# 3) call micromamba by absolute path to avoid PATH issues in this run
"$HOME/.local/bin/micromamba" env create -f environment.yml -y

# 4) clone only if the folder doesn't already exist
if [ ! -d data ]; then
  git submodule add https://huggingface.co/datasets/tony-pitchblack/yprac-microcase-generator data
else
  echo "gitmodule 'data' already registered - downloading..."
  git submodule update --init
fi