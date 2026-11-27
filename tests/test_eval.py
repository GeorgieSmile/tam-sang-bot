"""The offline eval (scripts/eval.py): the validator rejects labels the menu can't price, and the
runner grades the real app's end state (stub LLM only, so no paid calls)."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("order_eval", ROOT / "scripts" / "eval.py")
assert _spec is not None and _spec.loader is not None
order_eval: Any = importlib.util.module_from_spec(_spec)
sys.modules["order_eval"] = order_eval
_spec.loader.exec_module(order_eval)

MENU = order_eval.load_menu(ROOT / "menu.yaml")

GOOD: dict[str, Any] = {
    "id": "t-001",
    "category": "options",
    "turns": ["กะเพราหมูกรอบ ไข่ดาวไม่สุก เผ็ดน้อย 2 จาน รับเองค่ะ"],
    "shop_open": True,
    "expect": {
        "card": {
            "items": [
                {
                    "dish_id": "kaphrao",
                    "qty": 2,
                    "option_ids": ["crispy_pork", "fried_egg_runny", "spice_mild"],
                }
            ],
            "fulfillment": "pickup",
            "address_contains": None,
        },
        "no_card": False,
        "handover": None,
        "reply_contains": [],
    },
}


def _case(**changes: Any) -> dict[str, Any]:
    """GOOD with dotted-path changes, e.g. `**{"expect.card.fulfillment": "delivery"}`."""
    case = copy.deepcopy(GOOD)
    for path, value in changes.items():
        *parents, last = path.split(".")
        node: Any = case
        for key in parents:
            node = node[int(key)] if key.isdigit() else node[key]
        if isinstance(node, list):
            node[int(last)] = value
        else:
            node[last] = value
    return case


def test_a_well_formed_case_has_no_errors() -> None:
    assert order_eval.check_case(GOOD, MENU) == []


def test_default_options_may_be_left_out() -> None:
    case = _case(**{"expect.card.items.0.option_ids": ["chicken"]})
    assert order_eval.check_case(case, MENU) == []


@pytest.mark.parametrize(
    ("changes", "fragment"),
    [
        ({"expect.card.items.0.dish_id": "pizza"}, "pizza"),
        ({"expect.card.items.0.option_ids": ["crispy_pork", "no_such_option"]}, "no_such_option"),
        ({"expect.card.items.0.option_ids": ["fried_egg_runny"]}, "item 1 (kaphrao)"),
        ({"expect.card.items.0.option_ids": ["crispy_pork", "chicken"]}, "item 1 (kaphrao)"),
        ({"expect.card.items.0.qty": 0}, "item 1 (kaphrao)"),
        ({"expect.card.items.0.qty": "2"}, "qty must be an integer"),
        ({"expect.card.fulfillment": "delivery"}, "address_contains"),
        ({"expect.card.address_contains": "คอนโด"}, "pickup must have"),
        ({"expect.card.fulfillment": "dine_in"}, "pickup or delivery"),
        ({"expect.no_card": True}, "can't both be set"),
        ({"shop_open": False}, "closed shop"),
        ({"category": "not_order"}, "not_order can't expect a card"),
        ({"category": "side_question"}, "needs reply_contains"),
        ({"category": "multi_turn"}, "at least 2 turns"),
        ({"category": "fancy"}, "category must be one of"),
        ({"turns": []}, "1-3 non-empty strings"),
        ({"expect.handover": "greeting"}, "handover must be"),
        ({"expect.card": None}, "needs a card, no_card"),
        ({"expect.surprise": 1}, "unknown key"),
    ],
)
def test_bad_labels_are_rejected_by_case_id(changes: dict[str, Any], fragment: str) -> None:
    errors = order_eval.check_case(_case(**changes), MENU)
    assert errors, changes
    assert all(e.startswith("t-001") for e in errors)
    assert any(fragment in e for e in errors), errors


def test_a_protein_the_dish_does_not_allow_is_rejected() -> None:
    case = _case(**{"expect.card.items.0.dish_id": "garlic"})  # garlic has no crispy_pork
    case["expect"]["card"]["items"][0]["option_ids"] = ["crispy_pork"]
    assert order_eval.check_case(case, MENU)


def test_errors_never_quote_the_customer_text() -> None:
    case = _case(**{"expect.card.items.0.dish_id": "pizza", "category": "fancy"})
    for e in order_eval.check_case(case, MENU):
        assert GOOD["turns"][0] not in e


def _write(tmp: Path, name: str, cases: list[dict[str, Any]]) -> Path:
    path = tmp / name
    path.write_text(yaml.safe_dump(cases, allow_unicode=True), encoding="utf-8")
    return path


def _one_per_category(prefix: str) -> list[dict[str, Any]]:
    cases = []
    for i, cat in enumerate(order_eval.CATEGORIES):
        case = _case(id=f"{prefix}-{i}", category=cat)
        if cat == "multi_turn":
            case["turns"] = ["กะเพราไก่ รับเอง", "เปลี่ยนเป็นหมูกรอบ ไข่ดาวไม่สุก เผ็ดน้อย 2 จาน"]
        if cat == "side_question":
            case["expect"]["reply_contains"] = ["ซอยตามสั่ง"]
        if cat == "not_order":
            case["expect"] = {"card": None, "no_card": True, "handover": None}
        cases.append(case)
    return cases


def test_validate_passes_a_complete_file_and_prints_counts_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write(tmp_path, "a.yaml", _one_per_category("a"))
    assert order_eval.validate([tmp_path], MENU) == 0
    out = capsys.readouterr().out
    assert f"{path}: 7 cases" in out and "validate: PASS" in out
    assert "กะเพรา" not in out


def test_validate_fails_on_missing_categories_and_duplicate_ids(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path, "a.yaml", _one_per_category("a"))
    _write(tmp_path, "b.yaml", [_case(id="a-0")])
    assert order_eval.validate([tmp_path], MENU) == 2
    out = capsys.readouterr().out
    assert "missing categories" in out and "duplicate id a-0" in out
    assert "validate: FAIL" in out


# --- The runner and grader, end to end with the stub LLM (no paid calls) ----------------

STUB_LLM = {
    "LLM_BASE_URL": "http://127.0.0.1:9/none",
    "LLM_API_KEY": "none",
    "LLM_MODEL": "stub/model",
    "LLM_REASONING_EFFORT": "low",
}
PRICES = {"stub/model": {"input": 1000.0, "cache_read": 100.0, "cache_write": 0, "output": 2000.0}}
KAPHRAO = {
    "dish_id": "kaphrao",
    "qty": 2,
    "option_ids": ["crispy_pork", "fried_egg_runny", "spice_mild"],
}


def _script(*calls: tuple[str, dict[str, Any]]) -> str:
    return "!tools " + json.dumps([list(c) for c in calls], ensure_ascii=False)


def _order(items: list[dict[str, Any]], fulfillment: dict[str, Any]) -> str:
    return _script(
        ("update_cart", {"action": "add", "items": items}),
        ("set_fulfillment", fulfillment),
        ("build_order_card", {}),
    )


class UsageLLM(order_eval.demo.EchoLLM):
    """The demo's scripted LLM, plus token usage so the cost code has something to price."""

    def _create(self, **kwargs: Any) -> Any:
        resp = super()._create(**kwargs)
        resp.usage = SimpleNamespace(
            prompt_tokens=1000,
            completion_tokens=100,
            prompt_tokens_details=SimpleNamespace(cached_tokens=400),
        )
        return resp


