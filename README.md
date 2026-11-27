# tam-sang-bot

> [ภาษาไทย (Thai)](README.th.md)

A lightweight LINE AI assistant and single-screen kitchen board for a Thai made-to-order (ตามสั่ง) food shop.

During lunch rush, small shop owners are cooking and cannot keep up with LINE messages; during normal hours, repetitive questions ("เปิดยัง", "หมูกรอบหมดยัง", "กี่บาท"), manually totaling chats, and checking transfer slips still interrupt prep. `tam-sang-bot` replies instantly, turns messy Thai chats into accurate kitchen tickets, checks payment slips automatically, and fits how the owner already works in LINE.

---

## How it works

1. **Customers (LINE OA):** Message in normal Thai (slang, abbreviations, typos, multi-dish orders, or LINE location pins). The bot answers shop hours, address, menu and sold-out questions, sends the full menu with meat choices and surcharges when asked ("มีเมนูอะไรบ้าง"), asks short clarifying questions only when needed, and sends a **LINE Flex order card** with code-calculated prices and the current wait (`ตอนนี้รอประมาณ 10–15 นาที`). Tapping **ยืนยันออเดอร์** queues the order immediately. Tapping **ยกเลิก** clears the dishes but keeps pickup or delivery, the address, the pin and the time, so the next order is quick.
   - **Receipt:** shows the queue position and wait (`คิวที่ 2 · ประมาณ 10–15 นาที`) and a **PromptPay QR with the exact amount**. If the customer still owes for earlier orders, the QR covers the whole unpaid balance and the receipt lists each order.
   - **Cash:** if the customer says they'll pay cash, the receipt says `ชำระเงินสด X฿ ตอนรับอาหารได้เลยค่ะ` and no QR is sent, unless they ask for it.
   - **Slips:** when a customer sends a transfer slip, the bot reads its QR, verifies it with [SlipOK](https://slipok.com/), and marks the matching orders paid (`ได้รับยอด 60฿ แล้วค่ะ ✅ (#3)`). A slip sent before tapping Confirm also confirms the open order card. Duplicates, wrong amounts, wrong receivers and unreadable slips go to the owner. The bot never tells a customer a slip is fake.
   - **Status:** "เสร็จยัง" gets the queue position and wait (`ยังไม่เสร็จค่ะ ก่อนหน้าอีก 2 คิว ประมาณ 10–15 นาที`). Voice messages get `รบกวนพิมพ์เป็นข้อความนะคะ 🙏`.
2. **Owner (LINE text commands):** Text plain Thai directly to the same LINE OA from `OWNER_LINE_USER_ID` — no admin panel or login needed:

   | Command | What it does |
   |---|---|
   | `หมูกรอบหมด` / `ข้าวไข่เจียวหมด` | Marks an ingredient (across all dishes using it) or dish sold out |
   | `หมูกรอบมาแล้ว` / `เปิดครบ` | Restocks an item or clears all sold-out toggles |
   | `ยุ่ง 25 นาที` / `ยุ่ง` / `โหมดปกติ` | Sets a minimum wait for today (5–180 min; default 20 min) or clears it. The wait shown to customers is never below it, and grows with the queue |
   | `ปิดร้าน` / `เปิดร้าน` | Overrides today's open/closed schedule until midnight |
   | `คิว` / `สรุปวันนี้` | Shows queued ticket count + `/kitchen` link, or today's orders, sales, slip and cash totals, unpaid orders, earlier-day balances, and refunds due |
   | `เปิดแจ้งเตือน` / `ปิดแจ้งเตือน` | Toggles 1-phone LINE push alerts to the owner on each new order |
   | `หยุดบอท` / `เปิดบอท` | Pauses or resumes all automated customer replies (and slip checks) |

   Any other text from the owner account returns the command cheat sheet.

3. **Kitchen (`/kitchen`):** One mobile-first phone/tablet screen:
   - **Queue:** FIFO tickets (earlier-day leftovers first, labelled `เมื่อวาน`), highlighted notes (`ไม่ใส่ผัก`, `แยกน้ำ`), clickable `เปิดแผนที่` links for pinned locations, and 1-tap `ทำเสร็จแล้ว`, `เสร็จ + แจ้งลูกค้า`, and `ยกเลิก` buttons. On phones, cards collapse to queue number, time, items and badges, and expand on tap.
   - **Payments:** a badge on each order (`ยังไม่จ่าย`, `จะจ่ายสด`, `💵 เงินสด`, `โอนแล้ว (ร้านเช็ค)`, `โอนแล้ว (สลิป) ✅`), buttons **รับเงินสดแล้ว** / **รับโอนแล้ว** / **ยกเลิกจ่าย**, a prompt for cash when finishing an unpaid order, and a **ค้างจ่าย** section for finished orders still unpaid from any day.
   - **Inbox (ลูกค้ารอตอบ):** a card for each chat that needs a person: slip problems, images, allergy or ingredient questions, edits to confirmed orders, cancelling a paid order, or a chat over 30 messages today. The card shows the reason, the customer's last messages, and how long the bot stays quiet. It has its own beep and an optional LINE push to the owner.
   - **รวมจาน:** identical dishes across queued orders grouped for batch cooking, with each order's notes.
   - **Top bar:** open/busy/sold-out toggles, the two LINE alert switches (`LINE ออเดอร์ใหม่`, `LINE ลูกค้ารอตอบ`), a low SlipOK quota warning, and new-order chimes with Screen Wake Lock.

---

## Hand-over and the bot staying quiet

When a chat needs the owner, the bot replies once (`ส่งข้อความให้ร้านแล้วค่ะ เดี๋ยวร้านตอบกลับนะคะ`), opens an inbox card, and goes **quiet for that customer**. The owner replies in the LINE OA app as usual. The owner can also tap **คุยเอง** on a queued order or a ค้างจ่าย row to take over without the bot sending anything.

While quiet, the bot makes no replies and no LLM calls for that customer. Messages are saved and show on the card. The bot comes back on its own after **15 minutes** with no customer message, when the customer taps Confirm/Cancel, on a new shop day, or when the owner taps **ให้บอทตอบต่อ**. A chat over the 30-message daily limit comes back only by that button or on a new day. After coming back, the bot is told the owner already handled the earlier topic, so it doesn't answer it again.

---

## Key design choices

- **Code owns prices, payments and facts:** The LLM reads messy Thai and picks tools; Python validates menu choices, availability, delivery fees, totals, the menu list, slip matches, and the wait estimate from `menu.yaml` and SQLite.
- **Payments are tracked, not required:** Confirmed orders are cooked whether paid or not; payment can come before, during, or after cooking, or on a later day ([ADR-0001](docs/adr/0001-payments-tracked-not-gated.md)). A slip pays the oldest unpaid orders whose totals add up to exactly its amount; it is never split, and each slip counts once.
- **Few LINE pushes:** Customer replies, receipts, QR codes, and slip results use free LINE `reply` tokens. Pushes (counted against LINE's 300/month free plan) are only sent on owner cancellations, optional `เสร็จ + แจ้งลูกค้า` taps, and the owner alerts: new orders (off by default) and new inbox cards (on by default).
- **Built for real Thai chats:** Defaults unspecified spice to `เผ็ดปกติ` and sweetness to `หวานปกติ`, remembers returning customers' delivery address and location pins, replies to greetings and asks what the customer wants, answers dishes the shop doesn't sell with similar menu items, and hands the chat to the owner (`hand_over`) for allergy or ingredient questions and edits to confirmed orders.
- **Never talks over the owner:** the bot re-checks that it isn't quiet right before every reply, so an owner who taps **คุยเอง** mid-reply wins.
- **Fast one-message orders:** when a single message gives the whole order, the bot sends the order card with a fixed line instead of a second LLM call. That's about 1.1 s faster: 4.35 s → 3.23 s on Haiku. Messages containing a Thai question word still get the second call, so side questions are answered ([ADR-0002](docs/adr/0002-card-fast-path-question-word-check.md)).
- **Logs without personal data:** one log line per LLM call and per LINE event, with latency, token counts and cache hits. Message text, addresses and LINE user IDs are never logged.
- **Small, swappable stack:** Python 3.13, FastAPI, SQLite (WAL), `httpx`, `zxing-cpp` + `Pillow` (reading slip QR codes), `segno` (PromptPay QR), and the OpenAI SDK pointed at any OpenAI-compatible provider via `.env` (defaults to OpenRouter).

---

## Quick start (local + live LINE OA)

**Prerequisites:** [`uv`](https://docs.astral.sh/uv/), which installs Python 3.13 (pinned in `.python-version`); the `sqlite3` CLI (for helper scripts); a LINE Official Account with the Messaging API enabled; an OpenRouter (or other OpenAI-compatible) API key; and `cloudflared` (`brew install cloudflared`) for a public HTTPS webhook URL while testing locally. Optional: a PromptPay ID (for QR codes) and a [SlipOK](https://slipok.com/) account (for slip checks).

1. **Install dependencies:**
   ```bash
   uv sync --locked
   ```
2. **Create `.env`:**
   ```bash
   cp .env.example .env
   ```
   Fill in these values in `.env` *(shell-exported env vars take precedence over `.env`)*:
   - `KITCHEN_SECRET` — replace `change-me-kitchen-secret` with a random URL-safe string (e.g. `openssl rand -hex 24`)
   - `LINE_CHANNEL_SECRET` and `LINE_CHANNEL_ACCESS_TOKEN` — from LINE Developers Console
   - `OWNER_LINE_USER_ID` — your `U...` user ID from **LINE Developers Console → your channel → Basic settings → Your user ID** *(or leave blank for now and grab it in step 5)*
   - `LLM_API_KEY` — OpenRouter API key (or change `LLM_BASE_URL` / `LLM_MODEL` for another provider)
   - `PROMPTPAY_ID` *(optional)* — 10-digit phone number or 13-digit ID. Receipts then carry a PromptPay QR with the exact amount. Leave blank for no QR.
   - `PAYMENT_INFO_TEXT` *(optional)* — single-line PromptPay/bank note shown on receipts when `PROMPTPAY_ID` is blank
   - `SLIPOK_API_KEY` and `SLIPOK_BRANCH_ID` *(optional, set both or neither)* — from your SlipOK account. The branch ID is a number; the app refuses to start if it isn't (a common sign the two values are swapped). Register the same bank account as `PROMPTPAY_ID` in SlipOK's LINE setup, or every slip fails the receiver check. Leave both blank and slips go to the owner's inbox unchecked.
3. **Run the server and tunnel:**
   ```bash
   ./scripts/dev.sh run
   ```
   In a second terminal:
   ```bash
   cloudflared tunnel --url http://127.0.0.1:8000
   ```
   *(Quick-tunnel URLs change on every restart — update the LINE Webhook URL and `PUBLIC_BASE_URL` each time, or use a named Cloudflare tunnel.)*
4. **Configure LINE OA & `PUBLIC_BASE_URL`:**
   - In **LINE Developers Console → Messaging API**, set Webhook URL to `https://<tunnel-url>/line/webhook`, click **Verify**, and enable **Use webhook**.
   - In **LINE OA Manager → Response settings**, turn **Auto-reply messages** OFF (leave **Chat** ON so you can still reply manually).
   - Set `PUBLIC_BASE_URL=https://<tunnel-url>` in `.env` and restart `./scripts/dev.sh run`. LINE loads the PromptPay QR image from this HTTPS URL, and the owner `คิว` command links to it, so `localhost` won't work.
5. **Find your owner ID & test with 1 or 2 accounts:**
   - If you left `OWNER_LINE_USER_ID` blank, message your OA once, run `./scripts/dev.sh users` to print your `U...` ID, paste it into `.env`, and restart.
   - **Testing tip:** Every message from `OWNER_LINE_USER_ID` routes to Owner Commands (non-commands return the command list). To test customer ordering, message the OA from a second LINE account — or temporarily leave `OWNER_LINE_USER_ID=` blank and restart. Owner alert pushes need the owner ID set, so test those with two accounts.
6. **Open the kitchen board:**
   Visit `https://<tunnel-url>/kitchen?key=<KITCHEN_SECRET>` on your phone/tablet (the key is stored in an `HttpOnly` cookie on first visit so refreshes keep working, and it is redacted from server logs) and tap the top banner once to enable audio chimes and Screen Wake Lock.

---

## Deploying to production

The quick tunnel above is for testing only: its URL changes on every restart, and the bot stops when your computer sleeps. Production runs on a free Google Cloud `e2-micro` VM:
- systemd keeps the app running;
- Caddy serves HTTPS with automatic Let's Encrypt certificates;
- a DuckDNS name gives the LINE webhook a fixed URL;
- `shop.db` is backed up nightly to Cloud Storage;
- pushes to `main` deploy automatically once CI is green and the shop is closed, with a health check and automatic rollback.

[docs/deploy.md](docs/deploy.md) has the daily commands (deploy, rollback, logs), restoring a backup, and a step-by-step rebuild.

---

## Adapting to a real shop

Edit [menu.yaml](menu.yaml) directly — no admin UI needed:
- `shop.name`, `shop.hours` (`open` / `close`, `Asia/Bangkok`), and `shop.delivery` (`fee` and `free_above`; set `fee: 0` for free delivery).
- `shop.address` *(optional)*: the bot gives it when customers ask "ร้านอยู่ตรงไหน". If you leave it out, the bot has no address to give. It must be text, or the app won't start.
- `wait` (`minutes_per_box` and `min_wait`): the automatic wait is meal boxes in the queue × `minutes_per_box`, never below `min_wait`. Sides and drinks count 0.
- `ingredients`, `option_groups`, and `items` (base prices, `category`: `meal` / `side` / `drink`, allowed proteins, and Thai aliases). The menu list the bot sends is built from these, so it updates automatically.

---

## Development

```bash
./scripts/check.sh           # ruff format --check, ruff check, mypy, pytest; ends with "check.sh: PASS" or FAIL
./scripts/dev.sh reset-db    # clear local test orders/chats/slips without stopping uvicorn
./scripts/dev.sh users       # list LINE userIds recorded in shop.db
./scripts/dev.sh slips [dir] # decode real slip images (default local/slips/); no API calls
./scripts/dev.sh demo        # real app on a seeded throwaway DB, stub LINE/LLM/SlipOK, port 8765
./scripts/dev.sh demo-up     # the same demo in the background (demo-down stops it)
./scripts/dev.sh say U_DEMO slip 70  # send a fake LINE event to the demo, print what LINE got
```

`check.sh` also runs in the git pre-commit hook and in CI (GitHub Actions on Ubuntu 24.04, the same as the production VM).

The demo's stub LLM echoes the customer's text. To build an order card offline, start the message with `!tools` followed by a tool batch as JSON, for example `!tools [["update_cart", {...}], ["build_order_card", {}]]`. `DEMO_REAL_LLM=1` uses the real model from `.env`; those are paid calls.

*(Note: `reset-db` and `users` need an existing DB, created after the app handles its first request; `users` also needs the `sqlite3` CLI. If you changed `DB_PATH` in `.env`, export `DB_PATH` in your shell before running them. Keep real slips in git-ignored `local/` and never commit them.)*

Upgrading from v1: the first v2 start adds the new tables and columns in place and marks existing orders `legacy` (ก่อนระบบจ่ายเงิน), so they never count as unpaid. Back up `shop.db` first.

For full architecture, SQLite schema, acceptance criteria, and edge-case rules, see [SPEC.md](SPEC.md) (v2) and [docs/specs/v1.md](docs/specs/v1.md) (v1).
