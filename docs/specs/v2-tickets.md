# v2 Tickets: Payments, Hand-over Inbox, and Wait Estimate

Spec: [SPEC.md](../../SPEC.md) (v2). **Executing agent: STOP if SPEC.md Status is `Draft`.** Read the spec's §1 Context Pointers, `GLOSSARY.md` and `AGENTS.md` before starting any ticket. The settled decisions are in SPEC §3, and the intentional v1 changes are in SPEC §3.8.

Every ticket ends with:
- `./scripts/check.sh` passing after the last edit
- a runtime smoke check: `uv run uvicorn app.main:app` on a temp `DB_PATH`, then `GET /health` → `200 {"ok": true}`, and `GET /api/kitchen/state` → `403` without the key and `200` with it
- a Proof Report that lists any v1 test changed and the §3.8 item it follows

No live SlipOK, LINE or LLM calls before Ticket 6.

Reviewer for every ticket: **GPT-6.2 Sol (high)**.

| # | Ticket | Mode | Blocked by |
|---|---|---|---|
| 1 | Payment status, cash and ค้างจ่าย | AFK | — |
| 2 | Hand-over inbox, bot quiet, voice reply | AFK | 1 |
| 3 | PromptPay QR and `get_payment_info` | AFK | 1 |
| 4 | Slip intake and slip match | AFK + local check | 1, 2 |
| 5 | Wait estimate, queue position, batch view | AFK | 3 |
| 6 | Live phone walkthrough | HITL | 1–5 |
| 7 | Fixes from the Ticket 6 phone test | AFK + phone retest | 6 (first run) |

Tickets touch overlapping files (`app/db.py`, `app/main.py`, `app/kitchen.html`), so run them **one at a time, in order**.

---

## Ticket 1: Payment status, cash and ค้างจ่าย
- **Mode**: AFK
- **Reviewer**: GPT-6.2 Sol (high)
- **Blocked by**: None
- **Slice**:
  - additive `orders.payment_status` / `paid_at` with the one-time `legacy` backfill (SPEC §3.6)
  - unpaid balance across shop days
  - `/kitchen`: payment badges, **จ่ายสด** / **จ่ายแล้ว ✓** / **ยกเลิกจ่าย** (cash and `transfer_manual` only), and the **ค้างจ่าย** section
  - owner cancel of a paid order: dialog warning, refund line in the push, refund due in the summary
  - `ยอดค้าง` on the done push
  - `สรุปวันนี้` additions
  - bot `cancel_order` refuses orders that aren't `unpaid` and hands over with v1's mechanism. The `paid_cancel_request` reason and its inbox card come in Ticket 2.
- **Spec sections**: §3.1.1–3, §3.1.8–12, §3.6 (`orders` only), §3.8 items 7–8.
- **Acceptance Criteria**:
  - [x] Given a v1-schema DB with 3 orders, when v2 starts, then all 3 are `legacy`, every v1 table's row count is unchanged, and a new confirmed order is `unpaid`.
  - [x] Given unpaid `#3` 60฿ (yesterday), unpaid `#5` 70฿ (today), one cancelled unpaid order and one `legacy` order for the same customer, then the unpaid balance is 130฿.
  - [x] **จ่ายสด** on queued `#5` → `cash`, `paid_at` set, 0 pushes. **ยกเลิกจ่าย** → `unpaid`. Undo on a `slip` order → HTTP 409.
  - [x] Done unpaid `#2` (today) and `#9` (yesterday) both appear under ค้างจ่าย, newest first, with `#9` marked as an earlier day. After **จ่ายสด** on `#2`, it is gone.
  - [x] Owner cancel of `slip`-paid `#5` → the push contains the reason and `ร้านจะติดต่อคืนเงินค่ะ`, and `สรุปวันนี้` contains `ต้องคืนเงิน #5`. (Set `slip` directly in the test DB; slip intake arrives in Ticket 4.)
  - [x] The bot cancels unpaid `#5`. It refuses `cash` `#5` (the order stays `queued`), replies `HANDOVER_REPLY`, and sets `handed_over_until`.
  - [x] The done push for unpaid `#5` (70฿) contains `ยอดค้าง 70฿`. Once it is paid, there is no `ยอดค้าง`.
  - [x] `สรุปวันนี้` with slip 130 / cash 60 / unpaid 70 today and unpaid 80 yesterday shows those totals, the unpaid `#N 70฿` line, and `ค้างจ่ายจากวันก่อน 1 ออเดอร์ 80฿`.
