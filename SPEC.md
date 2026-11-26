# Spec v2: Payments, Hand-over Inbox, and Wait Estimate

- **Status**: Approved (2026-11-24, owner said "start building"); amended 2026-11-25 by the owner-approved Ticket 6 feedback (§3.9, Ticket 7) <!-- Executing agent: STOP if Status is Draft. Build only once Approved. -->
- **Date**: 2026-11-24
- **Scope**: On top of the shipped v1:
  - slip-verified payment tracking with an exact-amount PromptPay QR code
  - a hand-over inbox on `/kitchen`, with the bot coming back on its own
  - an automatic wait estimate and queue position
  - a batch-cooking view and replies to voice messages
  - the goal: the owner spends less time on chat and slips, in both busy and quiet hours
- **Default Execution Mode**: AFK (Tickets 1–5, 7) · HITL (Ticket 6)
- **Preferred Reviewer Model**: GPT-6.2 Sol (high), for all tickets
- **Tickets**: [docs/specs/v2-tickets.md](docs/specs/v2-tickets.md)
- **Previous version**: [docs/specs/v1.md](docs/specs/v1.md) (shipped 2026-11-23). Everything in v1 still holds unless §3.8 lists it as an intentional change. §3.1–3.8 describe the final v2 behavior, including the Ticket 6 phone-test changes; §3.9 is the changelog of those changes and the decisions not to change things.

---

## 1. Context Pointers (Read First)
- `docs/specs/v1.md`: v1 behavior, base SQLite schema, and v1 acceptance criteria.
- `GLOSSARY.md`: the project's terms (shop day, order card, order, queue number, queue position, meal box, wait estimate, minimum wait, payment status, unpaid balance, slip, slip match, refund due, hand-over, bot quiet, bot paused, inbox card). Use them in code names, comments and tests.
- `docs/adr/0001-payments-tracked-not-gated.md`: why unpaid orders are still cooked.
- `AGENTS.md`: the check gate, the rule against live API calls in tests, and the out-of-band DB change rule.
- `app/db.py`:
  - `SCHEMA_SQL` (created with `CREATE TABLE IF NOT EXISTS`, with no migrations yet), `ShopDB`, `_tx(immediate=True)`
  - the confirm path, `cancel_customer_order`, `cancel_order_by_owner`, `mark_order_done`
  - `get_kitchen_orders_today`, `set_handed_over` / `clear_handed_over` / `is_handed_over`
- `app/agent.py`:
  - `handle_customer_text`: hand-over wake-up through `_run_tool` and the `handed_over` checks, the 30-turn limit, and the live-state system message
  - `handle_image_message`, `handle_location_message`, `TOOLS`
- `app/main.py`:
  - `create_app(...)`: the injection points for stub clients
  - webhook routing order: postback → owner → `bot_enabled` → sticker/audio/video → image → location → text
  - the `/api/kitchen/*` endpoints
- `app/line.py`: the `LineClient` Protocol, `NoopLineClient`, `HttpLineClient`, `build_order_flex`, `format_order_receipt`.
- `app/owner.py`: owner commands (`ยุ่ง`, `โหมดปกติ`, `สรุปวันนี้`, `คิว`, …).
- `app/kitchen.html`: the board UI, with 3s polling, the beep, and the collapsed "วันนี้ทำแล้ว" list.
- `menu.yaml`: item `category` is `meal` (15), `side` (5) or `drink` (4).
- `tests/test_webhook.py`: the `SpyLineClient` pattern. New stubs follow it.
- External docs. Check each against the current docs before coding against it:
  - SlipOK API: <https://slipok.com/api-documentation/>, check-slip: <https://slipok.com/api-documentation/check-slip/>, quota: <https://slipok.com/api-documentation/check-slip-quota/>
  - LINE Messaging API reference: <https://developers.line.biz/en/reference/messaging-api/> (get message content, get profile, image message, reply token lifetime)
  - Thai QR / PromptPay (EMVCo merchant-presented QR, tag 29) and the Bank of Thailand slip-verification mini-QR (see §3.7).

---

## 2. Goal & Non-Goals

**Goal**
- The owner stops checking transfer slips by hand. A valid slip marks the right orders paid and tells the customer, with no owner action.
- Customers pay by scanning a PromptPay QR code with the exact amount filled in.
- The owner sees every conversation that needs a person on `/kitchen`. The bot never talks over the owner, and it never stays silent all day because the owner forgot to bring it back.
- Customers always see their queue position and a rough wait, and the owner no longer has to set the wait by hand.
- The cook sees identical dishes grouped for batch cooking.
- Voice messages get a reply instead of silence.

**Out of Scope (Do Not Build)**
- Requiring payment before cooking, limits on unpaid orders, or pay-first rules for any customer group (ADR-0001).
- Expiring QR codes, merchant or payment-gateway QR codes, or bank APIs other than SlipOK.
- Partial payments, adding several slips together, or splitting one slip across orders in any way other than the slip match rule.
- Automated refunds, or moving a cancelled order's payment to another order.
- A reply box on `/kitchen`, or any owner-to-customer messaging from the app. The owner replies in the LINE OA app.
- Speech-to-text for voice messages.
- Pre-orders, pickup time slots, queue caps, limited stock counts, shop-info answers (all v3), and one-tap reorder (dropped).
- Learning cook times from order history.
- Pushing wait-time updates to customers.
- Deep links into LINE OA Manager chats.
- New frameworks, databases, or auth libraries. The only new dependencies allowed are `zxing-cpp`, `Pillow` and `segno`.

---

## 3. Settled Decisions

### 3.1 Payments (see ADR-0001)
1. **Trust model.** A confirmed order goes on the queue right away, whether or not it is paid. Payment can come before, during or after cooking, or on a later shop day.
2. **Payment status** of an order:

   | Value | Shown on `/kitchen` | Set by |
   |---|---|---|
   | `unpaid` | ยังไม่จ่าย | default for new orders |
   | `slip` | โอนแล้ว (สลิป) ✅ | a verified slip match |
   | `cash` | 💵 เงินสด | owner button **รับเงินสดแล้ว** |
   | `transfer_manual` | โอนแล้ว (ร้านเช็ค) | owner button **รับโอนแล้ว** (after checking a transfer themselves) |
   | `legacy` | ก่อนระบบจ่ายเงิน | the one-time migration of v1 orders |

   - Only `cash` and `transfer_manual` can be undone with **ยกเลิกจ่าย**, which sets the order back to `unpaid`. Undo works at any time and has no confirmation dialog.
   - `slip` and `legacy` can't be undone.
   - Changing payment status never sends a LINE message.
