# Scholarship & Internship Website Watcher (Phase 1 + 2)

Watches a list of websites (NSP, AICTE, UGC, Buddy4Study, …) and sends a
Telegram alert when something new appears — a new scholarship, a scheme going
live, a deadline change, anything that adds keyword-matching text to the page.

## How it works

1. `watcher.py` fetches every site in `sites.json`
2. Extracts the visible text and strips noise (counters, timestamps, ads)
3. Compares with the previous snapshot stored in `state/`
4. New lines that match your keywords → alert to Telegram + `alerts.log`
5. The **first run only saves a baseline** (no alerts) — alerts start from run 2

## Phase 2 features

- **Headless browser** for JavaScript sites (Buddy4Study, PM Internship):
  mark a site with `"renderer": "browser"` in `sites.json`
- **NSP Deep Adapter** (`nsp_adapter.py`): parses every scheme on
  scholarships.gov.in/All-Scholarships into structured records and sends
  *specific* alerts — 🟢 application opened (live!), 🔴 closed, 📅 deadline
  changed, 🆕 new scheme, ❌ scheme removed

## Delivery modes: instant vs digest

In `sites.json` → `settings` → `"delivery_mode"`:

- `"digest"` (current): every run SAVES findings to `pending_alerts.json`
  instead of emailing. One combined email goes out when you (or the
  scheduler) run `python watcher.py --send-digest`. On GitHub Actions this
  happens automatically at **7 PM IST daily**.
- `"instant"`: every finding is emailed the moment it is detected.

Either way, every finding is also written to `alerts.log` immediately.

## Run it locally

```
pip install -r requirements.txt
python -m playwright install chromium
python watcher.py
```

Useful flags:

```
python watcher.py --site NSP       # only sites whose name contains "NSP"
python watcher.py --no-delay      # skip the 2s politeness delay (testing)
```

## Telegram alerts (5-minute setup, free)

1. In Telegram, message **@BotFather** → send `/newbot` → pick a name →
   BotFather gives you a **token** like `123456789:AAH-abc...`
2. Open a chat with your new bot and send it any message (e.g. "hi")
3. Get your **chat id**: open
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
   in a browser and copy the number at `"chat":{"id": ... }`
4. Set both values:
   - **Locally (PowerShell):**
     ```
     $env:TELEGRAM_BOT_TOKEN = "123456789:AAH-abc..."
     $env:TELEGRAM_CHAT_ID  = "123456789"
     python watcher.py
     ```
   - **On GitHub:** repo → Settings → Secrets and variables → Actions →
     add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`

Without Telegram configured, alerts still go to `alerts.log` and the console.

## Email alerts (Gmail, ~5 min setup)

You need an **App Password** — a special 16-character password Google issues
for programs. Your normal Gmail password will NOT work and should never be
put in code.

1. Turn on **2-Step Verification** on your Google account
   (myaccount.google.com → Security)
2. Go to **myaccount.google.com/apppasswords** → create one, name it
   "watcher" → Google shows a 16-character password like `abcd efgh ijkl mnop`
3. Set the values:
   - **Locally (easiest):** open the `.env` file in this folder with Notepad
     and fill in `EMAIL_ADDRESS=` and `EMAIL_APP_PASSWORD=`. Done — the
     watcher reads it automatically. (`.gitignore` keeps this file off
     GitHub, so your password stays on your PC.)
   - **On GitHub (for 24/7 cloud runs):** the `.env` file does NOT get
     uploaded, so set the same values as repo secrets instead:
     repo → Settings → Secrets and variables → Actions →
     add `EMAIL_ADDRESS` and `EMAIL_APP_PASSWORD`
     (optional: `EMAIL_TO` to deliver alerts to a different address)

Other providers work too — set `SMTP_HOST` (default is `smtp.gmail.com`).
You can enable Telegram, email, or **both at once**.

## Run it 24/7 for free (GitHub Actions)

1. Create a **private** GitHub repo and push this folder to it
2. Add the two Telegram secrets (step 4 above)
3. Done — `.github/workflows/watch.yml` runs the watcher every 3 hours and
   commits the updated snapshots back to the repo

To change the schedule, edit the `cron:` line in
`.github/workflows/watch.yml` (e.g. `"0 */2 * * *"` = every 2 hours).

**GitHub can't reach everything.** ~10 of the 30 sites (including NSP
itself) block foreign cloud IPs and always time out from GitHub's US
servers. Those are covered only by the local Task Scheduler runs
(`run_watcher.bat` / `run_digest.bat`), so keep the local tasks healthy.
On a laptop the tasks MUST be allowed to run on battery, or Task
Scheduler silently refuses them with result code `0x800710E0`:

```powershell
foreach ($n in "ScholarshipWatcher Scrape", "ScholarshipWatcher Digest 7PM") {
  $t = Get-ScheduledTask -TaskName $n
  $t.Settings.DisallowStartIfOnBatteries = $false
  $t.Settings.StopIfGoingOnBatteries = $false
  Set-ScheduledTask -InputObject $t
}
```

## Adding / removing websites

Edit `sites.json` — each entry is just:

```json
{ "name": "Some Portal", "url": "https://example.gov.in/scholarships" }
```

Optional per-site `"keywords": [...]` overrides the global keyword list in
`settings`. Sites that block bots or load everything with JavaScript will show
up as **failed** in the run summary — those need a custom adapter (Phase 2).

## Files

| File | Purpose |
|---|---|
| `HOW_IT_WORKS.md` | plain-language explanation of the whole code (start here!) |
| `watcher.py` | the engine |
| `nsp_adapter.py` | NSP structured scheme tracker (Phase 2) |
| `sites.json` | site list + keywords + settings |
| `state/` | last snapshot of each site (auto-created) |
| `alerts.log` | every alert ever sent (auto-created) |
| `.github/workflows/watch.yml` | free 24/7 scheduling |
