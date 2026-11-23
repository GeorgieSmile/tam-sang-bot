# Spec: tam-sang-bot (LINE AI Assistant for a Thai ตามสั่ง Shop)

- **Status**: Approved <!-- Approved by user on 2026-11-23. -->
- **Date**: 2026-11-23
- **Scope**: Build a greenfield, lightweight (~1,000–1,500 LOC) LINE Official Account ordering assistant + single-screen `/kitchen` board for a Thai made-to-order (ตามสั่ง) shop, using Python 3.13, FastAPI, SQLite, `httpx`, and the OpenAI SDK.
- **Default Execution Mode**: AFK (Tickets 1–4) · HITL (Ticket 5)

---

## 1. Context Pointers (Read First)
- `menu.yaml` — Source of truth for the 24-item `ครัวอิ่มจัง` menu (dishes, base prices, option groups, `ingredient_key` mappings, opening hours, and delivery fee rule).
- `SPEC.md` — This spec (single source of truth for schema, behavior, and verification).

---

## 2. Goal & Non-Goals

**Goal**
- Answer customer LINE messages in Thai immediately (open/closed, menu, prices, sold-out items, delivery fee rule, busy wait time) using free LINE `reply` tokens.
- Convert free-form Thai orders into code-priced LINE Flex order cards that auto-queue onto a single-screen `/kitchen` board upon customer confirmation using **0 LINE push messages**.
- Let the shop owner control sold-out items, open/closed overrides, busy wait time, bot pause, owner alert toggle, and daily sales summaries by texting plain Thai to the same LINE OA.
- Help in both normal hours (instant FAQ replies, structured tickets, daily revenue summary) and busy hours (upfront wait-time notice, hands-free auto-queued FIFO tickets).

**Out of Scope (Do Not Build)**
- Web chat clone or public demo site
- Multi-shop / SaaS onboarding or user/role management
- Web-based menu or option-group CRUD editor (menu definitions live in `menu.yaml`; live sold-out state lives in SQLite)
- Payment slip verification (SlipOK) or dynamic PromptPay QR image generation (deferred to post-v1)
- Automated LLM evaluation harness, simulated customer runner, or Playwright browser test suite
- PostgreSQL, Neon, Docker/Cloud Run, Terraform, or CI/CD deployment pipelines
- Editing an already-confirmed (`queued`) order via the bot (must hand over to the owner)

---

## 3. Settled Decisions

### Product & UX
1. **LINE-first owner control + 1-screen `/kitchen` board**: Owner texts the same LINE OA (`OWNER_LINE_USER_ID`) for state changes and reads FIFO tickets on `/kitchen` (protected by a shared `KITCHEN_SECRET` query param/cookie; no login form).
2. **Auto-queue on Confirm (0 LINE pushes)**: Tapping **ยืนยันออเดอร์** on a Flex card creates a `queued` order via plain Python code (no LLM call) and replies with the receipt using the free reply token.
3. **Hybrid kitchen alerting**: `/kitchen` polls `/api/kitchen/state` every 3s, requests Screen Wake Lock when open, and plays a Web Audio beep (unlocked on first user tap) when a new `queued` order appears. For 1-phone owners, an optional `notify_owner_on_order` toggle (default `false`, controlled via `"เปิดแจ้งเตือน"` / `"ปิดแจ้งเตือน"` or `/kitchen`) sends 1 LINE push per confirmed order to `OWNER_LINE_USER_ID`.
4. **Completion & cancel notifications**:
   - `ทำเสร็จแล้ว` marks `done` with **0 LINE pushes** (customer can ask `"เสร็จยัง"` for a free reply).
   - `เสร็จ + แจ้งลูกค้า` marks `done` and sends **1 LINE push** (`"ออเดอร์ #N เสร็จแล้ว มารับได้เลยค่ะ"` for pickup; `"ออเดอร์ #N กำลังออกไปส่งแล้วค่ะ"` for delivery).
   - `ยกเลิก` requires a reason (1-tap presets: `ของหมด`, `นอกพื้นที่ส่ง`, `ปิดรับชั่วคราว`, or custom), marks `cancelled`, and sends **1 LINE push** with the reason.