3. **Unpaid balance** is the sum of a customer's orders that are `unpaid` and not cancelled (status `queued` or `done`), across all shop days. `legacy` orders are never included.
4. **PromptPay QR**:
   - It is sent with the receipt right after an order is confirmed, and when the customer asks for it (agent tool `get_payment_info`: "ขอ QR", "จ่ายยังไง", "ค้างเท่าไหร่"). A customer who pays cash (item 13) gets no QR unless they ask for it.
   - The amount is the customer's whole unpaid balance including this order.
   - When only this order is owed, the receipt's payment block is one line: `โอน {total}฿ ตาม QR ด้านล่างได้เลยค่ะ`.
   - When earlier orders are owed too, the receipt lists them separately, then the total to pay:
     ```
     ออเดอร์นี้ #2 60฿
     ค้างจากเมื่อวาน #5 70฿
     รวมที่ต้องโอน 130฿
     ```
   - It is a standard PromptPay QR built from `PROMPTPAY_ID` and the amount, with no expiry.
   - When `PROMPTPAY_ID` is unset, there is no QR code and the receipt shows `PAYMENT_INFO_TEXT` as in v1. A cash customer never gets `PAYMENT_INFO_TEXT`.
   - When the unpaid balance is 0, no QR is sent and `get_payment_info` replies that nothing is owed. A replayed Confirm with nothing owed shows no payment details.
   - At the end of an LLM turn, the QR is sent only if the balance still equals the amount the bot quoted in that turn. (The Confirm reply has no such re-check; that millisecond window was rejected, §3.9 item 14.)
   - The image is served from `PUBLIC_BASE_URL` (HTTPS, as LINE requires) at a path that can't be guessed and contains no customer data.
   - The QR is **not** on the order card.
5. **Slip intake** (runs when an image arrives; never when the bot is paused). Each step that ends in a hand-over makes **no SlipOK call**:
   1. Download the image from LINE.
   2. Decode a QR code. If there is none, hand over with reason `image`.
   3. Parse it as a slip-verification mini-QR (§3.7). If it isn't one, hand over with reason `image`.
   4. Look up the transaction reference (`transRef`) in `slip_checks`. If it has been seen before, hand over with reason `slip_duplicate`.
   5. If the customer has no unpaid balance and no open order card, hand over with reason `slip_nothing_due`.
   6. If SlipOK isn't configured or its quota is known to be 0, hand over with reason `slip_unchecked`.
   7. Call SlipOK with the QR string and `log: true`. A duplicate error (1012) means `slip_duplicate`. A receiver error (1014) means `slip_wrong_receiver`: the returned amount and time are kept for the card (never credited), and the check counts against the cached quota. Any other error or a timeout means `slip_unchecked`.
   8. Run the slip match with the amount from SlipOK. If it fails, hand over with reason `slip_amount_mismatch` or `slip_too_old`.
      - Just before the SlipOK call, intake records what the slip can pay: the unpaid order IDs and the open card. If either has changed by payment time (e.g. the owner recorded the same transfer with **รับโอนแล้ว**), nothing is paid and the slip is handed over to the owner with reason `slip_balance_changed` (`ยอดค้างเปลี่ยนระหว่างตรวจสลิป`). One transfer is credited once.

   Every decoded slip is recorded in `slip_checks` with its result. A slip that has already been seen is never sent to SlipOK again.
6. **Slip match**:
   - Line up the customer's `unpaid`, non-cancelled orders oldest first (by `created_at`), with the open order card, if there is one, at the end.
   - The slip pays the **first run of oldest items** whose totals add up to exactly the slip amount.
   - If there is none, it pays the **oldest single item** whose total equals the amount.
   - If neither exists, the result is `slip_amount_mismatch`.
   - **Date rule:** the slip's transfer date (Asia/Bangkok) must be on or after the shop day of every order it pays. If the match includes the order card, the transfer date must be today. Otherwise the result is `slip_too_old`.
   - **Order card in the match:** confirm it through the same code path as the Confirm postback, with the same sold-out, card-revision and shop-open checks, then mark it `slip`. If confirming fails, hand over with reason `slip_card_confirm_failed`, and no order in that match is marked paid.
   - Paying the orders and recording the slip happen in one `BEGIN IMMEDIATE` transaction.
7. **What the customer sees**, sent with the free reply token. Wording may be polished, but each message must keep its meaning:
   - **Matched:** `ได้รับยอด {amount}฿ แล้วค่ะ ✅ (#3, #7)`. If an order card was confirmed, the receipt follows. The receipt shows no QR, because the balance is now 0 for those orders.
   - **`slip_amount_mismatch`:** `ได้รับสลิป {slip}฿ แต่ยอดค้างคือ {balance}฿ ส่งให้ร้านตรวจแล้วค่ะ`.
   - **Any other slip problem:** `ได้รับสลิปแล้วค่ะ ร้านจะตรวจให้นะคะ`. Never tell the customer a slip is fake.
   - **Image that isn't a slip:** v1's `IMAGE_HANDOVER_REPLY`.
   - **Shop closed and there's a problem:** add `ตอนนี้ร้านปิดแล้ว ร้านจะตรวจสลิปให้ตอนเปิดร้านค่ะ`. A matched slip while closed is handled normally. A card can't be confirmed while closed, so that becomes `slip_card_confirm_failed`.
   - **While the bot is quiet:** a match sends only the "matched" line. Any problem sends nothing to the customer and is added to the open inbox card.
   - Quiet is re-checked after the SlipOK call, since the owner may tap **คุยเอง** meanwhile. If the owner resolved the quiet card during the check, the problem is handed over normally, so it is never lost.
