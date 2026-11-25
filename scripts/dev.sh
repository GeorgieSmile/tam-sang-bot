#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
DB="${DB_PATH:-shop.db}"

case "${1:-help}" in
  run)
    exec uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 2>&1 | tee -a .dev.log
    ;;
  reset-db)
    # Clears every table and resets every shop_state setting, read from the schema
    exec uv run python scripts/reset_db.py "$DB"
    ;;
  slips)
    # Decode real slip images locally (no API calls). Keep them in git-ignored local/slips/.
    exec uv run python -m app.slips "${2:-local/slips}"
    ;;
  demo)
    # Real app on a seeded throwaway DB with stub LINE/LLM/SlipOK (see scripts/demo.py)
    exec uv run python scripts/demo.py serve
    ;;
  demo-up)
    # Start the demo in the background and return once it answers (log: local/demo-server.log)
    url="http://127.0.0.1:${DEMO_PORT:-8765}/health"
    if curl -sf -o /dev/null "$url"; then echo "Demo already running."; exit 0; fi
    mkdir -p local
    nohup uv run python scripts/demo.py serve > local/demo-server.log 2>&1 &
    for _ in $(seq 1 60); do
      if curl -sf -o /dev/null "$url"; then
        echo "Demo up: http://127.0.0.1:${DEMO_PORT:-8765}/kitchen?key=demo-kitchen-key"
        exit 0
      fi
      sleep 0.5
    done
    echo "Demo did not start; see local/demo-server.log"; tail -20 local/demo-server.log; exit 1
    ;;
  demo-down)
    pkill -f "scripts/demo.py serve" && echo "Demo stopped." || echo "No demo running."
    ;;
  say)
    # Send one fake LINE event to the running demo: say <user> <text|slip|photo|voice|location|confirm|cancel> [arg]
    shift
    exec uv run python scripts/demo.py say "$@"
    ;;
  users)
    sqlite3 "$DB" "SELECT DISTINCT line_user_id FROM conversations UNION SELECT DISTINCT line_user_id FROM orders;"
    ;;
  *)
    echo "Usage: ./scripts/dev.sh {run|reset-db|users|slips [dir]|demo|demo-up|demo-down|say <user> <kind> [arg]}"
    exit 1
    ;;
esac
