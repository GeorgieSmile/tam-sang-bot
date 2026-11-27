# Deploying tam-sang-bot

Production runs on one Google Cloud VM at `https://tamsang.duckdns.org`. This page covers daily operations, restoring a backup, and rebuilding the server from scratch.

| Piece | What it is |
|---|---|
| VM | Google Cloud project `tam-sang-bot`, `e2-micro` (Always Free), zone `us-west1-b`, Ubuntu 24.04, 30 GB standard disk, 2 GB swap |
| App | systemd unit `tam-sang-bot` runs `.venv/bin/uvicorn` on `127.0.0.1:8000` from `~/tam-sang-bot` |
| HTTPS | Caddy reverse proxy. It gets and renews a Let's Encrypt certificate on its own. |
| Domain | `tamsang.duckdns.org`. A cron job updates DuckDNS every 5 minutes, because the VM's IP is ephemeral. |
| Logs | journald, persistent and capped at 200 MB. Needed for the ADR-0002 `card_path` count. |
| Backups | Nightly at 03:00 Bangkok. 14 days kept on the VM and 30 days in `gs://tam-sang-bot-backups`. |
| Secrets | `~/tam-sang-bot/.env` and `~/.duckdns-token`, only on the VM. Never in git. |

**Deploying your own copy?** Replace these names throughout:
- `tamsang.duckdns.org` and the DuckDNS subdomain `tamsang`;
- the project ID `tam-sang-bot`;
- the bucket `tam-sang-bot-backups`. Bucket names are global across all of Google Cloud, so pick a unique one;
- the clone URL, with your fork's.

## Why this setup

- **Oracle Cloud** was the first choice. Singapore had no free capacity for either Always Free shape, and the Pay As You Go upgrade needed a card hold of about 138 SGD.
- **Railway's free tier** sleeps and gives $1 of credit a month, so it's not always on.
- **Google's free `e2-micro`** only exists in US regions. Oregon adds about 0.2 s per reply, which is small next to the 1–3 s LLM call. Google doesn't reclaim idle VMs.
- **No Docker:** with 1 GB of RAM and one app, systemd plus `uv` covers restarts, logs and pinned dependencies with less to run.

## Daily operations

