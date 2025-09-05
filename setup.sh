#!/bin/bash

set -e

echo "Setting up yprac-microcase-generator environment..."

# Install micromamba if not already installed
if ! command -v micromamba &> /dev/null; then
    echo "Installing micromamba..."
    "${SHELL}" <(curl -L micro.mamba.pm/install.sh)
    
    # Source the micromamba initialization
    if [ -f ~/.bashrc ]; then
        source ~/.bashrc
    fi
    if [ -f ~/.zshrc ]; then
        source ~/.zshrc
    fi
    
    # Initialize micromamba for current shell
    eval "$(micromamba shell hook --shell bash)"
else
    echo "micromamba is already installed"
fi

# Create environment from .yml file
echo "Creating micromamba environment from environment.yml..."
micromamba env create -f environment.yml -y

echo "Activating environment..."
micromamba activate ymg

# Download the dataset from Hugging Face (git submodule)
echo "Downloading dataset from Hugging Face..."
git submodule update --init --recursive

echo "Setup complete! To activate the environment in future sessions, run:"
echo "micromamba activate ymg"