8. **Paid orders that get cancelled**:
   - The bot's `cancel_order` refuses an order that isn't `unpaid` and hands over with reason `paid_cancel_request` (detail: `#N {total}฿`).
   - When the owner cancels a paid order on `/kitchen`:
     - the cancel dialog shows `ลูกค้าโอนแล้ว {total}฿ ต้องคืนเงิน` (or `จ่ายสดแล้ว` for cash)
     - the cancel push adds `ร้านจะติดต่อคืนเงินค่ะ`
     - the order becomes a **refund due**
   - The owner refunds by hand.
9. **On `/kitchen`**:
   - Each queued order shows its payment badge and **รับเงินสดแล้ว** / **รับโอนแล้ว** buttons while `unpaid`, or **ยกเลิกจ่าย** next to an owner-set paid badge. An unpaid order with `orders.pay_cash` set (item 13) also shows `จะจ่ายสด`.
   - **ทำเสร็จแล้ว** or **เสร็จ + แจ้งลูกค้า** on an `unpaid` order asks `💵 รับเงินสดแล้ว` / `ยังไม่จ่าย`. Cash is recorded in the same transaction as done, never overwriting a payment recorded meanwhile, and before the push.
   - A new **ค้างจ่าย (N)** section lists every `unpaid`, non-cancelled order that is no longer queued, from any shop day, newest first. Earlier days are labelled `เมื่อวาน` or with the date. It has the same buttons. Paid orders leave the section.
   - Queue numbers repeat across shop days, so these views label earlier days: the ค้างจ่าย list, the queued list and the finished list (§3.3 item 7), รวมจาน (§3.4), the **รับเงินแล้ว…** picker and the inbox card's `ค้าง:` line (`#1 (เมื่อวาน)`).
   - At ≤600px wide, order cards show only the queue number, order time, items and badges, and expand on tap (the header is keyboard-operable).
   - When SlipOK reports fewer than 20 checks left, the board shows `เช็คสลิปเหลือ N ครั้ง`. The quota is read at most once an hour.
10. **The `เสร็จ + แจ้งลูกค้า` push** adds `· ยอดค้าง {balance}฿` when the customer's unpaid balance is above 0. When the owner records cash at done (item 9), this order is no longer in that balance; other unpaid orders still are.
11. **`สรุปวันนี้`** adds:
    - today's slip-paid total
    - today's cash and owner-confirmed total
    - today's unpaid orders (`#N {total}฿`)
    - `ค้างจ่ายจากวันก่อน N ออเดอร์ X฿`
    - refunds due (`ต้องคืนเงิน #N {total}฿`), shown on the day the order was cancelled
    - Earlier-day orders still queued on `/kitchen` (§3.3 item 7) don't count in today's order count or sales; they appear only through the `ค้างจ่ายจากวันก่อน` line.
12. **v1 orders**: the first v2 start marks every existing order `legacy`. They never appear in the unpaid balance, the ค้างจ่าย section, or slip matches.
13. **Cash intent.** The QR is the default. When the customer says they'll pay cash, the tool `set_payment_method(method="cash")` sets `conversations.pay_cash` (today) and `orders.pay_cash` on their unpaid orders. New orders confirmed that day inherit it. `set_payment_method(method="transfer")` clears it.
    - **The cash rule**: the customer **pays cash** when they said cash today, or when every one of their unpaid orders is a cash order. It is used by the receipt (also a replayed Confirm tap), `get_payment_info`, the end-of-turn QR decision and the live state.
    - A cash customer's receipt says `ชำระเงินสด {total}฿ ตอนรับอาหารได้เลยค่ะ` (or lists the orders with `รวมที่ต้องจ่าย`) and sends no QR image. Without PromptPay it says `ชำระเงินสดตอนรับอาหารได้เลยค่ะ`.
    - `get_payment_info` sends no QR for a cash customer unless called with `send_qr=true`. Asking for the QR counts as switching to transfer (kept on purpose, §3.9 item 13).
    - The live-state system message adds ` | วิธีจ่าย=เงินสด (ไม่ต้องส่ง QR)` while cash intent is set.

### 3.2 Hand-over and bot control
1. **Reasons** (stored as codes, shown in Thai on the card):

   | Code | Thai label |
   |---|---|
   | `greeting` | ทักทาย/คุยเล่น (old cards only: greetings get a bot reply, §3.5) |
   | `edit_confirmed` | ขอแก้ออเดอร์ #N |
   | `off_menu` | ถามส่วนผสม/แพ้อาหาร |
   | `image` | ส่งรูป |
   | `turn_limit` | คุยเกิน 30 ข้อความวันนี้ |
   | `paid_cancel_request` | ขอยกเลิกออเดอร์ที่จ่ายแล้ว |
   | `owner_takeover` | ร้านคุยเอง |
   | the `slip_*` codes from §3.1, including `slip_balance_changed` | ยอดค้างเปลี่ยนระหว่างตรวจสลิป, … |

   The LLM's `hand_over` tool gets a required `reason` argument limited to `edit_confirmed` and `off_menu`. If the LLM still calls `hand_over(reason="greeting")`, the tool refuses and the LLM replies itself (§3.5).
2. **The owner starts a hand-over**: a **คุยเอง** button on every queued order and every ค้างจ่าย row creates the reason `owner_takeover`. It sends the customer nothing.
3. **Starting a hand-over** (bot-started): the bot sends v1's one-time `HANDOVER_REPLY`, or the slip/image reply from §3.1. It opens an inbox card and makes the bot quiet for that customer.
4. **While the bot is quiet**, for that customer:
   - **Text:** no reply, **no LLM call**. The message is saved to `messages_json`, and the inbox card shows it as new.
   - **Location pin:** saved to the draft as the delivery address and map URL, with no reply and no wake-up. The card shows `📍 ส่งพิกัดแล้ว` with a map link. Confirmed orders are never changed.
   - **Image:** goes through slip intake (§3.1.5 and §3.1.7, quiet rules). A non-slip image is added to the card with no reply.
   - **Voice:** no reply. Sticker and video are ignored, as in v1.
   - **Confirm or Cancel button tap:** wakes the bot (as in v1) and is handled normally.