def _run(tmp: Path, case: dict[str, Any], llm: Any = None) -> dict[str, Any]:
    res: dict[str, Any] = order_eval.run_case(
        case, tmp / case["id"], llm or order_eval.demo.EchoLLM(), STUB_LLM, PRICES
    )
    return res


def test_a_scripted_order_that_matches_the_label_passes(tmp_path: Path) -> None:
    case = _case(turns=[_order([KAPHRAO], {"fulfillment": "pickup"})])
    res = _run(tmp_path, case)
    assert res["passed"], res
    assert res["reply_ms"] and res["turns"] == 1


def test_defaults_and_split_lines_still_match(tmp_path: Path) -> None:
    # The bot states the default spice and splits the 2 boxes; the label omits the spice
    one = {**KAPHRAO, "qty": 1, "option_ids": ["crispy_pork", "fried_egg_runny", "spice_normal"]}
    case = _case(turns=[_order([one, one], {"fulfillment": "pickup"})])
    case["expect"]["card"]["items"] = [
        {**KAPHRAO, "option_ids": ["crispy_pork", "fried_egg_runny"]}
    ]
    assert _run(tmp_path, case)["passed"]


@pytest.mark.parametrize(
    ("items", "fulfillment", "field"),
    [
        (
            [{**KAPHRAO, "option_ids": ["crispy_pork", "fried_egg_cooked", "spice_mild"]}],
            None,
            "items",
        ),
        ([{**KAPHRAO, "qty": 3}], None, "items"),
        ([KAPHRAO, {"dish_id": "water", "qty": 1}], None, "items"),
        ([KAPHRAO], {"fulfillment": "delivery", "address": "คอนโดริมเมฆ"}, "fulfillment"),
    ],
)
def test_a_wrong_card_fails_and_names_the_field(
    tmp_path: Path, items: list[dict[str, Any]], fulfillment: dict[str, Any] | None, field: str
) -> None:
    case = _case(turns=[_order(items, fulfillment or {"fulfillment": "pickup"})])
    res = _run(tmp_path, case)
    assert not res["passed"]
    assert any(f.startswith(f"{field}:") for f in res["fails"]), res["fails"]


