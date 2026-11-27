"""Offline order evaluation (spec: docs/specs/2026-11-27-order-eval.md).

    uv run python scripts/eval.py validate evals/cases/ local/evals/hidden/
    uv run python scripts/eval.py run --set dev --ids gpt-001,gpt-008 --runs 1 --max-cost-usd 0.5

`validate` checks every case file against menu.yaml and prints only counts and errors,
never a case's text, so the hidden set stays unread.
`run` sends each case to a fresh copy of the real app (stub LINE and SlipOK, the real LLM
from the LLM_* settings in the env or .env: paid calls), grades the order card, hand-overs
and text replies, and writes a JSON report with accuracy, reply times and cost.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import logging
import math
import re
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import openai
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.db import ShopDB  # noqa: E402
from app.main import create_app  # noqa: E402
from app.menu import Menu, MenuError, load_menu  # noqa: E402

CATEGORIES = (
    "simple",
    "options",
    "multi_item",
    "multi_turn",
    "side_question",
    "not_order",
    "messy",
)
HANDOVERS = (None, "off_menu", "any")
CASE_KEYS = {"id", "category", "turns", "shop_open", "expect"}
EXPECT_KEYS = {"card", "no_card", "handover", "reply_contains"}
CARD_KEYS = {"items", "fulfillment", "address_contains"}
ITEM_KEYS = {"dish_id", "qty", "option_ids"}


def _unknown(where: str, got: dict[str, Any], allowed: set[str]) -> list[str]:
    extra = sorted(set(got) - allowed)
    return [f"{where}: unknown key(s) {extra}"] if extra else []


def _check_card(cid: str, card: Any, menu: Menu) -> list[str]:
    if not isinstance(card, dict):
        return [f"{cid}: expect.card must be a mapping"]
    errors = _unknown(f"{cid}: expect.card", card, CARD_KEYS)
    items = card.get("items")
    if not isinstance(items, list) or not items:
        return [*errors, f"{cid}: expect.card.items must be a non-empty list"]
    for i, item in enumerate(items):
        where = f"{cid}: item {i + 1}"
        if not isinstance(item, dict):
            errors.append(f"{where} must be a mapping")
            continue
        errors += _unknown(where, item, ITEM_KEYS)
        qty, opts = item.get("qty"), item.get("option_ids", [])
        if type(qty) is not int:
            errors.append(f"{where}: qty must be an integer")
            continue
        if not isinstance(opts, list) or not all(isinstance(o, str) for o in opts):
            errors.append(f"{where}: option_ids must be a list of IDs")
            continue
        try:
            menu.price_line(str(item.get("dish_id")), qty, opts, "", {})
        except MenuError as exc:
            errors.append(f"{where} ({item.get('dish_id')}): {exc}")
    fulfillment = card.get("fulfillment")
    address = card.get("address_contains")
    if fulfillment == "delivery":
        if not isinstance(address, str) or not address.strip():
            errors.append(f"{cid}: delivery needs a non-empty address_contains")
    elif fulfillment == "pickup":
        if address is not None:
            errors.append(f"{cid}: pickup must have address_contains: null")
    else:
        errors.append(f"{cid}: fulfillment must be pickup or delivery")
    return errors


def check_case(case: Any, menu: Menu) -> list[str]:
    """Every problem with one case, each prefixed with its id. Never quotes the case's text."""
    if not isinstance(case, dict):
        return ["a case is not a mapping"]
    cid = str(case.get("id") or "<no id>")
    errors = _unknown(cid, case, CASE_KEYS)
    if not isinstance(case.get("id"), str) or not case["id"]:
        errors.append(f"{cid}: id must be a non-empty string")
    category = case.get("category")
    if category not in CATEGORIES:
        errors.append(f"{cid}: category must be one of {list(CATEGORIES)}")
    turns = case.get("turns")
    if (
        not isinstance(turns, list)
        or not 1 <= len(turns) <= 3
        or not all(isinstance(t, str) and t.strip() for t in turns)
    ):
        errors.append(f"{cid}: turns must be 1-3 non-empty strings")
    elif category == "multi_turn" and len(turns) < 2:
        errors.append(f"{cid}: multi_turn needs at least 2 turns")
    shop_open = case.get("shop_open", True)
    if not isinstance(shop_open, bool):
        errors.append(f"{cid}: shop_open must be true or false")

    expect = case.get("expect")
    if not isinstance(expect, dict):
        return [*errors, f"{cid}: expect must be a mapping"]
    errors += _unknown(f"{cid}: expect", expect, EXPECT_KEYS)
    card = expect.get("card")
    no_card = expect.get("no_card", False)
    handover = expect.get("handover")
    replies = expect.get("reply_contains", [])
    if not isinstance(no_card, bool):
        errors.append(f"{cid}: expect.no_card must be true or false")
    if handover not in HANDOVERS:
        errors.append(f"{cid}: expect.handover must be null, off_menu or any")
    if not isinstance(replies, list) or not all(isinstance(r, str) and r.strip() for r in replies):
        errors.append(f"{cid}: expect.reply_contains must be a list of non-empty strings")
    if card is not None:
        errors += _check_card(cid, card, menu)
        if no_card is True:
            errors.append(f"{cid}: expect.card and expect.no_card can't both be set")
        if shop_open is False:
            errors.append(f"{cid}: a closed shop can't expect a card")
        if category == "not_order":
            errors.append(f"{cid}: not_order can't expect a card")
    elif no_card is not True and handover is None:
        errors.append(f"{cid}: expect needs a card, no_card: true or a handover")
    if category == "side_question" and not replies:
        errors.append(f"{cid}: side_question needs reply_contains")
    return errors


