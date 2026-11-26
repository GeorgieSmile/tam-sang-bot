# Spec: Agent Correctness, Telemetry & Latency Polish

- **Status**: Approved (2026-11-26) <!-- Executing agent: STOP if Status is Draft. Build only once Approved. -->
- **Date**: 2026-11-26
- **Scope**: Fix 7 reproduced agent/card/control bugs on top of v2 (`e1c8432`), add lean PII-free LLM and webhook telemetry with LINE HTTP connection pooling, and reduce LLM round-trips per order via richer live-state injection and prompt tool batching — with zero schema migrations.
- **Default Execution Mode**: AFK (Tickets 1–2: 0 live API calls; Ticket 3: within the owner-approved 10-message `DEMO_REAL_LLM=1` budget in `AGENTS.md`)
- **Preferred Reviewer Model**: GPT-6.2 Sol (high), for all tickets

---

## 1. Context Pointers (Read First)
- `SPEC.md` — current v2 product contract (§3.1–§3.9). Everything in v2 still holds unless §3.2 below notes an intentional change.
- `CODING_STANDARDS.md` — 4 review rules checked on every diff (owner takeover, history truthfulness, re-check at write / cancel undelivered cards, order inclusion).
- `AGENTS.md` — `./scripts/check.sh` gate, `demo-up` / `say` run-it verification rule, 10-message `DEMO_REAL_LLM=1` budget, and out-of-band DB sync rule.
- `GLOSSARY.md` — canonical domain terms (`order card`, `bot quiet`, `bot paused`, `inbox card`, `shop day`).
- `app/agent.py` — `handle_customer_text`, `handle_location_message`, `_system_prompt`, `_run_tool`, `owner_took_over`, `drop_unsent_card`, live-state system message, Rule 6.
- `app/db.py` — `save_draft`, `cancel_active_card`, `consume_resume_note`, `append_conversation_messages`, `append_conversation_event`.
- `app/main.py` — `create_app`, Cancel postback handler (`action=cancel`), webhook `started_at` loop, `_safe_reply`.
- `app/line.py` — `HttpLineClient` (currently creates a new `httpx.Client` per request).
- `app/slip_intake.py` — `_after_claim` and `pay_with_slip` commit path after `slip_client.check`.
- `tests/test_agent.py`, `tests/test_handover.py`, `tests/test_wait.py`, `tests/test_webhook.py`, `tests/test_guardrails.py`.
- `scripts/demo.py` and `scripts/dev.sh` — seeded throwaway demo server (`demo-up`, `say`, `demo-down`, `DEMO_REAL_LLM=1`).

---

## 2. Goal & Non-Goals

**Goal**
- Eliminate every reproduced agent/card/control failure: `resp.choices` crash leaving an orphan card, malformed JSON tool args triggering `hand_over`, stale/dead Flex card sent after a same-turn cart edit, `update_cart` mutating the draft before full-cart validation, Cancel-card tap leaving old items in the draft, `RESUME_NOTE` lost on LLM failure, `hand_over` sibling tool mutations, and mid-turn global pause (`bot_enabled = 0`) continuing to mutate or reply.
- Measure real LLM cost, cache reads, call count per order, lock wait, and end-to-end turn latency via lean, PII-free structured logs.
- Reuse a single pooled `httpx.Client` for LINE calls and budget turn deadlines from webhook receipt.
- Cut LLM round-trips on ordering turns by injecting indexed cart lines and `ที่อยู่เดิม=มี/ไม่มี` into System Message 2 and instructing the model to batch `update_cart` + `set_fulfillment` + `build_order_card` in one call — verified on 8–10 realistic Thai dialogues against the new telemetry.

**Out of Scope (Do Not Build)**
- Any SQLite schema change or migration (zero new columns or tables).
- Removing `"available"` booleans from System Message 1 (menu cache-prefix rewrite) — defer until Ticket 2 telemetry shows sold-out flips causing meaningful cache churn.
- Model-facing history pruning, `messages_json` compaction beyond existing Confirm compaction, or `menu.to_prompt_dict()` deduplication.
- Stale-cart expiry, `last_customer_at` tracking, or 2-hour idle prompts.
- Adding a `reply_text` argument to tools, or changing `show_menu`'s SPEC §3.5 LLM follow-up.
- Deterministic `build_order_card` short-circuit (skipping the post-card LLM call) — evaluate only after measuring Ticket 3's telemetry in a separate follow-up.
- OpenRouter `reasoning_details` replay, `#` header Markdown stripping (risks `#3` queue numbers), automatic LLM retries (wait for telemetry), multi-bubble text debouncing, async/durable webhook queue, second prompt-cache breakpoint, or a standalone 25-dialogue eval runner.
- Any new third-party dependencies.

