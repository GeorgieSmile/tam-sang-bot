# Spec: Fast Path Keeps the LLM for Information Requests

- **Status**: Approved (2026-11-27) <!-- Executing agent: STOP if Status is Draft. Build only once Approved. -->
- **Date**: 2026-11-27
- **Scope**: The order-card fast path drops a side request that has no Thai question word, such as "ขอที่อยู่ร้านหน่อยค่ะ". Extend the check that keeps the second LLM call so it also catches requests for shop information, without slowing down plain orders.
- **Default Execution Mode**: AFK up to the paid re-run, then HITL (the re-run needs owner approval)
- **Preferred Reviewer Model**: GPT-6.2 Sol (high)

---

## 1. Context Pointers (Read First)
- `AGENTS.md`: the `check.sh` gate, the run-it rule and the 10-message real-LLM budget.
- `CODING_STANDARDS.md`: review rules. Rule 2 (history = what was sent) applies, because the fast path decides what gets sent.
- `docs/adr/0002-card-fast-path-question-word-check.md`: why the question-word check exists, the `card_path` labels and the revisit rule.
- `docs/specs/2026-11-26-card-fast-path.md` §3.1: the fast-path conditions. Condition 5 is the question-word check this spec extends.
- `app/agent.py`: `_QUESTION_RE`, `fast_card_line` (it calls `_QUESTION_RE.search(text)`), `qword_line`, the `card_path` values.
- `tests/test_handover.py`: the fast-path tests (`test_a_question_word_keeps_the_llm_reply_and_logs_what_it_said` and the tests around line 1477 and line 1640), its `Harness` and `_card_paths` helpers.
- `scripts/eval.py` and `evals/cases/*.yaml`: the offline eval that found the bug. `evals/results/2026-11-27-1449-anthropic-claude-haiku-5.5-all.json` is the baseline: 236/240, and gem-029 failed 3/3.
- `SPEC.md` item 18, the "Fast one-message orders" bullet in `README.md` and `README.th.md`: docs that describe the check.

## 2. The Bug (Evidence)
Eval case gem-029, a customer's first message:

> คะน้าหมูชิ้นพิเศษ 1 กล่อง กับน้ำเปล่า 1 ขวด รับเองนะคะ ขอที่อยู่ร้านหน่อยค่ะ เพิ่งเคยสั่งครั้งแรก

- The order is complete, so the LLM's first call returns only the 3 order tools.
- `_QUESTION_RE` finds no question word, because "ขอที่อยู่ร้านหน่อยค่ะ" is a request, not a question. So the fast path sends the fixed line.
- The address is never given. It failed 3/3 runs on Haiku and 3/3 on Sonnet. The model never gets the second call, so changing the model doesn't help.

A prototype pattern (§3) was run, at no cost, on the first message of all 80 eval cases:
- it caught all 10 side-question messages (the current check catches 9);
- it added 0 new false alarms on the 58 order messages (the current check already has 1).

## 3. Goal & Non-Goals

**Goal**
- A message that asks for shop information keeps the second LLM call, even with no question word: the address, the way there, location, phone number, bank account or PromptPay QR, the menu, opening or closing time.
- Plain orders keep the fast path, including polite ones like "ขอกะเพราหมูกรอบ 1 กล่อง" and "ขอเผ็ดน้อยหน่อย".

**Out of Scope (Do Not Build)**
- Any other change to the fast-path conditions or the fixed lines.
- A prompt change. A prompt line asking the LLM to answer in its first call was tried and had no effect (ADR-0002).
- An LLM classifier for side questions.
- New `card_path` labels.
- Changes to the eval harness or the eval cases.

## 4. Settled Decisions
- **The approach: a second pattern next to `_QUESTION_RE`**, not a stricter "order words only" gate. It's cheap, it can be tested offline on the eval set, and each false alarm only costs about 1.3 s.
- **Shape of the pattern:** a request verb (`ขอ`, `บอก`, `แจ้ง`, `รบกวน`, `ส่ง`), followed within a few characters by a shop-information noun (`ที่อยู่ร้าน`, `ที่ตั้ง`, `โลเคชั่น`/`โลเคชัน`, `พิกัด`, `แผนที่`, `ทางไป`, `เบอร์`, `เลขบัญชี`/`บัญชี`, `คิวอาร์`/`QR`, `เวลา`, `เมนู`). The prototype was `(ขอ|บอก|แจ้ง|รบกวน|ส่ง)\S{0,8}?(ที่อยู่ร้าน|ที่ตั้ง|โลเคชั่น|โลเคชัน|โลเค|พิกัด|แผนที่|ทางไป|เบอร์|เลขบัญชี|บัญชี|คิวอาร์|qr|QR|เวลา|เมนู)`. The executing agent may tune it, but every example in §5 must hold.
- **Telemetry:** the new pattern is part of the same check. It reuses `qword_same` and `qword_diff`, so the ADR-0002 count and revisit rule stay as they are, with the ADR text updated to say "question word or information request".
- **Honest reporting:** gem-029 is now a known case. The re-run after the fix is reported as "after the fix, on known cases", not as a fresh test.

