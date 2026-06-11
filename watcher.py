"""
Scholarship/Internship Website Watcher - Phase 1
=================================================
Generic change-detection engine:
  1. Fetches every site in sites.json
  2. Extracts the meaningful visible text
  3. Compares with the snapshot from the previous run (stored in state/)
  4. New lines that match your keywords trigger an alert
  5. Alerts go to Telegram (if configured) and alerts.log (always)

Usage:
  python watcher.py              # normal run (first run = baseline only, no alerts)
  python watcher.py --site NSP   # only run sites whose name contains "NSP"
  python watcher.py --no-delay   # skip politeness delay (local testing only)

Alert channels (set one or both; without any, alerts go to alerts.log only):
  Telegram:  TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
  Email:     EMAIL_ADDRESS + EMAIL_APP_PASSWORD (+ optional EMAIL_TO, SMTP_HOST)

New to this code? Read HOW_IT_WORKS.md first - it explains every part
in plain language.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import urllib3
from bs4 import BeautifulSoup

# Some government sites have broken/expired SSL certs; sites.json can opt out
# of verification per-site, so silence the warning that would spam every run.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def load_env_file() -> None:
    """Read settings from the .env file next to this script (if it exists).
    Each line looks like:  EMAIL_ADDRESS=you@gmail.com
    Values typed in the terminal still win over the file."""
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if value and not value.startswith("PUT-") and key not in os.environ:
            os.environ[key] = value


load_env_file()

BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / "state"
ALERT_LOG = BASE_DIR / "alerts.log"
CONFIG_FILE = BASE_DIR / "sites.json"
PENDING_FILE = BASE_DIR / "pending_alerts.json"

# "instant" = email the moment something is found
# "digest"  = save findings all day, send ONE combined email via --send-digest
DELIVERY_MODE = "instant"  # overwritten from sites.json settings in main()

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Tags whose text is almost never a real announcement
STRIP_TAGS = ["script", "style", "noscript", "svg", "iframe", "head"]

# Lines matching these are volatile noise (visitor counters, clocks, etc.)
NOISE_PATTERNS = [
    re.compile(r"^\d[\d,.\s]*$"),                      # bare numbers / counters
    re.compile(r"visitor", re.I),
    re.compile(r"last\s+updated?\s*[:\-]", re.I),
    re.compile(r"\b\d{1,2}:\d{2}(:\d{2})?\s*(am|pm)?\b.*today", re.I),
    re.compile(r"^copyright|^©", re.I),
    # "empty search result" UI messages - not real announcements
    re.compile(r"no matching|could ?n[o']t find|no records? found|no results?", re.I),
    # tutorials, manuals and site furniture - never real scholarship news
    re.compile(r"^how to\b|user manual|guidance video|tutorial|^watch\b|^click here\b", re.I),
    re.compile(r"helpdesk|helpline|^faq[s]?$|^login$|^register$|^home$|^about us$", re.I),
]

# ---------------------------------------------------------------------------
# STRICT "LIVE EVENT" CLASSIFIER
# Only actionable entry points for students may alert:
#   - a fresh application window opening
#   - a newly announced scheme/scholarship/internship
#   - a deadline / last-date extension
#   - a structural change (stipend, eligibility, OTR or other mandatory step)
# Everything else is NOISE and is silently dropped.
# ---------------------------------------------------------------------------

# Stupidity filter: lines matching ANY of these are noise, no matter what
EXCLUSION_RULES = [
    # maintenance / site housekeeping
    re.compile(r"maintenance|under construction|downtime|temporarily unavailable"
               r"|server (down|busy)|website (updated|revamped)", re.I),
    # exams & recruitment - not scholarship entry points
    re.compile(r"answer key|admit card|hall ticket|exam (date|city|result)"
               r"|recruitment|walk-?in|job opening", re.I),
    # closed / inactive listings
    re.compile(r"applications? (are )?closed|no active schemes?|window closed"
               r"|last date (is )?over", re.I),
]

LIVE_EVENT_RULES = [
    ("🟢", "Application window OPEN", re.compile(
        r"\bis live\b|\bnow live\b|\blive now\b|\bopen(ed)? for application"
        r"|\bapplications? (are )?(open|invited|started)\b|\bapply (now|online)\b"
        r"|\bregistrations? (open|started|begins?)\b|\bopen till\b", re.I)),
    ("🆕", "New scheme announced", re.compile(
        r"\b(scholarship|fellowship|internship|scheme)s?\b.*\b20(2[6-9]|[3-9]\d)\b"
        r"|\bnew (scholarship|scheme|internship|fellowship)\b"
        r"|\blaunch(ed|ing)?\b|\bannounc(ed|ing|ement)\b|\bintroduc(ed|ing)\b", re.I)),
    ("📅", "Deadline / last date", re.compile(
        r"\blast date\b|\bdeadline\b|\bextend(ed)?\b|\bclosing date\b"
        r"|\bcloses? on\b|\bapply by\b|अंतिम तिथि", re.I)),
    ("⚙️", "Rule / stipend change", re.compile(
        r"\bstipend\b|\beligibilit(y|ies)\b|\bOTR\b|\bone[- ]time registration\b"
        r"|\bmandatory\b|\bamount (increased|revised|enhanced)\b"
        r"|\brevised (guidelines|amount|rate)\b", re.I)),
]


def is_old_cycle(line: str) -> bool:
    """A line whose newest mentioned year is before 2026 is old news."""
    years = [int(y) for y in re.findall(r"\b(20\d\d)\b", line)]
    return bool(years) and max(years) < 2026


def classify_line(line: str) -> tuple[str, str] | None:
    """Return (emoji, label) for a Live Event, or None if the line is noise."""
    if any(p.search(line) for p in EXCLUSION_RULES):
        return None
    if is_old_cycle(line):
        return None
    for emoji, label, pattern in LIVE_EVENT_RULES:
        if pattern.search(line):
            return emoji, label
    return None  # no live event signal -> noise


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:80]


def load_config() -> dict:
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


class _LegacySSLAdapter(requests.adapters.HTTPAdapter):
    """Lets us talk to very old government servers whose SSL is so outdated
    that modern Python refuses the connection by default."""

    def init_poolmanager(self, *args, **kwargs):
        import ssl
        ctx = ssl.create_default_context()
        ctx.options |= 0x4  # OP_LEGACY_SERVER_CONNECT
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


def fetch(url: str, timeout: int, verify_ssl: bool = True, retries: int = 2,
          legacy_ssl: bool = False) -> str:
    """Download a page. Retries once after a short pause, because government
    sites often fail for a few seconds and then work again."""
    session = requests.Session()
    if legacy_ssl:
        session.mount("https://", _LegacySSLAdapter())
        verify_ssl = False
    last_error = None
    for attempt in range(retries):
        try:
            resp = session.get(
                url,
                headers={"User-Agent": USER_AGENT, "Accept-Language": "en-IN,en;q=0.9"},
                timeout=timeout,
                verify=verify_ssl,
            )
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            last_error = e
            if attempt < retries - 1:
                time.sleep(5)
    raise last_error


# --- Headless browser support (Phase 2) -------------------------------------
# Sites marked "renderer": "browser" in sites.json are JavaScript apps whose
# content only exists after the page runs in a real browser.
_BROWSER = None
_PLAYWRIGHT = None


def fetch_browser(url: str, timeout: int) -> str:
    global _BROWSER, _PLAYWRIGHT
    from playwright.sync_api import sync_playwright

    if _BROWSER is None:
        _PLAYWRIGHT = sync_playwright().start()
        _BROWSER = _PLAYWRIGHT.chromium.launch(headless=True)

    page = _BROWSER.new_page(user_agent=USER_AGENT)
    try:
        page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
        page.wait_for_timeout(6000)  # let client-side data requests finish
        return page.content()
    finally:
        page.close()


def shutdown_browser() -> None:
    global _BROWSER, _PLAYWRIGHT
    if _BROWSER is not None:
        _BROWSER.close()
        _BROWSER = None
    if _PLAYWRIGHT is not None:
        _PLAYWRIGHT.stop()
        _PLAYWRIGHT = None


def extract_lines(html: str, base_url: str = "") -> tuple[list[str], dict[str, str]]:
    """Turn a page into clean text lines, plus a map of line -> link.

    The link map remembers which URL each clickable text pointed to, so an
    alert can show "Scholarship name" together with its direct link."""
    from urllib.parse import urljoin

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(STRIP_TAGS):
        tag.decompose()

    # Remember the destination of every clickable text on the page
    links: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        text = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
        href = a["href"].strip()
        if len(text) >= 4 and href and not href.lower().startswith("javascript"):
            links.setdefault(text.lower(), urljoin(base_url, href))

    lines = []
    seen = set()
    for raw in soup.get_text("\n").splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if len(line) < 4:
            continue
        if any(p.search(line) for p in NOISE_PATTERNS):
            continue
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        lines.append(line)
    return lines, links


def load_state(slug: str) -> dict | None:
    path = STATE_DIR / f"{slug}.json"
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def save_state(slug: str, lines: list[str]) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    payload = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "hash": hashlib.sha256("\n".join(lines).encode()).hexdigest(),
        "lines": lines,
    }
    with open(STATE_DIR / f"{slug}.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)


def keyword_hits(lines: list[str], keywords: list[str]) -> list[str]:
    kws = [k.lower() for k in keywords]
    return [ln for ln in lines if any(k in ln.lower() for k in kws)]


def send_telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    # Telegram caps messages at 4096 chars
    text = text[:4000]
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=20,
        )
        return resp.status_code == 200
    except requests.RequestException as e:
        print(f"  ! Telegram send failed: {e}")
        return False


def send_email(subject: str, text: str, html: str | None = None) -> bool:
    """Send the alert as an email. Needs two environment variables:
      EMAIL_ADDRESS       your email (e.g. you@gmail.com)
      EMAIL_APP_PASSWORD  an 'app password' (NOT your normal password!)
    Optional:
      EMAIL_TO            who receives the alert (default: send to yourself)
      SMTP_HOST           mail server (default: smtp.gmail.com)
    """
    import smtplib
    from email.mime.text import MIMEText

    address = os.environ.get("EMAIL_ADDRESS")
    # Google displays app passwords with spaces ("abcd efgh ...") - remove them
    password = os.environ.get("EMAIL_APP_PASSWORD", "").replace(" ", "")
    if not address or not password:
        return False
    to = os.environ.get("EMAIL_TO", address)
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")

    if html:
        from email.mime.multipart import MIMEMultipart
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(text, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))
    else:
        msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = address
    msg["To"] = to
    try:
        with smtplib.SMTP_SSL(host, 465, timeout=30) as server:
            server.login(address, password)
            server.sendmail(address, [to], msg.as_string())
        return True
    except Exception as e:
        print(f"  ! email send failed: {e}")
        return False


def log_alert(text: str) -> None:
    with open(ALERT_LOG, "a", encoding="utf-8") as f:
        f.write(f"\n{'=' * 60}\n{datetime.now().isoformat()}\n{text}\n")


def queue_alert(subject: str, text: str, html: str | None) -> None:
    """Digest mode: save the finding to pending_alerts.json instead of
    emailing right away. --send-digest delivers everything in one email."""
    pending = []
    if PENDING_FILE.exists():
        try:
            with open(PENDING_FILE, encoding="utf-8") as f:
                pending = json.load(f)
        except (json.JSONDecodeError, OSError):
            pending = []
    pending.append({
        "found_at": datetime.now(timezone.utc).isoformat(),
        "subject": subject,
        "text": text,
        "html": html or "",
    })
    with open(PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump(pending, f, ensure_ascii=False, indent=1)


def alert(text: str, html: str | None = None, subject: str | None = None) -> None:
    """Handle one finding. Instant mode: send through every configured
    channel now. Digest mode: save it for the evening digest email.
    The log file ALWAYS gets it immediately, so nothing is ever lost."""
    subject = subject or (text.splitlines()[0][:80] if text else "Website alert")
    log_alert(text)

    if DELIVERY_MODE == "digest":
        queue_alert(subject, text, html)
        print("  >> ALERT saved for evening digest")
        return

    channels = []
    if send_telegram(text):
        channels.append("telegram")
    if send_email(f"[Watcher] {subject}", text, html):
        channels.append("email")
    where = " + ".join(channels) if channels else "log only - no channel configured"
    print(f"  >> ALERT ({where})")


def send_digest() -> int:
    """Send ONE combined email with everything found since the last digest,
    then clear the pending list. Returns 0 on success."""
    pending = []
    if PENDING_FILE.exists():
        try:
            with open(PENDING_FILE, encoding="utf-8") as f:
                pending = json.load(f)
        except (json.JSONDecodeError, OSError):
            pending = []

    today = datetime.now().strftime("%d %b %Y")
    if not pending:
        print("Digest: nothing found today - sending short 'all quiet' mail")
        ok = send_email(
            f"[Watcher] Daily digest {today}: no new scholarship events",
            "All watched websites were checked today. "
            "No new scholarships, internships or deadline changes were found.",
        )
        send_telegram(f"📭 Daily digest {today}: no new scholarship events.")
        return 0 if ok else 1

    text = f"🎓 Daily Scholarship Digest — {today}\n" \
           f"{len(pending)} update(s) found today:\n\n"
    text += "\n\n========================\n\n".join(p["text"] for p in pending)

    html_parts = [p["html"] for p in pending if p["html"]]
    html = None
    if html_parts:
        html = (f"<h1 style='font-family:Arial'>🎓 Daily Scholarship Digest — {today}</h1>"
                f"<p style='font-family:Arial'>{len(pending)} update(s) found today</p><hr>"
                + "<hr>".join(html_parts))

    ok = send_email(
        f"[Watcher] 🎓 Daily digest {today}: {len(pending)} scholarship update(s)",
        text, html,
    )
    send_telegram(text)
    if ok:
        PENDING_FILE.unlink(missing_ok=True)
        print(f"Digest sent: {len(pending)} update(s). Pending list cleared.")
        return 0
    print("Digest email FAILED - pending list kept, will retry next digest.")
    return 1


def check_site(site: dict, settings: dict, no_delay: bool) -> dict:
    """Returns {'status': 'ok'|'baseline'|'changed'|'error', ...}"""
    name, url = site["name"], site["url"]
    slug = slugify(name)
    print(f"- {name}")

    try:
        if site.get("renderer") == "browser":
            html = fetch_browser(url, settings.get("timeout_seconds", 30) + 15)
        else:
            html = fetch(url, settings.get("timeout_seconds", 30),
                         verify_ssl=site.get("verify_ssl", True),
                         legacy_ssl=site.get("legacy_ssl", False))
    except Exception as e:
        print(f"  ! fetch failed: {e}")
        return {"status": "error", "name": name, "error": str(e)}

    lines, links = extract_lines(html, base_url=url)
    if not lines:
        return {"status": "error", "name": name, "error": "page produced no readable text"}

    prev = load_state(slug)
    save_state(slug, lines)

    if prev is None:
        print(f"  baseline saved ({len(lines)} lines)")
        return {"status": "baseline", "name": name}

    old_set = {ln.lower() for ln in prev.get("lines", [])}
    added = [ln for ln in lines if ln.lower() not in old_set]
    if not added:
        print("  no change")
        return {"status": "ok", "name": name}

    keywords = site.get("keywords") or settings.get("keywords", [])
    relevant = keyword_hits(added, keywords) if keywords else added

    # Strict gate: only Live Events survive; everything else is noise
    items = []
    for ln in relevant:
        event = classify_line(ln)
        if event is None:
            continue
        emoji, label = event
        items.append({"emoji": emoji, "label": label, "line": ln,
                      "link": links.get(ln.lower())})
    order = {"Application window OPEN": 0, "New scheme announced": 1,
             "Deadline / last date": 2, "Rule / stipend change": 3}
    items.sort(key=lambda it: order.get(it["label"], 9))

    print(f"  changed: {len(added)} new lines, {len(items)} live events")
    if not items:
        return {"status": "ok", "name": name}

    shown = items[:15]
    text_parts = []
    for it in shown:
        entry = f"{it['emoji']} [{it['label']}] {it['line'][:200]}"
        if it["link"] and it["link"].rstrip("/") != url.rstrip("/"):
            entry += f"\n   👉 {it['link']}"
        text_parts.append(entry)
    more = f"\n\n…and {len(items) - 15} more" if len(items) > 15 else ""
    text = (f"🔔 {name}\n\n" + "\n\n".join(text_parts) + more
            + f"\n\nSource page: {url}")

    rows = []
    for it in shown:
        link_cell = (f'<a href="{it["link"]}">Open&nbsp;➜</a>'
                     if it["link"] and it["link"].rstrip("/") != url.rstrip("/")
                     else "—")
        rows.append(
            f"<tr><td style='white-space:nowrap'>{it['emoji']} {it['label']}</td>"
            f"<td>{it['line'][:250]}</td><td>{link_cell}</td></tr>")
    html = f"""
    <h2 style="margin-bottom:4px">🔔 {name}</h2>
    <table border="1" cellpadding="8" cellspacing="0"
           style="border-collapse:collapse;font-family:Arial,sans-serif;font-size:14px">
      <tr style="background:#f0f0f0">
        <th align="left">Type</th><th align="left">Update</th><th align="left">Link</th>
      </tr>
      {''.join(rows)}
    </table>
    <p style="font-family:Arial,sans-serif;font-size:13px">
      {f"…and {len(items) - 15} more changes<br>" if len(items) > 15 else ""}
      Source page: <a href="{url}">{url}</a>
    </p>"""

    top = shown[0]
    subject = f"{top['emoji']} {name}: {top['line'][:60]}"
    alert(text, html=html, subject=subject)
    return {"status": "changed", "name": name, "new_lines": len(items)}


def main() -> int:
    global DELIVERY_MODE
    parser = argparse.ArgumentParser(description="Website change watcher")
    parser.add_argument("--site", help="only check sites whose name contains this text")
    parser.add_argument("--no-delay", action="store_true", help="skip politeness delay")
    parser.add_argument("--send-digest", action="store_true",
                        help="send the daily digest email of everything found, then exit")
    args = parser.parse_args()

    config = load_config()
    settings = config.get("settings", {})
    DELIVERY_MODE = settings.get("delivery_mode", "instant")

    if args.send_digest:
        return send_digest()
    sites = config.get("sites", [])
    run_nsp_adapter = not args.site or "nsp" in args.site.lower()
    if args.site:
        sites = [s for s in sites if args.site.lower() in s["name"].lower()]
        if not sites and not run_nsp_adapter:
            print(f"No site matches '{args.site}'")
            return 1

    delay = 0 if args.no_delay else settings.get("request_delay_seconds", 2)
    print(f"Checking {len(sites)} site(s)...\n")

    results = []
    try:
        for i, site in enumerate(sites):
            results.append(check_site(site, settings, args.no_delay))
            if delay and i < len(sites) - 1:
                time.sleep(delay)
    finally:
        shutdown_browser()

    # Phase 2: NSP deep adapter (structured scheme tracking)
    if run_nsp_adapter:
        import nsp_adapter
        results.append(nsp_adapter.run(STATE_DIR, alert))

    errors = [r for r in results if r["status"] == "error"]
    changed = [r for r in results if r["status"] == "changed"]
    baselines = [r for r in results if r["status"] == "baseline"]

    print(f"\nDone. {len(results)} checked | {len(changed)} alerted | "
          f"{len(baselines)} baselined | {len(errors)} failed")

    # Health report: only nag about failures when something actually failed
    if errors:
        report = "\n".join(f"• {e['name']}: {e['error'][:120]}" for e in errors)
        print(f"\nFailed sites:\n{report}")
        # Send a health warning only if MANY sites fail (likely systemic)
        if len(errors) >= max(3, len(results) // 3):
            health = f"⚠️ Watcher health: {len(errors)}/{len(results)} sites failed:\n{report[:3000]}"
            send_telegram(health)
            send_email("[Watcher] Health warning", health)

    return 0


if __name__ == "__main__":
    sys.exit(main())