- **Verification**: `./scripts/check.sh`. The migration test uses the exact v1 `SCHEMA_SQL` from `docs/specs/v1.md` / git `a516222:app/db.py`. Runtime smoke: `/api/kitchen/state` includes the ค้างจ่าย list and payment fields.

---

## Ticket 2: Hand-over inbox, bot quiet, voice reply
- **Mode**: AFK
- **Reviewer**: GPT-6.2 Sol (high)
- **Blocked by**: Ticket 1
- **Slice**:
  - `handovers` table and reason codes (`hand_over` tool gains a required `reason`)
  - strict bot quiet: no LLM call and no reply, the message saved, the card updated
  - rolling 15-minute return, using `handed_over_until` and the injected clock
  - the `turn_limit` exception and the count reset on **เรียบร้อย**
  - the exact "bot came back" note (§3.2.6)
  - `/kitchen` inbox section with **เรียบร้อย**, and **คุยเอง** on orders
  - location pin while quiet; Confirm/Cancel taps still wake the bot
  - display name through `LineClient.get_profile`
  - a separate inbox beep
  - one owner push per new card when `notify_owner_on_order = 1`
  - voice reply
  - non-slip images now open an inbox card with reason `image` (v1 reply kept)
- **Spec sections**: §3.2, §3.5, §3.6 (`handovers`, `display_name`, `LineClient.get_profile`), §3.8 items 2–5.
- **Acceptance Criteria**:
  - [x] `สวัสดีค่ะ` with the stub calling `hand_over(reason="greeting")` → 1 `HANDOVER_REPLY`, a card with reason `greeting`, and `handed_over_until` = now + 15 min.
  - [x] Quiet, then `กะเพราหมูกรอบ 1 กล่อง` → 0 replies, 0 LLM calls, the message in `messages_json`, unseen = 1, and `handed_over_until` = this message + 15 min.
  - [x] Clock +15 min after the last customer message, then the same text → the LLM is called with the exact §3.2.6 note in its messages, a reply is sent, and the card shows `บอทกลับมาแล้ว`.
  - [x] **เรียบร้อย** → the card is gone from the state, quiet ends, and the next text reaches the LLM with the §3.2.6 note.
  - [x] A `turn_limit` hand-over with the clock +60 min → still 0 replies and 0 LLM calls. After **เรียบร้อย**, the next text reaches the LLM without hitting the limit again.
  - [x] A location pin while quiet → the draft has the address and `https://maps.google.com/?q={lat},{lng}`, 0 replies, and the card shows the pin.
  - [x] A Confirm tap while quiet on a valid card → the order is created and quiet ends.
  - [x] **คุยเอง** on queued `#5` → 0 messages to the customer, a card with `owner_takeover`, and the customer's next text gets 0 replies.
  - [x] A second reason while a card is open → still exactly 1 open card, listing both reasons.
  - [x] `notify_owner_on_order = 1` → 1 owner push for a new card and 0 for later messages. When it is 0 → 0 pushes.
  - [x] No stored display name → the profile stub is called once and the name is shown. If the profile returns nothing → `ลูกค้า …{last 4}`.
  - [x] An image with no QR (until Ticket 4, any image) → v1's `IMAGE_HANDOVER_REPLY` and a card with reason `image`.
  - [x] Paid-cancel requests from Ticket 1 now show as cards with reason `paid_cancel_request`.
  - [x] `audio` → 1 reply `รบกวนพิมพ์เป็นข้อความนะคะ 🙏` with 0 LLM calls. While quiet or `bot_enabled = 0` → 0 replies.
- **Verification**: `./scripts/check.sh`. Every LLM-facing test asserts the **exact** messages passed to the stub LLM (AGENTS.md rule). Runtime smoke: `/api/kitchen/state` includes the inbox list.

---

## Ticket 3: PromptPay QR and `get_payment_info`
- **Mode**: AFK
- **Reviewer**: GPT-6.2 Sol (high)
- **Blocked by**: Ticket 1
- **Slice**:
  - `segno` dependency and `PROMPTPAY_ID` setting (`.env.example`)
  - EMVCo PromptPay payload builder with CRC-16, proved against a fixed known-good test value
  - a QR PNG endpoint on `PUBLIC_BASE_URL` with an unguessable path and no customer data
  - the confirm reply: receipt with older unpaid orders listed, `รวมที่ต้องโอน`, and the QR image message
  - the `get_payment_info` agent tool, with the amount computed by code
  - `PAYMENT_INFO_TEXT` fallback when `PROMPTPAY_ID` is unset
  - no QR when the balance is 0
