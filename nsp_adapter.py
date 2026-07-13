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
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

URL = "https://scholarships.gov.in/All-Scholarships"
APPLY_URL = "https://scholarships.gov.in/ApplicationForm/"
OTR_URL = "https://scholarships.gov.in/otrapplication/#/login-page"
STATE_FILE = "nsp-schemes.json"
ALERTED_FILE = "nsp-alerted.json"
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
                    "url": "",
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

    # Second pass: attach each scheme's own link (its guidelines PDF).
    # Every scheme card has a "Specifications" anchor; walking up from it,
    # the first ancestor whose text contains a scheme name is that card.
    name_in_text_re = re.compile(r"([^|]{20,200}?\([^()]*Based Scheme\))", re.I)
    lower_keys = {k.lower(): k for k in schemes}
    for a in soup.find_all("a", href=True):
        if a.get_text(strip=True).lower() != "specifications":
            continue
        href = a["href"].strip()
        # the portal itself has a few href="null" placeholders - skip those
        if not href or href.lower() in ("null", "#") \
                or href.lower().startswith("javascript"):
            continue
        node = a
        for _ in range(8):
            node = node.parent
            if node is None:
                break
            m = name_in_text_re.search(
                re.sub(r"\s+", " ", node.get_text(" ", strip=True)))
            if m:
                found = m.group(1).strip().lower()
                key = lower_keys.get(found) or next(
                    (k for lk, k in lower_keys.items()
                     if lk.endswith(found) or found in lk), None)
                if key is not None and not schemes[key]["url"]:
                    schemes[key]["url"] = urljoin(URL, href)
                break

    return schemes


def deadline_passed(rec: dict) -> bool:
    """True when the 'apply till' date is already in the past - a leftover
    listing from the previous academic year, not a live opportunity."""
    m = re.match(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", rec.get("app_date", ""))
    if not m:
        return False
    day, month, year = map(int, m.groups())
    try:
        return datetime(year, month, day).date() < datetime.now().date()
    except ValueError:
        return False


def diff_schemes(old: dict[str, dict], new: dict[str, dict]) -> list[str]:
    """Deadline changes only. New / newly-opened schemes are handled by the
    never-alert-twice logic in run(), so they are NOT reported here."""
    events = []
    for name, rec in new.items():
        if name not in old:
            continue
        label = f"{name} [{rec['ministry']}]" if rec["ministry"] else name
        link = f"\n   👉 {rec['url']}" if rec.get("url") else ""
        o = old[name]
        if o.get("app_status") == rec["app_status"] == "OPEN" \
                and o.get("app_date") != rec["app_date"] and rec["app_date"] \
                and not deadline_passed(rec):
            events.append(
                f"📅 Deadline changed: {label} — was {o.get('app_date') or '?'}, "
                f"now {rec['app_date']}{link}"
            )
    return events


def _scheme_key(name: str, rec: dict) -> str:
    """Identity of one scholarship CYCLE. The year of the opening date is
    part of the key, so the same scheme alerts again next academic year
    but never twice within the same cycle."""
    cycle = (rec.get("open_from") or rec.get("app_date") or "")[-4:]
    return f"{name.lower()}|{cycle}"


def _load_alerted(state_dir: Path) -> dict:
    path = state_dir / ALERTED_FILE
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_alerted(state_dir: Path, alerted: dict) -> None:
    state_dir.mkdir(exist_ok=True)
    # prune entries older than ~14 months so the file never grows forever
    cutoff = datetime.now(timezone.utc).timestamp() - 425 * 86400
    alerted = {k: v for k, v in alerted.items()
               if datetime.fromisoformat(v).timestamp() >= cutoff}
    with open(state_dir / ALERTED_FILE, "w", encoding="utf-8") as f:
        json.dump(alerted, f, ensure_ascii=False, indent=1)


def verify_link(url: str, timeout: int = 15) -> bool:
    """True when the link actually resolves (status < 400). Dead or fake
    links are replaced with the portal page before the alert goes out."""
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT},
                            timeout=timeout, stream=True)
        ok = resp.status_code < 400
        resp.close()
        return ok
    except requests.RequestException:
        return False


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

    open_now = [(n, d) for n, d in schemes.items()
                if d.get("app_status") == "OPEN" and not deadline_passed(d)]

    # Never alert the same scholarship cycle twice: only schemes that are
    # live now AND have never been emailed before make it into the alert.
    alerted = _load_alerted(state_dir)
    fresh = [(n, d) for n, d in open_now if _scheme_key(n, d) not in alerted]

    events = diff_schemes(old, schemes) if old else []
    if events:
        print(f"  {len(events)} deadline change(s) detected")
        body = "\n\n".join(events[:20])
        more = f"\n\n…and {len(events) - 20} more events" if len(events) > 20 else ""
        alert_fn(f"🎓 NSP Scheme Update\n{URL}\n\n{body}{more}")

    if fresh:
        print(f"  {len(fresh)} NEW live scholarship(s), verifying links...")
        _alert_new_open(fresh, alert_fn)
        now = datetime.now(timezone.utc).isoformat()
        for n, d in fresh:
            alerted[_scheme_key(n, d)] = now
        _save_alerted(state_dir, alerted)
    else:
        print(f"  {len(open_now)} open, 0 new (all alerted before), "
              f"{len(events)} deadline change(s)")

    status = "changed" if (fresh or events) else ("baseline" if not old else "ok")
    return {"status": status, "name": "NSP Deep Adapter",
            "new_lines": len(fresh) + len(events)}


