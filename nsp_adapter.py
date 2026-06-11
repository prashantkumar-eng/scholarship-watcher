"""
NSP Deep Adapter - Phase 2
==========================
Parses https://scholarships.gov.in/All-Scholarships into structured scheme
records and raises *specific* alerts:

  - NEW scheme appeared on NSP
  - Scheme application OPENED (the "it's live!" moment)
  - Scheme application CLOSED
  - Deadline changed / extended
  - Scheme removed from the portal

State lives in state/nsp-schemes.json. First run saves a baseline silently.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

URL = "https://scholarships.gov.in/All-Scholarships"
STATE_FILE = "nsp-schemes.json"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

SCHEME_NAME_RE = re.compile(r"\(.*Based Scheme\)\s*$", re.I)
OPEN_FROM_RE = re.compile(r"Scheme\s+Open\s+from\s*:?\s*([\d\-/]+)", re.I)
APP_STATUS_RE = re.compile(
    r"Student\s+Application\s*(Open\s+till|Closed\s+on|:?\s*NOT\s+YET\s+OPENED)\s*:?\s*([\d\-/]*)",
    re.I,
)
MINISTRY_HINT_RE = re.compile(
    r"^(Ministry|Department|All India|Council|.*Commission|.*Aayog)", re.I
)


def fetch_schemes(timeout: int = 40) -> dict[str, dict]:
    """Returns {scheme_name: {ministry, open_from, app_status, app_date}}"""
    resp = requests.get(URL, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    lines = [re.sub(r"\s+", " ", l).strip()
             for l in soup.get_text("\n").splitlines()]
    lines = [l for l in lines if l]

    schemes: dict[str, dict] = {}
    ministry = ""
    current: dict | None = None

    for line in lines:
        if SCHEME_NAME_RE.search(line) and len(line) > 25:
            name = re.sub(r"\s+", " ", line).strip()
            if name not in schemes:
                schemes[name] = {
                    "ministry": ministry,
                    "open_from": "",
                    "app_status": "",
                    "app_date": "",
                }
            current = schemes[name]
            continue

        m = OPEN_FROM_RE.search(line)
        if m and current is not None and not current["open_from"]:
            current["open_from"] = m.group(1)

        m = APP_STATUS_RE.search(line)
        if m and current is not None and not current["app_status"]:
            status_word = m.group(1).strip().lower()
            if "not yet" in status_word:
                current["app_status"] = "NOT YET OPENED"
            elif "open" in status_word:
                current["app_status"] = "OPEN"
            else:
                current["app_status"] = "CLOSED"
            current["app_date"] = m.group(2).strip()

        if MINISTRY_HINT_RE.match(line) and len(line) < 100 \
                and not SCHEME_NAME_RE.search(line):
            ministry = line

    return schemes


def diff_schemes(old: dict[str, dict], new: dict[str, dict]) -> list[str]:
    """Only actionable entry points alert: a scheme opening, a new scheme,
    or a deadline change. Closures/removals are noise (nothing to apply to)."""
    events = []
    for name, rec in new.items():
        label = f"{name} [{rec['ministry']}]" if rec["ministry"] else name
        if name not in old:
            status = f" — application {rec['app_status']}" if rec["app_status"] else ""
            when = f" till {rec['app_date']}" if rec["app_status"] == "OPEN" and rec["app_date"] else ""
            events.append(f"🆕 NEW scheme on NSP: {label}{status}{when}")
            continue
        o = old[name]
        if o.get("app_status") != rec["app_status"]:
            if rec["app_status"] == "OPEN":
                events.append(
                    f"🟢 APPLICATION OPEN (live now!): {label}"
                    + (f" — apply till {rec['app_date']}" if rec["app_date"] else "")
                )
            # CLOSED / NOT YET OPENED transitions: not entry points, skip
        elif rec["app_status"] == "OPEN" and o.get("app_date") != rec["app_date"] \
                and rec["app_date"]:
            events.append(
                f"📅 Deadline changed: {label} — was {o.get('app_date') or '?'}, "
                f"now {rec['app_date']}"
            )
    return events


def run(state_dir: Path, alert_fn) -> dict:
    """Check NSP schemes. alert_fn(text) is called for each alert message.
    Returns a status dict like watcher.check_site()."""
    print("- NSP Deep Adapter (All-Scholarships)")
    try:
        schemes = fetch_schemes()
    except requests.RequestException as e:
        print(f"  ! fetch failed: {e}")
        return {"status": "error", "name": "NSP Deep Adapter", "error": str(e)}

    if len(schemes) < 5:
        msg = f"only parsed {len(schemes)} schemes - page layout may have changed"
        print(f"  ! {msg}")
        return {"status": "error", "name": "NSP Deep Adapter", "error": msg}

    state_path = state_dir / STATE_FILE
    old = {}
    if state_path.exists():
        try:
            with open(state_path, encoding="utf-8") as f:
                old = json.load(f).get("schemes", {})
        except (json.JSONDecodeError, OSError):
            old = {}

    state_dir.mkdir(exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(
            {"checked_at": datetime.now(timezone.utc).isoformat(), "schemes": schemes},
            f, ensure_ascii=False, indent=1,
        )

    if not old:
        print(f"  baseline saved ({len(schemes)} schemes)")
        return {"status": "baseline", "name": "NSP Deep Adapter"}

    events = diff_schemes(old, schemes)
    if not events:
        print(f"  no change ({len(schemes)} schemes tracked)")
        return {"status": "ok", "name": "NSP Deep Adapter"}

    print(f"  {len(events)} scheme event(s) detected")
    body = "\n\n".join(events[:20])
    more = f"\n\n…and {len(events) - 20} more events" if len(events) > 20 else ""
    alert_fn(f"🎓 NSP Scheme Update\n{URL}\n\n{body}{more}")
    return {"status": "changed", "name": "NSP Deep Adapter", "new_lines": len(events)}