5. **Pickup and delivery + returning customer address memory**:
   - Supports both `pickup` (`รับเองที่ร้าน`) and `delivery` (`ส่ง`). Ask once before creating the card if neither is specified.
   - Confirmed delivery orders save `last_address` and `last_location_url` on `customers`. On subsequent delivery orders, the bot pre-fills that saved address, states it once (`"ส่งที่เดิม (...) นะคะ ถ้าเปลี่ยนที่อยู่บอกได้เลยค่ะ"`), and lets new text or a new location pin override it.
   - Delivery fee is computed strictly by code from `menu.yaml` (default: 0 baht for pickup; 10 baht for delivery when food total < 100 baht, 0 baht when >= 100 baht; setting `delivery_fee: 0` disables fees). Delivery area judgment stays with the owner.
6. **Smart option defaults & off-menu boundary**:
   - Default unspecified `spice` to `เผ็ดปกติ` and unspecified `sweetness` to `หวานปกติ`.
   - Clarify only genuinely ambiguous choices: missing required protein (e.g., `"กะเพราหมู"` where both `หมูสับ` and `หมูชิ้น` exist, or `"ผัดซีอิ๊ว"` with no protein), missing dish variant (`"สุกี้"` -> แห้ง or น้ำ), or `"ไข่ดาว"` without `สุก` / `ไม่สุก`.
   - `"พิเศษ"` applies the `+10` portion option on meals without asking what extra.
   - Preserve free-form prep notes on listed dishes (`"ไม่ใส่ผัก"`, `"แยกน้ำ"`). Do **not** guess or price unlisted dishes/proteins/noodles or answer ingredient/allergy safety questions; state it is not on the bot menu and hand over to the owner.
7. **Payment text (no slip check in v1)**: Confirmation reply includes order `#`, total, and optional `PAYMENT_INFO_TEXT` from `.env` (e.g., PromptPay number + account name) when configured.
8. **Non-text LINE messages**:
   - `location`: save pin address + `https://maps.google.com/?q={lat},{lng}` as delivery address (rendered as a clickable `เปิดแผนที่` link on `/kitchen` and saved to `customers` on confirm).
   - `image`: do not call the LLM; if not already handed over, reply once `"ได้รับรูปแล้วค่ะ ส่งให้ร้านดูให้นะคะ"` and set `handed_over_until` to end of shop day; if already handed over, send nothing.
   - `sticker` / `audio` / `video`: ignore with HTTP 200 and no reply (no LLM call).
9. **Hand-over coexistence**:
   - Non-order chat, greetings alone (`"สวัสดีค่ะ"`), complaints, edits to an already-confirmed order, or allergy/unlisted-dish questions reply once (`"ส่งข้อความให้ร้านแล้วค่ะ เดี๋ยวร้านตอบกลับนะคะ"`) and set `handed_over_until` to the end of today (`Asia/Bangkok`).
   - While handed over, the bot sends **no reply** to non-order messages (e.g. `"ขอบคุณค่ะ"`). It wakes back up automatically when the customer sends a clear new order, menu/open/status query, `queued` order cancel request, postback button tap, or on the next shop day.
10. **Rough schedule + same-day manual override**:
    - `menu.yaml` sets default daily hours (e.g. `09:00–20:00`, `Asia/Bangkok`).
    - Open status is computed from scheduled hours unless `shop_state.override_date == today_th` and `open_override` is `'open'` or `'closed'`.
    - Owner's `"ปิดร้าน"` / `"เปิดร้าน"` sets `open_override` for today only; both `open_override` and `busy_mode` expire automatically when the local date changes.

