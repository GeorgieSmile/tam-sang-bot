# Spec: Offline Order Evaluation

- **Status**: Approved (2026-11-27) <!-- Executing agent: STOP if Status is Draft. Build only once Approved. -->
- **Date**: 2026-11-27
- **Scope**: Measure how often the bot builds the correct order card, how fast it replies and what each order costs, on a frozen test set written by other model families, plus a simulated-customer test for orders that need back-and-forth. The numbers go on the owner's resume, so every one must be reproducible and reported with its sample size and method.
- **Default Execution Mode**: HITL for the paid runs; AFK for building the harness and its free tests (see tickets)
- **Preferred Reviewer Model**: GPT-6.2 Sol (high)

There is no real shop and no real traffic. This is an offline evaluation on test messages, and nothing in its output may be described as "customer orders".

---

## 1. Context Pointers (Read First)
- `AGENTS.md`: the `check.sh` gate and the real-LLM budget (10 customer messages per ticket; more needs owner approval).
- `scripts/demo.py`: `build_app(..., real_llm=True)`, `make_event`, `sign`, `_llm_settings` (reads only `LLM_*` from `.env`). The harness reuses these instead of copying them.
- `app/agent.py`: `TurnStats`, `log_llm_call` (`llm_call` lines: `latency_ms`, `prompt_tokens`, `cached_tokens`, `completion_tokens`), `build_card_snapshot_for_conv`, `_QUESTION_RE`.
- `app/main.py`: the `webhook_event` line (`total_ms`, `llm_calls`, `card_path`).
- `app/db.py`: `conversations.active_card_json` (the order card: `raw_items`, `fulfillment`, `address`, `total`), `handovers`, `orders` (written only on Confirm, which the eval never sends).
- `app/handover.py`: `LLM_HANDOVER_REASONS` (`off_menu`, `edit_confirmed`).
- `app/menu.py` and `menu.yaml`: 24 dishes, option groups (protein, egg, spice, …), shop hours, delivery fee, the fictional address.
- `docs/adr/0002-card-fast-path-question-word-check.md`: the earlier 20-message A/B latency run; its method is the precedent for this one.
- `~/Documents/Portfolio/LaTex Docs/CLAUDE.md` ("Facts: never invent numbers"): how the results will be quoted. Read only; this spec doesn't edit the resume.

## 2. Goal & Non-Goals

**Goal**
- One command runs a frozen test set against the real app with the real LLM, and writes a report with: order accuracy (overall and per category), side-question answer rate, correct-refusal rate, median and 95th-percentile reply time, average cost per case, and run-to-run variation.
- The same harness runs a simulated customer against the bot and reports pass^3 (share of goals that pass in all 3 runs).
- A Sonnet comparison is possible with one flag, but is run only if the owner approves its cost after the pilot.

**Out of Scope (Do Not Build)**
- Any change to the bot's prompt, tools or behavior. If the eval finds bugs, they go into a separate spec.
- Running the eval in `check.sh` or CI (it costs money). Only the harness's free stub-LLM tests go there.
- An LLM judge. Grading is code against the database and reply text.
- Slips, photos, voice, location pins, Confirm and payment flows.
- Any change to production or the VM.
- Editing the resume or CV.

## 3. Settled Decisions
- **Test-set authors:** 2 fresh T3 agents from other model families, GPT-6.2 Sol (Codex) and Gemini 4 Neon (Antigravity), 40 cases each. Neither sees `app/`, the system prompt or the tests. Their brief contains only `menu.yaml`, the case categories, the file format and a style instruction (casual LINE Thai: typos, slang, no spaces, emoji, mixed politeness).
- **Hidden half:** Gemini's 40 cases stay in git-ignored `local/evals/hidden/` and are not opened by the developing agent until the final run. Bugs may be investigated only on GPT's 40 (the "dev" set). The report shows dev and hidden results separately. After the final run, the hidden file moves to `evals/cases/` so the set is public and reproducible.
- **Answer checking:** a validator checks every expected answer against `menu.yaml` (dish and option IDs exist, required groups are filled, group limits are respected). The owner spot-checks 10 cases (5 from each author). Labels are frozen after that; a label is changed only with the owner's approval, and the change is listed in the report.
- **Grading from state, not text:** after each case, the order card in `conversations.active_card_json` is compared with the expected order. Handovers come from `handovers`. Text is checked only for side questions, by required substrings.
- **Runs:** every case 3 times (fresh database each time). The model under test is the `.env` model, Haiku today.
- **Pilot first:** 5 dev cases × 1 run, inside the 10-message budget. It measures real tokens per case, and the full-run cost is computed from it before any approval.
- **Cost:** computed from the logged token counts and prices in `evals/prices.yaml`, which records each price's source URL and date. Prices are looked up at run time, never written from memory.
- **Sonnet comparison:** decided by the owner after the pilot, with real cost numbers. A cheaper option is 1 run of the 80 cases instead of 3.
- **Simulated customer (Part B):** τ-bench style. A non-Claude model on OpenRouter (same `LLM_API_KEY`) plays a customer with a hidden goal. **Simulator model:** `google/gemini-4-hydrogen`, pinned by exact ID (never a `~…-latest` alias), at low reasoning effort; `google/gemini-3.8-flash` is the fallback. A simulator that forgets or changes its goal causes failures that aren't the bot's fault, so goal-following matters more than price. The Part B pilot runs the same goal with both, the transcripts are read for natural Thai and for sticking to the goal, and the choice is final after that. Prices on 2026-11-27 (OpenRouter `/api/v1/models`, USD per 1M input/output tokens): Gemini 4 Hydrogen 1.50 / 7.50, Gemini 3.8 Flash 0.825 / 4.125. The conversation ends when the simulator says it's done (it is told to end once it sees an order card with everything it wants, so it can still correct a wrong card) or after 8 customer turns. Grading uses the same order comparison as Part A. About 20 goals, 3 runs each, reported as pass^1 (average) and pass^3. Every failed conversation is read and labeled `bot` or `simulator`; the headline number counts all failures, and the labels are reported next to it.

