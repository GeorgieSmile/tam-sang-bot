# Spec: Order-Card Fast Path & Shop Address

- **Status**: Approved (2026-11-26) <!-- Executing agent: STOP if Status is Draft. Build only once Approved. -->
- **Date**: 2026-11-26
- **Scope**: Skip the second LLM call when a customer's one-message order passes the §3.1 gate (the three order tools only, all ok, no side question), and send a fixed line instead. Also give the bot a (fictional) shop address so it can answer "ร้านอยู่ตรงไหน". Zero schema changes.
- **Default Execution Mode**: AFK (Ticket 1: ≤10 real-LLM messages; Ticket 2: ≤3 real-LLM messages; both within the `AGENTS.md` budget)
- **Preferred Reviewer Model**: GPT-6.2 Sol (high), for both tickets

---

## 1. Context Pointers (Read First)
- `CODING_STANDARDS.md`: the 4 review rules. The fast path is a new reply path, so Rule 1 (owner takeover re-checked on every reply path), Rule 2 (history = what was sent) and Rule 3 (re-check at the write, cancel undelivered cards by exact ID) all apply.
- `AGENTS.md`: the `./scripts/check.sh` gate, the `demo-up` / `say` run-it rule, and the 10-message `DEMO_REAL_LLM=1` budget.
- `docs/specs/2026-11-26-agent-polish.md`: the previous spec. Its Rule 6 batching sentence and telemetry (`TurnStats`, `llm_call` / `webhook_event` lines) are what this spec builds on.
- `app/agent.py`:
  - the tool loop in `handle_customer_text`: the `if not tool_calls:` final-reply branch, the batch execution (builds run last, `edit_failed`), `control_stop()` and `drop_unsent_card()`;
  - `TurnStats`;
  - the `set_fulfillment` result (`fulfillment`, `used_saved_address`);
  - `build_order_card`;
  - `_system_prompt` (Rules 5–6);
  - the `get_shop_status` tool.
- `app/main.py`: the `webhook_event` log line built from `TurnStats`.
- `app/menu.py`: `ShopConfig` and `load_menu`.
- `menu.yaml`: the `shop:` section.
- Tests that may need the pre-approved expectation change (§5): in `tests/test_agent.py`, `test_agent_orders_with_default_spice_and_busy_wait_time_on_flex_card`, `test_agent_reuses_returning_customer_saved_address`, `test_agent_preserves_location_pin_and_handles_bad_tool_args_and_closed_shop`, `test_rule_6_asks_for_one_batched_call_when_the_order_is_complete` and `test_card_is_not_built_after_a_failed_edit_in_the_same_batch`; in `tests/test_handover.py`, `test_resume_note_never_clears_a_takeover_started_meanwhile` and `test_fresh_review_batch_order_and_cleared_cards`; in `tests/test_wait.py`, `test_normal_mode_flex_card_and_full_live_state`.
- Evidence: the owner-approved A/B run of 2026-11-26 is recorded in §3.3.

---

## 2. Goal & Non-Goals

**Goal**
- A one-message order that passes the §3.1 gate is answered with 1 LLM call instead of 2. In the A/B run this was about 1.1 s faster (4.35 s → 3.23 s average), with the same reply text.
- Every customer message containing a side question still gets an LLM-written answer.
- Telemetry shows how often the fast path runs, and whether the question-word check ever catches a real question. That data decides later whether to keep the check.
- The bot can tell customers where the shop is.

**Out of Scope (Do Not Build)**
- A fast path for any other batch shape. Examples: `update_cart` + `build_order_card` without `set_fulfillment`; a lone `build_order_card`; a location pin. Widen it only after telemetry, in a new spec.
- A prompt line asking the LLM to answer side questions in the same response as its tool calls. It was tested and had no effect (call-1 text was empty in 10/10 turns).
- Changing Rule 6's wording, `show_menu`, or any other LLM follow-up.
- A map link or real coordinates for the shop address.
- Any SQLite schema change, new dependency, or LLM retry logic.

---

## 3. Settled Decisions