def _alert_new_open(open_now: list, alert_fn) -> None:
    """Send one alert listing NEW live scholarships, each with its deadline
    and a VERIFIED link (dead links fall back to the portal page)."""
    if not open_now:
        return
    ranked = sorted(open_now, key=lambda x: x[1].get("app_date", ""))
    shown = ranked[:25]

    # Authenticity check: only include links that actually resolve.
    checked: dict[str, bool] = {}
    for _, d in shown:
        u = d.get("url")
        if u and u not in checked:
            checked[u] = verify_link(u)
    for _, d in shown:
        if d.get("url") and not checked.get(d["url"], False):
            d["url"] = ""  # dead link - the alert falls back to the portal URL

    lines = []
    for n, d in shown:
        label = f"{n} [{d['ministry']}]" if d.get("ministry") else n
        when = f" → apply by {d['app_date']}" if d.get("app_date") else ""
        entry = f"🟢 {label}{when}\n   👉 Apply: {APPLY_URL}"
        if d.get("url"):
            entry += f"\n   📄 Guidelines: {d['url']}"
        lines.append(entry)
    body = "\n\n".join(lines)
    more = f"\n…and {len(open_now) - 25} more" if len(open_now) > 25 else ""

    rows = []
    for n, d in shown:
        guide = (f'<a href="{d["url"]}" style="font-size:11px;color:#888">'
                 f'guidelines</a>' if d.get("url") else "")
        rows.append(
            "<tr>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #eee'>{n}"
            f"<br>{guide}</td>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #eee;color:#555'>"
            f"{d.get('ministry', '')}</td>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #eee;"
            f"white-space:nowrap;font-weight:700'>{d.get('app_date') or '—'}</td>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #eee'>"
            f"<a href='{APPLY_URL}' style='background:#1a6e3c;color:#fff;"
            f"padding:6px 14px;border-radius:4px;text-decoration:none;"
            f"font-weight:700;white-space:nowrap'>Apply&nbsp;➜</a></td>"
            "</tr>")
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:760px">
      <h2 style="margin-bottom:4px">🆕 {len(open_now)} NEW Scholarship(s) LIVE on NSP</h2>
      <p style="font-size:13px;margin-top:0">
        Hit <strong>Apply</strong> to go straight to the NSP application form.
        New to NSP? Complete
        <a href="{OTR_URL}">One Time Registration (OTR)</a> first.</p>
      <table cellpadding="0" cellspacing="0"
             style="border:1px solid #e0e0e0;border-collapse:collapse;font-size:13px">
        <tr style="background:#f0f0f0">
          <th style="padding:10px 12px;text-align:left">Scheme Name</th>
          <th style="padding:10px 12px;text-align:left">Ministry / Dept</th>
          <th style="padding:10px 12px;text-align:left">Apply By</th>
          <th style="padding:10px 12px;text-align:left">Apply</th>
        </tr>
        {''.join(rows)}
      </table>
      {f"<p style='font-size:12px'>…and {len(open_now) - 25} more</p>" if len(open_now) > 25 else ""}
    </div>"""

    alert_fn(
        f"🆕 {len(open_now)} NEW scholarship(s) LIVE on NSP\n{URL}\n\n"
        + body + more,
        html=html,
    )