All commands run from the Mac. `ssh tamsang` is set up in [SSH access](#ssh-access).

**Deploy what's on `main`:**
```bash
ssh tamsang 'cd ~/tam-sang-bot && git pull && ~/.local/bin/uv sync --no-dev && sudo systemctl restart tam-sang-bot \
  && curl -s --retry 10 --retry-delay 2 --retry-all-errors http://127.0.0.1:8000/health'
```
- **Expected output:** `{"ok":true}`. The app takes about 4 s to start on this VM.
- **Why the full path `~/.local/bin/uv`:** a one-off `ssh tamsang '...'` command doesn't load the shell setup that puts `uv` on the PATH. It works without the path in an interactive session.

**Roll back to an earlier commit:**
```bash
ssh tamsang 'cd ~/tam-sang-bot && git checkout <sha> && ~/.local/bin/uv sync --no-dev && sudo systemctl restart tam-sang-bot'
```
This leaves the repo on a detached commit. To go back to normal deploys, run `git checkout main && git pull` there. If the bad version changed the database schema, also [restore a backup](#restore-a-backup) from before the deploy.

**Status and logs:**
```bash
ssh tamsang 'systemctl status tam-sang-bot --no-pager | head -5'
ssh tamsang 'journalctl -u tam-sang-bot -n 50 --no-pager'
ssh tamsang 'journalctl -u tam-sang-bot -f'          # live, Ctrl+C to stop
```
Requests for `/.env`, `/wp-admin` and similar paths are internet scanners. They all get 404.

**ADR-0002 count** (see [docs/adr/0002](adr/0002-card-fast-path-question-word-check.md)):
```bash
ssh tamsang "journalctl -u tam-sang-bot | grep -o 'card_path=\S*' | sort | uniq -c"
```

**Check that backups still run:**
```bash
ssh tamsang 'tail -5 ~/backup.log; /snap/bin/gcloud storage ls -l gs://tam-sang-bot-backups/'
```

**Kitchen board:** `https://tamsang.duckdns.org/kitchen?key=<KITCHEN_SECRET>`

## Restore a backup

The database runs in WAL mode, so `shop.db` has two companion files, `shop.db-wal` and `shop.db-shm`. Move all three aside before restoring. A leftover `-wal` file would be replayed onto the restored copy.

```bash
ssh tamsang
sudo systemctl stop tam-sang-bot
cd ~/tam-sang-bot
mkdir -p ~/pre-restore && mv shop.db shop.db-wal shop.db-shm ~/pre-restore/ 2>/dev/null
/snap/bin/gcloud storage ls gs://tam-sang-bot-backups/                     # pick a date
/snap/bin/gcloud storage cp gs://tam-sang-bot-backups/shop-YYYY-MM-DD.db shop.db
sqlite3 shop.db "PRAGMA integrity_check;"                                  # must print: ok
sudo systemctl start tam-sang-bot
```
Copies from the last 14 days are also on the VM in `~/backups/`; you can use `cp ~/backups/shop-YYYY-MM-DD.db shop.db` instead.

## SSH access

The Mac logs in with a key that `gcloud` registered with the project:

```bash
gcloud compute ssh <linux-user>@tam-sang-bot --project=tam-sang-bot --zone=us-west1-b
```
- **That command:**
  - creates `~/.ssh/google_compute_engine` (private key) and `.pub` (public key);
  - adds the public key to the project's `ssh-keys` metadata;
  - Google's guest agent then copies it into `~/.ssh/authorized_keys` on the VM.
- **Use the existing Linux user, the one that owns `~/tam-sang-bot`.** Without `<linux-user>@`, gcloud uses the Mac's username and the agent creates a new, empty user.
- **Always pass `--project=tam-sang-bot`.** The Mac's default gcloud project is a different one.

Then add this to the Mac's `~/.ssh/config`:
```
Host tamsang
  HostName tamsang.duckdns.org
  User <linux-user>
  IdentityFile ~/.ssh/google_compute_engine
  IdentitiesOnly yes
  StrictHostKeyChecking accept-new
  ServerAliveInterval 30
```

**To revoke a lost Mac's access:** Compute Engine → Metadata → SSH keys, delete the entry. The browser **SSH** button in the console keeps working without this key.

## Rebuild from scratch

Use this if the VM is lost or you move to a new project. Steps 1–2 happen in the browser. After step 3, everything runs over `ssh tamsang`.

**1. Google Cloud console:**
- **Project and budget:** a project linked to the billing account, and a budget alert set to "Alerts only" (not "Spend cap"). Ours is ฿50.
- **VM:** Compute Engine → Create instance:

| Setting | Value | Why |
|---|---|---|
| Region | `us-west1` (Oregon) | The free tier only covers `us-west1`, `us-central1`, `us-east1`. Singapore costs about $9/month. |
| Machine type | E2 → `e2-micro` | The free tier |
| Provisioning model | Standard | Spot VMs can be stopped at any time |
| Boot disk | Ubuntu 24.04 LTS x86/64, **Standard persistent disk**, 30 GB | The default "Balanced" disk is billed |
| Data protection | No backups | Snapshots cost money; `backup.sh` handles backups |
| Firewall | Allow HTTP and Allow HTTPS | Adds the `http-server` and `https-server` tags |
| Observability | **Untick** Install Ops Agent | It uses 100–200 MB of the 1 GB RAM, and journald is enough |

The cost estimate still shows about $6/month for Oregon. The free-tier discount appears on the bill, not in the estimate.

**2. DuckDNS:** at duckdns.org, set the subdomain `tamsang` to the VM's External IP. Copy the account token.

**3. SSH from the Mac:** follow [SSH access](#ssh-access). Use your Google account's username as `<linux-user>`; that matches what the browser SSH button creates.

**4. Server packages, swap, persistent logs:**
```bash
ssh tamsang
sudo apt update && sudo apt install -y git sqlite3 caddy
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
sudo mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nStorage=persistent\nSystemMaxUse=200M\n' | sudo tee /etc/systemd/journald.conf.d/keep.conf
sudo systemctl restart systemd-journald
```

**5. App code:** the repo is public, so clone over HTTPS. `.python-version` pins Python 3.13, and `uv` installs it.
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh && source $HOME/.local/bin/env
git clone https://github.com/GeorgieSmile/tam-sang-bot.git && cd tam-sang-bot
uv sync --no-dev
```

**6. Secrets:**
1. From the Mac, run `scp .env tamsang:tam-sang-bot/.env`.
2. On the VM, run `chmod 600 ~/tam-sang-bot/.env`, and set `PUBLIC_BASE_URL=https://tamsang.duckdns.org` in that file.
3. Optional: run `scp shop.db tamsang:tam-sang-bot/` to carry over existing data. Otherwise the app creates an empty database.

**7. systemd service:**
```bash
sudo tee /etc/systemd/system/tam-sang-bot.service >/dev/null <<EOF
[Unit]
Description=tam-sang-bot
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$HOME/tam-sang-bot
ExecStart=$HOME/tam-sang-bot/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload && sudo systemctl enable --now tam-sang-bot
curl -s --retry 10 --retry-delay 2 --retry-all-errors http://127.0.0.1:8000/health
```

**8. Caddy (HTTPS):** DuckDNS must already point at the VM.
```bash
printf 'tamsang.duckdns.org {\n    reverse_proxy 127.0.0.1:8000\n}\n' | sudo tee /etc/caddy/Caddyfile
sudo systemctl reload caddy
curl -s --retry 10 --retry-delay 3 --retry-all-errors https://tamsang.duckdns.org/health
```

**9. DuckDNS updater:**
```bash
echo '<duckdns-token>' > ~/.duckdns-token && chmod 600 ~/.duckdns-token
cat > ~/duckdns.sh <<'EOF'
#!/bin/sh
curl -s "https://www.duckdns.org/update?domains=tamsang&token=$(cat $HOME/.duckdns-token)&ip=" -o /dev/null
EOF
chmod +x ~/duckdns.sh && ~/duckdns.sh
```

**10. Backup bucket and VM access.** Run these from the Mac. The scope change needs the VM stopped, which takes the bot offline for a few minutes, so do it while the shop is closed.
```bash
P=tam-sang-bot; Z=us-west1-b; B=tam-sang-bot-backups
gcloud storage buckets create gs://$B --project=$P --location=us-west1 --default-storage-class=STANDARD \
  --uniform-bucket-level-access --public-access-prevention
echo '{"rule":[{"action":{"type":"Delete"},"condition":{"age":30}}]}' > /tmp/lifecycle.json
gcloud storage buckets update gs://$B --project=$P --lifecycle-file=/tmp/lifecycle.json
gcloud compute instances stop tam-sang-bot --project=$P --zone=$Z
gcloud compute instances set-service-account tam-sang-bot --project=$P --zone=$Z --scopes=default,storage-rw
gcloud compute instances start tam-sang-bot --project=$P --zone=$Z
```
- **Public access prevention** is on because the backups hold customer LINE IDs and orders.
- **The IP may change on restart.** The DuckDNS cron fixes it within 5 minutes.

**11. Backup script and cron (on the VM):**
```bash
cat > ~/backup.sh <<'EOF'
#!/bin/sh
set -e
export PATH="/snap/bin:/usr/bin:/bin:$PATH"
F="$HOME/backups/shop-$(date +%F).db"
mkdir -p "$HOME/backups"
sqlite3 "$HOME/tam-sang-bot/shop.db" ".backup $F"
gcloud storage cp "$F" gs://tam-sang-bot-backups/ --quiet
find "$HOME/backups" -name 'shop-*.db' -mtime +14 -delete
EOF
chmod +x ~/backup.sh
(echo "*/5 * * * * $HOME/duckdns.sh"; echo "0 20 * * * $HOME/backup.sh >> $HOME/backup.log 2>&1") | crontab -
env -i HOME="$HOME" PATH=/usr/bin:/bin /bin/sh -c "$HOME/backup.sh" && echo BACKUP-OK
```
- **`/snap/bin` is on the script's PATH** because cron's default PATH doesn't include it, and that's where `gcloud` is.
- **The last line runs the script with cron's bare environment,** so a cron-only failure shows up now.
- **`0 20` is 20:00 UTC,** which is 03:00 in Bangkok.

**12. LINE:**
1. In LINE Developers → Messaging API, set the Webhook URL to `https://tamsang.duckdns.org/line/webhook`.
2. Click **Verify**, and make sure **Use webhook** is on.
3. Send the bot a message, then check `journalctl -u tam-sang-bot -n 20` for a `webhook_event … reply_ok=true` line.

**13. Reboot test:**
1. Run `sudo reboot`.
2. A minute later, `curl https://tamsang.duckdns.org/health` should return `{"ok":true}`.
3. `free -h` should show the 2 GB swap.

## Secrets

| Secret | Lives in | To rotate |
|---|---|---|
| LINE, LLM, SlipOK keys, `KITCHEN_SECRET` | `~/tam-sang-bot/.env` on the VM, and the Mac's local `.env` | Edit both, then `sudo systemctl restart tam-sang-bot` |
| DuckDNS token | `~/.duckdns-token` on the VM | Recreate it on duckdns.org, then overwrite the file |
| SSH key | `~/.ssh/google_compute_engine` on the Mac | See [SSH access](#ssh-access) |

Never paste a secret into chat, an issue or a commit.

## Troubleshooting

| Symptom | Check |
|---|---|
| The health check prints nothing right after a restart | The app takes about 4 s to start. Use the `--retry` curl above. If it still fails: `journalctl -u tam-sang-bot -n 50`. |
| HTTPS fails, but `http://127.0.0.1:8000/health` works on the VM | Check that `dig +short tamsang.duckdns.org` matches the VM's External IP. Then `journalctl -u caddy -n 30` for certificate errors. Ports 80 and 443 must stay open; the Let's Encrypt check uses port 80. |
| The domain points at an old IP after a stop and start | Run `~/duckdns.sh` and wait for DNS, about 1 minute |
| `backup.log` shows `403` or "insufficient scopes" | The VM lost the `storage-rw` scope. Redo step 10. |
| `uv sync` picks the wrong Python | `.python-version` should say `3.13`. Rebuild with `rm -rf .venv && uv sync --no-dev`. |
| Out of memory | `free -h`. If swap is in use all the time, change the machine type to `e2-small` (about $12/month): stop the VM, edit it, start it. No reinstall is needed. |