def load_cases(path: Path) -> list[Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path}: the file must be a YAML list of cases")
    return data


def case_files(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for p in paths:
        files += sorted(p.glob("*.yaml")) if p.is_dir() else [p]
    return files


def validate(paths: list[Path], menu: Menu, require_all_categories: bool = True) -> int:
    """Print per-file counts and every error; return the error count."""
    seen: Counter[str] = Counter()
    errors: list[str] = []
    files = case_files(paths)
    if not files:
        print("no case files found")
        return 1
    for path in files:
        try:
            cases = load_cases(path)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            errors.append(f"{path}: {type(exc).__name__}: {exc}")
            continue
        counts = Counter(str(c.get("category")) for c in cases if isinstance(c, dict))
        file_errors = [e for c in cases for e in check_case(c, menu)]
        for c in cases:
            if isinstance(c, dict) and isinstance(c.get("id"), str):
                seen[c["id"]] += 1
        missing = [cat for cat in CATEGORIES if not counts[cat]]
        if require_all_categories and missing:
            file_errors.append(f"{path.name}: missing categories {missing}")
        summary = ", ".join(f"{cat} {counts[cat]}" for cat in CATEGORIES)
        print(f"{path}: {len(cases)} cases ({summary}); {len(file_errors)} error(s)")
        errors += file_errors
    errors += [f"duplicate id {cid} ({n} times)" for cid, n in sorted(seen.items()) if n > 1]
    for e in errors:
        print(f"  ERROR {e}")
    print(f"validate: {'PASS' if not errors else 'FAIL'} ({len(errors)} error(s))")
    return len(errors)


# --- Running cases against the real app ---------------------------------------------------

EVAL_USER = "U_EVAL"
OPEN_AT = (12, 0)  # Bangkok time inside menu.yaml hours
CLOSED_AT = (21, 30)  # Bangkok time outside them
ERROR_REASONS = {"error", "exception", "deadline", "max_tool_calls", "turn_limit"}
PRICES_PATH = ROOT / "evals" / "prices.yaml"
SETS = {
    "dev": [ROOT / "evals" / "cases" / "gpt.yaml"],
    "hidden": [ROOT / "local" / "evals" / "hidden" / "gemini.yaml"],
}
SETS["all"] = SETS["dev"] + SETS["hidden"]
_FIELD = re.compile(r"(\w+)=(\S+)")

_demo_spec = importlib.util.spec_from_file_location("demo", ROOT / "scripts" / "demo.py")
assert _demo_spec is not None and _demo_spec.loader is not None
demo: Any = importlib.util.module_from_spec(_demo_spec)
sys.modules.setdefault("demo", demo)
_demo_spec.loader.exec_module(demo)


class EvalClock:
    """A fixed Bangkok time of day today that still moves forward in real time."""

    def __init__(self, hour: int, minute: int) -> None:
        today = datetime.now(demo.BKK).date()
        self.base = datetime(today.year, today.month, today.day, hour, minute, tzinfo=demo.BKK)
        self.start = time.monotonic()

    def __call__(self) -> datetime:
        return self.base + timedelta(seconds=time.monotonic() - self.start)


class Telemetry(logging.Handler):
    """Collects the app's `llm_call` and `webhook_event` lines as key=value dicts."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.llm_calls: list[dict[str, str]] = []
        self.events: list[dict[str, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        kind = msg.split(" ", 1)[0]
        if kind == "llm_call":
            self.llm_calls.append(dict(_FIELD.findall(msg)))
        elif kind == "webhook_event":
            self.events.append(dict(_FIELD.findall(msg)))


def _int(value: str | None) -> int:
    return int(value) if value and value.isdigit() else 0


def call_cost(call: dict[str, str], prices: dict[str, Any]) -> float | None:
    """USD for one LLM call, an upper bound: uncached input is priced as a cache write when
    that costs more than plain input. None when the model has no price."""
    price = prices.get(call.get("model", ""))
    if price is None:
        return None
    prompt, cached = _int(call.get("prompt_tokens")), _int(call.get("cached_tokens"))
    uncached_rate = max(float(price["input"]), float(price.get("cache_write", 0)))
    return (
        (prompt - cached) * uncached_rate
        + cached * float(price.get("cache_read", price["input"]))
        + _int(call.get("completion_tokens")) * float(price["output"])
    ) / 1_000_000


def build_eval_app(
    work: Path, shop_open: bool, llm_client: Any, llm: dict[str, str]
) -> tuple[FastAPI, ShopDB, Path]:
    """A fresh, unseeded app on the real menu and hours, with stub LINE and SlipOK."""
    work.mkdir(parents=True, exist_ok=True)
    db_path, log_path = work / "eval.db", work / "line.log"
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(f"{db_path}{suffix}").unlink(missing_ok=True)
    log_path.write_text("", encoding="utf-8")
    clock = EvalClock(*(OPEN_AT if shop_open else CLOSED_AT))
    menu = load_menu(ROOT / "menu.yaml")
    db = ShopDB(db_path, clock=clock)
    settings = Settings(
        db_path=db_path,
        menu_path=ROOT / "menu.yaml",
        kitchen_secret=demo.KITCHEN_KEY,
        public_base_url="https://demo.invalid",
        payment_info_text="พร้อมเพย์ 081-234-5678",
        line_channel_secret=demo.CHANNEL_SECRET,
        line_channel_access_token="",
        owner_line_user_id="U_OWNER",
        llm_base_url=llm["LLM_BASE_URL"],
        llm_api_key=llm["LLM_API_KEY"],
        llm_model=llm["LLM_MODEL"],
        llm_reasoning_effort=llm["LLM_REASONING_EFFORT"],
        promptpay_id="0812345678",
    )
    app = create_app(
        settings=settings,
        db=db,
        menu=menu,
        line_client=demo.LogLine(log_path, {}),
        llm_client=llm_client,
        slip_client=demo.StubSlipOk(clock, {}),
    )
    return app, db, log_path


def _canonical(menu: Menu, items: list[dict[str, Any]]) -> Counter[tuple[str, tuple[str, ...]]]:
    """Dish quantities keyed by (dish, sorted options with defaults filled in); identical
    lines are merged, so "2 boxes" and "1 box + 1 box" of the same thing are equal."""
    out: Counter[tuple[str, tuple[str, ...]]] = Counter()
    for item in items:
        dish, qty = str(item.get("dish_id")), int(item.get("qty", 1))
        opts = [str(o) for o in item.get("option_ids", [])]
        try:
            opts = list(menu.price_line(dish, qty, opts, "", {}).option_ids)
        except MenuError:
            pass  # graded as given; it can't match a valid expected item anyway
        out[(dish, tuple(sorted(opts)))] += qty
    return out


def _fmt(items: Counter[tuple[str, tuple[str, ...]]]) -> list[str]:
    return [f"{dish} x{qty} {list(opts)}" for (dish, opts), qty in sorted(items.items())]


def grade(case: dict[str, Any], db: ShopDB, log_path: Path, menu: Menu) -> list[str]:
    """Every failed expectation, by field name; empty when the case passes."""
    expect = case["expect"]
    with sqlite3.connect(db.db_path) as conn:
        row = conn.execute(
            "SELECT active_card_json FROM conversations WHERE line_user_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (EVAL_USER,),
        ).fetchone()
        handovers = [
            json.loads(r[0])
            for r in conn.execute(
                "SELECT reasons_json FROM handovers WHERE line_user_id = ? AND resolved_at IS NULL",
                (EVAL_USER,),
            )
        ]
    card = json.loads(row[0]) if row and row[0] else None
    texts = [
        m["text"]
        for line in log_path.read_text(encoding="utf-8").splitlines()
        for m in json.loads(line)["messages"]
        if m.get("type") == "text"
    ]
    fails: list[str] = []
    want = expect.get("card")
    if want is not None:
        if card is None:
            fails.append("card: no order card")
        else:
            got_items = _canonical(menu, card.get("raw_items", []))
            want_items = _canonical(menu, want["items"])
            if got_items != want_items:
                fails.append(
                    f"items: missing {_fmt(want_items - got_items)}, "
                    f"extra {_fmt(got_items - want_items)}"
                )
            if card.get("fulfillment") != want["fulfillment"]:
                fails.append(f"fulfillment: got {card.get('fulfillment')}")
            needle = want.get("address_contains")
            if needle and needle not in str(card.get("address") or ""):
                fails.append("address: missing the expected part")
    if expect.get("no_card") and card is not None:
        fails.append("no_card: an order card was built")
    reasons = {r for rs in handovers for r in rs}
    if expect.get("handover") == "any" and not reasons:
        fails.append("handover: none opened")
    elif expect.get("handover") == "off_menu" and "off_menu" not in reasons:
        fails.append(f"handover: off_menu not opened (got {sorted(reasons)})")
    joined = "\n".join(texts)
    for needle in expect.get("reply_contains") or []:
        if needle not in joined:
            fails.append(f"reply_contains: {needle!r} not in the bot's text replies")
    return fails


def run_case(
    case: dict[str, Any], work: Path, llm_client: Any, llm: dict[str, str], prices: dict[str, Any]
) -> dict[str, Any]:
    """Send a case's turns to a fresh app, then grade the end state."""
    telemetry = Telemetry()
    app_log = logging.getLogger("app")
    app_log.addHandler(telemetry)
    errors: list[str] = []
    try:
        app, db, log_path = build_eval_app(work, case.get("shop_open", True), llm_client, llm)
        with TestClient(app) as client:
            for turn in case["turns"]:
                raw = json.dumps(
                    {"events": [demo.make_event(EVAL_USER, "text", turn, db.db_path)]},
                    ensure_ascii=False,
                ).encode()
                resp = client.post(
                    "/line/webhook",
                    content=raw,
                    headers={
                        "x-line-signature": demo.sign(raw),
                        "content-type": "application/json",
                    },
                )
                if resp.status_code != 200:
                    errors.append(f"webhook HTTP {resp.status_code}")
        menu = load_menu(ROOT / "menu.yaml")
        fails = grade(case, db, log_path, menu)
    finally:
        app_log.removeHandler(telemetry)
    errors += [
        f"turn {e.get('reason')}" for e in telemetry.events if e.get("reason") in ERROR_REASONS
    ]
    errors += [f"llm {c['error']}" for c in telemetry.llm_calls if c.get("error", "-") != "-"]
    costs = [call_cost(c, prices) for c in telemetry.llm_calls]
    return {
        "id": case["id"],
        "category": case["category"],
        "passed": not fails and not errors,
        "fails": fails,
        "errors": errors,
        "turns": len(case["turns"]),
        "reply_ms": [_int(e.get("total_ms")) for e in telemetry.events],
        "llm_calls": len(telemetry.llm_calls),
        "prompt_tokens": sum(_int(c.get("prompt_tokens")) for c in telemetry.llm_calls),
        "cached_tokens": sum(_int(c.get("cached_tokens")) for c in telemetry.llm_calls),
        "completion_tokens": sum(_int(c.get("completion_tokens")) for c in telemetry.llm_calls),
        "cost_usd": None if any(c is None for c in costs) else sum(c for c in costs if c),
    }


def _pct(values: list[int], q: float) -> int:
    """Nearest-rank percentile."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)] if ordered else 0


def summarize(results: list[dict[str, Any]], runs: int) -> dict[str, Any]:
    by_set: dict[str, dict[str, Any]] = {}
    for name in sorted({r["set"] for r in results}):
        rows = [r for r in results if r["set"] == name]
        per_run = []
        for run in range(1, runs + 1):
            done = [r for r in rows if r["run"] == run]
            if done:
                per_run.append(sum(r["passed"] for r in done) / len(done))
        cats = {}
        for cat in CATEGORIES:
            cr = [r for r in rows if r["category"] == cat]
            if cr:
                cats[cat] = f"{sum(r['passed'] for r in cr)}/{len(cr)}"
        outcomes: dict[str, set[bool]] = {}
        for r in rows:
            outcomes.setdefault(r["id"], set()).add(r["passed"])
        by_set[name] = {
            "cases": len({r["id"] for r in rows}),
            "accuracy_per_run": [round(a, 4) for a in per_run],
            "accuracy_mean": round(sum(per_run) / len(per_run), 4) if per_run else None,
            "by_category": cats,
            "flipped_ids": sorted(cid for cid, seen in outcomes.items() if len(seen) > 1),
        }
    reply_ms = [ms for r in results for ms in r["reply_ms"]]
    costs = [r["cost_usd"] for r in results]
    known = [c for c in costs if c is not None]
    n = len(results) or 1
    return {
        "sets": by_set,
        "errors": sum(bool(r["errors"]) for r in results),
        "reply_ms": {
            "n": len(reply_ms),
            "median": _pct(reply_ms, 0.5),
            "p95": _pct(reply_ms, 0.95),
        },
        "tokens_per_case": {
            k: round(sum(r[k] for r in results) / n)
            for k in ("prompt_tokens", "cached_tokens", "completion_tokens")
        },
        "llm_calls_per_case": round(sum(r["llm_calls"] for r in results) / n, 2),
        "cost_usd_total": round(sum(known), 6) if len(known) == len(costs) else None,
        "cost_usd_per_case": round(sum(known) / n, 6) if len(known) == len(costs) else None,
    }


def project(
    results: list[dict[str, Any]], prices: dict[str, Any], cases: int, runs: int
) -> dict[str, Any]:
    """Full-run cost estimates: the measured mean cost per case, and the same tokens repriced
    for every other model in prices.yaml (an estimate: other models use different tokens)."""
    n = len(results) or 1
    out: dict[str, Any] = {}
    for model in prices:
        total = 0.0
        for r in results:
            total += (
                call_cost(
                    {
                        "model": model,
                        "prompt_tokens": str(r["prompt_tokens"]),
                        "cached_tokens": str(r["cached_tokens"]),
                        "completion_tokens": str(r["completion_tokens"]),
                    },
                    prices,
                )
                or 0.0
            )
        out[model] = round(total / n * cases * runs, 4)
    return out


def _git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def run_eval(
    cases: list[tuple[str, dict[str, Any]]],
    *,
    runs: int,
    max_cost_usd: float,
    llm_client: Any,
    llm: dict[str, str],
    prices: dict[str, Any],
    work: Path,
    show_details: bool = True,
) -> tuple[list[dict[str, Any]], bool]:
    """Run every case `runs` times. Stops early (complete=False) once the cost passes the cap
    or can't be computed for a paid model."""
    results: list[dict[str, Any]] = []
    spent = 0.0
    for run in range(1, runs + 1):
        for set_name, case in cases:
            res = run_case(case, work / f"{set_name}-{case['id']}-r{run}", llm_client, llm, prices)
            res.update(set=set_name, run=run)
            results.append(res)
            status = "PASS" if res["passed"] else "FAIL"
            detail = "; ".join(res["fails"] + res["errors"]) if show_details else ""
            print(f"run {run} {res['id']}: {status} {detail}".rstrip(), flush=True)
            if res["cost_usd"] is None and llm_client is None:
                print(f"STOP: no price for {llm['LLM_MODEL']} in {PRICES_PATH.name}")
                return results, False
            spent += res["cost_usd"] or 0.0
            if spent > max_cost_usd:
                print(f"STOP: cost ${spent:.4f} passed --max-cost-usd {max_cost_usd}")
                return results, False
    return results, True


def _load_set(name: str, final: bool) -> tuple[list[Path], list[tuple[str, dict[str, Any]]]]:
    paths: list[Path] = []
    for path in SETS[name]:
        if not path.exists() and path.parent.name == "hidden":
            path = ROOT / "evals" / "cases" / path.name  # moved there after the final run
        if path.stem == "gemini" and not final:
            raise SystemExit("the hidden set runs only with --final (spec §3)")
        paths.append(path)
    cases = [("hidden" if p.stem == "gemini" else "dev", c) for p in paths for c in load_cases(p)]
    return paths, cases


def cmd_run(args: argparse.Namespace) -> int:
    paths, cases = _load_set(args.set, args.final)
    if validate(paths, load_menu(ROOT / "menu.yaml")):
        return 1
    if args.ids:
        wanted = args.ids.split(",")
        cases = [sc for sc in cases if sc[1]["id"] in wanted]
    if args.limit:
        cases = cases[: args.limit]
    messages = sum(len(c["turns"]) for _, c in cases) * args.runs
    print(f"{len(cases)} case(s) x {args.runs} run(s) = {messages} customer message(s)")
    if args.max_messages is not None and messages > args.max_messages:
        print(f"STOP: {messages} messages is over --max-messages {args.max_messages}")
        return 1
    llm = demo._llm_settings()
    if args.model:
        llm["LLM_MODEL"] = args.model
    if not llm["LLM_API_KEY"]:
        print("STOP: no LLM_API_KEY in the env or .env")
        return 1
    prices = yaml.safe_load(PRICES_PATH.read_text(encoding="utf-8"))["models"]
    partial = bool(args.ids or args.limit)
    out_dir = ROOT / ("local/evals/results" if partial else "evals/results")
    work = ROOT / "local" / "evals" / "work" / re.sub(r"[^a-z0-9.]+", "-", llm["LLM_MODEL"].lower())
    results, complete = run_eval(
        cases,
        runs=args.runs,
        max_cost_usd=args.max_cost_usd,
        llm_client=None,
        llm=llm,
        prices=prices,
        work=work,
        show_details=not any(s == "hidden" for s, _ in cases) or args.final,
    )
    summary = summarize(results, args.runs)
    report = {
        "status": "COMPLETE" if complete else "INCOMPLETE",
        "date": datetime.now(demo.BKK).isoformat(timespec="seconds"),
        "model": llm["LLM_MODEL"],
        "reasoning_effort": llm["LLM_REASONING_EFFORT"],
        "commit": _git_commit(),
        "case_files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()[:16] for p in paths},
        "runs": args.runs,
        "summary": summary,
        "projected_cost_usd_80x3": project(results, prices, 80, 3),
        "results": results,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9.]+", "-", llm["LLM_MODEL"].lower())
    out = out_dir / f"{datetime.now(demo.BKK):%Y-%m-%d-%H%M}-{slug}-{args.set}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {k: report[k] for k in ("status", "model", "summary", "projected_cost_usd_80x3")},
            indent=2,
        )
    )
    print(f"report: {out.relative_to(ROOT)}")
    return 0 if complete else 1