### 3.1 Fast-path gate (Ticket 1)
After the **first** LLM response of a turn returns tool calls and the batch has run, skip the next LLM call only when **all** of these hold:
1. The batch's tool names are drawn only from `update_cart`, `set_fulfillment` and `build_order_card`, and each of the three appears at least once. Repeats are allowed, for example two `update_cart` calls for two dishes.
2. Every tool result in the batch has `ok: true`. That rules out bad arguments, the `CARD_AFTER_FAILED_EDIT` refusal, and a sold-out or closed-shop error.
3. The card built in this batch is still the active card: `built_flex` is set and `active_card_id == built_card_id`.
4. The first response's text content is empty after `.strip()`.
5. The customer's text contains none of these question words (the "question-word check"): `ไหม` `มั้ย` `มั๊ย` `ไม๊` `กี่` `อะไร` `ยังไง` `อย่างไร` `เท่าไหร่` `เท่าไร` `ไหน` `เมื่อไหร่` `ทำไม` `รึเปล่า` `หรือเปล่า` `?`. The owner chose to keep this check and log it (2026-11-26).

If any condition fails, the turn runs exactly as today.

### 3.2 Fixed lines (owner-approved 2026-11-26)
The line is chosen from the **last** `set_fulfillment` result in the batch:

| Case | Line |
|---|---|
| `fulfillment == "pickup"` | `ตรวจสอบรายการบนการ์ดแล้วกดยืนยันได้เลยนะคะ` |
| `fulfillment == "delivery"` and `used_saved_address` is true | `ส่งที่เดิมนะคะ ถ้าเปลี่ยนที่อยู่บอกได้เลยค่ะ\nตรวจสอบรายการบนการ์ดแล้วกดยืนยันได้เลยนะคะ` |
| `fulfillment == "delivery"` otherwise | `ตรวจสอบรายการและที่อยู่บนการ์ดแล้วกดยืนยันได้เลยนะคะ` |

### 3.3 Reply path, history and telemetry
1. **Takeover and pause re-check:** before replying, call `control_stop()`. If the owner tapped คุยเอง or paused the bot while the tools ran, follow its existing behaviour: the card is cancelled and there is no reply (CODING_STANDARDS Rule 1).
2. **Same final-reply path:** after that check, the fast path goes through the same code as a no-tool-call response:
   - the send-time `active_card_id` check, which drops the card if it changed;
   - `save_turn({"role": "assistant", "content": <fixed line>})`;
   - `AgentTurn(reply_text=<fixed line>, flex_card=built_flex, card_id=built_card_id, reason="ok")`.

   History therefore holds the assistant tool message, the tool results and the fixed line, which is exactly what was sent (Rule 2).
3. **Telemetry:** add one PII-free field, `card_path`, to the `webhook_event` line. It has 4 possible values:
   - `fast`: the fast path ran.
   - `qword_same`: conditions 1–4 held, but the question-word check (5) failed. Call 2 ran, and the turn's final reply equals the fixed line that would have been sent.
   - `qword_diff`: as `qword_same`, but the final reply differs from that line, meaning the check caught real content.
   - `-`: anything else.
4. **A/B evidence** (20 real Haiku messages, owner-approved, 2026-11-26). The prototype was a scratch-script patch:
   - On the 6 messages that qualified, the current system averaged 4.35 s and the prototype 3.23 s.
   - The reply was identical in 5 of 6. For the new address, the prototype's line was better.
   - All 4 side-question messages kept call 2.

### 3.4 Shop address (Ticket 2)
1. **Config:** `menu.yaml` gets `shop.address: "เลขที่ 88 ซอยตามสั่ง 7 ถนนอร่อยดี"`. The shop is imaginary, so this address is deliberately fictional (owner decision, 2026-11-26), and there is no map link.
2. **Loading:** `ShopConfig` gains `address: str | None = None`. `load_menu` reads `shop_raw.get("address")`, and a blank value counts as `None`.
3. **Exposure:**
   - When `address` is set, `_system_prompt` adds one sentence giving the shop address and saying to answer with it when a customer asks where the shop is or how to come and pick up.
   - `get_shop_status` returns `"shop_address": menu.shop.address`.
   - When `address` is unset, the prompt is exactly as today and `get_shop_status` returns `"shop_address": null` (as §4 requires). A non-text `address` (number, list, bool) fails `load_menu` with `MenuError`.

