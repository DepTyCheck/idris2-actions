#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
.venv/bin/python -m isort --settings-path .github/linters/pyproject.toml tests
.venv/bin/python -m ruff format --config .github/linters/pyproject.toml tests
.venv/bin/python -m black --config .github/linters/pyproject.toml tests
