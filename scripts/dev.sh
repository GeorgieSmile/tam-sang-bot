#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
DB="${DB_PATH:-shop.db}"

case "${1:-help}" in
  run)
    exec uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 2>&1 | tee -a .dev.log
    ;;
  reset-db)
    sqlite3 "$DB" "BEGIN IMMEDIATE;
      DELETE FROM conversations;
      DELETE FROM orders;
      DELETE FROM customers;
      DELETE FROM menu_overrides;
      DELETE FROM line_events;
      UPDATE shop_state SET
        override_date=NULL, open_override=NULL, bot_enabled=1,
        busy_date=NULL, busy_mode=0, wait_minutes=NULL, notify_owner_on_order=0;
      COMMIT;"
    echo "Reset $DB cleanly."
    ;;
  users)
    sqlite3 "$DB" "SELECT DISTINCT line_user_id FROM conversations UNION SELECT DISTINCT line_user_id FROM orders;"
    ;;
  *)
    echo "Usage: ./scripts/dev.sh {run|reset-db|users}"
    exit 1
    ;;
esac
