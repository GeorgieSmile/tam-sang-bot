# AGENTS.md

Repo instructions for `tam-sang-bot`. Global `AGENTS.md` applies unless overridden here.

## Navigation
- [SPEC.md](SPEC.md) — current spec (v2: payments, hand-over inbox, wait estimate); tickets in [docs/specs/v2-tickets.md](docs/specs/v2-tickets.md).
- [docs/specs/v1.md](docs/specs/v1.md) — archived v1 spec: base SQLite schema and shipped behavior.
- [GLOSSARY.md](GLOSSARY.md) — canonical domain terms; [docs/adr/](docs/adr/) — decision records.
- [docs/deploy.md](docs/deploy.md) — production (GCP VM): deploy, rollback, logs, restore a backup, rebuild.
- [CODING_STANDARDS.md](CODING_STANDARDS.md) — review-only rules: reviewers check every diff against it.
- [menu.yaml](menu.yaml) — 24-item menu, prices, option groups, schedule (`Asia/Bangkok`), and delivery fee rule.
- `app/` — `config.py`, `menu.py`, `db.py`, `line.py`, `agent.py`, `owner.py`, `main.py`, `kitchen.html`; v2: `handover.py` (inbox labels), `promptpay.py` (QR), `slips.py` + `slip_intake.py` (SlipOK, slip match), `wait.py` (wait estimate), `batch.py` (รวมจาน).
- `tests/` — behavioral tests (`test_menu.py`, `test_orders.py`, `test_webhook.py`, `test_agent.py`, `test_owner.py`; v2: `test_payments.py`, `test_handover.py`, `test_promptpay.py`, `test_slips.py`, `test_wait.py`).
- `scripts/check.sh` — full static + test gate (also run by `.git/hooks/pre-commit` and CI in `.github/workflows/check.yml`).
- `scripts/dev.sh` — local dev helpers (`run`, `reset-db`, `users`, `slips [dir]` — decodes real slips in git-ignored `local/slips/`, no API calls; `demo` / `demo-up` / `demo-down` + `say` — see `scripts/demo.py`).

## Verification
Run `./scripts/check.sh` (`ruff format --check`, `ruff check`, `mypy app tests scripts`, `pytest -q`). Run `uv run ruff format .` before checking if formatting fails.

**Run it, not just test it**: after `check.sh`, exercise the changed flow in the running app. `./scripts/dev.sh demo-up` starts the real app on a seeded throwaway DB with stub LINE, LLM and SlipOK; `./scripts/dev.sh say <user> <kind> [arg]` sends signed LINE events and prints what LINE would receive; open `/kitchen?key=demo-kitchen-key` in a browser for UI changes. A UI or webhook change verified only by pytest is `PARTIAL`. Stop it with `demo-down`.

**Real-model budget (owner-approved 2026-11-25)**: AFK agents may run `DEMO_REAL_LLM=1 ./scripts/dev.sh demo-up` with the `.env` model (Haiku today) for up to 10 customer `text` messages per ticket, when the change touches the prompt, agent tools or LLM-driven flows. Report the message count in the Proof Report; more needs approval.

## Repo Gotchas
- Never commit `.env`, `shop.db*`, `.dev.log`, or anything under `local/` (real slips).
- Never make live paid LLM, LINE or SlipOK calls in automated tests — inject stub/spy clients via `create_app(..., slip_client=...)`.
- Any out-of-band DB mutation affecting customer state (postbacks, media handlers, owner overrides) must either sync `conversations.messages_json` or be reflected in the trailing live-state system message in `app/agent.py`, verified by asserting the exact messages passed to the stub LLM.