5. **The bot comes back** at the first of:
   - the owner taps **ให้บอทตอบต่อ**
   - **15 minutes** with no customer message. The timer starts when the hand-over begins and restarts with every customer message.
   - a Confirm/Cancel button tap
   - a new shop day

   Exception: a `turn_limit` hand-over comes back only with **ให้บอทตอบต่อ** (which also resets that customer's message count for the shop day) or on a new shop day.

   Coming back sends the customer nothing.
6. **Note after coming back**: the first LLM call after a hand-over ends must include this exact system message in the messages passed to the LLM (except when the hand-over crossed midnight: the shop is closed then, and the new day starts with empty history):
   `ร้านคุยกับลูกค้าเรื่องก่อนหน้านี้แล้ว บอทไม่เห็นข้อความของร้าน อย่าตอบคำถามเดิมซ้ำ ให้ช่วยเฉพาะเรื่องใหม่`
   This follows the `AGENTS.md` rule for changes made outside the chat flow.
7. **Inbox card**:
   - There is one open card per customer. A new reason while a card is open is added to that card and moves it to the top.
   - The card shows:
     - the customer's display name: from the LINE profile API, saved to `customers.display_name` when first needed, otherwise `ลูกค้า …{last 4 characters of the user ID}`
     - the reasons, with details
     - the last 2 customer messages and a new-message count
     - time since the hand-over started
     - bot state: `บอทพัก {minutes left} น.` or `บอทกลับมาแล้ว`
     - pins and slips (amount plus problem)
   - **Buttons:**
     - **ให้บอทตอบต่อ** (shown as **ปิดการ์ด** once the bot is already back) resolves the card and ends bot quiet.
     - **รับเงินแล้ว…** (on cards with a `slip_*` reason) opens the customer's unpaid orders as checkboxes, all ticked, and sets the confirmed ones to `transfer_manual`.
   - The card stays after the bot comes back on its own, labelled `บอทกลับมาตอบแล้ว`, until **ปิดการ์ด**. Resolved cards are hidden.
   - A new card, or a new message on an open card, plays a beep that sounds different from the new-order beep.
   - **Card text:** placeholder quotes such as `[สลิป]` or `[รูปภาพ]` are hidden; only text the customer typed is quoted. Slip cards show `ค้าง: #3 50฿, …`. `slip_duplicate` shows `เคยใช้แล้วกับ #N เมื่อ HH:MM` (or `เคยส่งมาแล้วเมื่อ HH:MM`, prefixed `ลูกค้าคนอื่น` for another customer); a SlipOK 1012 shows `SlipOK แจ้งว่าสลิปนี้เคยใช้แล้ว`. `slip_unchecked` always shows `ดูรูปสลิปใน LINE OA` (after the amount, when SlipOK returned one).
   - With `notify_owner_on_inbox = 1` (on by default; the `/kitchen` switch `LINE ลูกค้ารอตอบ`, separate from `notify_owner_on_order`), each **new** card (not each new message) sends one LINE push to the owner.
8. **Bot paused** (`หยุดบอท`):
   - Everything automatic stops for customers: no replies, no slip intake, no bot-started hand-overs.
   - Confirm/Cancel buttons still work (as in v1).
   - **คุยเอง** still works.
9. The owner replies in the LINE OA app. The app can't see those replies.
10. **Never talk over the owner.** Bot quiet is re-checked right after every LLM response, before any tool runs, and on every reply path: normal reply, error, deadline, tool limit and hand-over (slip intake: §3.1 item 7). If the owner tapped **คุยเอง** meanwhile, no reply is sent, history keeps only the customer's message, and the message lands on the card.
    - An order card built in a turn that wasn't delivered (silenced by takeover, error, deadline, tool limit, hand-over, or a failed LINE reply, on the text or location path) is cancelled, so a slip can't confirm a card the customer never saw.
    - Consuming the "bot came back" note (item 6) clears only a quiet window that has already ended, so a takeover started meanwhile survives.
11. **History matches what was sent.** Bot replies, the done and cancel pushes, and slip results are added to `messages_json`. History writes (`append_conversation_messages`, `append_conversation_event`) append to the stored history in one transaction, so owner pushes written during an LLM turn are never overwritten. (A replayed Confirm tap re-sends the receipt without a new history entry.) A slip that confirms a card records exactly the messages sent (only the "matched" line while quiet). Compaction on Confirm keeps done/cancel push events.

### 3.3 Wait estimate and queue position
1. `menu.yaml` gets:
   ```yaml
   wait:
     minutes_per_box: 3
     min_wait: 10
   ```
2. **Automatic estimate** = (meal boxes in queued orders ahead + meal boxes in this order or card) × `minutes_per_box`, never below `min_wait`.
   - A meal box is one unit of quantity of a `category: meal` item.
   - Sides and drinks count 0.
3. **Minimum wait**: busy mode (`ยุ่ง N นาที`, `ยุ่ง` = 20, the `/kitchen` chips) now sets the owner's minimum wait for the shop day. `โหมดปกติ` clears it. It still expires at midnight. The **wait estimate** is `max(automatic estimate, minimum wait if busy mode is on today)`.
4. **Display** is a range: lower = estimate rounded down to a multiple of 5, upper = lower + 5. Examples: 10 → `10–15`, 24 → `20–25`, 39 → `35–40`.
5. **Queue position** = the number of queued orders created before this one + 1, never counting `legacy` orders. It is different from the queue number `#N`. A `legacy` order gets no queue position or estimate.
6. **Where it's shown**, always and not only in busy mode:
   - the order card: `ตอนนี้รอประมาณ X–Y นาที`
   - the receipt: `คิวที่ N · ประมาณ X–Y นาที`
   - `get_my_orders`, for each queued order except `legacy` ones (item 5): queue position, orders ahead, and the estimate range. A "เสร็จยัง" reply reads like `ยังไม่เสร็จค่ะ ก่อนหน้าอีก 2 คิว ประมาณ 10–15 นาที`.
   - Delivery orders add `(ไม่รวมเวลาส่ง)`.
   - Never pushed.
7. **Earlier shop days.** `legacy` orders never count in queue position, the wait estimate, or รวมจาน.
   - `/kitchen` lists queued orders from earlier shop days first, labelled `เมื่อวาน` or with the date, so the owner can mark them done.
   - An earlier-day order finished or paid today stays in the finished list with its day label, so **ยกเลิกจ่าย** stays reachable.
   - `get_my_orders` also lists this customer's earlier-day orders still waiting, `legacy` included.

### 3.4 Batch view
- A **รวมจาน** section on `/kitchen` groups queued orders' meal and side items by dish plus the same set of options.
- Each group shows the total quantity and the queue numbers.
- Free-text notes are listed under the group per order (`└ #4: ไม่ใส่ผัก`).
- Done and cancelled orders are not included. Drinks are included like any other item. `legacy` orders are not included.
- Orders are listed oldest first, and earlier days are labelled (`#5 (เมื่อวาน)`), since queue numbers repeat across days.

### 3.5 Voice messages and chat replies
- A customer `audio` message gets `รบกวนพิมพ์เป็นข้อความนะคะ 🙏`, every time, with no LLM call.
- No reply while bot quiet or bot paused.
- **Greeting.** A greeting or small talk gets a short bot reply that asks what the customer wants. It never hands over (§3.2 item 1).
- **Dishes the shop doesn't sell.** When the customer asks for a dish, noodle or meat that isn't on the menu, the bot says so and suggests similar dishes. It doesn't hand over.
- **Ingredients and allergies.** Ingredient, allergy or MSG questions, and requests the bot can't handle, hand over with reason `off_menu`. The bot never vouches for allergy safety. (These three are prompt rules.)
- **Menu list.** "มีเมนูอะไรบ้าง" calls the tool `show_menu`. The list is built in code from `menu.yaml` and sent as its own message, followed by the LLM's one-line reply, so it is never cut off at the token limit.
  - It leaves out sold-out dishes, and a dish whose required choice is entirely sold out.
  - Each dish lists the choices the customer must make (required groups with no default, such as meat or ไข่ดาว สุก/ไม่สุก), with any extra cost, e.g. `• ข้าวกะเพรา 50฿ (หมูสับ/หมูชิ้น/ไก่/หมูกรอบ +10/กุ้ง +10/ปลาหมึก +10)`. Sold-out choices are dropped.
  - Optional add-ons (เพิ่มข้าว, พิเศษ), spice and sweetness aren't listed.
- **Saved address.** The bot says `ส่งที่เดิม` without repeating the full address; the order card shows it.
- **Daily message limit.** Only messages the customer typed count toward the 30-message limit (system notes start with `[`). Compaction on Confirm lowers `turn_offset` by the customer turns it drops, so the count keeps working after the owner resets it; the offset may go below 0.

### 3.6 Data model changes (additive only)
- `orders`:
  - add `payment_status TEXT NOT NULL DEFAULT 'unpaid'` and `paid_at TEXT`
  - **migration**: when `payment_status` is missing, `ALTER TABLE ADD COLUMN`, then `UPDATE orders SET payment_status = 'legacy'` for all existing rows, in one transaction at startup
- New `slip_checks(trans_ref TEXT PRIMARY KEY, line_user_id TEXT NOT NULL, amount_satang INTEGER, transfer_at TEXT, result TEXT NOT NULL, order_ids_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL)`.
  - Amounts are stored in satang. A slip with satang (e.g. 70.50) can never match whole-baht orders.
- New `handovers(id INTEGER PRIMARY KEY AUTOINCREMENT, line_user_id TEXT NOT NULL, shop_date TEXT NOT NULL, reasons_json TEXT NOT NULL, details_json TEXT NOT NULL, created_at TEXT NOT NULL, last_customer_message_at TEXT, unseen_count INTEGER NOT NULL DEFAULT 0, resolved_at TEXT)`. The column shape may be adjusted (reversible); the behavior in §3.2 may not.
- Bot quiet keeps using `conversations.handed_over_until`. It is set to `now + 15 min` and extended with each customer message, or set to the end of the shop day for `turn_limit`.
- `customers.display_name` is now filled from the LINE profile API.
- Settings (`.env.example`): `SLIPOK_API_KEY`, `SLIPOK_BRANCH_ID`, `PROMPTPAY_ID` (phone number or 13-digit national ID). All optional: when unset, the feature is off and v1 behavior applies.
- `create_app(...)` gets a `slip_client` injection point (`SlipClient` Protocol plus an `HttpSlipOkClient`). `LineClient` gains `get_message_content(message_id) -> bytes` and `get_profile(user_id) -> str | None`, with matching no-op/spy versions.
- Dependencies: `zxing-cpp`, `Pillow`, `segno` (added with `uv add`).
- Later additive columns (Ticket 7): `orders.pay_cash`, `conversations.pay_cash` (INTEGER NOT NULL DEFAULT 0), `shop_state.notify_owner_on_inbox` (INTEGER NOT NULL DEFAULT 1), added at startup when missing.
- `?key=` is redacted from uvicorn access logs.
- `shop.db*` is git-ignored, including backups such as `shop.db.v1-backup`.

### 3.7 External formats to verify (not to assume)
- **SlipOK**:
  - Request: `POST https://api.slipok.com/api/line/apikey/{SLIPOK_BRANCH_ID}` with header `x-authorization: {SLIPOK_API_KEY}` and body `{"data": "<QR string>", "log": true}`. We don't send `amount`, because we do the matching ourselves.
  - The response gives the amount, transfer date/time, and `transRef`.
  - Error codes: `1012` duplicate, `1013` wrong amount (not used), `1014` wrong receiver.
  - The receiver check needs the owner's bank account registered in SlipOK's LINE setup, and it must be the same account as `PROMPTPAY_ID`.
  - The free tier is reported as 100 slips a month, so confirm it.
- **Slip-verification mini-QR** (expected; confirm with real slips in Ticket 4):
  - Format: EMV-style TLV. Tag `00` is nested (`00` API ID `000001`, `01` sending bank code, `02` transaction ref), then `51` = `TH`, then `91` = CRC-16.
  - It carries no amount.
- **PromptPay QR**:
  - EMVCo TLV with:
    - `00`=`01`
    - `01`=`12` (amount included)
    - tag `29` with AID `A000000677010111` and either sub-tag `01` mobile (`0066` + 9 digits) or `02` national ID
    - `53`=`764`, `54`=amount with 2 decimals, `58`=`TH`
    - `63` = CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF) over the payload including `6304`
  - Prove it with a test against a known-good payload from an established PromptPay generator, and in Ticket 6 by scanning with real bank apps.
