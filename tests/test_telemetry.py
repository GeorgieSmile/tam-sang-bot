"""Agent polish Ticket 2: PII-free telemetry, the pooled LINE client, webhook receipt time."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main_mod
from app.config import Settings
from app.db import ShopDB
from app.main import create_app
from app.menu import load_menu

MENU_PATH = Path(__file__).resolve().parent.parent / "menu.yaml"
BKK = ZoneInfo("Asia/Bangkok")
SECRET = "test-channel-secret"
USER = "U_SECRET_USER_1234"
TEXT = "กะเพราหมูกรอบ 1 ส่งบ้าน 45/2 ซอยลับ"
ADDRESS = "บ้าน 45/2 ซอยลับ"


class SpyLine:
    def __init__(self) -> None:
        self.replies: list[tuple[str, list[dict[str, Any]]]] = []

    def reply_messages(self, reply_token: str, messages: list[dict[str, Any]]) -> None:
        self.replies.append((reply_token, messages))

    def push_text(self, user_id: str, text: str) -> None:
        return None

    def show_loading(self, user_id: str, seconds: int = 20) -> None:
        return None

    def get_profile(self, user_id: str) -> str | None:
        return None

    def get_message_content(self, message_id: str) -> bytes:
        return b""


class StubLLM:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> Any:
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _resp(content: str = "", tool_calls: list[Any] | None = None, **extra: Any) -> Any:
    choice = SimpleNamespace(
        message=SimpleNamespace(content=content, tool_calls=tool_calls),
        **({"finish_reason": extra.pop("finish_reason")} if "finish_reason" in extra else {}),
    )
    return SimpleNamespace(choices=[choice], **extra)


def _call(call_id: str, name: str, args: dict[str, Any]) -> Any:
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(args, ensure_ascii=False)),
    )


def _settings(tmp_path: Path, token: str = "token") -> Settings:
    return Settings(
        db_path=tmp_path / "shop.db",
        menu_path=MENU_PATH,
        kitchen_secret="secret-123",
        public_base_url="http://localhost:8000",
        payment_info_text="",
        line_channel_secret=SECRET,
        line_channel_access_token=token,
        owner_line_user_id="U_OWNER",
        llm_base_url="https://openrouter.ai/api/v1",
        llm_api_key="test-key",
        llm_model="anthropic/claude-haiku-5.5",
        llm_reasoning_effort="low",
    )


def _app(tmp_path: Path, llm: Any) -> tuple[TestClient, SpyLine]:
    db = ShopDB(tmp_path / "shop.db", clock=lambda: datetime(2026, 11, 23, 12, 0, tzinfo=BKK))
    line = SpyLine()
    app = create_app(
        settings=_settings(tmp_path),
        db=db,
        menu=load_menu(MENU_PATH),
        line_client=line,
        llm_client=llm,
    )
    return TestClient(app), line


def _text_event(n: int, text: str = TEXT, user: str = USER) -> dict[str, Any]:
    return {
        "webhookEventId": f"evt-{n}",
        "type": "message",
        "replyToken": f"rt-secret-token-{n}",
        "source": {"type": "user", "userId": user},
        "message": {"id": f"m-{n}", "type": "text", "text": text},
    }


def _post(client: TestClient, events: list[dict[str, Any]]) -> None:
    raw = json.dumps({"events": events}, ensure_ascii=False).encode("utf-8")
    sig = base64.b64encode(hmac.new(SECRET.encode(), raw, hashlib.sha256).digest()).decode()
    res = client.post(
        "/line/webhook",
        content=raw,
        headers={"Content-Type": "application/json", "x-line-signature": sig},
    )
    assert res.status_code == 200


def _fields(record: logging.LogRecord) -> dict[str, str]:
    return dict(re.findall(r"(\w+)=(\S+)", record.getMessage()))


def _lines(caplog: pytest.LogCaptureFixture, logger: str, prefix: str) -> list[dict[str, str]]:
    return [
        _fields(r)
        for r in caplog.records
        if r.name == logger and r.getMessage().startswith(prefix + " ")
    ]


def test_each_llm_call_and_webhook_event_logs_one_pii_free_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    tools = [
        _call(
            "c1",
            "update_cart",
            {
                "action": "add",
                "items": [{"dish_id": "kaphrao", "qty": 1, "option_ids": ["crispy_pork"]}],
            },
        ),
        _call("c2", "set_fulfillment", {"fulfillment": "delivery", "address": ADDRESS}),
        _call("c3", "cancel_order", {"order_id": 999}),
    ]
    usage = SimpleNamespace(
        prompt_tokens=2100,
        completion_tokens=40,
        prompt_tokens_details=SimpleNamespace(cached_tokens=1900),
    )
    llm = StubLLM(
        [
            _resp(tool_calls=tools),  # no usage or finish_reason (like the stubs)
            _resp(content="ส่งบ้าน 45/2 ซอยลับ นะคะ", usage=usage, finish_reason="stop"),
        ]
    )
    client, line = _app(tmp_path, llm)
    _post(client, [_text_event(1)])

    calls = _lines(caplog, "app.agent", "llm_call")
    events = _lines(caplog, "app.main", "webhook_event")
    assert len(calls) == 2 and len(events) == 1
    assert {c["turn_id"] for c in calls} == {events[0]["turn_id"]}
    assert re.fullmatch(r"[0-9a-f]{8}", events[0]["turn_id"])
    assert calls[0] | {"latency_ms": "0"} == {
        "turn_id": events[0]["turn_id"],
        "model": "anthropic/claude-haiku-5.5",
        "latency_ms": "0",
        "prompt_tokens": "-",
        "cached_tokens": "-",
        "completion_tokens": "-",
        "finish_reason": "-",
        "tools": "update_cart:ok,set_fulfillment:ok,cancel_order:err",
        "error": "-",
    }
    assert (calls[1]["prompt_tokens"], calls[1]["cached_tokens"]) == ("2100", "1900")
    assert (calls[1]["completion_tokens"], calls[1]["finish_reason"]) == ("40", "stop")
    assert (calls[1]["tools"], calls[1]["error"]) == ("-", "-")
    assert events[0] | {"lock_wait_ms": "0", "total_ms": "0"} == {
        "turn_id": events[0]["turn_id"],
        "event_type": "message.text",
        "reason": "ok",
        "llm_calls": "2",
        "lock_wait_ms": "0",
        "total_ms": "0",
        "reply_ok": "true",
    }
    for value in (calls[0]["latency_ms"], events[0]["lock_wait_ms"], events[0]["total_ms"]):
        assert value.isdigit()
    assert line.replies  # the customer got the reply
    for secret in (TEXT, ADDRESS, "45/2", USER, "rt-secret-token-1", "crispy_pork"):
        assert secret not in caplog.text


def test_llm_errors_log_the_type_and_status_but_no_body(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)

    class RateLimited(Exception):
        status_code = 429

    client, _ = _app(tmp_path, StubLLM([RateLimited(f"slow down {USER} {TEXT}")]))
    _post(client, [_text_event(1)])
    (call,) = _lines(caplog, "app.agent", "llm_call")
    assert call["error"] == "RateLimited:429"
    (event,) = _lines(caplog, "app.main", "webhook_event")
    assert (event["reason"], event["llm_calls"]) == ("error", "1")
    assert USER not in caplog.text and TEXT not in caplog.text


def test_line_calls_share_one_pooled_client_closed_on_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[httpx.Client] = []
    sent: list[tuple[str, float | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        timeout = request.extensions.get("timeout", {}).get("read")
        sent.append((request.url.path, timeout))
        return httpx.Response(200, json={})

    class CountingClient(httpx.Client):
        def __init__(self, **kwargs: Any) -> None:
            created.append(self)
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    with monkeypatch.context() as m:
        m.setattr(httpx, "Client", CountingClient)
        app = create_app(
            settings=_settings(tmp_path),
            db=ShopDB(tmp_path / "shop.db"),
            menu=load_menu(MENU_PATH),
            llm_client=StubLLM([]),
        )
    with TestClient(app):
        app.state.line.show_loading("U_A")
        app.state.line.reply_messages("rt-1", [{"type": "text", "text": "hi"}])
        app.state.line.push_text("U_A", "hi")
        assert len(created) == 1 and not created[0].is_closed
    assert sent == [
        ("/v2/bot/chat/loading/start", 3.0),
        ("/v2/bot/message/reply", 8.0),
        ("/v2/bot/message/push", 8.0),
    ]
    assert len(created) == 1 and created[0].is_closed


def test_every_event_in_a_payload_counts_from_webhook_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[float] = []
    first_done: list[float] = []
    real = main_mod.handle_customer_text

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs["started_at"])
        turn = real(*args, **kwargs)
        first_done.append(time.monotonic())
        return turn

    monkeypatch.setattr(main_mod, "handle_customer_text", spy)
    client, _ = _app(tmp_path, StubLLM([_resp(content="ค่ะ"), _resp(content="ค่ะ")]))
    _post(client, [_text_event(1, "หนึ่ง", "U_A"), _text_event(2, "สอง", "U_B")])
    assert len(seen) == 2
    assert seen[1] == seen[0] < first_done[0]  # not restarted after the first event


def test_free_text_from_the_provider_or_payload_never_reaches_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Review fix: finish reasons, event types and tool names come from fixed lists."""
    caplog.set_level(logging.INFO)
    odd_tool = SimpleNamespace(id="c1", function=SimpleNamespace(name=["x"], arguments="{}"))
    llm = StubLLM(
        [
            _resp(tool_calls=[odd_tool], finish_reason=f"stop\n{ADDRESS}"),
            _resp(content="ค่ะ"),
        ]
    )
    client, line = _app(tmp_path, llm)
    weird = _text_event(1)
    weird["message"]["type"] = f"text\n{ADDRESS}"
    _post(client, [_text_event(2), weird])
    calls = _lines(caplog, "app.agent", "llm_call")
    assert [(c["finish_reason"], c["tools"]) for c in calls] == [
        ("other", "unknown:err"),
        ("-", "-"),
    ]
    events = _lines(caplog, "app.main", "webhook_event")
    assert [e["event_type"] for e in events] == ["message.text", "message.other"]
    assert line.replies  # the odd tool name did not break the turn
    assert ADDRESS not in caplog.text and "\n" not in "".join(
        r.getMessage() for r in caplog.records
    )