# --- Part B: a simulated customer -------------------------------------------------------

GOALS_PATH = ROOT / "evals" / "goals.yaml"
GOAL_KEYS = {"id", "persona", "opening", "goal", "expect"}
MAX_SIM_TURNS = 8
END_TOKEN = "<END>"
SIMULATOR_RULES = """You are role-playing a customer chatting with a Thai food shop's LINE account.
Write only the customer's next message, in casual Thai as people type on LINE: short, informal,
sometimes with typos or emoji. Never write the shop's side and never explain yourself.

Your persona: {persona}
What you want (keep it to yourself; reveal it the way a real customer would): {goal}
How you start, and what you leave out or change along the way: {opening}

Rules:
- Answer the shop's questions using only what you want above. Don't invent other wishes.
- Follow that plan. Otherwise don't change your mind, and don't order anything that isn't in
  what you want.
- When the shop shows an order summary that has everything you want, reply with exactly {end}.
- If the summary is wrong, say what to fix, like a real customer would.
- If the shop can't help or keeps asking the same thing, reply with exactly {end}."""


def check_goal(goal: Any, menu: Menu) -> list[str]:
    if not isinstance(goal, dict):
        return ["a goal is not a mapping"]
    gid = str(goal.get("id") or "<no id>")
    errors = _unknown(gid, goal, GOAL_KEYS)
    for key in ("id", "persona", "opening", "goal"):
        if not isinstance(goal.get(key), str) or not goal[key].strip():
            errors.append(f"{gid}: {key} must be a non-empty string")
    expect = goal.get("expect")
    if not isinstance(expect, dict) or set(expect) != {"card"}:
        return [*errors, f"{gid}: expect must hold exactly one key, card"]
    return errors + _check_card(gid, expect["card"], menu)


