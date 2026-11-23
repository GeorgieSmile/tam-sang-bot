# tam-sang-bot

> [ภาษาไทย (Thai)](README.th.md)

A lightweight LINE AI assistant and single-screen kitchen board for a Thai made-to-order (ตามสั่ง) food shop.

During lunch rush, small shop owners are cooking and cannot keep up with LINE messages; during normal hours, repetitive questions ("เปิดยัง", "หมูกรอบหมดยัง", "กี่บาท") and manually totaling chats still interrupt prep. `tam-sang-bot` replies instantly, turns messy Thai chats into accurate kitchen tickets, and fits how the owner already works in LINE.

---

## How it works

1. **Customers (LINE OA):** Message in normal Thai (slang, abbreviations, typos, multi-dish orders, or LINE location pins). The bot answers shop/menu/sold-out questions, asks short clarifying questions only when needed, and sends a **LINE Flex order card** with code-calculated prices. Tapping **ยืนยันออเดอร์** queues the order immediately.
2. **Owner (LINE text commands):** Text plain Thai directly to the same LINE OA from `OWNER_LINE_USER_ID` — no admin panel or login needed:

   | Command | What it does |
   |---|---|
   | `หมูกรอบหมด` / `ข้าวไข่เจียวหมด` | Marks an ingredient (across all dishes using it) or dish sold out |
   | `หมูกรอบมาแล้ว` / `เปิดครบ` | Restocks an item or clears all sold-out toggles |
   | `ยุ่ง 25 นาที` / `ยุ่ง` / `โหมดปกติ` | Turns busy mode on (5–180 min; default 20 min) or off |
   | `ปิดร้าน` / `เปิดร้าน` | Overrides today's open/closed schedule until midnight |
   | `คิว` / `สรุปวันนี้` | Shows queued ticket count + `/kitchen` link, or today's order & sales summary |
   | `เปิดแจ้งเตือน` / `ปิดแจ้งเตือน` | Toggles 1-phone LINE push alerts to the owner on each new order |
   | `หยุดบอท` / `เปิดบอท` | Pauses or resumes all automated customer replies |

   Any other text from the owner account returns the command cheat sheet.

3. **Kitchen (`/kitchen`):** One mobile-first phone/tablet screen showing FIFO tickets, highlighted notes (`ไม่ใส่ผัก`, `แยกน้ำ`), clickable `เปิดแผนที่` links for pinned locations, top-bar open/busy/sold-out toggles, new-order audio chimes with Screen Wake Lock, and 1-tap `ทำเสร็จแล้ว`, `เสร็จ + แจ้งลูกค้า`, and `ยกเลิก` buttons.

---

## Key design choices

- **Code owns prices and facts:** The LLM reads messy Thai and picks tools; Python validates menu choices, availability, delivery fees, and totals from `menu.yaml` and SQLite.
- **0 LINE pushes on normal orders:** Customer replies and order confirmations use free LINE `reply` tokens. Pushes (counted against LINE's 300/month free plan) are only sent on owner cancellations, optional `เสร็จ + แจ้งลูกค้า` taps, or when the 1-phone `notify_owner_on_order` toggle is turned on.
- **Built for real Thai chats:** Defaults unspecified spice to `เผ็ดปกติ` and sweetness to `หวานปกติ`, remembers returning customers' delivery address and location pins, replies once to images (e.g. payment slips) leaving them for the owner to check in chat, and hands the chat to the owner (`hand_over`) for greetings, chit-chat, allergy questions, off-menu requests, or edits to confirmed orders.
- **Small, swappable stack:** Python 3.13, FastAPI, SQLite (WAL), `httpx`, and the OpenAI SDK pointed at any OpenAI-compatible provider via `.env` (defaults to OpenRouter).

---

## Quick start (local + live LINE OA)

**Prerequisites:** Python 3.13+, [`uv`](https://docs.astral.sh/uv/), `sqlite3` CLI (for helper scripts), a LINE Official Account (Messaging API enabled), an OpenRouter (or OpenAI-compatible) API key, and `cloudflared` (`brew install cloudflared`) for a public HTTPS webhook URL.

1. **Install dependencies:**
   ```bash
   uv sync
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
   - `PAYMENT_INFO_TEXT` *(optional)* — single-line PromptPay/bank note appended to order receipts
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
   - Set `PUBLIC_BASE_URL=https://<tunnel-url>` in `.env` and restart `./scripts/dev.sh run` (otherwise the owner `คิว` command links to `localhost`).
5. **Find your owner ID & test with 1 or 2 accounts:**
   - If you left `OWNER_LINE_USER_ID` blank, message your OA once, run `./scripts/dev.sh users` to print your `U...` ID, paste it into `.env`, and restart.
   - **Testing tip:** Every message from `OWNER_LINE_USER_ID` routes to Owner Commands (non-commands return the command list). To test customer ordering, message the OA from a second LINE account — or temporarily leave `OWNER_LINE_USER_ID=` blank and restart.
6. **Open the kitchen board:**
   Visit `https://<tunnel-url>/kitchen?key=<KITCHEN_SECRET>` on your phone/tablet (the key is stored in an `HttpOnly` cookie on first visit so refreshes keep working) and tap the top banner once to enable audio chimes and Screen Wake Lock.

---

## Adapting to a real shop

Edit [menu.yaml](menu.yaml) directly — no admin UI needed:
- `shop.name`, `shop.hours` (`open` / `close`, `Asia/Bangkok`), and `shop.delivery` (`fee` and `free_above`; set `fee: 0` for free delivery).
- `ingredients`, `option_groups`, and `items` (base prices, allowed proteins, and Thai aliases).

---

## Development

```bash
./scripts/check.sh        # ruff format --check, ruff check, mypy, pytest
./scripts/dev.sh reset-db # clear local test orders/chats without stopping uvicorn
./scripts/dev.sh users    # list LINE userIds recorded in shop.db
```

*(Note: `reset-db` and `users` require the `sqlite3` CLI and an existing DB created after the app handles its first request. If you changed `DB_PATH` in `.env`, export `DB_PATH` in your shell before running them.)*

For full architecture, SQLite schema, acceptance criteria, and edge-case rules, see [SPEC.md](SPEC.md).
