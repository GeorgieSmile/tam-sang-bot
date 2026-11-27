#!/usr/bin/env bash
# Pull-based auto-deploy for the production VM, run every 5 minutes by a systemd timer
# (setup: docs/deploy.md, "Automatic deploys").
#
# Deploys origin/main when all of these hold:
#   - the repo is on branch main (a manual `git checkout <sha>` rollback pauses auto-deploy);
#   - the shop is closed, by menu.yaml shop.hours in Asia/Bangkok;
#   - every GitHub check run on the new commit finished with success;
#   - the commit isn't one that already failed its health check here.
# After a deploy, /health must answer; otherwise it rolls back to the previous commit,
# remembers the bad commit and exits 1.
#
# Env overrides (for testing): DRY_RUN=1 logs instead of deploying; AUTODEPLOY_NOW=HH:MM
# fakes the Bangkok time; REPO_DIR, UV, SERVICE, HEALTH_URL, FAILED_FILE.
set -euo pipefail

main() {
  local repo_dir="${REPO_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
  local uv="${UV:-$HOME/.local/bin/uv}"
  local service="${SERVICE:-tam-sang-bot}"
  local health_url="${HEALTH_URL:-http://127.0.0.1:8000/health}"
  local failed_file="${FAILED_FILE:-$HOME/.autodeploy-failed-sha}"
  cd "$repo_dir"

  local branch
  branch="$(git symbolic-ref --quiet --short HEAD || echo detached)"
  if [ "$branch" != main ]; then
    log "skip: repo is on '$branch', not main (manual rollback?); run 'git checkout main' to resume"
    return 0
  fi

  git fetch --quiet origin main
  local current target
  current="$(git rev-parse HEAD)"
  target="$(git rev-parse origin/main)"
  [ "$current" = "$target" ] && return 0 # up to date: the usual case, stay quiet

  if [ -f "$failed_file" ] && [ "$(cat "$failed_file")" = "$target" ]; then
    log "skip: ${target:0:7} already failed its health check here; push a fix"
    return 0
  fi

  local hours now open close
  hours="$(.venv/bin/python -c 'import yaml; h = yaml.safe_load(open("menu.yaml"))["shop"]["hours"]; print(h["open"], h["close"])')"
  open="${hours% *}"
  close="${hours#* }"
  now="${AUTODEPLOY_NOW:-$(TZ=Asia/Bangkok date +%H:%M)}"
  if shop_open "$now" "$open" "$close"; then
    log "wait: ${target:0:7} is ready, but the shop is open ($open-$close, now $now)"
    return 0
  fi

  local ci
  ci="$(ci_status "$target")"
  if [ "$ci" != success ]; then
    log "wait: CI for ${target:0:7} is '$ci'"
    return 0
  fi

  if [ "${DRY_RUN:-0}" = 1 ]; then
    log "dry run: would deploy ${current:0:7} -> ${target:0:7}"
    return 0
  fi

  log "deploy: ${current:0:7} -> ${target:0:7}"
  git merge --quiet --ff-only "$target"
  if install_and_restart "$uv" "$service" "$health_url"; then
    rm -f "$failed_file"
    log "deployed ${target:0:7}: health ok"
    return 0
  fi

  log "health check failed on ${target:0:7}: rolling back to ${current:0:7}"
  echo "$target" >"$failed_file"
  git reset --quiet --hard "$current"
  if install_and_restart "$uv" "$service" "$health_url"; then
    log "rolled back to ${current:0:7}: health ok"
  else
    log "ROLLBACK ALSO FAILED: check 'journalctl -u $service'"
  fi
  return 1
}

log() { echo "autodeploy: $*"; }

# True when HH:MM `now` is inside [open, close); handles hours that cross midnight.
shop_open() {
  local now="$1" open="$2" close="$3"
  if [[ "$open" < "$close" ]]; then
    [[ ! "$now" < "$open" && "$now" < "$close" ]]
  else
    [[ ! "$now" < "$open" || "$now" < "$close" ]]
  fi
}

# Prints success, pending, failure or none for a commit's GitHub check runs.
ci_status() {
  local repo json
  repo="$(git remote get-url origin | sed -E 's#^.*github\.com[:/]##; s#\.git$##')"
  json="$(curl -sf -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/$repo/commits/$1/check-runs")" || {
    echo unknown
    return 0
  }
  jq -r '.check_runs as $r
    | if ($r | length) == 0 then "none"
      elif any($r[]; .status != "completed") then "pending"
      elif all($r[]; .conclusion == "success") then "success"
      else "failure" end' <<<"$json" || echo unknown
}

install_and_restart() {
  local uv="$1" service="$2" health_url="$3"
  "$uv" sync --locked --no-dev --quiet &&
    sudo systemctl restart "$service" &&
    curl -sf --retry 10 --retry-delay 2 --retry-all-errors -o /dev/null "$health_url"
}

# Everything above is parsed before this line runs, so a deploy that rewrites
# this file mid-run can't change the commands bash executes next.
main "$@"
exit $?