### Technical Architecture, Schema & Guardrails
- **Stack**: Python 3.13 (`uv`), FastAPI, `uvicorn`, SQLite (`sqlite3` in WAL mode, `BEGIN IMMEDIATE` for writes), `httpx`, `openai` SDK, `pyyaml`, `pytest`, `ruff`, `mypy`.
- **LLM config**: `.env` provides `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` (default OpenRouter `https://openrouter.ai/api/v1` + `anthropic/claude-haiku-5.5` with `reasoning.effort = "low"`).
- **Guardrails in code**: max **6 tool calls/turn**, **40s deadline/turn**, max **30 customer messages/day** per conversation (hitting 30 replies once handing over to the owner for the rest of today).
- **Concurrency**: Per-customer in-process `asyncio.Lock` keyed by `line_user_id` + SQLite `BEGIN IMMEDIATE` transactions.
- **SQLite schema (`shop.db`, created automatically on startup)**:
  - `shop_state(id INTEGER PRIMARY KEY CHECK (id = 1), override_date TEXT, open_override TEXT, bot_enabled INTEGER NOT NULL DEFAULT 1, busy_date TEXT, busy_mode INTEGER NOT NULL DEFAULT 0, wait_minutes INTEGER, notify_owner_on_order INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL)`
  - `menu_overrides(kind TEXT NOT NULL, key TEXT NOT NULL, is_available INTEGER NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (kind, key))`
  - `customers(line_user_id TEXT PRIMARY KEY, display_name TEXT, last_address TEXT, last_location_url TEXT, updated_at TEXT NOT NULL)`
  - `conversations(id INTEGER PRIMARY KEY AUTOINCREMENT, line_user_id TEXT NOT NULL, shop_date TEXT NOT NULL, messages_json TEXT NOT NULL, draft_json TEXT NOT NULL, draft_revision INTEGER NOT NULL DEFAULT 0, active_card_id TEXT, active_card_json TEXT, handed_over_until TEXT, updated_at TEXT NOT NULL, UNIQUE (line_user_id, shop_date))`
  - `orders(id INTEGER PRIMARY KEY AUTOINCREMENT, shop_date TEXT NOT NULL, queue_number INTEGER NOT NULL, line_user_id TEXT NOT NULL, card_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL, cancel_reason TEXT, fulfillment TEXT NOT NULL, address TEXT, location_url TEXT, requested_time TEXT, items_json TEXT NOT NULL, food_total INTEGER NOT NULL, delivery_fee INTEGER NOT NULL, total INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE (shop_date, queue_number))`
  - `line_events(event_id TEXT PRIMARY KEY, received_at TEXT NOT NULL)`

- **Target file layout**:
```text
tam-sang-bot/
├── SPEC.md
├── pyproject.toml
├── .env.example
├── menu.yaml
├── app/
│   ├── __init__.py
│   ├── config.py
│   ├── db.py
│   ├── menu.py
│   ├── line.py
│   ├── agent.py
│   ├── owner.py
│   ├── main.py
│   └── kitchen.html
└── tests/
    ├── test_menu.py
    ├── test_orders.py
    ├── test_webhook.py
    ├── test_agent.py
    └── test_owner.py
```

---

## 4. Acceptance Criteria (Observable Behavior)

### Menu, Pricing & Shop Hours
- [x] Given `menu.yaml` seeded with the 24 `ครัวอิ่มจัง` items, when pricing `ข้าวกะเพรา` (`50`) + `หมูกรอบ` (`+10`) + `ไข่ดาวไม่สุก` (`+10`) x 2 boxes, then code computes `food_total = 140`, `delivery_fee = 0`, `total = 140` for both pickup and delivery.
- [x] Given a 1-box delivery order with `food_total = 70` (< 100), then code computes `delivery_fee = 10` and `total = 80`; given the same order as `pickup`, then `delivery_fee = 0` and `total = 70`.
- [x] Given `menu.yaml` with `delivery_fee: 0`, then any delivery order has `delivery_fee = 0`.
- [x] Given `ingredient_key = "crispy_pork"` marked unavailable in `menu_overrides`, then `หมูกรอบ` is disabled across `kaphrao`, `curry_paste`, and `kale`, while other proteins on those dishes remain available.
- [x] Given `menu.yaml` schedule `09:00–20:00` (`Asia/Bangkok`), when local time is `21:00` with no override, shop is closed; when owner sets `open_override = 'open'` for today, shop is open until midnight, and at `00:01` next day the override and `busy_mode` no longer apply.

### Order Lifecycle & `/kitchen` Board
- [x] Given a valid active order card, when Confirm is invoked once, then exactly 1 `orders` row is created with sequential `queue_number` (`1`, `2`, ...) for `shop_date`, `status = 'queued'`, draft is cleared, and `customers.last_address` / `last_location_url` are updated for delivery orders.
- [x] Given an already-confirmed `card_id`, when Confirm is invoked a second time, then no duplicate row is created and the existing `queue_number` and `total` are returned.
- [x] Given a card whose dish/option became sold out or whose `draft_revision` changed after card creation, when Confirm is invoked, then no order is created and an error describing what changed is returned.
- [x] Given `/kitchen` without valid `KITCHEN_SECRET`, then HTTP 401/403 is returned; with valid secret, `/kitchen` renders HTML and `/api/kitchen/state` returns active `queued` tickets (oldest first), today's completed/cancelled totals, shop status, busy mode, and sold-out states.
- [x] Given a `queued` order on `/kitchen`, when `POST /api/kitchen/orders/{id}/done` is called with `notify=false`, status becomes `done` and 0 LINE pushes are sent; with `notify=true`, status becomes `done` and 1 LINE push is sent; when `POST /api/kitchen/orders/{id}/cancel` is called with a reason, status becomes `cancelled` and 1 LINE push containing that reason is sent.