def test_no_card_fails_a_card_case_and_passes_a_no_card_case(tmp_path: Path) -> None:
    res = _run(tmp_path, _case(turns=["สวัสดีค่ะ"]))
    assert res["fails"] == ["card: no order card"]
    nothing = _case(id="t-002", category="not_order", turns=["สวัสดีค่ะ"])
    nothing["expect"] = {"card": None, "no_card": True, "handover": None, "reply_contains": []}
    assert _run(tmp_path, nothing)["passed"]


def test_delivery_address_is_checked(tmp_path: Path) -> None:
    case = _case(
        turns=[_order([KAPHRAO], {"fulfillment": "delivery", "address": "หอพักต้นฝน ห้อง 12"})]
    )
    case["expect"]["card"].update(fulfillment="delivery", address_contains="ต้นฝน")
    assert _run(tmp_path, case)["passed"]
    case["expect"]["card"]["address_contains"] = "ริมเมฆ"
    assert _run(tmp_path, case)["fails"] == ["address: missing the expected part"]


def test_a_closed_shop_case_runs_outside_menu_hours(tmp_path: Path) -> None:
    case = _case(id="t-003", category="not_order", shop_open=False)
    case["turns"] = [_order([KAPHRAO], {"fulfillment": "pickup"})]
    case["expect"] = {"card": None, "no_card": True, "handover": None, "reply_contains": []}
    app, db, _ = order_eval.build_eval_app(
        tmp_path / "clock", False, order_eval.demo.EchoLLM(), STUB_LLM
    )
    assert not MENU.is_scheduled_open(db.now_bkk())
    _, open_db, _ = order_eval.build_eval_app(
        tmp_path / "clock2", True, order_eval.demo.EchoLLM(), STUB_LLM
    )
    assert MENU.is_scheduled_open(open_db.now_bkk())
    assert _run(tmp_path, case)["passed"]  # the app refuses the card while closed


def test_handover_is_graded(tmp_path: Path) -> None:
    case = _case(
        id="t-004", category="not_order", turns=[_script(("hand_over", {"reason": "off_menu"}))]
    )
    case["expect"] = {"card": None, "no_card": True, "handover": "off_menu", "reply_contains": []}
    assert _run(tmp_path, case)["passed"]
    case["turns"] = ["สวัสดีค่ะ"]
    assert _run(tmp_path, case)["fails"][0].startswith("handover:")