- **LINE**:
  - Content: `GET https://api-data.line.me/v2/bot/message/{messageId}/content`.
  - Profile: `GET https://api.line.me/v2/bot/profile/{userId}`.
  - An image message needs HTTPS `originalContentUrl` and `previewImageUrl`.
  - Reply tokens are valid for about 1 minute, so slip intake must finish within the webhook deadline.

### 3.8 Intentional changes to v1 behavior
These v1 behaviors change on purpose. A v1 test that asserts the old behavior may be updated **only** for these items, and each one is listed in the Proof Report:
1. An image no longer always hands over: a verified slip that matches marks orders paid and keeps the bot active.
2. A hand-over now lasts a rolling 15 minutes instead of until the end of the day, except `turn_limit`.
3. While the bot is quiet, customer text **never** wakes it and makes no LLM call. In v1, a new order, `ถึงไหนแล้ว`, or a queued-order cancel woke it. Affects the v1 criterion "a new order or `ถึงไหนแล้ว` wakes the bot and replies normally".
4. A location pin no longer wakes the bot while it is quiet.
5. Voice (`audio`) messages now get a reply.
6. The wait estimate is always shown. Busy mode now sets a minimum wait rather than a fixed wait.
7. The bot can't cancel an order that isn't `unpaid`.
8. Receipt, order card, done push, cancel push and `สรุปวันนี้` texts gain the additions in §3.1–3.3.
9. Greetings, small talk and dishes the shop doesn't sell get a bot reply instead of a hand-over (§3.5).