---

## 3. Settled Decisions

### 3.1 Card Integrity & LLM Response Safety
1. **Empty/missing choices**: Check `resp.choices` and `resp.choices[0].message` inside the handled failure path in `app/agent.py` (never raising `IndexError`/`AttributeError` out of `handle_customer_text`). On missing choice/message, run the exact same recovery as an LLM exception: check takeover/pause, `drop_unsent_card()`, `save_turn({"role": "assistant", "content": ERROR_REPLY})`, return `AgentTurn(reply_text=ERROR_REPLY, reason="error")`.
2. **Malformed tool arguments**: When `json.loads(tc.function.arguments)` fails or does not decode to a `dict`, do **not** coerce to `{}` and do **not** execute `_run_tool`. Return `{"ok": False, "error": "รูปแบบคำสั่งไม่ถูกต้อง กรุณาลองใหม่ค่ะ"}` as the tool result (preventing a broken `hand_over` call from defaulting to `reason="off_menu"`).
3. **Card capture & send-time validity**:
   - Capture `built_card_id` immediately when `build_card_snapshot_for_conv` returns a snapshot (before `card_wait_line` / `build_order_flex`), on both the text and location paths, so any exception while rendering still cancels that exact card.
   - If a later `update_cart` or `set_fulfillment` succeeds in the same turn (which clears `active_card_id` in SQLite), clear `built_flex = None` and `built_card_id = None`.
   - Before returning `flex_card` from `handle_customer_text`, verify `db.get_or_create_conversation(line_user_id).active_card_id == built_card_id`; if not, omit `flex_card` and `card_id`.

### 3.2 `update_cart` Validation & Cancel-Card Behavior (Option A)
1. **Validate before save**: In `update_cart`, build the candidate `items` list first. If `items` is non-empty, validate the entire cart via `menu.price_cart(items, ff, ov)` **before** calling `db.save_draft(conv.id, draft)`. If `MenuError` is raised (e.g., >20 lines or an earlier cart item became sold out), leave the stored draft and active card untouched and return `{"ok": False, "error": str(exc)}` with the specific Thai message. `action="clear"` and removing the last item to an empty list remain allowed even if an existing item is sold out.
2. **Cancel-card tap clears draft items (Option A, owner-approved 2026-11-26)**:
   - Add a dedicated DB method (e.g., `cancel_card_and_clear_items(line_user_id, card_id) -> bool`) called **only** by the customer Cancel postback button (`action=cancel`).
   - In one `BEGIN IMMEDIATE` transaction, match `line_user_id`, `shop_date = today`, and `active_card_id = card_id`. If matched: set `active_card_id = NULL`, `active_card_json = NULL`, set `draft["items"] = []` while preserving `fulfillment`, `address`, `location_url`, and `requested_time`, and increment `draft_revision = draft_revision + 1`.
   - A stale Cancel tap (wrong/expired `card_id`) returns `False` and leaves the newer draft and card untouched.
   - Internal cleanup (`drop_unsent_card`, failed `_safe_reply` on text/location) keeps calling `cancel_active_card()`, which invalidates only the card and preserves `draft_json`.
   - Record a 1-line changelog note in `SPEC.md` §3.9 when Ticket 1 lands.