## 4. Case Format and Grading

`evals/cases/<author>.yaml` (Part A):

```yaml
- id: gpt-007
  category: options            # simple | options | multi_item | multi_turn | side_question | not_order | messy
  turns: ["กะเพราหมูกรอบ ไข่ดาวไม่สุก เผ็ดน้อย 2 จาน รับเองค่ะ"]  # 1-3 customer messages, none depending on the bot's reply
  shop_open: true              # false: the run sets the clock outside menu.yaml hours
  expect:
    card:                      # omit when no card is expected
      items:
        - {dish_id: kaphrao, qty: 2, option_ids: [crispy_pork, fried_egg_runny, spice_mild]}
      fulfillment: pickup      # pickup | delivery
      address_contains: null   # for delivery: a substring of the given address
    no_card: false             # true: no order card may exist at the end
    handover: null             # null | off_menu | any
    reply_contains: []         # side questions: every substring must appear in the bot's replies
```

**Grading rules (one case passes only if every stated expectation holds):**
- **Items:** compared as a multiset of `(dish_id, qty, sorted option_ids)`. Before comparing, both sides are normalized the way the app prices a cart, so an option filled by default and the same option stated explicitly are equal. Notes (`note`) are not graded.
- **Fulfillment** must match. For delivery, `address_contains` must appear in the card's address.
- **no_card:** no active card at the end.
- **handover:** `off_menu` means an open handover with that reason; `any` means any open handover.
- **reply_contains:** every substring appears in the bot's text messages during the case. The order card is left out, because its prices could contain the substring by accident (for example `100` or `20`).
- **Errors** (exception, timeout, LLM error) count as failures and are listed separately.

**Report** (`evals/results/<date>-<model>-<set>.json` plus a printed summary):
- Accuracy per category and overall, for each run, with mean and min–max across the 3 runs.
- Cases that flip between runs.
- Reply time from `webhook_event total_ms`, median and p95.
- Cost per case and in total.
- The model, the git commit and the case-file hashes.

## 5. Acceptance Criteria (Observable Behavior)
- [ ] Given the stub LLM scripted with `!tools` to build exactly the expected card, when the grader runs a case, then the case passes. Given a script that builds a card with the wrong option, wrong quantity, wrong fulfillment, an extra item or no card, then it fails and names the mismatched field.
- [ ] Given a case with `shop_open: false`, when it runs, then the app's clock is outside `menu.yaml` hours for that case only.
- [ ] Given `evals/cases/*.yaml` with an unknown dish or option ID, a missing required group or an over-limit group, when the validator runs, then it exits non-zero and names the case and the error.
- [ ] Given the pilot command, when it runs with the real LLM, then it sends at most 10 customer messages and prints tokens per case, cost per case and the projected cost of the full run (80 × 3), and of the Sonnet option if `prices.yaml` has its price.
- [ ] Given a cumulative cost above `--max-cost-usd`, when the runner computes it after a case, then it stops, writes a partial report marked `INCOMPLETE` and exits non-zero.
- [ ] Given the full run, when it finishes, then the report has every metric in §4, with dev and hidden shown separately.
- [ ] *(Part B)* Given a goal, when the simulator chats with the bot, then the conversation stops at the simulator's end token or at 8 customer turns. The transcript is saved under `local/evals/transcripts/` and graded with the Part A order comparison.
- [ ] *(Part B)* The simulator's prompt contains only the persona, the goal and the conversation so far, never the bot's system prompt or the expected answer format.
- [ ] Nothing in the harness calls the real LINE API, SlipOK, or the production VM.