def test_reply_contains_reads_text_messages_not_the_card(tmp_path: Path) -> None:
    case = _case(turns=[_order([KAPHRAO], {"fulfillment": "pickup"})])
    case["expect"]["reply_contains"] = ["140"]  # the card's total, absent from the text line
    res = _run(tmp_path, case)
    assert res["fails"] == ["reply_contains: '140' not in the bot's text replies"]
    chat = _case(id="t-005", category="not_order", turns=["สวัสดีค่ะ"])
    chat["expect"] = {"card": None, "no_card": True, "handover": None, "reply_contains": ["ได้รับ"]}
    assert _run(tmp_path, chat)["passed"]


def test_cost_is_priced_from_logged_tokens_and_the_cap_stops_the_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    case = _case(turns=["สวัสดีค่ะ"])
    res = _run(tmp_path, case, UsageLLM())
    assert res["llm_calls"] == 1 and res["prompt_tokens"] == 1000 and res["cached_tokens"] == 400
    # 600 uncached x 1000 + 400 cached x 100 + 100 output x 2000, per 1M tokens
    assert res["cost_usd"] == pytest.approx((600 * 1000 + 400 * 100 + 100 * 2000) / 1e6)
    cases = [("dev", _case(id=f"t-{i}", turns=["สวัสดีค่ะ"])) for i in range(3)]
    results, complete = order_eval.run_eval(
        cases,
        runs=1,
        max_cost_usd=1.0,
        llm_client=UsageLLM(),
        llm=STUB_LLM,
        prices=PRICES,
        work=tmp_path / "cap",
    )
    assert not complete and len(results) == 2  # 0.84 after one case, 1.68 after two
    assert "STOP: cost" in capsys.readouterr().out


def test_summary_reports_per_run_accuracy_flips_and_percentiles() -> None:
    rows = [
        {"id": "a", "category": "simple", "set": "dev", "run": 1, "passed": True},
        {"id": "a", "category": "simple", "set": "dev", "run": 2, "passed": False},
        {"id": "b", "category": "messy", "set": "dev", "run": 1, "passed": True},
        {"id": "b", "category": "messy", "set": "dev", "run": 2, "passed": True},
    ]
    for i, r in enumerate(rows):
        r.update(
            errors=[],
            reply_ms=[1000 * (i + 1)],
            llm_calls=1,
            cost_usd=0.01,
            prompt_tokens=10,
            cached_tokens=0,
            completion_tokens=1,
        )
    s = order_eval.summarize(rows, 2)
    assert s["sets"]["dev"]["accuracy_per_run"] == [1.0, 0.5]
    assert s["sets"]["dev"]["accuracy_mean"] == 0.75
    assert s["sets"]["dev"]["flipped_ids"] == ["a"]
    assert s["sets"]["dev"]["by_category"] == {"simple": "1/2", "messy": "2/2"}
    assert s["reply_ms"] == {"n": 4, "median": 2000, "p95": 4000}
    assert s["cost_usd_total"] == pytest.approx(0.04)


# --- Part B: the simulated customer, with a scripted simulator and the stub LLM -----------

GOAL: dict[str, Any] = {
    "id": "sim-t1",
    "persona": "นักศึกษา",
    "opening": "Says only the dish.",
    "goal": "กะเพราหมูกรอบ 2 กล่อง ไข่ดาวไม่สุก เผ็ดน้อย รับเอง",
    "expect": {"card": {"items": [KAPHRAO], "fulfillment": "pickup", "address_contains": None}},
}
SIM_PRICES = {**PRICES, "stub/sim": {"input": 0.0, "output": 0.0}}


