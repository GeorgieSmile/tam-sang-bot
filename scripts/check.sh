#!/usr/bin/env bash
set -euo pipefail
# The last line always states the result, so `check.sh | tail` can't hide a failure
trap 'echo "check.sh: FAIL (exit $?)"' ERR

cd "$(dirname "$0")/.."
uv run ruff format --check .
uv run ruff check .
uv run mypy app tests scripts
uv run pytest -q
echo "check.sh: PASS"