### LINE Webhook, Media & Customer Agent
- [x] Given a `POST /line/webhook` request with an invalid `x-line-signature`, then HTTP 400 is returned and no handler runs; given a valid request with a duplicate `webhookEventId`, then HTTP 200 is returned without re-processing.
- [x] Given a customer `location` message, then delivery address and `https://maps.google.com/?q={lat},{lng}` are stored on the draft and returned to the customer without requiring re-typing.
- [x] Given a customer `image` message, then 0 LLM calls are made, the bot replies once with `"ได้รับรูปแล้วค่ะ ส่งให้ร้านดูให้นะคะ"` and sets `handed_over_until`; a second `image` while handed over sends 0 replies.
- [x] Given a customer `sticker`, `audio`, or `video` message, then HTTP 200 is returned with 0 replies and 0 LLM calls.
- [x] Given a customer ordering `"กะเพราหมูกรอบ 1 กล่อง รับเอง"`, then the agent applies default spice (`เผ็ดปกติ`) without asking for spice level, builds a Flex order card with code-calculated total `60`, and includes `wait_minutes` text on the card when `busy_mode` is active.
- [x] Given a returning delivery customer with saved `last_address`, when they order for delivery without typing an address, then the draft pre-fills `last_address` (and `last_location_url` if present) and states it once.
- [x] Given a customer message asking to edit a `queued` order, a bare greeting (`"สวัสดีค่ะ"`), or an unlisted dish/allergy question, then the bot replies once with the hand-over message and sets `handed_over_until`; a follow-up `"ขอบคุณค่ะ"` produces 0 replies; a new order or `"ถึงไหนแล้ว"` wakes the bot and replies normally.
- [x] Given `bot_enabled = 0`, then customer text messages produce 0 replies (owner commands still work).
- [x] Given a turn hitting 6 tool calls, 40s deadline, or 30 customer turns in a day, then execution stops safely (at 30 turns, hands over to the owner for the rest of today).

### Owner LINE Commands & Hybrid Alerts
- [x] Given a webhook message from `OWNER_LINE_USER_ID`:
  - `"หมูกรอบหมด"` disables `ingredient:crispy_pork`; `"หมูกรอบมาแล้ว"` re-enables it; `"เปิดครบ"` clears all sold-out overrides.
  - `"ยุ่ง 25 นาที"` sets `busy_mode=1, wait_minutes=25` for today; `"ยุ่ง"` defaults `wait_minutes=20`; `"โหมดปกติ"` clears `busy_mode`.
  - `"ปิดร้าน"` / `"เปิดร้าน"` sets today's `open_override` to `'closed'` / `'open'`.
  - `"หยุดบอท"` / `"เปิดบอท"` sets `bot_enabled` to `0` / `1`.
  - `"เปิดแจ้งเตือน"` / `"ปิดแจ้งเตือน"` sets `notify_owner_on_order` to `1` / `0`.
  - `"คิว"` replies with active `queued` count + `/kitchen` link; `"สรุปวันนี้"` replies with today's `done` count, `cancelled` count, and sum of `done` totals in baht.
- [x] Given `notify_owner_on_order = 1`, when a customer confirms an order, then 1 LINE push with the ticket summary is sent to `OWNER_LINE_USER_ID`; when `notify_owner_on_order = 0`, 0 pushes are sent to the owner.

---

## 5. Verification Contract

The executing agent must run and pass all checks below after its last edit before reporting done:
- **Static**: `uv run ruff check . && uv run mypy app tests`
- **Behavioral (Automated)**: `uv run pytest -q`
- **Manual / Runtime Proof**:
  - Tickets 1–4: Start local `uvicorn app.main:app` against a temp SQLite DB and verify `GET /health` returns `200 {"ok": true}` and `GET /api/kitchen/state` returns `401`/`403` without secret and `200` JSON with secret.
  - Ticket 5 (HITL): Live phone walkthrough via `cloudflared tunnel` against the real LINE OA covering normal mode, busy mode (`ยุ่ง 25 นาที`), location pin, returning customer address reuse, slip image hand-over, owner sold-out command (`หมูกรอบหมด`), and `/kitchen` Done / Cancel push.