---

## 4. Acceptance Criteria (Observable Behavior)

### Fast path (Ticket 1), proved with stub-LLM signed-webhook tests
- [ ] Given the shop is open and the customer has an empty cart, the customer sends `กะเพราหมูสับไข่ดาวไม่สุก 1 รับเองค่ะ`. The stub's first response is one batch of `update_cart` + `set_fulfillment(pickup)` + `build_order_card`, all ok. Then:
  - the stub LLM is called exactly **once**;
  - the LINE reply is `[text "ตรวจสอบรายการบนการ์ดแล้วกดยืนยันได้เลยนะคะ", the order-card Flex]`;
  - the last history message is `{"role": "assistant", "content": <that line>}`;
  - the `webhook_event` log line has `card_path=fast` and `llm_calls=1`.
- [ ] Given a returning customer with a saved address, and a batch whose `set_fulfillment(delivery)` uses it (`used_saved_address: true`), then there is 1 LLM call and the reply text is the saved-address line from §3.2.
- [ ] Given a batch whose `set_fulfillment(delivery)` carries a new address, then there is 1 LLM call and the reply text is `ตรวจสอบรายการและที่อยู่บนการ์ดแล้วกดยืนยันได้เลยนะคะ`.
- [ ] In each of the following cases, the stub is called **twice**, the LINE text is the stub's second response, and `card_path` is `-`:
  - (a) the batch also contains `get_shop_status`;
  - (b) `update_cart` returns `ok: false` (a sold-out dish);
  - (c) the first response has non-empty text alongside the 3 tools;
  - (d) the batch is only `update_cart` + `build_order_card`;
  - (e) `build_order_card` fails (delivery with no address).
- [ ] Given the customer text `กะเพราหมูสับ 1 เผ็ดน้อยได้ไหมคะ รับเอง` and a qualifying batch:
  - if the stub's second response is exactly the pickup fixed line, there are 2 LLM calls and `card_path=qword_same`;
  - if it is any other text, `card_path=qword_diff`.
- [ ] Given a qualifying batch where the owner's คุยเอง (bot quiet) is set during the batch, after call 1 returns and before the reply (for example, triggered from a wrapped tool write), then:
  - 0 LINE messages are sent;
  - the card built this turn is cancelled in SQLite;
  - history follows the existing takeover behaviour.
- [ ] *(Real LLM, run-it.)* `DEMO_REAL_LLM=1` with ≤10 customer messages: the 6 qualifying message types (pickup 1 dish, pickup 2 dishes, saved address, new address, order + `ขอบคุณมากนะคะ`, order + `ขอช้อนส้อมพลาสติกด้วยนะ`), plus the side-question messages `…เปิดถึงกี่โมงคะ`, `…จ่ายเงินสดนะคะ` and `…เผ็ดน้อยได้ไหมคะ`.
  - Every qualifying message gets 1 LLM call and the right fixed line.
  - Every side question gets an LLM-written answer.
  - Report each turn's `total_ms` against the A/B baseline (4.35 s average for qualifying turns).

### Shop address (Ticket 2)
- [ ] Given `menu.yaml` with `shop.address` set, then `load_menu(...).shop.address == "เลขที่ 88 ซอยตามสั่ง 7 ถนนอร่อยดี"`, the System Message 1 text contains that address, and the `get_shop_status` tool result has `shop_address` equal to it.
- [ ] Given a menu file with no `shop.address`, then `shop.address is None`, System Message 1 contains no address sentence, and `get_shop_status` returns `shop_address: None`.
- [ ] *(Real LLM, run-it.)* `DEMO_REAL_LLM=1` with ≤3 customer messages, including `ร้านอยู่ตรงไหนคะ` and `กะเพราหมูสับไข่ดาวสุก 1 รับเองค่ะ ร้านอยู่ตรงไหน`. Each reply contains `ซอยตามสั่ง 7`. The second message still gets its order card, and no hand-over is started.

---

