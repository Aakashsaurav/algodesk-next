#!/usr/bin/env bash
# scripts/env.sh — local isolation: keep all uv/python/cache files inside the project.
# Must be sourced: source scripts/env.sh

# Compute ROOT from the script location (bash and zsh safe)
if [ -n "${BASH_SOURCE[0]:-}" ]; then
    _ENV_SCRIPT="${BASH_SOURCE[0]}"
elif [ -n "${ZSH_VERSION:-}" ]; then
    _ENV_SCRIPT="${(%):-%x}"
else
    _ENV_SCRIPT="$0"
fi

_ENV_DIR="$(cd "$(dirname "$_ENV_SCRIPT")" && pwd -P)"
ROOT="$(cd "$_ENV_DIR/.." && pwd -P)"
unset _ENV_SCRIPT _ENV_DIR

export ROOT
export UV_CACHE_DIR="$ROOT/.local/cache/uv"
export UV_PYTHON_INSTALL_DIR="$ROOT/.local/python"
export UV_PYTHON_BIN_DIR="$ROOT/.local/bin"
export UV_TOOL_DIR="$ROOT/.local/tools"
export UV_TOOL_BIN_DIR="$ROOT/.local/tools-bin"
export UV_PROJECT_ENVIRONMENT="$ROOT/.venv"
export UV_NO_CONFIG=1
export PRE_COMMIT_HOME="$ROOT/.local/cache/pre-commit"
export PYTHONNOUSERSITE=1
export PIP_REQUIRE_VIRTUALENV=true

# Add graphify venv to PATH if present
if [ -d "$ROOT/.local/graphify-venv/bin" ]; then
    export PATH="$ROOT/.local/graphify-venv/bin:$PATH"
fi

mkdir -p "$ROOT/.local/cache/uv" \
         "$ROOT/.local/python" \
         "$ROOT/.local/bin" \
         "$ROOT/.local/tools" \
         "$ROOT/.local/tools-bin" \
         "$ROOT/.local/cache/pre-commit"

echo "ENV: isolated at $ROOT"