- **Baseline Rule**: Do not weaken assertions, skip failing tests, or silently alter expected outputs/menu prices. List any intentional change in the Proof Report for human approval.

---

## 6. Autonomy Boundaries & Rollback

- **Agent may decide and log (reversible)**: Internal function/module structure, exact Thai wording polish (preserving required meanings), `/kitchen` CSS styling, local test fixture data, and `ruff`/`mypy` config flags.
- **Agent MUST stop and ask (irreversible / intent-changing)**:
  - Any live paid LLM API call (e.g., OpenRouter) or live LINE push/reply call during Tickets 1–4 (all automated tests must use mocked/stubbed HTTP & LLM clients).
  - Adding frameworks, external databases, auth libraries, or unrequested dependencies beyond the listed stack.
  - Committing, pushing to a remote, deploying to cloud infrastructure, or changing external LINE OA console settings.
- **Rollback Plan**: `N/A` (greenfield local SQLite project; deleting `shop.db` cleanly re-initializes schema on restart).

---

## Vertical Tracer-Bullet Tickets

### Ticket 1: Menu, SQLite Store & Kitchen Board — Done
- **Mode**: AFK
- **Blocked by**: None
- **Slice**: Initialize project (`pyproject.toml`, `.env.example`, `menu.yaml`, `app/config.py`, `app/menu.py`, `app/db.py`, `app/main.py`, `app/kitchen.html`) delivering the complete menu/pricing engine, SQLite persistence, rough schedule + same-day overrides, order lifecycle (`queued` / `done` / `cancelled`), and live mobile-first `/kitchen` UI + API.
- **Acceptance Criteria**:
  - [x] All 5 **Menu, Pricing & Shop Hours** criteria in Section 4 pass.
  - [x] All 5 **Order Lifecycle & `/kitchen` Board** criteria in Section 4 pass (with LINE push client injected as a no-op/spy stub in tests).
  - [x] `/kitchen` includes Wake Lock request, audio beep unlock on first tap, 3s polling, Open/Close toggle, Busy chips (`ปกติ`, `ยุ่ง 15น.`, `ยุ่ง 30น.`), sold-out chips, clickable `เปิดแผนที่` link when `location_url` is set, `ทำเสร็จแล้ว`, `เสร็จ + แจ้งลูกค้า`, and `ยกเลิก` with preset reasons.
- **Verification**:
  - `uv run ruff check . && uv run mypy app tests`
  - `uv run pytest -q tests/test_menu.py tests/test_orders.py`
  - Local `uvicorn` smoke check via `curl` on `/health` and `/api/kitchen/state` (unauthorized vs. authorized).

### Ticket 2: LINE Webhook, Media Routing & Confirm/Cancel Postbacks — Done
- **Mode**: AFK
- **Blocked by**: Ticket 1
- **Slice**: Implement `app/line.py` (HMAC-SHA256 signature verification, `line_events` dedupe, loading animation, reply/push HTTP helpers, Flex order card builder) and wire `POST /line/webhook` in `app/main.py` for non-text media (`location`, `image`, `sticker`/`audio`/`video`) and idempotent `confirm` / `cancel` postbacks (including `PAYMENT_INFO_TEXT` in confirm reply and returning customer address persistence).
- **Acceptance Criteria**:
  - [x] Invalid signature returns HTTP 400; duplicate `webhookEventId` returns HTTP 200 without re-running.
  - [x] `location` pin updates draft delivery address + Google Maps URL; `image` skips LLM, replies once + hands over (silent on repeat while handed over); `sticker`/`audio`/`video` ignored with 0 replies.
  - [x] Confirm postback creates 1 `queued` order via free reply token (0 pushes), returns same receipt on double-tap, refuses stale/sold-out cards, and persists `last_address`/`last_location_url` for delivery orders.
  - [x] Cancel postback cancels the active draft card and replies via reply token.
- **Verification**:
  - `uv run ruff check . && uv run mypy app tests`
  - `uv run pytest -q tests/test_webhook.py tests/test_orders.py`

