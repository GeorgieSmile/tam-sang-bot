# Writing goals for the simulated-customer test

This is the brief given to the agent that wrote `evals/goals.yaml` (spec: `docs/specs/2026-11-27-order-eval.md`, Part B).

A second LLM will play a customer chatting with a LINE chatbot that takes food orders for a small, fictional Thai made-to-order shop. You write that customer's **goal**: what they want, who they are, and how their first message leaves things out. The simulator then chats with the bot until the bot shows an order summary card, and code checks the card against your expected order.

## What you may read
Only `menu.yaml` and this file. Don't open anything else in the repo (`app/`, `tests/`, `docs/`, `SPEC.md`, `scripts/` except to run the validator, `evals/cases/`, `local/`).

## Shop rules
- The bot builds an order card once every dish has its required choices (for example the protein for กะเพรา) and the customer has said pickup or delivery with an address. Until then it asks.
- Options the customer doesn't mention take the menu's default, and a default counts as correct either way. List only options the customer wants, plus required choices.
- Notes like `ไม่ใส่ผัก` aren't graded and never go in `option_ids`.

## The file
Write exactly 20 goals to `evals/goals.yaml` as a YAML list:

```yaml
- id: sim-001
  persona: "นักศึกษาหอใน พิมพ์สั้นๆ ไม่ค่อยใส่ค่ะครับ"
  opening: "Says only the dish and quantity. No protein, no pickup or delivery."
  goal: "กะเพราหมูกรอบ 2 กล่อง ไข่ดาวไม่สุกทั้งคู่ เผ็ดน้อย ส่งที่หอพักต้นฝน ห้อง 12"
  expect:
    card:
      items:
        - {dish_id: kaphrao, qty: 2, option_ids: [crispy_pork, fried_egg_runny, spice_mild]}
      fulfillment: delivery
      address_contains: "ต้นฝน"
```

- `goal` is the complete order in Thai, everything the customer wants. It must match `expect.card` exactly.
- `opening` (in English) says what the first message leaves out, so the bot has to ask. Every goal needs at least one follow-up.
- Mix the reasons for follow-ups across the 20: a missing protein or other required choice, missing pickup or delivery, delivery without an address, an extra item added later, a change to an item after the first message, several items given over several messages.
- Use at least 12 different `dish_id`s, quantities 1–5, and delivery in at least 8 goals.
- Vary the personas: students, office workers, a parent ordering for a family, an older customer, someone in a hurry.
- Every goal must be unambiguous to a Thai shop owner.

## Before you finish
Run `uv run python scripts/eval.py validate-goals` until it ends with `validate: PASS`. Don't edit any other file and don't commit. In your final reply give only the file path and the validator output.
