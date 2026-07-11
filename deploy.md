# 🔒 AlphaScanner Pro — Personal Deployment Runbook

**⚠️ DO NOT commit this file to the GitHub repo.** It contains your real VPS IP and domain.
Keep it locally on your own machine (or in a private notes app), not in `/opt/alpha_scanner` where it could accidentally get `git add`ed.

If you ever add this file to the repo folder for convenience, add this line to `.gitignore` first:
```
deploy-notes.md
```

---

## Your Details

| Item | Value |
|---|---|
| VPS IP | `187.127.187.235` |
| Domain | `rahulintratrading.online` |
| GitHub Repo | `https://github.com/Rahul070994/Screener.git` |
| App Directory | `/opt/alpha_scanner` |
| Service Name | `alpha_scanner` |
| Redirect URL (Kite app config) | `https://rahulintratrading.online/api/broker/callback` |

---

## Quick SSH In

```bash
ssh root@187.127.187.235
cd /opt/alpha_scanner
```

---

## Your `.env` (reference only — never paste real secret values into chat or commits)

```ini
MASTER_SECRET_KEY=<your real value — stored in .env on VPS>
FLASK_SECRET_KEY=<your real value — stored in .env on VPS, different from MASTER_SECRET_KEY>
REDIRECT_URL=https://rahulintratrading.online/api/broker/callback
FORCE_HTTPS=true
```

Check current live values anytime with:
```bash
cat .env
```

If you ever need to regenerate either key:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```
Then rewrite `.env` fully (never append with `>>`):
```bash
cat > .env << 'EOF'
MASTER_SECRET_KEY=paste_here
FLASK_SECRET_KEY=paste_here
REDIRECT_URL=https://rahulintratrading.online/api/broker/callback
FORCE_HTTPS=true
EOF
```
**Note:** if you regenerate `MASTER_SECRET_KEY`, you must re-run `python encrypt_keys.py` to re-encrypt `users.json`, or existing broker credentials will fail to decrypt.

---

## Deploying a Code Update (your normal workflow)

```bash
cd /opt/alpha_scanner
git fetch origin
git log HEAD..origin/main --oneline    # see what's coming in
git status                             # check nothing local is in the way
git pull origin main
sudo systemctl restart alpha_scanner
sleep 3 && sudo systemctl status alpha_scanner
```

If `git status` shows local changes to `ultimate_scanner.py`, `strategies/`, or config files (not just `alpha_scanner.log` / `data/rahul/*` which are just runtime files and safe to ignore), stash before pulling:
```bash
git stash push -u -m "pre-pull backup"
git pull origin main
```

---

## Service Management Cheat Sheet

```bash
# Check status
sudo systemctl status alpha_scanner

# Restart after any code/env change
sudo systemctl restart alpha_scanner

# Stop
sudo systemctl stop alpha_scanner

# View live logs
sudo journalctl -u alpha_scanner -f

# View app log file directly
tail -f /opt/alpha_scanner/alpha_scanner.log
```

**Confirm it's always running single-worker + threads** (critical — do not let this drift to `-w 2` or higher):
```bash
cat /etc/systemd/system/alpha_scanner.service | grep ExecStart
```
Should show:
```
ExecStart=/opt/alpha_scanner/venv/bin/gunicorn -w 1 --threads 4 -b 127.0.0.1:8000 ultimate_scanner:app
```

---

## Nginx / SSL

```bash
# Test nginx config after any change
sudo nginx -t

# Restart nginx
sudo systemctl restart nginx

# Renew SSL manually (normally auto-renews via certbot timer)
sudo certbot renew --dry-run
```

Config file location:
```bash
sudo nano /etc/nginx/sites-available/alpha_scanner
```

---

## Your Known Fixes / History (so you don't re-diagnose the same issues)

1. **Session / "Not logged in" on Backtest, random logouts** — caused by `app.secret_key = os.urandom(24)` regenerating randomly per Gunicorn worker/restart, combined with 2 worker processes not sharing memory. Fixed by: fixed `FLASK_SECRET_KEY` in `.env`, and reducing Gunicorn to `-w 1 --threads 4`.
2. **Scanner progress bar flickering/resetting mid-scan, pinned/wallet values inconsistent on refresh** — same root cause as #1: multiple worker processes each holding separate in-memory copies of `FullScanner` / `PaperTradingEngine` state. Fixed by single-worker config.
3. **`.env` had duplicate `FLASK_SECRET_KEY` / `FORCE_HTTPS` lines** from repeated `>>` appends — cleaned up to one value per key.
4. **`.env` and `users.json` were not properly gitignored** despite commit messages claiming so — confirmed `.gitignore` now actually contains `.env`.
5. **Market Movers used a fake `prev_close` fallback** (today's own first candle) when no real prior-day close was found within a 3-day lookback — could produce misleading change% around holidays/long weekends. Fixed by widening lookback to 10 days and skipping (not faking) symbols with no genuine prior close.

---

## Emergency Rollback

If a deploy breaks something and you need to go back to the previous working commit:
```bash
cd /opt/alpha_scanner
git log --oneline -5          # find the last known-good commit hash
git reset --hard <commit_hash>
sudo systemctl restart alpha_scanner
```


cd /opt/alpha_scanner


git pull origin main
sudo systemctl restart alpha_scanner
sleep 3 && sudo systemctl status alpha_scanner



cd /opt/alpha_scanner
source venv/bin/activate
python3 set_password.py rahul