### Ticket 3: Customer Ordering Agent & Hand-Over — Done
- **Mode**: AFK
- **Blocked by**: Ticket 2
- **Slice**: Implement `app/agent.py` (Thai system prompt + cacheable menu block, OpenAI SDK client configured from `.env`, tools: `get_shop_status`, `update_cart`, `set_fulfillment`, `build_order_card`, `get_my_orders`, `cancel_order`, `hand_over`, smart defaults for spice/sweetness, returning delivery address reuse, hand-over silence, and 6-call / 40s / 30-turn guardrails) wired into `/line/webhook` under per-customer `asyncio.Lock`.
- **Acceptance Criteria**:
  - [x] Messy Thai orders with default spice/sweetness build accurate code-priced Flex cards (including busy wait-time notice when `busy_mode=1`).
  - [x] Returning delivery customer reuses `last_address` (and `last_location_url`) automatically and states it once.
  - [x] Closed shop refuses cart additions/confirmations and states normal opening hours.
  - [x] Bare greeting, confirmed-order edit request, unlisted dish, or allergy question triggers 1 hand-over reply; subsequent non-order messages produce 0 replies; new order / status / `queued` cancel wakes the bot.
  - [x] `bot_enabled=0` silences customer replies; 6-call, 40s, and 30-turn guardrails terminate safely.
- **Verification**:
  - `uv run ruff check . && uv run mypy app tests`
  - `uv run pytest -q tests/test_agent.py tests/test_webhook.py`

### Ticket 4: Owner LINE Commands & Push Alerts — Done
- **Mode**: AFK
- **Blocked by**: Ticket 3
- **Slice**: Implement `app/owner.py` (route `OWNER_LINE_USER_ID` messages in `/line/webhook`, parse natural Thai owner commands with deterministic matching + optional LLM fallback, execute state updates and reply via free reply token) and connect live LINE push calls for `notify_owner_on_order`, `/kitchen` `เสร็จ + แจ้งลูกค้า`, and `/kitchen` `ยกเลิก`.
- **Acceptance Criteria**:
  - [x] All owner commands (`หมูกรอบหมด`, `หมูกรอบมาแล้ว`, `เปิดครบ`, `ยุ่ง 25 นาที`, `ยุ่ง`, `โหมดปกติ`, `ปิดร้าน`, `เปิดร้าน`, `หยุดบอท`, `เปิดบอท`, `เปิดแจ้งเตือน`, `ปิดแจ้งเตือน`, `คิว`, `สรุปวันนี้`) update SQLite state and reply accurately with 0 LINE pushes.
  - [x] `notify_owner_on_order=1` sends 1 compact ticket push to `OWNER_LINE_USER_ID` on order confirmation; `0` sends none.
  - [x] `/kitchen` `done` (`notify=true`) and `cancel` send the expected Thai LINE push to the customer; `done` (`notify=false`) sends 0 pushes.
- **Verification**:
  - `uv run ruff check . && uv run mypy app tests`
  - `uv run pytest -q` (entire suite passes with 0 skips).

### Ticket 5: Live LINE Phone Walkthrough — Done (HITL)
- **Mode**: HITL
- **Blocked by**: Ticket 4
- **Slice**: Configure `.env` with real LINE OA + OpenRouter credentials, expose local `uvicorn` via `cloudflared tunnel`, set the LINE webhook URL, and test end-to-end from a real phone in both normal and busy modes.
- **Acceptance Criteria**:
  - [x] Customer orders messy Thai on LINE -> receives Flex card -> taps Confirm -> ticket appears + chimes on `/kitchen` (0 pushes) -> double-tap returns same order `#`.
  - [x] Customer sends a LINE location pin -> clickable `เปิดแผนที่` appears on `/kitchen` -> second order reuses saved address automatically.
  - [x] Customer sends a slip image -> receives single hand-over reply -> follow-up `"ขอบคุณค่ะ"` gets silence.
  - [x] Owner texts `"หมูกรอบหมด"` and `"ยุ่ง 25 นาที"` -> customer bot blocks crispy pork and quotes 25-min wait time; owner texts `"สรุปวันนี้"` and gets accurate total.
  - [x] Owner cancels a test ticket on `/kitchen` with reason -> customer receives the cancellation push on LINE.
- **Verification**: Manual live walkthrough checklist signed off by human user (2026-11-23).