def validate_goals(path: Path, menu: Menu) -> int:
    goals = load_cases(path)
    errors = [e for g in goals for e in check_goal(g, menu)]
    ids = Counter(str(g.get("id")) for g in goals if isinstance(g, dict))
    errors += [f"duplicate id {gid}" for gid, n in sorted(ids.items()) if n > 1]
    print(f"{path}: {len(goals)} goals; {len(errors)} error(s)")
    for e in errors:
        print(f"  ERROR {e}")
    print(f"validate: {'PASS' if not errors else 'FAIL'} ({len(errors)} error(s))")
    return len(errors)


def _flex_text(node: Any) -> list[str]:
    """Every visible `text` in a LINE Flex message, in order."""
    if isinstance(node, dict):
        own = (
            [node["text"]]
            if node.get("type") == "text" and isinstance(node.get("text"), str)
            else []
        )
        return own + [t for k, v in node.items() if k != "text" for t in _flex_text(v)]
    if isinstance(node, list):
        return [t for v in node for t in _flex_text(v)]
    return []


def _bot_view(lines: list[str]) -> str:
    """What the customer sees: text messages, and an order card as its visible text."""
    parts: list[str] = []
    for line in lines:
        for m in json.loads(line)["messages"]:
            if m.get("type") == "text":
                parts.append(str(m["text"]))
            elif m.get("type") == "flex":
                parts.append("[order summary card]\n" + "\n".join(_flex_text(m.get("contents"))))
            elif m.get("type") == "image":
                parts.append("[image]")
    return "\n\n".join(parts) or "(no reply)"


