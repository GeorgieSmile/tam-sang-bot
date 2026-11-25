"""Clear local test data from the SQLite DB (`./scripts/dev.sh reset-db`).

Empties every table except `shop_state`, and puts each `shop_state` setting back to its
schema default. Both lists are read from the database itself, so a new table or setting
is reset without editing this script. Safe while uvicorn is running (one IMMEDIATE
transaction).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

# Row identity and its timestamp are not settings
KEEP_COLUMNS = {"id", "updated_at"}


def reset(db_path: Path) -> None:
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' AND name != 'shop_state'"
            )
        ]
        for table in tables:
            conn.execute(f'DELETE FROM "{table}"')
        # PRAGMA table_info rows: (cid, name, type, notnull, dflt_value, pk)
        defaults = [
            f'"{name}" = {default if default is not None else "NULL"}'
            for _, name, _, _, default, _ in conn.execute("PRAGMA table_info(shop_state)")
            if name not in KEEP_COLUMNS
        ]
        conn.execute(f"UPDATE shop_state SET {', '.join(defaults)}")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def main() -> int:
    db_path = Path(sys.argv[1] if len(sys.argv) > 1 else "shop.db")
    if not db_path.is_file():
        print(f"No database at {db_path} (it is created on the app's first request).")
        return 1
    reset(db_path)
    print(f"Reset {db_path} cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