### 3.3 Mid-Turn Owner Control & Formatting
1. **`RESUME_NOTE` consumption**: Check whether an expired `handed_over_until` marker is pending before the loop and include the exact `RESUME_NOTE` string in `system_msgs` for that turn. Call `db.consume_resume_note(conv)` only **after** the first usable LLM response arrives (before executing tools or returning the reply), preserving its `handed_over_until <= now_iso` conditional `UPDATE`. A failed or timed-out first LLM call leaves the marker pending for the customer's next text.
2. **`hand_over` batch priority**: Before executing a tool batch, scan `tool_calls` for any valid parsed `hand_over` call with `reason in LLM_HANDOVER_REASONS`. If present, execute no sibling mutating tools (`update_cart`, `set_fulfillment`, `build_order_card`, `cancel_order`, `set_payment_method`), call `drop_unsent_card()`, append `[user_msg, {"role": "assistant", "content": HANDOVER_REPLY}]`, and return `start_handover(...)`. Preserve the `reason="greeting"` refusal when no valid hand-over is present. If `cancel_order` triggers `paid_cancel_request` mid-batch, stop executing remaining sibling tools immediately.
3. **Global pause (`bot_enabled = 0`) re-check**:
   - In `app/agent.py`, re-check `bot_enabled` after every slow LLM call (and on error/deadline) alongside `owner_took_over()`. When paused, call `drop_unsent_card()`, append only `[user_msg]` to history, do **not** call `record_quiet_activity()` (global pause opens/updates no inbox card), and return `AgentTurn(reply_text=None, reason="bot_disabled")`.
   - In `app/slip_intake.py`, re-check `bot_enabled` after `slip_client.check(raw_qr)` returns and before committing `pay_with_slip(...)` or calling `problem(...)`. When paused, record the slip check row as `slip_unchecked` (with verified `amount_satang` and `transfer_at` when returned, so the claim never stays `pending`), commit no payment or order confirmation, open no inbox card, and return empty `SlipTurn()`.
4. **Narrow Markdown strip**: Before `save_turn` and returning `reply_text`, strip `**` bold markers (`**text**` → `text`, plus any stray `**`). Never strip `#` so `#3` queue numbers remain untouched.

### 3.4 Lean Telemetry & LINE Connection Pooling
1. **Structured logs (PII-free)**:
   - Assign an opaque 8-char hex `turn_id` per webhook event.
   - Log **one `INFO` line per LLM call** (`app.agent`): `turn_id`, `model`, `latency_ms`, `prompt_tokens`, `cached_tokens`, `completion_tokens`, `finish_reason`, `tools` (comma-separated `name:ok` / `name:err`), and `error` (`type(exc).__name__` + HTTP `status_code` when available). Read `resp.usage`, `prompt_tokens_details`, and `finish_reason` with `getattr(..., None)` so stub responses without usage fields never raise.
   - Log **one `INFO` line per webhook event** (`app.main`): `turn_id`, `event_type`, `reason`, `llm_calls`, `lock_wait_ms`, `total_ms`, `reply_ok`.
   - **Never log**: customer message text, addresses, map URLs, tool arguments, provider error bodies, reply tokens, or `line_user_id`.
2. **Pooled `HttpLineClient`**: Store one shared `httpx.Client(timeout=self._timeout_s)` on `HttpLineClient`, pass `timeout=3.0` on `show_loading`, add `close() -> None`, and close it on FastAPI lifespan shutdown when `create_app()` created the client itself.
3. **Webhook-level `started_at`**: Capture `started_at = time.monotonic()` once at the start of `line_webhook` before iterating `events` so lock wait and earlier events in the same payload count toward `TURN_DEADLINE_S` and `SLIPOK_START_DEADLINE_S`.

### 3.5 Live-State Injection & Prompt Tool Batching
1. **System Message 2 exact format** (`app/agent.py`):
   - Keep all existing v2 fields in order:
     `สถานะระบบล่าสุดตอนนี้ (ยึดข้อมูลนี้เหนือข้อความเก่าในแชท): ร้าน={open_text} | {busy_text} | ของหมดตอนนี้={sold_text} | ตะกร้าปัจจุบัน={cart_text} | ยอดค้างชำระ={unpaid_total} บาท | ที่อยู่เดิม={saved_addr_flag}` (+ ` | วิธีรับ={ff_text}` when `draft.get("fulfillment")` is set; + ` | วิธีจ่าย=เงินสด (ไม่ต้องส่ง QR)` when `db.pays_cash(line_user_id)` is true).
   - `cart_text`:
     - Empty: `ว่าง (0 รายการ)` (unchanged).
     - Non-empty: indexed lines matching `remove_index`, e.g. `[0] ข้าวกะเพรา (หมูกรอบ) ×1, [1] น้ำเปล่า (ขวด) ×1` (include dish Thai name, selected option Thai names if any, note if any, and `×qty`; fall back to `dish_id` if an item cannot be resolved so rendering never raises).
   - `saved_addr_flag`: `มี` when `customer.last_address` is non-empty, else `ไม่มี` (no full address text or coordinates, matching Rule 5).
   - `ff_text` (only when `draft.get("fulfillment")` is set): `รับเอง` for pickup; for delivery, `ส่ง (มีที่อยู่+พิกัดแล้ว)` / `ส่ง (มีที่อยู่แล้ว)` / `ส่ง (ยังไม่มีที่อยู่)`.