## 5. Verification Contract
The executing agent must run and pass all checks below after its last edit before reporting done:
- **Static & automated tests**: `./scripts/check.sh` (`ruff format --check`, `ruff check`, `mypy app tests scripts`, `pytest -q`).
- **No live paid calls in automated tests**: stub or spy clients only, as `AGENTS.md` requires.
- **Run-it check (each ticket)**:
  1. `DEMO_REAL_LLM=1 ./scripts/dev.sh demo-up`.
  2. Send the §4 messages with `./scripts/dev.sh say <user> text "<message>"`, using a fresh user per order.
  3. Read the `webhook_event` / `llm_call` lines for `card_path`, `llm_calls` and `total_ms`.
  4. Stop with `./scripts/dev.sh demo-down`.
  5. Report the exact real-message count in the Proof Report: ≤10 for Ticket 1 and ≤3 for Ticket 2. Stop and ask before going over.
- **Baseline Rule**: do not weaken or delete assertions. The **only** pre-approved expectation change applies to tests in the §1 list whose stub scripts one first-response batch that meets §3.1 (all three tools, all ok, empty text, no question word) and that assert the second LLM reply or the call count. For those, expect one fewer LLM call and the §3.2 fixed line.
  - Where a test's *purpose* is the second LLM call, give it a question-word text or an extra tool so it still exercises that path. Do not change what it asserts.
  - List every changed test, and why, in the Proof Report. Any other baseline change needs owner approval.

---

## 6. Autonomy Boundaries & Rollback
- **Agent may decide and log (reversible)**:
  - helper names (e.g. `_fast_card_line`, `_QUESTION_RE`);
  - where in the loop the gate is computed, as long as §3.3's order holds: `control_stop` first, then the send-time card check, then `save_turn`;
  - the exact Thai sentence that adds the shop address to the prompt;
  - the exact real-LLM test messages, within budget.
- **Agent MUST stop and ask** (or end with `NEEDS APPROVAL` when AFK):
  - widening the gate (other tool sets, dropping a condition, changing the question-word list's meaning) or changing a §3.2 line;
  - using a real address or a map link;
  - any baseline change outside §5;
  - exceeding the real-LLM budgets;
  - any schema change;
  - committing, pushing or deploying.
- **Rollback Plan**: no schema or data changes. `git revert <commit>` restores the 2-call flow. `card_path` is a log field only.

---

## Vertical Tracer-Bullet Tickets
Both tickets edit `app/agent.py`, so run them one at a time, in order.

### Ticket 1: Order-Card Fast Path
- **Mode**: AFK (≤10 real-LLM messages)
- **Reviewer**: GPT-6.2 Sol (high)
- **Blocked by**: None
- **Slice**:
  - the §3.1 gate and the §3.2 line choice in `handle_customer_text`, plus the §3.3 reply path (`app/agent.py`);
  - a `card_path` field on `TurnStats`, logged on the `webhook_event` line (`app/agent.py`, `app/main.py`);
  - tests for every Ticket 1 criterion, and the §5 expectation updates;
  - a 1-line `SPEC.md` §3.9 changelog item (18).
- **Acceptance Criteria**:
  - [ ] All **Fast path (Ticket 1)** criteria in §4.
- **Verification**: `./scripts/check.sh`.
- **Run-it check**: §5 run-it steps with the Ticket 1 messages. Each qualifying turn's log line has `card_path=fast llm_calls=1`, and each side-question turn has `llm_calls≥2` and an answer in the reply.

### Ticket 2: Shop Address
- **Mode**: AFK (≤3 real-LLM messages)
- **Reviewer**: GPT-6.2 Sol (high)
- **Blocked by**: Ticket 1 (same file; sequential only)
- **Slice**:
  - `shop.address` in `menu.yaml`, `ShopConfig.address` and its loading (`app/menu.py`);
  - the prompt sentence and the `get_shop_status` field (`app/agent.py`);
  - tests for both the set and unset cases;
  - a 1-line `SPEC.md` §3.9 changelog item (19).
- **Acceptance Criteria**:
  - [ ] All **Shop address (Ticket 2)** criteria in §4.
- **Verification**: `./scripts/check.sh`.
- **Run-it check**: §5 run-it steps with the 2 address messages. Each reply contains `ซอยตามสั่ง 7`, and the order message also returns a card.