class ScriptedSim:
    """An OpenAI-shaped client that answers with a fixed list of customer messages."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.seen: list[list[dict[str, str]]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> Any:
        self.seen.append(kwargs["messages"])
        text = self.replies.pop(0) if self.replies else "ขอกะเพราค่ะ"
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=text))], usage=None
        )


def _simulate(tmp: Path, replies: list[str]) -> tuple[dict[str, Any], ScriptedSim]:
    client = ScriptedSim(replies)
    sim = order_eval.Simulator(client, "stub/sim")
    res = order_eval.simulate_goal(
        GOAL, tmp / "sim", sim, order_eval.demo.EchoLLM(), STUB_LLM, SIM_PRICES
    )
    return res, client


def test_the_simulator_ends_the_chat_and_the_card_is_graded(tmp_path: Path) -> None:
    res, client = _simulate(tmp_path, [_order([KAPHRAO], {"fulfillment": "pickup"}), "<END>"])
    assert res["passed"], res
    assert res["ended_by"] == "simulator" and res["customer_turns"] == 1
    saved = json.loads((tmp_path / "sim" / "transcript.json").read_text(encoding="utf-8"))
    assert [who for who, _ in saved["history"]] == ["customer", "shop"]
    assert "[order summary card]" in saved["history"][1][1]  # the card as the customer sees it
    assert "ข้าวกะเพรา" in saved["history"][1][1]


def test_the_chat_stops_at_the_turn_limit_and_fails_without_a_card(tmp_path: Path) -> None:
    res, client = _simulate(tmp_path, [])
    assert res["ended_by"] == "turn_limit"
    assert res["customer_turns"] == order_eval.MAX_SIM_TURNS
    assert res["fails"] == ["card: no order card"]


def test_the_simulator_sees_only_its_brief_and_the_chat(tmp_path: Path) -> None:
    _, client = _simulate(tmp_path, ["ขอกะเพราค่ะ", "<END>"])
    system = client.seen[0][0]
    assert system["role"] == "system"
    assert system["content"] == order_eval.SIMULATOR_RULES.format(
        persona=GOAL["persona"], goal=GOAL["goal"], opening=GOAL["opening"], end="<END>"
    )
    second = client.seen[1]
    assert [m["role"] for m in second] == ["system", "assistant", "user"]
    assert second[1]["content"] == "ขอกะเพราค่ะ"
    assert second[2]["content"] == "[demo LLM] ได้รับ: ขอกะเพราค่ะ"  # only the shop's reply
    assert "dish_id" not in json.dumps(second, ensure_ascii=False)  # no expected answer leaks


def test_pass_hat_k_needs_every_run_to_pass() -> None:
    rows = [
        {"id": "a", "passed": True, "ended_by": "simulator"},
        {"id": "a", "passed": True, "ended_by": "simulator"},
        {"id": "a", "passed": True, "ended_by": "simulator"},
        {"id": "b", "passed": True, "ended_by": "simulator"},
        {"id": "b", "passed": False, "ended_by": "turn_limit"},
        {"id": "b", "passed": True, "ended_by": "simulator"},
    ]
    s = order_eval.pass_hat_k(rows, 3)
    assert s["pass^1"] == round(5 / 6, 4) and s["pass^3"] == 0.5
    assert s["goals"] == 2 and s["ended_by_turn_limit"] == 1


def test_goals_are_validated_against_the_menu(tmp_path: Path) -> None:
    assert order_eval.check_goal(GOAL, MENU) == []
    bad = copy.deepcopy(GOAL)
    bad["expect"]["card"]["items"][0]["dish_id"] = "pizza"
    del bad["persona"]
    errors = order_eval.check_goal(bad, MENU)
    assert any("persona" in e for e in errors) and any("pizza" in e for e in errors)


def test_the_fast_path_check_catches_every_side_question_in_the_eval_set() -> None:
    """Over the frozen eval cases' first messages, every side question keeps the LLM reply and
    order messages gain no new false alarms (docs/specs/2026-11-27-fast-path-request-check.md)."""
    from app.agent import _QUESTION_RE, has_side_question

    cases = [
        c
        for f in sorted((ROOT / "evals" / "cases").glob("*.yaml"))
        for c in yaml.safe_load(f.read_text(encoding="utf-8"))
    ]
    side = [c["turns"][0] for c in cases if c["category"] == "side_question"]
    orders = [c["turns"][0] for c in cases if c["category"] not in ("side_question", "not_order")]
    assert len(side) == 10 and len(orders) == 58
    assert [t for t in side if not has_side_question(t)] == []
    question_word_alarms = sum(bool(_QUESTION_RE.search(t)) for t in orders)
    assert question_word_alarms == 1  # the baseline this spec compares against
    assert sum(has_side_question(t) for t in orders) == question_word_alarms