2. **Prompt tool batching**: Add 1 sentence to Rule 6 in `_system_prompt`: when the customer provides all required dish choices and fulfillment (or delivery with a saved address) in one message, call `update_cart`, `set_fulfillment`, and `build_order_card` together in one tool-call response rather than one per turn.

---

## 4. Acceptance Criteria (Observable Behavior)

### Correctness & Owner Control (Ticket 1)
- [ ] Given an LLM response with `choices=[]`, when `handle_customer_text` runs after a card was built earlier in the turn, then no exception escapes, the unsent card is cancelled in SQLite, history appends `ERROR_REPLY`, and the customer receives `ERROR_REPLY`.
- [ ] Given a tool call with invalid JSON arguments (`"{bad"` for `hand_over`), then no hand-over is started, no inbox card is created, and the tool returns `ok=False`.
- [ ] Given a turn where `build_order_card` succeeds and a subsequent `update_cart` or `set_fulfillment` modifies the draft in the same turn, then no stale Flex card is sent to LINE.
- [ ] Given a customer's draft contains a dish that became sold out, when the customer adds a second dish, then `update_cart` returns `ok=False` with the sold-out dish's Thai name, the stored draft is unchanged (no duplicate lines), and any prior active card is untouched. Given a cart with 20 lines, when a 21st line is added, then `update_cart` returns `ok=False` and the stored draft still has 20 lines.
- [ ] Given a customer with an active order card for delivery (`address="บ้าน 99"`, 1 item), when they tap **ยกเลิก** with that `card_id`, then `active_card_id` is `None`, `draft["items"] == []`, `draft["fulfillment"] == "delivery"`, `draft["address"] == "บ้าน 99"`, and `draft_revision` incremented. Given a stale `card_id`, then the current draft and card are untouched. Given a failed LINE reply on a text or location card, then `active_card_id` is `None` while `draft["items"]` is preserved.
- [ ] Given a hand-over just ended (resume note owed) and the first LLM call raises an exception, when the customer sends a second message and the LLM succeeds, then the second call's `messages` contains the exact `RESUME_NOTE`.
- [ ] Given an LLM batch `[cancel_order(oid), hand_over(reason="edit_confirmed")]` or `[update_cart(...), hand_over(reason="off_menu")]`, then neither `cancel_order` nor `update_cart` mutates SQLite, the customer receives `HANDOVER_REPLY`, and the inbox card records the hand-over reason.
- [ ] Given `bot_enabled` is flipped to `0` while an LLM call is in flight, then 0 replies are sent, any card built earlier in the turn is cancelled, no inbox card is created/updated, and only the customer's user message is appended to history.
- [ ] Given `bot_enabled` is flipped to `0` while `slip_client.check` is in flight, then 0 replies are sent, no order is marked paid or confirmed, no inbox card is created, and `slip_checks` records `slip_unchecked` (not `pending`).
- [ ] Given an LLM reply `"สั่ง **กะเพราหมูกรอบ** คิว #3 ค่ะ"`, then the sent and saved text is `"สั่ง กะเพราหมูกรอบ คิว #3 ค่ะ"`.

### Telemetry & LINE Pooling (Ticket 2)
- [ ] Given a text webhook turn (with stub LLM lacking `usage`/`finish_reason`, and with stub LLM providing `usage` + `finish_reason`), then 1 `app.agent` log line per LLM call and 1 `app.main` log line per webhook event are emitted with the same `turn_id`, numeric `latency_ms` / `lock_wait_ms` / `total_ms`, and zero PII (verified by asserting customer text, address, `line_user_id`, and `replyToken` do not appear in captured log output).
- [ ] Given `HttpLineClient` sends `show_loading`, `reply_messages`, and `push_text`, then all calls reuse one shared `httpx.Client` instance, and shutting down the FastAPI app closes it.
- [ ] Given a webhook payload with 2 events where the first takes time, then `started_at` for the second event reflects webhook receipt time rather than post-first-event time.

