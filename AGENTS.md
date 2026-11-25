# AGENTS.md

Repo instructions for `tam-sang-bot`. Global `AGENTS.md` applies unless overridden here.

## Navigation
- [SPEC.md](SPEC.md) — current spec (v2: payments, hand-over inbox, wait estimate); tickets in [docs/specs/v2-tickets.md](docs/specs/v2-tickets.md).
- [docs/specs/v1.md](docs/specs/v1.md) — archived v1 spec: base SQLite schema and shipped behavior.
- [GLOSSARY.md](GLOSSARY.md) — canonical domain terms; [docs/adr/](docs/adr/) — decision records.
- [menu.yaml](menu.yaml) — 24-item menu, prices, option groups, schedule (`Asia/Bangkok`), and delivery fee rule.
- `app/` — `config.py`, `menu.py`, `db.py`, `line.py`, `agent.py`, `owner.py`, `main.py`, `kitchen.html`; v2: `handover.py` (inbox labels), `promptpay.py` (QR), `slips.py` + `slip_intake.py` (SlipOK, slip match), `wait.py` (wait estimate), `batch.py` (รวมจาน).
- `tests/` — behavioral tests (`test_menu.py`, `test_orders.py`, `test_webhook.py`, `test_agent.py`, `test_owner.py`; v2: `test_payments.py`, `test_handover.py`, `test_promptpay.py`, `test_slips.py`, `test_wait.py`).
- `scripts/check.sh` — full static + test gate (also wired into `.git/hooks/pre-commit`).
- `scripts/dev.sh` — local dev helpers (`run`, `reset-db`, `users`, `slips [dir]` — decodes real slips in git-ignored `local/slips/`, no API calls).

## Verification
Run `./scripts/check.sh` (`ruff format --check`, `ruff check`, `mypy app tests`, `pytest -q`). Run `uv run ruff format .` before checking if formatting fails.

## Repo Gotchas
- Never commit `.env`, `shop.db*`, `.dev.log`, or anything under `local/` (real slips).
- Never make live paid LLM, LINE or SlipOK calls in automated tests — inject stub/spy clients via `create_app(..., slip_client=...)`.
- Any out-of-band DB mutation affecting customer state (postbacks, media handlers, owner overrides) must either sync `conversations.messages_json` or be reflected in the trailing live-state system message in `app/agent.py`, verified by asserting the exact messages passed to the stub LLM.
