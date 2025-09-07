#!/bin/bash

# Useful aliases for the yprac-microcase-generator project

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

alias estctx="$PROJECT_ROOT/scripts/estimate_context.sh"

echo "Project aliases loaded!"