## 5. Acceptance Criteria (Observable Behavior)
- [x] Given a first tool batch of exactly the 3 order tools, all ok, and the customer text **"…รับเองนะคะ ขอที่อยู่ร้านหน่อยค่ะ…"** (gem-029 verbatim), then the second LLM call runs and the turn logs `card_path=qword_same` or `qword_diff`.
- [x] The same holds for each of these:
  - "กะเพราไก่ 1 รับเองค่ะ ขอเบอร์ร้านด้วยครับ"
  - "ขอโลเคชั่นร้านหน่อยค่ะ ข้าวผัดกุ้ง 1 รับเอง"
  - "บอกทางไปร้านหน่อยค่ะ ผัดไทย 1 รับเอง"
  - "ขอเลขบัญชีด้วยค่ะ กะเพรา 1 รับเอง"
  - "ขอคิวอาร์ด้วย ข้าวผัด 1 รับเอง"
  - "แจ้งเวลาปิดร้านด้วยนะคะ สุกี้น้ำ 1 รับเอง"
- [x] Given the same batch and any of these texts, the fast path still runs (1 LLM call, `card_path=fast`):
  - "ขอกะเพราหมูกรอบ 1 กล่อง รับเองค่ะ"
  - "ขอเผ็ดน้อยหน่อยนะคะ ข้าวผัด 1 รับเอง"
  - "ขอข้าวผัดกุ้ง 2 ส่งที่หอพักต้นฝน ห้อง 12"
  - "ส่งตอนเที่ยงนะคะ กะเพรา 1 ส่งคอนโดริมเมฆ"
  - "รบกวนแยกน้ำด้วยค่ะ สุกี้น้ำ 1 รับเอง"
- [x] Over the first message of every case in `evals/cases/*.yaml`, the combined check matches every `side_question` case. It matches no more `simple`, `options`, `multi_item`, `multi_turn` or `messy` cases than `_QUESTION_RE` alone does today (1). This is a free pytest test over the frozen files.
- [x] *(Paid, after approval)* A re-run of Part A on Haiku (`--set all --final --runs 3`, about $0.14, 282 messages): gem-029 passes 3/3. No case that passed 3/3 in the baseline report fails 2 or more of 3. **Result (2026-11-27, `evals/results/2026-11-27-1541-anthropic-claude-haiku-5.5-all.json`):** 239/240; gem-029 0/3 → 3/3; gem-015 3/3 → 2/3 (Haiku added an unrequested `special`, as gpt-040 did in the baseline).
- [x] ADR-0002, `SPEC.md` item 18 and the README fast-path bullets in both languages describe the check as "question word or information request". The ADR's evidence section adds gem-029 as a real catch found by the eval.

## 6. Verification Contract
- **Static and tests:** `./scripts/check.sh`.
- **Behavioral:** the new tests in `tests/test_handover.py` (stub LLM with a scripted 3-tool batch, asserting the LLM call count and `card_path`), plus the eval-set test from §5 (for example in `tests/test_eval.py`).
- **Run it (paid, HITL):** `uv run python scripts/eval.py run --set all --final --runs 3 --max-cost-usd 1.0 --max-messages 300`. Quote the summary and compare it with the baseline report case by case.
- **Baseline rule:** no existing assertion is weakened or deleted. If an existing test's text newly matches the pattern and loses its fast path, change the test's text only when the test isn't about the check, and list each such change in the Proof Report.

## 7. Autonomy Boundaries & Rollback
- **Agent may decide and log:** the exact regex as long as every §5 example holds, the constant's name, the test names, the ADR wording.
- **Agent MUST stop and ask:**
  - the paid re-run;
  - any change to the fast-path conditions other than this check;
  - new `card_path` labels;
  - editing eval cases or the baseline report;
  - committing or pushing.
- **Rollback:** revert the commit. There are no schema or data changes.
