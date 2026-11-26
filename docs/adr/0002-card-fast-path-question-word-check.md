---
status: accepted (revisit after 50 real qword turns, see below)
---

# The order-card fast path keeps a question-word check until real traffic says otherwise

When a one-message order builds its order card cleanly, the bot sends a fixed line instead of making a second LLM call. That saves about 1.1 s per order: 4.35 s → 3.23 s in an A/B run of 20 Haiku messages on 2026-11-26.

The risk is a side question that needs no tool, such as "…รับเองค่ะ ร้านอยู่ตรงไหน". The LLM returns only the 3 order tools and plans to answer in the second call. If that call is skipped, the question is silently dropped.

So the fast path is also skipped when the customer's message contains a Thai question word (`_QUESTION_RE` in `app/agent.py`). This costs about 1.3 s on orders phrased as polite questions, such as "เผ็ดน้อยได้ไหม", where the second call only repeats the fixed line.

Evidence so far:
- **False alarms:** 2, both "เผ็ดน้อยได้ไหม".
- **Real catches:** 1, "…ร้านอยู่ตรงไหน". The LLM answered with the shop address, which the fast path would have dropped.

Spec: `docs/specs/2026-11-26-card-fast-path.md`.

## Considered Options

- **No check, trusting the LLM to add a tool for real questions (rejected for now):** this is faster. In tests, Haiku added a tool for every side question that needed data. It added none for questions it could answer from the prompt, so those would be lost.
- **A prompt line asking the LLM to answer side questions in the same response as its tool calls (rejected):** it had no effect. The text alongside the tool calls was empty in 10 of 10 turns.

## Revisit rule

Each `webhook_event` log line has a `card_path` field:

| Value | Meaning |
|---|---|
| `fast` | The fast path ran. |
| `qword_same` | Only the question word blocked the fast path, and the LLM said nothing more than the fixed line. This is a false alarm that cost ~1.3 s. |
| `qword_diff` | Only the question word blocked the fast path, and the LLM said something different (an answer or a hand-over). This is a real catch. |
| `-` | The fast path didn't apply. |

After the first **50** real-traffic turns logged `qword_same` or `qword_diff`:
- if `qword_diff` is **0**, drop the check;
- otherwise keep it.

To count them, run this on the log the bot writes. Locally that's `.dev.log` from `./scripts/dev.sh run`; on a server it's the service's kept log:
`grep -o 'card_path=\S*' .dev.log | sort | uniq -c`

A single `qword_diff` is a customer question that would otherwise go unanswered. That is worth more than the delay on 49 false alarms.
