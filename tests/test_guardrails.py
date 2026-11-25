"""Mechanical checks for rules a reviewer would otherwise have to remember."""

from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"


def test_app_never_overwrites_whole_chat_history() -> None:
    """`save_conversation_messages` replaces the whole history: test setup only.

    App code appends with `append_conversation_messages` / `append_conversation_event`,
    so history written meanwhile (e.g. an owner push during an LLM turn) is never lost.
    """
    calls = [
        f"{path.name}:{node.lineno}"
        for path in sorted(APP.glob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Attribute) and node.attr == "save_conversation_messages"
    ]
    assert calls == [], f"use the append helpers instead: {calls}"