class Simulator:
    """The customer side: one OpenAI-compatible chat call per customer message."""

    def __init__(self, client: Any, model: str, effort: str = "low") -> None:
        self.client, self.model, self.effort = client, model, effort
        self.calls: list[dict[str, str]] = []

    def next_message(self, goal: dict[str, Any], history: list[tuple[str, str]]) -> str:
        system = SIMULATOR_RULES.format(
            persona=goal["persona"], goal=goal["goal"], opening=goal["opening"], end=END_TOKEN
        )
        messages: list[dict[str, str]] = [{"role": "system", "content": system}]
        for who, text in history:  # the simulator is the "assistant"; the shop is the "user"
            messages.append({"role": "assistant" if who == "customer" else "user", "content": text})
        if not history:
            messages.append({"role": "user", "content": "(you open the chat)"})
        start = time.monotonic()
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            extra_body={"reasoning": {"effort": self.effort}},
        )
        usage = getattr(resp, "usage", None)
        details = getattr(usage, "prompt_tokens_details", None)
        self.calls.append(
            {
                "model": self.model,
                "latency_ms": str(round((time.monotonic() - start) * 1000)),
                "prompt_tokens": str(getattr(usage, "prompt_tokens", 0) or 0),
                "cached_tokens": str(getattr(details, "cached_tokens", 0) or 0),
                "completion_tokens": str(getattr(usage, "completion_tokens", 0) or 0),
            }
        )
        return str(resp.choices[0].message.content or "").strip()