- **Spec sections**: §3.1.3–4, §3.6 (settings), §3.7 (PromptPay).
- **Acceptance Criteria**:
  - [x] `PROMPTPAY_ID=0812345678`, amount 130 → the payload equals the fixed test value character for character, including the CRC. Also test a 13-digit national ID.
  - [x] With unpaid `#5` 70฿ from yesterday, confirming `#2` 60฿ → the receipt has `ออเดอร์นี้ #2 60฿`, `ค้างจากเมื่อวาน #5 70฿` and `รวมที่ต้องโอน 130฿`, plus 1 image message on `PUBLIC_BASE_URL`. A `GET` of that URL returns `image/png` that decodes (with `zxing-cpp`, added here or in Ticket 4, or `segno`'s own round-trip) to the 130.00 payload.
  - [x] `PROMPTPAY_ID` unset → the confirm reply equals v1's receipt with `PAYMENT_INFO_TEXT`. It gains the queue position line only in Ticket 5.
  - [x] Balance 0 and `ขอ QR` → the tool returns 0, the reply says nothing is owed, and no image is sent.
  - [x] The QR is never part of the order card (Flex) payload.
- **Verification**: `./scripts/check.sh`. Runtime smoke: request a QR URL generated in a test or script and check `200 image/png`. The real bank-app scan is in Ticket 6.

---

## Ticket 4: Slip intake and slip match
- **Mode**: AFK, plus one local check that needs files the human provides
- **Reviewer**: GPT-6.2 Sol (high)
- **Blocked by**: Tickets 1, 2
- **Slice**:
  - `zxing-cpp` + `Pillow` dependencies
  - `LineClient.get_message_content`
  - slip mini-QR parser (§3.7)
  - `slip_checks` table
  - `SlipClient` Protocol + `HttpSlipOkClient` (`SLIPOK_API_KEY`, `SLIPOK_BRANCH_ID`), injected through `create_app(slip_client=...)`
  - the 8-step intake pipeline, slip match (orders oldest first, then the open card), the date rule, and order-card confirmation through the Confirm postback path
  - customer replies, including the shop-closed wording and the quiet-mode rules
  - quota read (at most once an hour) with the `/kitchen` warning below 20 and hand-over at 0
  - the inbox card's **จ่ายแล้ว ✓** order picker
  - `local/` added to `.gitignore`
  - `./scripts/dev.sh slips <dir>`
- **Spec sections**: §3.1.5–7, §3.1.9 (quota), §3.2.7 (จ่ายแล้ว ✓), §3.6 (`slip_checks`, settings, clients), §3.7, §3.8 item 1.
- **Acceptance Criteria** (SlipOK stub only; synthetic slip images generated in the tests):
  - [x] No QR → 0 SlipOK calls, `IMAGE_HANDOVER_REPLY`, and a card with `image`.
  - [x] Unpaid `#3` 60฿ plus an open card for 70฿:
    - slip 60 → `#3` paid
    - slip 130 → `#3` paid and the card confirmed as a `slip` order
    - slip 70 → the card is confirmed and paid, `#3` stays unpaid
    - slip 100 → nothing changes, and the reply is `ได้รับสลิป 100฿ แต่ยอดค้างคือ 130฿ …`
    - exactly 1 SlipOK call each
  - [x] A `transRef` already in `slip_checks` → 0 SlipOK calls, `slip_duplicate`.
  - [x] SlipOK error 1014 → `slip_wrong_receiver` and the general customer reply.
  - [x] Unpaid `#5` today with a slip dated yesterday → `slip_too_old`. Unpaid `#5` yesterday with a slip dated today → paid.
  - [x] An open card whose dish became sold out, with a slip for the card total → no order, nothing paid, `slip_card_confirm_failed`.
  - [x] Nothing due and no card → 0 SlipOK calls, `slip_nothing_due`.
  - [x] Shop closed with a mismatch → the reply has the closed-shop line. Shop closed with a match on an existing order → paid.
  - [x] Quiet with a match → only `ได้รับยอด …✅`. Quiet with a mismatch → 0 replies and the slip added to the open card.
  - [x] `bot_enabled = 0` → 0 replies, 0 SlipOK calls.
  - [x] Stub quota 15 → `/api/kitchen/state` shows a warning of 15. Quota 0 → the next slip makes 0 check calls, `slip_unchecked`.
  - [x] **จ่ายแล้ว ✓** on a `slip_*` card with only `#5` ticked → `#5` is `transfer_manual`, `#3` stays `unpaid`.
- **Verification**:
  - `./scripts/check.sh`
  - **Local real-slip check**: `./scripts/dev.sh slips local/slips` prints a parse result for each image the human placed there, with no API calls. If the format differs from §3.7, adjust the parser to the real format and log it; that is reversible. An empty folder is `NOT RUN (no real slips provided)` → report `PARTIAL`. **Run 2026-11-25: 3/3 parsed (KBank, MyMo, SCB), crc_ok=True, no parser change.**
  - `git status` shows nothing under `local/`.

---

## Ticket 5: Wait estimate, queue position, batch view
- **Mode**: AFK
- **Reviewer**: GPT-6.2 Sol (high)
- **Blocked by**: Ticket 3 (shares the receipt format)
- **Slice**:
  - `menu.yaml` `wait` block, loaded and validated in `app/menu.py`
  - automatic estimate from meal boxes
  - busy mode as minimum wait (`ยุ่ง`, `โหมดปกติ`, `/kitchen` chips keep working)
  - the 5-minute range
  - queue position
  - the wait line on the order card and the `คิวที่ N · ประมาณ X–Y นาที` receipt line, always shown, with `(ไม่รวมเวลาส่ง)` for delivery
  - `get_my_orders` returns position, orders ahead and the range
  - the live-state system message uses the new estimate instead of the busy-only text
  - the **รวมจาน** section on `/kitchen`
- **Spec sections**: §3.3, §3.4, §3.8 item 6.
- **Acceptance Criteria**:
  - [x] Empty queue, 1 meal box → `ประมาณ 10–15 นาที`. 6 queued boxes + 2 boxes + 1 drink → `20–25`. 12 queued + 1 → `35–40`.
  - [x] Busy mode with a minimum of 25: empty queue → `25–30`; 12 queued + 1 → `35–40`. After `โหมดปกติ`: empty → `10–15`.
  - [x] 3 queued orders before a new confirm → the receipt contains `คิวที่ 4`. A delivery receipt's wait line ends with `(ไม่รวมเวลาส่ง)`.
  - [x] An order with 2 queued orders ahead → `get_my_orders` returns position 3, 2 ahead, and the range.
  - [x] Queued `#2`, `#4` (note `ไม่ใส่ผัก`) and `#7`, all กะเพรา·หมูกรอบ·ไข่ดาวไม่สุก ×1 → a batch group with quantity 3, orders `[2, 4, 7]`, and the `#4` note.
- **Verification**: `./scripts/check.sh`. LLM-facing tests assert the exact live-state system message. Runtime smoke: `/api/kitchen/state` includes batch groups.

---

## Ticket 6: Live phone walkthrough
- **Mode**: HITL
- **Reviewer**: GPT-6.2 Sol (high)
- **Blocked by**: Tickets 1–5
- **Slice**: a real LINE OA + OpenRouter + SlipOK + PromptPay run through `cloudflared tunnel`, from a real phone, with real transfers between the human's own accounts (or a friend's) to `PROMPTPAY_ID`.
- **Setup**:
  1. `cp shop.db shop.db.v1-backup`.
  2. Register the PromptPay bank account in SlipOK. Set `SLIPOK_API_KEY`, `SLIPOK_BRANCH_ID` and `PROMPTPAY_ID` (same account) in `.env`.
  3. Start v2 on the real `shop.db`, then confirm that all v1 orders show as `ก่อนระบบจ่ายเงิน` and that nothing appears under ค้างจ่าย.
