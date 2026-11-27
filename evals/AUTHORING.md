# Writing test cases for the order evaluation

This is the brief given to the agents that wrote the test cases (spec: `docs/specs/2026-11-27-order-eval.md`). It is kept in the repo so the method can be checked and repeated.

You are writing test cases for a LINE chatbot that takes food orders for a small Thai made-to-order shop. The shop is fictional. Customers type in Thai; the bot builds an **order card** (the dishes, their options, pickup or delivery). Your cases will measure how often the bot gets the order right, so each expected answer must be exactly what a careful Thai shop owner would write down.

## What you may read
- `menu.yaml`: the whole menu, its option groups, prices, hours, delivery rule and address.
- This file.

Don't open anything else in the repo (`app/`, `tests/`, `docs/`, `SPEC.md`, `scripts/` except for running the validator, `evals/cases/`, `local/`). The point is that you write cases without knowing how the bot works.

## Shop rules (what the bot is supposed to do)
- **Complete order → order card.** An order is complete when every dish has its required choices (for example the protein for กะเพรา) and the customer says pickup (`รับเอง`, `มารับ`) or delivery with an address. Then the bot builds the card right away.
- **Options:** list only what the customer asked for, plus required choices. Leave out anything they didn't mention: the menu's defaults fill those in, and a default counts as correct either way.
- **Notes** like `ไม่ใส่ผัก` or `แยกน้ำ` aren't graded. Never put them in `option_ids`.
- **Dishes the shop doesn't sell:** the bot replies, no card, no hand-over.
- **Ingredient, allergy or MSG questions:** the bot hands the chat to the owner (`handover: off_menu`), no card.
- **Shop closed** (`shop_open: false`, the bot sees a time outside 09:00–20:00): no card, even for a complete order.
- **Side questions in an order** (the order is complete and the customer also asks something): the bot builds the card and answers.

## The file
Write exactly 40 cases as a YAML list. Use the ID prefix and output path you were given. Keep this shape:

```yaml
- id: gpt-001
  category: options            # simple | options | multi_item | multi_turn | side_question | not_order | messy
  turns: ["กะเพราหมูกรอบ ไข่ดาวไม่สุก เผ็ดน้อย 2 จาน รับเองค่ะ"]
  shop_open: true
  expect:
    card:
      items:
        - {dish_id: kaphrao, qty: 2, option_ids: [crispy_pork, fried_egg_runny, spice_mild]}
      fulfillment: pickup      # pickup | delivery
      address_contains: null   # delivery: a distinctive part of the address the customer gave
    no_card: false
    handover: null             # null | off_menu
    reply_contains: []         # side_question only
```

For a case without a card, set `card: null` and `no_card: true` (plus `handover: off_menu` for ingredient or allergy questions).

## How many of each (40 in total)

| Category | Count | What it is |
|---|---|---|
| `simple` | 6 | One dish, the required choices, pickup or delivery |
| `options` | 7 | Extras, egg, spice, special size, protein upgrades |
| `multi_item` | 6 | 2–4 different dishes or drinks in one message |
| `multi_turn` | 5 | 2–3 messages: add an item, change a protein or option, switch pickup to delivery with an address. Write each message without knowing the bot's reply, and never answer a question the bot might ask. Grade the final order. |
| `side_question` | 5 | A complete order plus a question. `reply_contains` holds 1 short piece of text that any correct answer must contain: for the address `ซอยตามสั่ง`, for closing time `20`, for the free-delivery threshold `100`. |
| `not_order` | 6 | 2 dishes the shop doesn't sell, 2 ingredient or allergy questions (`handover: off_menu`), 2 complete orders with `shop_open: false` |
| `messy` | 5 | Complete orders written very messily: heavy slang, typos, no spaces, emoji |

Across the 40, use at least 15 different `dish_id`s (including drinks and sides), quantities from 1 to 5, and delivery in at least 12 card cases.

## How to write the messages
- Write like real customers on LINE: short and casual, `ค่ะ`/`ครับ`/`จ้า`/`นะคะ`, typos, missing spaces, emoji, a little English. Vary the personas.
- Use the names people actually say (nicknames, the menu's aliases, shortened names), not only the exact menu wording.
- If a Thai shop owner could reasonably read a message in two ways, rewrite it. Every expected answer must be unambiguous.
- Delivery messages include an address (a condo, a soi, an office); `address_contains` is a distinctive part of it, like the condo name.

## Before you finish
Run `uv run python scripts/eval.py validate <your file>` and fix every error until the last line says `validate: PASS`. Don't edit any other file and don't commit.

In your final reply, give only the file path, the validator's output and the number of cases per category. **Never paste case text into your reply**: some sets stay hidden from the developer until the final run.