def simulate_goal(
    goal: dict[str, Any],
    work: Path,
    simulator: Simulator,
    llm_client: Any,
    llm: dict[str, str],
    prices: dict[str, Any],
    max_turns: int = MAX_SIM_TURNS,
) -> dict[str, Any]:
    """Let the simulator chat with a fresh app until it ends or hits the turn limit; grade
    the order card it ended with."""
    telemetry = Telemetry()
    app_log = logging.getLogger("app")
    app_log.addHandler(telemetry)
    history: list[tuple[str, str]] = []
    ended_by = "turn_limit"
    sim_calls_before = len(simulator.calls)
    try:
        app, db, log_path = build_eval_app(work, True, llm_client, llm)
        with TestClient(app) as client:
            for _ in range(max_turns):
                text = simulator.next_message(goal, history)
                if not text or END_TOKEN in text:
                    ended_by = "simulator"
                    break
                history.append(("customer", text))
                before = len(log_path.read_text(encoding="utf-8").splitlines())
                raw = json.dumps(
                    {"events": [demo.make_event(EVAL_USER, "text", text, db.db_path)]},
                    ensure_ascii=False,
                ).encode()
                client.post(
                    "/line/webhook",
                    content=raw,
                    headers={
                        "x-line-signature": demo.sign(raw),
                        "content-type": "application/json",
                    },
                )
                new = log_path.read_text(encoding="utf-8").splitlines()[before:]
                history.append(("shop", _bot_view(new)))
        case = {"expect": {**goal["expect"], "no_card": False, "handover": None}}
        fails = grade(case, db, log_path, load_menu(ROOT / "menu.yaml"))
    finally:
        app_log.removeHandler(telemetry)
    errors = [
        f"turn {e.get('reason')}" for e in telemetry.events if e.get("reason") in ERROR_REASONS
    ]
    sim_calls = simulator.calls[sim_calls_before:]
    bot_costs = [call_cost(c, prices) for c in telemetry.llm_calls]
    sim_costs = [call_cost(c, prices) for c in sim_calls]
    transcript = work / "transcript.json"
    transcript.write_text(
        json.dumps(
            {"goal": goal, "history": history, "ended_by": ended_by}, ensure_ascii=False, indent=2
        ),
        encoding="utf-8",
    )
    return {
        "id": goal["id"],
        "passed": not fails and not errors,
        "fails": fails,
        "errors": errors,
        "ended_by": ended_by,
        "customer_turns": sum(who == "customer" for who, _ in history),
        "reply_ms": [_int(e.get("total_ms")) for e in telemetry.events],
        "bot_cost_usd": None if None in bot_costs else sum(c for c in bot_costs if c),
        "simulator_cost_usd": None if None in sim_costs else sum(c for c in sim_costs if c),
        "transcript": str(transcript.relative_to(ROOT))
        if transcript.is_relative_to(ROOT)
        else str(transcript),
    }