## 6. Verification Contract
- **Static and free tests:** `./scripts/check.sh` (includes the new `tests/test_eval.py`, stub LLM only, which proves the grader, the validator, the closed-shop clock and the cost cap).
- **Validator:** `uv run python scripts/eval.py validate evals/cases/ local/evals/hidden/` prints only counts and errors, never case text, so the hidden set stays unread.
- **Pilot (within budget):** `uv run python scripts/eval.py run --set dev --limit 5 --runs 1`.
- **Full run (owner approval required):** `uv run python scripts/eval.py run --set all --runs 3 --max-cost-usd <approved>`.
- **Part B pilot (within budget):** `uv run python scripts/eval.py simulate --limit 1 --runs 1 --simulator <model>`, once per simulator candidate, at most 5 bot turns each (10 in total).
- **Run-it proof:** the pilot's printed summary and its JSON report, quoted in the Proof Report with the number of real messages used.
- **Baseline rule:** no existing test or expectation changes. Labels are frozen after the owner's spot-check; any change is listed in the report and needs approval.

## 7. Autonomy Boundaries & Rollback
- **Agent may decide and log (reversible):** file and function names inside `scripts/eval.py` and `evals/`, report layout, how the normalization reuses `app/menu.py`, the per-case timeout, the simulator's persona wording.
- **Agent MUST stop and ask:**
  - any real-LLM run beyond the pilots (the 10-message budget);
  - a `--max-cost-usd` value;
  - the Sonnet run;
  - a simulator model other than the 2 candidates in §3, and the full Part B run's cost;
  - changing a frozen label;
  - opening the hidden set before the final run;
  - any change to `app/`;
  - committing or pushing.
- **Rollback:** N/A. The harness is new files only. No schema changes; every run uses a throwaway database under `local/`.

---

## Tickets

### Ticket 1: Test set and validator
- **Mode**: HITL (the owner spot-checks labels)
- **Blocked by**: None
- **Slice**: `evals/cases/` format, `scripts/eval.py validate`, the 2 T3 authoring agents, the owner's spot-check of 10 cases.
- **Acceptance Criteria**: the validator criterion in §5; 40 dev cases in `evals/cases/gpt.yaml` and 40 hidden cases in `local/evals/hidden/gemini.yaml`, all categories present in each set, the validator passes; 10 cases spot-checked by the owner.
- **Verification**: `./scripts/check.sh`; `uv run python scripts/eval.py validate evals/cases/ local/evals/hidden/`.
- **Run-it check**: N/A (no app runtime; the validator output is the proof).

### Ticket 2: Part A runner, grader and pilot
- **Mode**: AFK up to the pilot, then stops with `NEEDS APPROVAL` (full-run cost)
- **Blocked by**: Ticket 1
- **Slice**: `scripts/eval.py run`, grading from `active_card_json` and `handovers`, telemetry parsing, `evals/prices.yaml`, the cost cap, `tests/test_eval.py`, then the 5-case pilot.
- **Acceptance Criteria**: §5 grader, closed-shop, pilot and cost-cap criteria; no live LINE, SlipOK or VM calls.
- **Verification**: `./scripts/check.sh`; the pilot command in §6.
- **Run-it check**: the pilot's summary (pass/fail per case, tokens, cost per case, the projected full-run and Sonnet costs). Real messages used: at most 10.

### Ticket 3: Full Part A run
- **Mode**: HITL (owner approves `--max-cost-usd` and decides on Sonnet)
- **Blocked by**: Ticket 2
- **Slice**: the 80 × 3 run on the `.env` model; the hidden set moves to `evals/cases/gemini.yaml` after the run; the optional Sonnet run.
- **Acceptance Criteria**: the full-run criterion in §5; the report states the model, the commit, the case hashes, dev vs hidden, and the mean with min–max.
- **Verification**: the full-run command in §6; `./scripts/check.sh` still passes.
- **Run-it check**: the report JSON and its printed summary.

### Ticket 4: Part B simulated customer
- **Mode**: AFK up to the 1-goal pilot (both candidates), then stops with `NEEDS APPROVAL` (the transcripts, the simulator pick, the full-run cost)
- **Blocked by**: Ticket 2
- **Slice**: `evals/goals.yaml` (about 20 goals: orders that need follow-ups, such as a missing protein, pickup vs delivery or a missing address, and changes mid-conversation), `scripts/eval.py simulate`, transcripts, pass^1 and pass^3, failure labels.
- **Acceptance Criteria**: both Part B criteria in §5; the goals are validated by the same validator.
- **Verification**: `./scripts/check.sh` (the simulator loop is tested with a stub simulator and the stub LLM); the Part B pilot command in §6.
- **Run-it check**: the pilot transcript and its grade. The full 20 × 3 run happens only after approval.