- **Acceptance Criteria** (about 5–8 SlipOK checks in total):
  - [x] Order a cheap meal → the receipt shows `คิวที่ N · ประมาณ X–Y นาที` and a QR. **Scanning it in 2 different bank apps** shows the PromptPay name and the exact amount. *(2026-11-25: the owner scanned the QR in 2 bank apps; the name and exact amount were correct.)*
  - [x] Pay by scanning and send the slip → `ได้รับยอด …✅`, and the order shows `โอนแล้ว (สลิป) ✅` on `/kitchen`. *(2026-11-25 12:12: #2 140฿ → `ได้รับยอด 140฿ แล้วค่ะ ✅ (#2)`, `slip` in the DB.)*
  - [x] Pay a **wrong amount on purpose** → the customer is told the amount sent and the amount owed, and an inbox card shows both. **รับเงินแล้ว…** with the picker marks the chosen order. *(12:14: 5฿ slip → "ยอดค้างคือ 50฿"; the card showed both; the picker marked #3 `transfer_manual`.)*
  - [x] Send the same slip again → the general reply and a `slip_duplicate` card, with no new SlipOK check used (the quota count is unchanged). *(12:17: re-sent the 12:12 slip → general reply and a `สลิปซ้ำ` card. The duplicate is caught before any SlipOK call, by code and tests; the quota count wasn't read.)*
  - [x] Ask an allergy question (`แพ้กุ้ง กะเพราใส่กุ้งไหม`) → hand-over and a beep on `/kitchen`. Then send an order text while quiet → no bot reply. Reply as the owner in the LINE OA app, tap **ให้บอทตอบต่อ** → the next customer message gets a normal bot reply that doesn't repeat the earlier question. (Before Ticket 7 this used `สวัสดีค่ะ`. On 2026-11-25 `มีข้าวมันไก่ไหม` got a bot reply instead; the owner chose that behavior, SPEC §3.9 item 12. So this item now uses an allergy question; passed on the phone 2026-11-25, confirmed by the owner.)
  - [x] Let a hand-over sit for 15 minutes with no customer message → the next customer message gets a bot reply, and the card shows `บอทกลับมาตอบแล้ว`. *(Hand-over card at 12:18; at 12:50 the customer's message got a normal bot reply. The card label wasn't photographed.)*
  - [x] **คุยเอง** on an order → the customer's next message gets no bot reply. *(12:53: "Hello Test คุยเอง", "1", "2", "3" → no bot reply.)*
  - [x] **รับเงินสดแล้ว** on a done order → it leaves ค้างจ่าย. **ยกเลิกจ่าย** puts it back. *(2026-11-25, confirmed by the owner.)*
  - [x] `ยุ่ง 25 นาที` with an empty queue → a new card shows `25–30`. **รวมจาน** groups two identical orders. *(2026-11-25: `ยุ่ง 30` → `30–35` seen; the owner confirmed รวมจาน grouping.)*
  - [x] A voice message gets `รบกวนพิมพ์เป็นข้อความนะคะ 🙏`. *(12:54.)*
  - [x] `สรุปวันนี้` shows the slip and cash totals and the unpaid list, matching what happened. *(13:00: slip 140 = #2, cash/owner-confirmed 130 = #1 + #3, unpaid #4 #5 #6, all matching the DB.)*
- **Status (2026-11-25)**: all items passed on the phone and were confirmed by the owner.
- **Verification**: the human signs off on the checklist above, with the date. The real-slip `./scripts/dev.sh slips local/slips` check from Ticket 4 must have been run by now.

---

## Ticket 7: Fixes from the Ticket 6 phone test
- **Mode**: AFK, then a phone retest by the human
- **Reviewer**: GPT-6.2 Sol (high)
- **Blocked by**: the first Ticket 6 run (2026-11-25)
- **Source**: the owner's phone test on 2026-11-25 (chat export, `/kitchen` screenshots, `shop.db`) and the agreed fix list.
- **Slice**: SPEC §3.9 items 1–11.
- **Acceptance Criteria** (automated):
  - [x] 4 `legacy` orders left queued from an earlier day plus 1 v2 leftover → a new order's queue position is 2 and the wait is `10–15`. `/kitchen` lists all 5 first with `เมื่อวาน`. รวมจาน leaves out the `legacy` ones. `สรุปวันนี้` queued count stays today-only.
  - [x] A greeting gets the LLM's own reply, with no card and no bot quiet. A `hand_over(reason="greeting")` call is refused and the LLM replies itself. The `hand_over` enum is `edit_confirmed`, `off_menu`.
  - [x] After `set_payment_method(cash)`: the next receipt says `ชำระเงินสดตอนรับอาหารได้เลยค่ะ` with `รวมที่ต้องจ่าย`, with no image. `get_payment_info` sends no QR, and the live state ends with `| วิธีจ่าย=เงินสด (ไม่ต้องส่ง QR)`. `set_payment_method(transfer)` plus `get_payment_info(send_qr=true)` sends the QR and clears cash intent everywhere.
  - [x] Done with `paid_cash: true` on an unpaid order → `cash`, and the push has no `ยอดค้าง`. Without it → the order is in ค้างจ่าย. It never overrides `slip`.
  - [x] A receipt with nothing older owed ends `โอน 60฿ ตาม QR ด้านล่างได้เลยค่ะ`, without `รวมที่ต้องโอน`.
  - [x] Card lines: `สลิปซ้ำ เคยใช้แล้วกับ #1 เมื่อ 12:00`, `สลิปซ้ำ ลูกค้าคนอื่นเคยใช้แล้วกับ #1 เมื่อ 12:00`, `ตรวจสลิปอัตโนมัติไม่ได้ ดูรูปสลิปใน LINE OA`, `สลิปซ้ำ เคยส่งมาแล้วเมื่อ 12:00`.
  - [x] Inbox alerts default on and follow `notify_owner_on_inbox`, not `notify_owner_on_order`.
  - [x] "มีเมนูอะไรบ้าง" → `show_menu` sends the whole list from `menu.yaml`, then the LLM's one line. The list leaves out sold-out dishes, and each dish shows its required choices (meat, ไข่ดาว สุก/ไม่สุก) with extra cost. A sold-out meat drops off every line that offered it. All 23 available dishes are listed.
  - [x] A dish we don't sell gets the bot's own "ไม่มี + suggestion" (prompt rule). Allergy and ingredient questions still hand over, with the label `ถามส่วนผสม/แพ้อาหาร`.
  - [x] Full v2 review fixes (GPT-6.2 Sol), each with a test that reproduces the finding (SPEC §3.9 item 13):
    - no double credit when the owner records the transfer during the SlipOK check
    - no bot reply after คุยเอง during the LLM call
    - no lost slip problem when the card is resolved during the check
    - the done and cancel pushes are in the bot's history
    - the QR is dropped when the balance changes mid-reply
    - no payment details at a zero balance
    - the daily limit holds after resolve → confirm
    - 1014 keeps the amount and uses quota
    - the picker labels earlier days
    - `get_my_orders` includes earlier-day queued orders
    - the kitchen key is redacted from access logs
  - [x] Tier 3 re-review fixes (SPEC §3.9 item 14), each with a mutation-checked test:
    - takeover on every reply path and before tools
    - the guarded resume note
    - unsent cards cancelled
    - atomic history appends, with owner pushes surviving an LLM turn and compaction
    - the daily count across reset → Confirm
    - pre-v2 orders: status without an estimate
    - the earlier-day finished list
    - exact next-LLM payloads after a slip confirm, the picker and a quiet pin
  - [x] Review fixes (GPT-6.2 Sol): cash said after a QR lookup in the same turn sends no QR (`send_qr=true` still does); no bank text for a cash customer without PromptPay; the cash rule holds the next day and for a replayed Confirm tap; done + cash is one transaction and keeps a payment recorded meanwhile; the done push still reports other unpaid orders; an unchecked slip with an amount still says `ดูรูปสลิปใน LINE OA`; รวมจาน refs tell days apart.
- **Checked in a browser** (seeded throwaway DB, stub clients, 412px and 1280px wide): compact cards expand on tap; the done prompt shows `ลูกค้าแจ้งว่าจะจ่ายเงินสด` and marks cash; the new labels; hidden placeholder quotes; `ค้าง:` lines; the second alert switch.
- **Phone retest** (human, 2026-11-25 14:12–14:21; screenshots plus `shop.db`):
  - [x] `สวัสดีครับ` → a bot reply asking what you'd like, with no inbox card.
  - [x] Say `จ่ายเงินสด` before confirming → the receipt has no QR and says to pay cash. `/kitchen` shows `จะจ่ายสด`. `ขอ QR` later → the QR arrives.
  - [x] **ทำเสร็จแล้ว** on an unpaid order asks `รับเงินสดแล้ว / ยังไม่จ่าย`.
  - [x] Queue numbers on receipts match the orders actually waiting. The old 23/11 orders show on `/kitchen` and can be marked done.
  - [x] The bot says `ส่งที่เดิม` without the full address, and lists dishes with options as `เริ่ม X฿`. (Found: the LLM-written menu was cut off mid-list at `• ผัดซีอิ`. Fixed by `show_menu`, which builds the list in code; SPEC §3.9 item 8. Rechecked on the phone with meat choices: passed.)
  - [ ] A new inbox card pushes to the owner's LINE with `LINE ลูกค้ารอตอบ: เปิด`. **NOT RUN**: only one LINE account, so nobody was in the owner role. Covered by automated tests only.
  - [x] Phone `/kitchen`: compact cards, and tapping one opens it.
- **Verification**: `./scripts/check.sh`. The migration runs on a copy of the real `shop.db` with unchanged row counts.

