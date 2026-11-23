# AGENTS.md

Repo instructions for `tam-sang-bot`. Global `AGENTS.md` applies unless overridden here.

## Navigation
- [SPEC.md](SPEC.md) — approved product & technical spec, SQLite schema, and ticket history.
- [menu.yaml](menu.yaml) — 24-item menu, prices, option groups, schedule (`Asia/Bangkok`), and delivery fee rule.
- `app/` — `config.py`, `menu.py`, `db.py`, `line.py`, `agent.py`, `owner.py`, `main.py`, `kitchen.html`.
- `tests/` — behavioral tests (`test_menu.py`, `test_orders.py`, `test_webhook.py`, `test_agent.py`, `test_owner.py`).
- `scripts/check.sh` — full static + test gate (also wired into `.git/hooks/pre-commit`).
- `scripts/dev.sh` — local dev helpers (`run`, `reset-db`, `users`).

## Verification
Run `./scripts/check.sh` (`ruff format --check`, `ruff check`, `mypy app tests`, `pytest -q`). Run `uv run ruff format .` before checking if formatting fails.

## Repo Gotchas
- Never commit `.env`, `shop.db*`, or `.dev.log`.
- Never make live paid LLM or LINE API calls in automated tests — inject stub/spy clients via `create_app(...)`.
- Any out-of-band DB mutation affecting customer state (postbacks, media handlers, owner overrides) must either sync `conversations.messages_json` or be reflected in the trailing live-state system message in `app/agent.py`, verified by asserting the exact messages passed to the stub LLM.