### Live State & Round-Trip Reduction (Ticket 3)
- [ ] Given a returning customer with `last_address="บ้าน 45/2"` and 2 items in `draft` (delivery, address set), when `handle_customer_text` calls the stub LLM, then System Message 2 contains exact indexed lines (`[0] ...`, `[1] ...`), `วิธีรับ=ส่ง (มีที่อยู่แล้ว)`, `ที่อยู่เดิม=มี`, and does **not** contain `"บ้าน 45/2"`.
- [ ] Given `_system_prompt`, then Rule 6 includes the single-call batching instruction for `update_cart`, `set_fulfillment`, and `build_order_card`.
- [ ] Given 8–10 realistic Thai dialogues run through `DEMO_REAL_LLM=1 ./scripts/dev.sh say` (within the 10-message `AGENTS.md` budget) covering: 1-shot pickup order, returning delivery customer, ambiguous `"กะเพราหมู"`, remove item by index, sold-out item, `"มีเมนูอะไรบ้าง"`, cash switch, and allergy hand-over — then all reach the expected SQLite state, 1-shot orders complete in ≤2 LLM calls, and before/after call counts and latencies from Ticket 2 logs are reported in the Proof Report.

---

## 5. Verification Contract
The executing agent must run and pass all checks below after its last edit before reporting done:
- **Static & automated tests**: `./scripts/check.sh` (`ruff format --check`, `ruff check`, `mypy app tests scripts`, `pytest -q`) — including `tests/test_guardrails.py`.
- **No live paid API calls in Tickets 1–2**: all automated tests inject stub/spy clients (`StubLLM`, `SpyLineClient`, `StubSlipClient`) via `create_app(...)` / `ShopDB(clock=...)`.
- **Runtime smoke (every ticket)**: `./scripts/dev.sh demo-up` → exercise changed flows via `./scripts/dev.sh say ...` + inspect `/kitchen` / logs → `./scripts/dev.sh demo-down`.
- **Real-LLM check (Ticket 3 only)**: `DEMO_REAL_LLM=1 ./scripts/dev.sh demo-up` for up to 10 customer text messages total (per `AGENTS.md` owner-approved budget); report exact message count, call counts, and latencies in the Proof Report. Stop and ask before exceeding 10 messages.
- **Baseline Rule**: Do not weaken or delete existing assertions. The **only** pre-approved baseline updates are:
  - Ticket 1: `SPEC.md` §3.9 note for Cancel-tap Option A (if any existing test checks draft after Cancel, list it explicitly).
  - Ticket 3: exact `LIVE_STATE` assertions in `tests/test_handover.py` (line 40) and `tests/test_wait.py` (lines 282–290) updated to include ` | ที่อยู่เดิม=ไม่มี`.
  - List every changed baseline in the Proof Report.

---

## 6. Autonomy Boundaries & Rollback
- **Agent may decide and log (reversible)**:
  - Internal helper names (`cancel_card_and_clear_items`, `_format_live_state`, `_strip_bold`, log formatting layout).
  - Exact Thai wording of the malformed-JSON tool error and Rule 6 batching sentence (preserving meaning).
  - Exact 8–10 Thai test sentences chosen for Ticket 3's demo check (within the 10-message budget).
- **Agent MUST stop and ask** (or end with `NEEDS APPROVAL` when AFK):
  - Any live LLM call in Tickets 1–2, or exceeding 10 real-LLM messages in Ticket 3.
  - Any SQLite schema change or migration.
  - Changing `show_menu` (SPEC §3.5), adding a `build_order_card` deterministic short-circuit, or implementing any item in Section 2 Out of Scope.
  - Modifying any test baseline other than the two pre-approved ones in Section 5.
  - Committing, pushing, or deploying.
- **Rollback Plan**: Zero schema changes; roll back any ticket cleanly with `git revert <commit>`.

---

## Vertical Tracer-Bullet Tickets

Tickets touch overlapping files (`app/agent.py`, `app/main.py`, `app/db.py`), so execute them **one at a time, in order**.