### 3.9 Changelog: the Ticket 6 phone test and the reviews (Ticket 7, approved 2026-11-25)
The owner tested on a real phone and approved these changes. They are already written into §3.1–3.8; this list records what changed, why, and where it now lives. Item numbers are stable because code comments cite them.
1. **Queue.** `legacy` orders left the queue maths, and earlier-day orders stay reachable → §3.3 items 5 and 7, §3.4. Why: a pre-v2 leftover inflated every queue position.
2. **Greeting.** Greetings get a bot reply instead of a hand-over → §3.2 item 1, §3.5. Why: a greeting hand-over silenced the bot for 15 minutes.
3. **Cash intent.** Customers can say they'll pay cash → §3.1 item 13. Why: cash customers still got a QR. The QR stays the default.
4. **Done prompt.** Finishing an unpaid order asks about cash → §3.1 items 9 and 10.
5. **Labels.** Buttons became **รับเงินสดแล้ว** / **รับโอนแล้ว** / **ยกเลิกจ่าย**, **รับเงินแล้ว…** and **ให้บอทตอบต่อ** (were จ่ายสด, จ่ายแล้ว ✓, เรียบร้อย); badges `💵 เงินสด`, `โอนแล้ว (ร้านเช็ค)`, `โอนแล้ว (สลิป) ✅`. Why: the owner found the old words ambiguous.
6. **Inbox card detail** → §3.2 item 7 (card text). Why: cards said only "สลิป" with nothing to act on.
7. **Shorter messages.** One-line payment block and `ส่งที่เดิม` → §3.1 item 4, §3.5.
8. **Menu list in code** (`show_menu`) → §3.5. Why: an LLM-written list was cut off mid-list at the token limit.
9. **Inbox alerts** got their own switch → §3.2 item 7.
10. **Phone layout** of `/kitchen` → §3.1 item 9.
11. **Schema (additive)** → §3.6.
12. **Dishes we don't sell** get a bot reply; allergy questions still hand over (`off_menu` relabelled `ถามส่วนผสม/แพ้อาหาร`) → §3.5. Owner decision 2026-11-25.
13. **Full v2 review fixes** (GPT-6.2 Sol): one transfer one credit (§3.1 item 5), never talk over the owner and history matches what was sent (§3.2 items 10–11), QR and payment-text rules (§3.1 item 4), daily limit (§3.5), SlipOK 1014 keeps amount and time and counts against the quota, earlier-day labels (§3.1 item 9), `get_my_orders` earlier-day orders (§3.3 item 7), log redaction (§3.6).
    - **Kept on purpose:** asking for the QR counts as switching to transfer (reviewer finding #6; the owner confirmed the flow on the phone).
    - **Not changed:** no resume note when a hand-over crosses midnight (the shop is closed then, and the new day starts with empty history); the v1 → v2 migration ended v1 hand-overs still running (decided in Ticket 2, already run on the real database); a refund due shows in `สรุปวันนี้` only on the day the order was cancelled.
14. **Tier 3 re-review fixes** (GPT-6.2 Sol and Gemini 4 Neon; the one re-review the tiered-review rules allow): takeover re-check on every reply path, the resume-note guard, cancelling undelivered cards, single-transaction history writes (§3.2 items 10–11), counting only customer-typed messages (§3.5), earlier-day order handling (§3.3 items 5 and 7), the order time on compact phone cards (§3.1 item 9).
    - **Rejected:** the stale QR between building and sending the Confirm reply. The window is milliseconds, and the owner can only mark orders that existed before the tap.
15. **Deferred / dropped:** a slip problem still makes the bot quiet (deferred until real data shows how often it happens). Rechecking a never-verified slip on resend was dropped.
16. **Later fix (2026-11-25):** the location-pin path also cancels its order card when the LINE reply fails (§3.2 item 10), found while drafting `CODING_STANDARDS.md`.
17. **Cancel tap clears the cart (2026-11-26, owner-approved Option A):** tapping **ยกเลิก** on an order card also empties the draft's items and keeps fulfillment, address, pin and time; internal card cleanup still keeps the items (`docs/specs/2026-11-26-agent-polish.md` §3.2).

---

## 4. Acceptance Criteria (Observable Behavior)
The criteria for the Ticket 7 changes (cash intent, done prompt, labels, inbox detail, menu list, earlier-day orders) are in [docs/specs/v2-tickets.md](docs/specs/v2-tickets.md) under Ticket 7.

### Payment status, ค้างจ่าย and summaries
- [x] Given a v1-schema `shop.db` with 3 orders, when v2 starts, then the 3 orders have `payment_status = 'legacy'`, the row counts in every v1 table are unchanged, and a new confirmed order has `payment_status = 'unpaid'`.
- [x] Given a customer with unpaid `#3` 60฿ from yesterday, unpaid `#5` 70฿ today, a cancelled unpaid order, and a `legacy` order, then their unpaid balance is 130฿.
- [x] Given queued `#5` unpaid, when the owner taps **รับเงินสดแล้ว**, then it becomes `cash` with `paid_at` set and 0 LINE pushes; when they tap **ยกเลิกจ่าย**, it returns to `unpaid`. Given a `slip` order, then the undo endpoint refuses with HTTP 409.
- [x] Given done `#2` unpaid from today and done `#9` unpaid from yesterday, then `/api/kitchen/state` lists both under ค้างจ่าย, newest first, with `#9` labelled as an earlier day. After **รับเงินสดแล้ว** on `#2`, it is no longer listed.
- [x] Given queued `#5` with `payment_status = 'slip'`, when the owner cancels it with a reason, then the cancel push contains the reason and `ร้านจะติดต่อคืนเงินค่ะ`, and `สรุปวันนี้` contains `ต้องคืนเงิน #5`.
- [x] Given unpaid `#5` (70฿), when the customer asks the bot to cancel `#5`, then it is still cancelled. Given `#5` is `cash`, then the bot does not cancel it, replies with the hand-over message, and an inbox card with reason `paid_cancel_request` exists.
- [x] Given done `#5` unpaid (70฿) and the owner taps **เสร็จ + แจ้งลูกค้า**, then the push contains `ยอดค้าง 70฿`. Given it is paid, then the push has no `ยอดค้าง`.
- [x] Given today has one `slip` 130฿, one `cash` 60฿, one unpaid 70฿, and one unpaid 80฿ from yesterday, then `สรุปวันนี้` shows a slip total of 130, a cash/owner-confirmed total of 60, unpaid `#N 70฿`, and `ค้างจ่ายจากวันก่อน 1 ออเดอร์ 80฿`.

### PromptPay QR
- [x] Given `PROMPTPAY_ID=0812345678` and amount 130, then the generated payload equals a fixed known-good test value, character for character, including the CRC.
- [x] Given `PROMPTPAY_ID` is set and a customer with unpaid `#5` 70฿ from yesterday confirms `#2` 60฿, then the reply contains the receipt text with `ออเดอร์นี้ #2 60฿`, `ค้างจากเมื่อวาน #5 70฿` and `รวมที่ต้องโอน 130฿`, plus one image message whose URL is on `PUBLIC_BASE_URL`. A `GET` of that URL returns `image/png` that decodes to the 130.00 payload.
- [x] Given `PROMPTPAY_ID` is unset, then the confirm reply equals v1's receipt with `PAYMENT_INFO_TEXT`, plus the queue position line from §3.3.
- [x] Given unpaid balance 0, when the customer says `ขอ QR`, then `get_payment_info` returns balance 0, the reply says nothing is owed, and no image is sent.

### Slip intake (SlipOK stub only)
- [x] Given an image with no QR code, then the SlipOK stub gets 0 calls, the reply is `IMAGE_HANDOVER_REPLY`, and an inbox card with reason `image` exists.
- [x] Given unpaid `#3` 60฿ and an open card for 70฿, then a slip for 60 pays `#3` only; a slip for 130 pays `#3` and confirms the card as a new order with status `slip`; a slip for 70 confirms the card only and leaves `#3` unpaid; a slip for 100 changes nothing and replies `ได้รับสลิป 100฿ แต่ยอดค้างคือ 130฿ …`. Every case makes exactly one SlipOK call.
- [x] Given a slip whose `transRef` is already in `slip_checks`, then the SlipOK stub gets 0 calls and the reason is `slip_duplicate`.
- [x] Given SlipOK returns error 1014, then the reason is `slip_wrong_receiver` and the customer reply is the general `ได้รับสลิปแล้วค่ะ ร้านจะตรวจให้นะคะ`.
- [x] Given unpaid `#5` from today and a slip dated yesterday, then the reason is `slip_too_old`. Given unpaid `#5` from yesterday and a slip dated today, then `#5` is paid.
- [x] Given an open card for a dish that became sold out, and a slip for the card's total, then no order is created, nothing is marked paid, and the reason is `slip_card_confirm_failed`.
- [x] Given no unpaid balance and no open card, then the SlipOK stub gets 0 calls and the reason is `slip_nothing_due`.
- [x] Given the shop is closed and the slip mismatches, then the reply contains `ตอนนี้ร้านปิดแล้ว ร้านจะตรวจสลิปให้ตอนเปิดร้านค่ะ`. Given the shop is closed and the slip matches an existing unpaid order, then that order is paid.
- [x] Given the bot is quiet for the customer, then a matching slip replies only `ได้รับยอด …✅`, and a mismatching slip sends 0 replies and adds the slip to the open card.
- [x] Given `bot_enabled = 0`, then an image gets 0 replies and 0 SlipOK calls.
- [x] Given the SlipOK stub reports 15 checks left, then `/api/kitchen/state` includes a quota warning of 15. Given it reports 0, then the next slip makes 0 check calls and is handed over as `slip_unchecked`.
- [x] Given a `slip_*` card for a customer with unpaid `#3` and `#5`, when the owner confirms **รับเงินแล้ว…** with only `#5` ticked, then `#5` is `transfer_manual` and `#3` stays `unpaid`.

### Hand-over inbox and bot quiet
- [x] Given the customer asks an allergy/ingredient question and the LLM stub calls `hand_over(reason="off_menu")`, then 1 reply with `HANDOVER_REPLY` is sent, an inbox card with reason `off_menu` exists, and `handed_over_until` is 15 minutes ahead.
- [x] Given the bot is quiet, when the customer sends `กะเพราหมูกรอบ 1 กล่อง`, then 0 replies are sent, 0 LLM calls are made, the message is in `messages_json`, the card's unseen count is 1, and `handed_over_until` moves to 15 minutes after this message.
- [x] Given the bot is quiet and the clock is advanced 15 minutes past the last customer message, when the customer sends `กะเพราหมูกรอบ 1 กล่อง`, then the LLM stub is called, the messages passed to it contain the exact note from §3.2.6, a reply is sent, and the card is still listed as `บอทกลับมาแล้ว`.
- [x] Given an open card, when the owner taps **ให้บอทตอบต่อ**, then the card is gone from `/api/kitchen/state`, bot quiet has ended, and the next customer text reaches the LLM with the §3.2.6 note.
- [x] Given a `turn_limit` hand-over and the clock advanced 60 minutes, then customer text still gets 0 replies and 0 LLM calls. After **ให้บอทตอบต่อ**, the next text reaches the LLM and doesn't trigger the turn limit.
- [x] Given the bot is quiet, when the customer sends a location pin, then the draft holds the address and `https://maps.google.com/?q={lat},{lng}`, 0 replies are sent, and the card shows the pin.
- [x] Given the bot is quiet, when the customer taps Confirm on a valid card, then the order is created (as in v1) and bot quiet has ended.
- [x] Given queued `#5`, when the owner taps **คุยเอง**, then 0 messages go to the customer, a card with reason `owner_takeover` exists, and the customer's next text gets 0 replies.
- [x] Given an open card for a customer, when a second hand-over reason happens, then there is still exactly one open card for that customer and it lists both reasons.
- [x] Given `notify_owner_on_inbox = 1` (the default), then a new card sends 1 push to `OWNER_LINE_USER_ID`, and later messages on the same card send 0 pushes. Given it is 0, then no pushes are sent, whatever `notify_owner_on_order` is.
- [x] Given a customer with no stored display name, when a card is created, then the LINE profile stub is called once and the card shows the profile name. If it returns nothing, the card shows `ลูกค้า …{last 4}`.

### Wait estimate, queue position, batch view and voice
- [x] Given `minutes_per_box: 3`, `min_wait: 10`, and an empty queue, then a card for 1 meal box shows `ประมาณ 10–15 นาที`. Given 6 meal boxes queued and a card with 2 meal boxes plus 1 drink, then it shows `20–25`. Given 12 meal boxes queued and a card for 1 meal box, then it shows `35–40`.
- [x] Given busy mode with a minimum wait of 25 and an empty queue, then a card for 1 box shows `25–30`. Given 12 boxes queued, then it shows `35–40`. After `โหมดปกติ`, the empty-queue case shows `10–15`.
- [x] Given 3 queued orders before the customer's newly confirmed order, then the receipt contains `คิวที่ 4`. For delivery, the wait line ends with `(ไม่รวมเวลาส่ง)`.
- [x] Given the customer's order has 2 queued orders ahead, when the LLM stub calls `get_my_orders`, then the tool result includes queue position 3, 2 orders ahead, and the estimate range.
- [x] Given queued `#2` (กะเพรา·หมูกรอบ·ไข่ดาวไม่สุก ×1), `#4` (the same ×1, note `ไม่ใส่ผัก`) and `#7` (the same ×1), then `/api/kitchen/state` contains a batch group with quantity 3, orders `[2, 4, 7]`, and the note for `#4`.
- [x] Given an `audio` message, then 1 reply `รบกวนพิมพ์เป็นข้อความนะคะ 🙏` is sent with 0 LLM calls. While bot quiet or bot paused, 0 replies are sent.

---

## 5. Verification Contract
The executing agent must run and pass all of the below after its last edit:
- **Static and tests (gate)**: `./scripts/check.sh` (`ruff format --check`, `ruff check`, `mypy app tests scripts`, `pytest -q`).
- **Behavioral**: `uv run pytest -q`, with new test files as needed (e.g. `tests/test_payments.py`, `tests/test_slips.py`, `tests/test_handover.py`, `tests/test_wait.py`).
  - SlipOK, LINE (reply, push, content, profile) and the LLM are always injected stubs or spies through `create_app(...)` / `ShopDB(clock=...)`.
  - Time-based rules use the injected clock, never `sleep`.
- **Synthetic slips only in git**: test slip images are generated in the tests (or in a committed fixture generator) from made-up mini-QR payloads using `segno` and `Pillow`. They contain no real names, accounts or references.
- **Real-slip format check (Ticket 4, local)**: `./scripts/dev.sh slips local/slips` decodes every image in the git-ignored `local/slips/` and prints the bank code, the shortened transaction ref, and whether it parsed. It makes no API calls. If the folder is empty, mark it `NOT RUN (no real slips provided)` and report status `PARTIAL`.
- **Migration proof**: a test builds a DB with the exact v1 `SCHEMA_SQL` and rows, starts v2 on it, and checks the `legacy` backfill and unchanged row counts.
- **Runtime smoke**: `uv run uvicorn app.main:app` against a temp DB:
  - `GET /health` → `200 {"ok": true}`
  - `GET /api/kitchen/state?key=…` → `200` JSON with the payment, inbox, batch and quota keys
  - the QR image URL from a confirm in the tests returns `200 image/png`
- **Live proof**: Ticket 6 (HITL) checklist only.
- **Baseline Rule**: don't weaken or delete v1 assertions. A v1 test may change only to match a numbered item in §3.8. Each changed test is listed in the Proof Report with the §3.8 item it follows.

---

## 6. Autonomy Boundaries & Rollback
- **The agent may decide and log (reversible)**:
  - module and helper names and file split (e.g. `app/payments.py`, `app/slips.py`, `app/promptpay.py`)
  - Thai wording polish that keeps the meaning of every quoted string, except the exact §3.2.6 note
  - `/kitchen` layout and CSS, beep tones, QR image size
  - how the quota is cached
  - the `handovers` column shape
  - how the "bot came back" note is triggered, as long as the tests assert the exact messages passed to the LLM
- **The agent MUST stop and ask** (or end with `NEEDS APPROVAL` when unattended):
  - any live SlipOK, LINE or LLM call outside Ticket 6
  - changing the slip match rule, the date rule, the hand-over return rules, the wait formula, or any §3.8 item
  - dependencies beyond `zxing-cpp`, `Pillow` and `segno`
  - destructive schema changes (dropping or renaming columns or tables, deleting rows), or any backfill other than the `legacy` one
  - committing real slip images or anything under `local/`
  - committing, pushing, deploying, or changing LINE OA / SlipOK console settings
- **Rollback Plan**:
  - Before the first v2 start on the real shop DB, the human runs `cp shop.db shop.db.v1-backup` (Ticket 6 step 1).
  - The schema change only adds columns and tables. v1 code reads orders with `SELECT *` and inserts with named columns, so the v1 commit (`a516222`) still runs against a v2 DB.
  - To roll back: check out the v1 commit and restart. Restore `shop.db.v1-backup` only if v2 data must be discarded.