def test_skipped_events_still_get_one_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    client, _ = _app(tmp_path, StubLLM([_resp(content="ค่ะ")]))
    no_user = {"webhookEventId": "evt-join", "type": "join", "source": {"type": "group"}}
    _post(client, [_text_event(1), _text_event(1), no_user])
    events = _lines(caplog, "app.main", "webhook_event")
    assert [(e["event_type"], e["reason"]) for e in events] == [
        ("message.text", "ok"),
        ("message.text", "duplicate"),
        ("join", "no_user"),
    ]
    assert len({e["turn_id"] for e in events}) == 3


def test_app_log_handler_is_added_only_when_nothing_else_prints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_log = logging.getLogger("app")
    monkeypatch.setattr(app_log, "handlers", [])
    monkeypatch.setattr(app_log, "level", logging.NOTSET)
    main_mod._install_app_logging()  # root has a handler (pytest's): no second printer
    assert app_log.handlers == []
    monkeypatch.setattr(logging.getLogger(), "handlers", [])
    main_mod._install_app_logging()  # bare uvicorn: one INFO handler for app.*
    assert len(app_log.handlers) == 1 and app_log.level == logging.INFO
    main_mod._install_app_logging()
    assert len(app_log.handlers) == 1


def test_a_crash_after_the_llm_call_still_logs_that_call_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=2, prompt_tokens_details=None)
    llm = StubLLM([_resp(content="ค่ะ", usage=usage)])
    client, _ = _app(tmp_path, llm)
    db = client.app.state.db  # type: ignore[attr-defined]
    real_status = db.get_shop_status
    real_create = llm.chat.completions.create
    broken = {"on": False}

    def create(**kwargs: Any) -> Any:
        broken["on"] = True  # the DB fails right after the LLM answers
        return real_create(**kwargs)

    def status(menu: Any) -> Any:
        if broken["on"]:
            raise RuntimeError("database is locked")
        return real_status(menu)

    llm.chat.completions.create = create
    db.get_shop_status = status
    _post(client, [_text_event(1)])
    (call,) = _lines(caplog, "app.agent", "llm_call")
    assert (call["prompt_tokens"], call["error"]) == ("10", "RuntimeError")
    (event,) = _lines(caplog, "app.main", "webhook_event")
    assert (event["turn_id"], event["reason"]) == (call["turn_id"], "exception")


def test_app_telemetry_level_and_httpx_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Final re-review I2: INFO telemetry survives a WARNING root, no second printer is added
    later, and httpx (which logs URLs with LINE user IDs) stays at WARNING."""
    app_log = logging.getLogger("app")
    httpx_log = logging.getLogger("httpx")
    monkeypatch.setattr(app_log, "handlers", [])
    monkeypatch.setattr(app_log, "level", logging.NOTSET)
    monkeypatch.setattr(app_log, "propagate", True)
    monkeypatch.setattr(httpx_log, "level", logging.NOTSET)
    monkeypatch.setattr(logging.getLogger(), "handlers", [])
    main_mod._install_app_logging()
    assert app_log.getEffectiveLevel() == logging.INFO
    assert app_log.propagate is False and len(app_log.handlers) == 1
    assert httpx_log.getEffectiveLevel() == logging.WARNING