### Ticket 1: Agent, Card & Mid-Turn Control Fixes
- **Mode**: AFK (0 live API calls)
- **Reviewer**: GPT-6.2 Sol (high)
- **Blocked by**: None
- **Slice**:
  - `resp.choices` empty/missing guard + malformed JSON tool-args rejection (`app/agent.py`).
  - Immediate `built_card_id` capture, reset on subsequent draft write, and send-time `active_card_id` check on text and location paths (`app/agent.py`).
  - `update_cart` full `price_cart()` validation before `save_draft()` returning specific `MenuError` (`app/agent.py`).
  - `cancel_card_and_clear_items()` on customer Cancel postback only, clearing `draft["items"]` while preserving fulfillment/address/pin and bumping `draft_revision` (`app/db.py`, `app/main.py`, 1-line note in `SPEC.md` §3.9).
  - `RESUME_NOTE` consumed only after a usable LLM response; `hand_over` batch priority & paid-cancel early stop; mid-turn `bot_enabled = 0` re-check in `app/agent.py` and `app/slip_intake.py`; narrow `**` bold strip (`app/agent.py`).
- **Acceptance Criteria**:
  - [ ] All 10 criteria under **Correctness & Owner Control (Ticket 1)** in Section 4 pass via stubbed signed-webhook tests.
- **Verification**: `./scripts/check.sh` (including new regression tests for all 10 criteria and `tests/test_guardrails.py`).
- **Run-it check**: `./scripts/dev.sh demo-up`; send a customer text to build an order card via `./scripts/dev.sh say`, tap Cancel via `say <user> cancel <card_id>`, then send a new order and verify only the new item appears on the next card; stop with `./scripts/dev.sh demo-down`.

### Ticket 2: Lean Telemetry & LINE Connection Pooling
- **Mode**: AFK (0 live API calls)
- **Reviewer**: GPT-6.2 Sol (high)
- **Blocked by**: Ticket 1
- **Slice**:
  - Per-LLM-call and per-webhook-event structured `INFO` logging correlated by `turn_id`, PII-free and safe for `StubLLM` (`app/agent.py`, `app/main.py`).
  - Shared `httpx.Client` on `HttpLineClient` with `close()` wired into FastAPI lifespan shutdown (`app/line.py`, `app/main.py`).
  - Single webhook-level `started_at` timestamp before the event loop (`app/main.py`).
- **Acceptance Criteria**:
  - [ ] All 3 criteria under **Telemetry & LINE Pooling (Ticket 2)** in Section 4 pass.
- **Verification**: `./scripts/check.sh` (including `caplog` tests asserting structured fields and zero PII, plus pooled-client spy/close test).
- **Run-it check**: `./scripts/dev.sh demo-up`; run 2 `./scripts/dev.sh say` commands (text + postback), inspect `.dev.log` to confirm `turn_id`, `latency_ms`, `lock_wait_ms`, `total_ms`, and `reply_ok` appear with zero customer text/user IDs; stop with `./scripts/dev.sh demo-down`.

### Ticket 3: Live-State Draft Injection & Round-Trip Reduction
- **Mode**: AFK (up to 10 real-LLM messages via `DEMO_REAL_LLM=1`)
- **Reviewer**: GPT-6.2 Sol (high)
- **Blocked by**: Ticket 2
- **Slice**:
  - Enrich System Message 2 in `app/agent.py` with indexed draft lines, draft fulfillment/address/pin status, and `ที่อยู่เดิม=มี/ไม่มี` (no raw address/coords).
  - Add 1-sentence Rule 6 tool-batching instruction to `_system_prompt` (`app/agent.py`).
  - Update pre-approved `LIVE_STATE` assertions in `tests/test_handover.py` and `tests/test_wait.py`, plus new exact-message tests for populated cart and returning customer.
- **Acceptance Criteria**:
  - [ ] All 3 criteria under **Live State & Round-Trip Reduction (Ticket 3)** in Section 4 pass.
- **Verification**: `./scripts/check.sh`.
- **Run-it check**: `DEMO_REAL_LLM=1 ./scripts/dev.sh demo-up`; run 8–10 realistic Thai customer messages via `./scripts/dev.sh say` (≤10 total), inspect `.dev.log` telemetry for `llm_calls`, `cached_tokens`, and `latency_ms`, verify resulting SQLite cart/orders/handovers, stop with `./scripts/dev.sh demo-down`, and include the exact message count + metrics table in the Proof Report.
