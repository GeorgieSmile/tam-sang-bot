"""`./scripts/dev.sh reset-db` clears every table and every shop_state setting, from the schema."""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path
from typing import Any

from app.db import ShopDB

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("reset_db", ROOT / "scripts" / "reset_db.py")
assert _spec is not None and _spec.loader is not None
reset_db: Any = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reset_db)


def _shop_state(path: Path) -> dict[str, Any]:
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        row = dict(conn.execute("SELECT * FROM shop_state WHERE id = 1").fetchone())
    row.pop("updated_at")
    return row


def test_reset_clears_all_tables_and_restores_every_setting(tmp_path: Path) -> None:
    fresh, used = tmp_path / "fresh.db", tmp_path / "used.db"
    ShopDB(fresh)
    db = ShopDB(used)
    db.get_or_create_conversation("U_TEST")
    db.open_handover("U_TEST", "image", None, "[รูปภาพ]")
    db.record_line_event("evt-1")
    with sqlite3.connect(used) as conn:
        # Change every setting, whatever it is, so a column the reset skips can't hide
        for _, name, col_type, _, _, _ in conn.execute("PRAGMA table_info(shop_state)"):
            if name not in ("id", "updated_at"):
                value = 7 if "INT" in col_type.upper() else "x"
                conn.execute(f'UPDATE shop_state SET "{name}" = ?', (value,))
    assert _shop_state(used) != _shop_state(fresh)

    reset_db.reset(used)

    assert _shop_state(used) == _shop_state(fresh)
    with sqlite3.connect(used) as conn:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        counts = {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
    assert counts.pop("shop_state") == 1
    assert counts and all(n == 0 for n in counts.values()), counts
    # The app still opens the reset DB normally
    assert ShopDB(used).get_or_create_conversation("U_TEST").id
