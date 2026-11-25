# Coding Standards (review only)

Reviewers check every rule below against the diff. This list is a floor, not a ceiling:
check these four on every diff, then review everything else as you normally would.

These are not open bugs: every case found so far is fixed and tested. They are the
mistakes most likely to come back when new code adds a customer message, a card or
payment write, or an order query. Implementers follow AGENTS.md.

Upkeep: add a rule only when the same kind of bug is found twice, or once with real
harm. Delete a rule once a test or lint check covers its whole scope.

## 1. Respect owner takeover
The owner can tap คุยเอง while the bot waits on the LLM or SlipOK; a reply sent after
that talks over the owner.
- Look for: a fresh `is_handed_over` check (`owner_took_over()` in the agent loop) after
  the last slow call and before any bot-initiated write or reply, including error and
  fallback paths. A suppressed reply still updates the inbox card. Confirm/Cancel taps,
  the matched-slip line and owner-triggered pushes have their own defined behavior
  (SPEC §3.2).
- Origin: SPEC §3.9 items 13–14 (5 paths over 2 review rounds).

## 2. The next LLM turn sees what the customer was actually given
Missing or invented history makes the bot repeat an action or contradict the shop.
- Look for: every text chosen to send (reply or push) recorded in history with
  `append_conversation_messages` / `append_conversation_event`. Never read history
  before a slow call and write it back after. Check what the next LLM call receives.
  (Whole-history overwrites are blocked by `tests/test_guardrails.py`.)
- Origin: owner pushes erased during an LLM turn; slip messages recorded differently
  from what was sent.

## 3. Re-check order and payment commitments at the write
An owner action or another message can change what a slip should pay or whether an
order can still be confirmed.
- Look for: eligibility checks inside the committing transaction, after any slow call
  (`slip_snapshot` → `pay_with_slip(expected=)`). A new confirm rule must apply to both
  the Confirm tap and slip confirmation (the shared `_confirm_card_in_tx` path). A card that was
  built but not delivered (suppressed reply, failed `_safe_reply`) is cancelled by its
  exact card ID.
- Origin: one transfer credited twice; a slip confirming a card never shown (the text
  path, then the location path).

## 4. Check which orders each view or calculation includes
- Look for: the actual rows feeding a new calculation. `legacy` orders stay visible for
  cleanup but out of payment totals, slip matching, queue position, wait estimate and
  รวมจาน. Earlier-day orders still waiting stay reachable. Actions target order IDs,
  never `#N` (numbers repeat each shop day).
- Origin: legacy counted twice (queue position, wait estimate); earlier-day orders
  hidden from /kitchen and the slip picker (SPEC §3.9 items 1, 14).
