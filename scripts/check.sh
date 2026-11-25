#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
uv run ruff format --check .
uv run ruff check .
uv run mypy app tests scripts
uv run pytest -q