def pass_hat_k(results: list[dict[str, Any]], runs: int) -> dict[str, Any]:
    """pass^1: the mean pass rate; pass^k: the share of goals that passed in every run."""
    by_goal: dict[str, list[bool]] = {}
    for r in results:
        by_goal.setdefault(r["id"], []).append(r["passed"])
    full = {g: p for g, p in by_goal.items() if len(p) == runs}
    return {
        "goals": len(by_goal),
        "pass^1": round(sum(r["passed"] for r in results) / len(results), 4) if results else None,
        f"pass^{runs}": round(sum(all(p) for p in full.values()) / len(full), 4) if full else None,
        "ended_by_turn_limit": sum(r["ended_by"] == "turn_limit" for r in results),
    }


def cmd_simulate(args: argparse.Namespace) -> int:
    menu = load_menu(ROOT / "menu.yaml")
    if validate_goals(GOALS_PATH, menu):
        return 1
    goals = load_cases(GOALS_PATH)
    if args.ids:
        goals = [g for g in goals if g["id"] in args.ids.split(",")]
    if args.limit:
        goals = goals[: args.limit]
    llm = demo._llm_settings()
    if not llm["LLM_API_KEY"]:
        print("STOP: no LLM_API_KEY in the env or .env")
        return 1
    prices = yaml.safe_load(PRICES_PATH.read_text(encoding="utf-8"))["models"]
    for model in (llm["LLM_MODEL"], args.simulator):
        if model not in prices:
            print(f"STOP: no price for {model} in {PRICES_PATH.name}")
            return 1
    print(f"{len(goals)} goal(s) x {args.runs} run(s), up to {args.max_turns} customer turns each")
    sim = Simulator(
        openai.OpenAI(base_url=llm["LLM_BASE_URL"], api_key=llm["LLM_API_KEY"], max_retries=0),
        args.simulator,
    )
    slug = re.sub(r"[^a-z0-9.]+", "-", args.simulator.lower())
    work = ROOT / "local" / "evals" / "transcripts" / slug
    results: list[dict[str, Any]] = []
    spent, complete = 0.0, True
    for run in range(1, args.runs + 1):
        for goal in goals:
            res = simulate_goal(
                goal, work / f"{goal['id']}-r{run}", sim, None, llm, prices, args.max_turns
            )
            res["run"] = run
            results.append(res)
            detail = "; ".join(res["fails"] + res["errors"])
            print(
                f"run {run} {res['id']}: {'PASS' if res['passed'] else 'FAIL'} "
                f"({res['customer_turns']} turns, ended by {res['ended_by']}) {detail}".rstrip(),
                flush=True,
            )
            spent += (res["bot_cost_usd"] or 0.0) + (res["simulator_cost_usd"] or 0.0)
            if spent > args.max_cost_usd:
                print(f"STOP: cost ${spent:.4f} passed --max-cost-usd {args.max_cost_usd}")
                complete = False
                break
        if not complete:
            break
    reply_ms = [ms for r in results for ms in r["reply_ms"]]
    report = {
        "status": "COMPLETE" if complete else "INCOMPLETE",
        "date": datetime.now(demo.BKK).isoformat(timespec="seconds"),
        "bot_model": llm["LLM_MODEL"],
        "simulator_model": args.simulator,
        "commit": _git_commit(),
        "goals_file": hashlib.sha256(GOALS_PATH.read_bytes()).hexdigest()[:16],
        "runs": args.runs,
        "summary": {
            **pass_hat_k(results, args.runs),
            "reply_ms": {
                "n": len(reply_ms),
                "median": _pct(reply_ms, 0.5),
                "p95": _pct(reply_ms, 0.95),
            },
            "bot_cost_usd": round(sum(r["bot_cost_usd"] or 0 for r in results), 6),
            "simulator_cost_usd": round(sum(r["simulator_cost_usd"] or 0 for r in results), 6),
        },
        "results": results,
    }
    partial = bool(args.ids or args.limit)
    out_dir = ROOT / ("local/evals/results" if partial else "evals/results")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{datetime.now(demo.BKK):%Y-%m-%d-%H%M}-simulate-{slug}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("status", "simulator_model", "summary")}, indent=2))
    print(f"report: {out.relative_to(ROOT)}")
    return 0 if complete else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    val = sub.add_parser("validate", help="check case files against menu.yaml")
    val.add_argument("paths", nargs="+", type=Path, help="case files or folders of *.yaml")
    run = sub.add_parser("run", help="run cases against the real LLM (paid)")
    run.add_argument("--set", choices=sorted(SETS), default="dev")
    run.add_argument("--runs", type=int, default=3)
    run.add_argument("--max-cost-usd", type=float, required=True)
    run.add_argument("--max-messages", type=int, default=None, help="refuse to start above this")
    run.add_argument("--limit", type=int, default=0, help="only the first N cases")
    run.add_argument("--ids", default="", help="comma-separated case ids")
    run.add_argument("--model", default="", help="override LLM_MODEL (e.g. for Sonnet)")
    run.add_argument("--final", action="store_true", help="allow the hidden set")
    sim = sub.add_parser("simulate", help="a simulated customer chats with the bot (paid)")
    sim.add_argument("--simulator", required=True, help="OpenRouter model id for the customer")
    sim.add_argument("--runs", type=int, default=3)
    sim.add_argument("--max-cost-usd", type=float, required=True)
    sim.add_argument("--limit", type=int, default=0, help="only the first N goals")
    sim.add_argument("--max-turns", type=int, default=MAX_SIM_TURNS, help="customer turns per chat")
    sim.add_argument("--ids", default="", help="comma-separated goal ids")
    vg = sub.add_parser("validate-goals", help="check evals/goals.yaml against menu.yaml")
    vg.add_argument("path", nargs="?", type=Path, default=GOALS_PATH)
    args = parser.parse_args()
    if args.cmd == "validate-goals":
        return 1 if validate_goals(args.path, load_menu(ROOT / "menu.yaml")) else 0
    if args.cmd == "simulate":
        return cmd_simulate(args)
    if args.cmd == "validate":
        return 1 if validate(args.paths, load_menu(ROOT / "menu.yaml")) else 0
    if args.cmd == "run":
        return cmd_run(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
