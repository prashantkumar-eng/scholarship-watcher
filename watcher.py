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
import html as html_lib
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
import urllib3
from bs4 import BeautifulSoup

# Some third-party errors contain Unicode box characters that Windows' legacy
# console encoding cannot print. Replace only unsupported console characters.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

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
SENT_FILE = STATE_DIR / "sent-alerts.json"
ALERT_LOCK = threading.Lock()

# "instant" = email the moment something is found
# "digest"  = save findings all day, send ONE combined email via --send-digest
DELIVERY_MODE = "instant"  # overwritten from sites.json settings in main()
DEFAULT_EMAIL_TO = "prashant.kumar@buddy4study.com"

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
               r"|recruitment|walk-?in|job opening|vacanc(?:y|ies)|tender"
               r"|entrance exam|counselling|result declared", re.I),
    # closed / inactive listings
    re.compile(r"applications? (are )?closed|no active schemes?|window closed"
               r"|last date (is )?over", re.I),
]

TOPIC_RE = re.compile(
    r"\bscholarships?\b|\binternships?\b|\bfellowships?\b"
    r"|\bgrants?\b|\bfinancial aid\b|\bstipend\b"
    r"|छात्रवृत्ति|इंटर्नशिप", re.I
)
CATALOG_EXCLUSION_RE = re.compile(
    r"\b(result|results|final list|provisional list|selected candidates?"
    r"|awardees?|winners?|success stor(?:y|ies)|closed|expired|archive"
    r"|answer key|admit card|recruitment|vacanc(?:y|ies)|jobs?)\b"
    r"|^(latest |all |explore |list of |apply for )?"
    r"(scholarships?|internships?|fellowships?|grants?)(,? stipend)?$"
    r"|^(see all|about)\s+(scholarships?|internships?|fellowships?|grants?)$"
    r"|\b(ways to find scholarships?|categories of scholarships?"
    r"|scholarship admission test|above scholarships?)\b",
    re.I,
)
CATALOG_BODY_RE = re.compile(
    r"\b(applicants?|students?|candidates?|selected fellows?|scholarship amount"
    r"|can apply|must apply|will receive|will be awarded|eligible for"
    r"|eligibility criteria|committee|tuition fee|terms and conditions"
    r"|in order to|please share|reserves? the right|based on)\b",
    re.I,
)
CATEGORY_RULES = (
    ("Internship", re.compile(r"\binternships?\b|इंटर्नशिप", re.I)),
    ("Fellowship", re.compile(r"\bfellowships?\b", re.I)),
    ("Grant", re.compile(r"\bgrants?\b|\bfinancial aid\b", re.I)),
    ("Scholarship", re.compile(r"\bscholarships?\b|छात्रवृत्ति", re.I)),
)
TRUSTED_DOMAINS = {
    "aicte-india.org", "azimpremjifoundation.org", "bevicascholarship.dk",
    "britishcouncil.in", "centralcoalfields.in", "chevening.org", "cummins.com",
    "daad.de", "epfl.ch", "fulbrightonline.org", "icgeb.org", "iiap.res.in",
    "iwm.at", "kcmet.org", "mitacs.ca", "naeducation.org", "pmrf.in",
    "rbi.org.in", "savethechildren.org", "sbifoundation.info",
    "scholarships.gov.in", "swamidayanand.org", "twas.org", "ugc.gov.in",
    "un.org",
}
TRUSTED_SUFFIXES = (
    ".gov.in", ".nic.in", ".ac.in", ".edu.in", ".gov", ".edu",
    ".ac.uk", ".gov.uk", ".gc.ca", ".gov.au", ".edu.au", ".ac.th", ".res.in",
)

LIVE_EVENT_RULES = [
    ("🟢", "Application window OPEN", re.compile(
        r"\bis live\b|\bnow live\b|\blive now\b|\bopen(ed)? for application"
        r"|\bapplications? (are )?(now )?(open|invited|started|accepted)\b"
        r"|\bapply (now|online)\b"
        r"|\bregistrations? (open|started|begins?|starts?)\b"
        r"|\bregistration (starts?|begins?|open) (from|on)\b"
        r"|\bopen till\b|\bopen from\b"
        r"|\bapplication(s)? (window|portal|form|link) (is |are )?(now )?open"
        r"|\binvit(ed|ing) applications?\b|\bonline applications? (are )?invited\b"
        r"|\bapplications? (are )?accepted\b|\bapply (at|through|via)\b"
        r"|\bportal (is |are |now |is now )?(open|live)\b|\bfresh application\b|\brenewal application\b"
        # Hindi
        r"|आवेदन (आमंत्रित|शुरू|खुले|खुला)|पंजीकरण (खुला|शुरू)|अभी आवेदन करें", re.I)),
    ("🆕", "New scheme announced", re.compile(
        r"\b(scholarship|fellowship|internship|scheme)s?\b.*\b20(2[6-9]|[3-9]\d)\b"
        r"|\bnew (scholarship|scheme|internship|fellowship)\b"
        r"|\blaunch(ed|ing)?\b|\bannounc(ed|ing|ement)\b|\bintroduc(ed|ing)\b"
        r"|\bfor (the year|academic year|session) 20(2[6-9]|[3-9]\d)\b"
        r"|\bnotification (for|of|regarding)\b.*\b(scholarship|fellowship|internship)\b"
        r"|\bcircular\b.*\b(scholarship|fellowship|scheme|portal|open)\b"
        r"|\b(scholarship|fellowship|scheme|portal)\b.*\bcircular\b"
        r"|\badvt\.?\b|\badvertisement\b"
        # Hindi
        r"|नई (योजना|छात्रवृत्ति)|छात्रवृत्ति (शुरू|घोषित|लॉन्च)|(योजना|छात्रवृत्ति) 20(2[6-9]|[3-9]\d)", re.I)),
    ("📅", "Deadline / last date", re.compile(
        r"\blast date\b|\bdeadline\b|\bextend(ed)?\b|\bclosing date\b"
        r"|\bcloses? on\b|\bapply by\b"
        r"|\bapply (by|before)\b|\bsubmit (by|before|on or before)\b|\bdue (by|date)\b"
        r"|\blast date (of|for) (submission|application|registration)\b"
        r"|\bopen (till|until|upto|up to)\b|\bopen from .* to \b"
        r"|\bdate of (application|submission|registration)\b"
        # Hindi
        r"|अंतिम तिथि|अंतिम (तिथि|दिनांक)|तिथि (बढ़ाई|विस्तार)", re.I)),
    ("⚙️", "Rule / stipend change", re.compile(
        r"\bstipend\b|\beligibilit(y|ies)\b|\bOTR\b|\bone[- ]time registration\b"
        r"|\bmandatory\b|\bamount (increased|revised|enhanced)\b"
        r"|\brevised (guidelines|amount|rate)\b|\bincome (limit|ceiling)\b"
        r"|\b₹\s*[\d,]+\b.*\b(scholar|stipend|fellow|month|year|annum)\b"
        r"|\b(scholar|stipend|fellow)\b.*\b₹\s*[\d,]+\b"
        # Hindi
        r"|छात्रवृत्ति (राशि|नियम|पात्रता)|अनिवार्य|पात्रता (मानदंड|शर्त)", re.I)),
]


def is_old_cycle(line: str) -> bool:
    """A line whose newest mentioned year is before this year is old news."""
    years = [int(y) for y in re.findall(r"\b(20\d\d)\b", line)]
    return bool(years) and max(years) < datetime.now().year


def classify_line(line: str, topic_context: str = "") -> tuple[str, str, int] | None:
    """Return a high-confidence scholarship/internship event, otherwise None."""
    if any(p.search(line) for p in EXCLUSION_RULES):
        return None
    if is_old_cycle(line):
        return None
    topic_match = TOPIC_RE.search(f"{line} {topic_context}")
    if not topic_match:
        return None
    for emoji, label, pattern in LIVE_EVENT_RULES:
        if pattern.search(line):
            confidence = 2  # topic + actionable event
            if re.search(r"\b20\d\d\b|\b\d{1,2}[-/ ]\w*[-/ ]20\d\d\b|₹", line):
                confidence += 1
            return emoji, label, confidence
    return None  # no live event signal -> noise


def extract_deadline(text: str) -> str:
    """Return the first recognizable deadline in YYYY-MM-DD format."""
    patterns = (
        (r"\b(20\d{2}-\d{2}-\d{2})\b", "%Y-%m-%d"),
        (r"\b(\d{1,2}[/-]\d{1,2}[/-]20\d{2})\b", None),
        (r"\b(\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+\s+20\d{2})\b", None),
        (r"\b([A-Za-z]+\s+\d{1,2},?\s+20\d{2})\b", None),
    )
    for pattern, date_format in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        value = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", match.group(1), flags=re.I)
        formats = ([date_format] if date_format else
                   ["%d/%m/%Y", "%d-%m-%Y", "%d %B %Y", "%d %b %Y",
                    "%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y"])
        for candidate in formats:
            try:
                return datetime.strptime(value, candidate).strftime("%Y-%m-%d")
            except (TypeError, ValueError):
                continue
    return "Not specified"


def format_scholarship_table(items: list[dict]) -> tuple[str, str]:
    """Build plain-text and HTML email rows with a usable application link."""
    text_rows = ["ScholarshipTitle\tDeadline\tApplyLink"]
    html_rows = []
    for item in items:
        title = item["line"][:250]
        deadline = item.get("deadline") or extract_deadline(title)
        link = item.get("link") or item.get("source") or ""
        text_rows.append(f"{title}\t{deadline}\t{link}")
        safe_title = html_lib.escape(title)
        safe_link = html_lib.escape(link, quote=True)
        link_cell = f'<a href="{safe_link}">Apply / view details</a>' if link else "—"
        html_rows.append(
            f"<tr><td>{safe_title}</td>"
            f"<td style='white-space:nowrap'>{html_lib.escape(deadline)}</td>"
            f"<td>{link_cell}</td></tr>"
        )
    html = (
        "<table border='1' cellpadding='8' cellspacing='0' "
        "style='border-collapse:collapse;font-family:Arial,sans-serif;font-size:14px'>"
        "<tr style='background:#f0f0f0'><th align='left'>ScholarshipTitle</th>"
        "<th align='left'>Deadline</th><th align='left'>ApplyLink</th></tr>"
        + "".join(html_rows)
        + "</table>"
    )
    return "\n".join(text_rows), html


def find_event_title(lines: list[str], index: int, fallback: str) -> str:
    """Find the nearest preceding opportunity heading for a status sentence."""
    for position in range(index - 1, max(-1, index - 50), -1):
        candidate = lines[position].strip()
        sentence_like = (
            bool(re.search(r"[.!?]\s", candidate))
            or bool(re.search(r"\b(is|are|was|were|to|the|and|of)$", candidate, re.I))
            or (
                len(candidate.split()) > 10
                and bool(re.match(r"^(the|this|these|those)\b", candidate, re.I))
            )
        )
        if sentence_like:
            continue
        if not opportunity_category(candidate) or not is_catalog_title(candidate):
            continue
        if re.match(r"^(scholarship|internship|fellowship|grant)\b", candidate, re.I) \
                and position > 0:
            prefix = lines[position - 1].strip()
            if 2 <= len(prefix.split()) <= 12:
                candidate = f"{prefix} {candidate}"
        return candidate[:250]
    return fallback


def find_application_link(links: dict[str, str], title: str, source: str) -> str:
    """Choose a confidently matched detail/apply link, else the source page."""
    title_tokens = {
        token for token in re.findall(r"[a-z0-9]+", title.lower())
        if len(token) >= 4 and token not in {"scholarship", "internship", "fellowship"}
    }
    scored = []
    apply_links = []
    for anchor, href in links.items():
        anchor_tokens = set(re.findall(r"[a-z0-9]+", anchor.lower()))
        overlap = len(title_tokens & anchor_tokens)
        if overlap:
            scored.append((overlap, href))
        if re.search(
            r"\b(apply now|apply online|click here to apply|application form|register now)\b",
            anchor,
            re.I,
        ):
            apply_links.append(href)
    if scored and max(scored)[0] >= 2:
        return max(scored)[1]
    if len(set(apply_links)) == 1:
        return apply_links[0]
    return source


def opportunity_category(text: str) -> str | None:
    for category, pattern in CATEGORY_RULES:
        if pattern.search(text):
            return category
    return None


def is_catalog_title(text: str) -> bool:
    words = text.split()
    return (
        2 <= len(words) <= 20
        and not CATALOG_EXCLUSION_RE.search(text)
        and not CATALOG_BODY_RE.search(text)
        and not text.lower().startswith(("question:", "faq", "frequently asked"))
        and not re.match(r"^[*.\-–—()]", text)
        and not text.endswith(".")
    )


def is_authentic_source(url: str) -> bool:
    host = urlparse(url).hostname or ""
    host = host.lower().removeprefix("www.")
    return (
        any(host == domain or host.endswith(f".{domain}") for domain in TRUSTED_DOMAINS)
        or host.endswith(TRUSTED_SUFFIXES)
    )


def deadline_variants(deadline: str) -> set[str]:
    value = datetime.strptime(deadline, "%Y-%m-%d")
    return {
        deadline,
        value.strftime("%d/%m/%Y"),
        value.strftime("%d-%m-%Y"),
        value.strftime("%B %d, %Y"),
        value.strftime("%B %d %Y"),
        value.strftime("%d %B %Y"),
        value.strftime("%b %d, %Y"),
        value.strftime("%d %b %Y"),
    }


def page_confirms_item(item: dict, page_text: str) -> bool:
    normalized_page = re.sub(r"[^a-z0-9]+", " ", page_text.lower())
    ignored = {
        "scholarship", "scholarships", "internship", "fellowship", "fellowships",
        "grant", "grants", "program", "programme", "application", "deadline",
    }
    tokens = {
        token for token in re.findall(r"[a-z0-9]+", item["title"].lower())
        if len(token) >= 4 and token not in ignored
    }
    title_confirmed = len(tokens & set(normalized_page.split())) >= max(
        2, (len(tokens) + 1) // 2
    )
    date_confirmed = any(
        variant.lower() in page_text.lower()
        for variant in deadline_variants(item["deadline"])
    )
    return title_confirmed and date_confirmed


def verify_current_catalog(catalog: list[dict], timeout: int = 20) -> list[dict]:
    """Keep only dated items confirmed on a reachable, trusted source page."""
    candidates = [
        item for item in catalog
        if item["deadline"] != "Not specified" and is_authentic_source(item["source"])
    ]
    pages: dict[str, str] = {}
    for source in dict.fromkeys(item["source"] for item in candidates):
        try:
            html = fetch(source, timeout=timeout, retries=1)
            lines, _ = extract_lines(html, base_url=source)
            if lines and not looks_like_error_page(lines):
                pages[source] = " ".join(lines)
        except Exception as exc:
            print(f"  ! verification failed for {source}: {exc}")
    return [
        item for item in candidates
        if item["source"] in pages and page_confirms_item(item, pages[item["source"]])
    ]


def extract_explicit_deadline(text: str) -> str:
    """Extract a date only when it follows a deadline label."""
    marker = re.compile(
        r"\b(deadline(?: date)?|application deadline|last date|apply by"
        r"|open (?:till|until)|closes? on)\b",
        re.I,
    )
    for match in marker.finditer(text):
        deadline = extract_deadline(text[match.start():match.start() + 120])
        if deadline != "Not specified":
            return deadline
    return "Not specified"


def build_current_catalog(config: dict) -> list[dict]:
    """Build a strict, deduplicated catalogue from the latest snapshots."""
    today = datetime.now().date()
    catalog: list[dict] = []
    seen: set[str] = set()

    for site in config.get("sites", []):
        if not site.get("enabled", True):
            continue
        state = load_state(slugify(site["name"]))
        if not state:
            continue
        lines = state.get("lines", [])
        for index, title in enumerate(lines):
            title = title.replace("�", "").strip()
            if not 8 <= len(title) <= 160 or not is_catalog_title(title):
                continue
            category = opportunity_category(title)
            if not category or is_old_cycle(title):
                continue

            nearby = " ".join(lines[index:index + 6])
            active = any(pattern.search(nearby) for _, _, pattern in LIVE_EVENT_RULES)
            current_year = re.search(rf"\b{today.year}(?:\s*[-–]\s*\d{{2,4}})?\b", title)
            if not active and not current_year:
                continue
            if re.search(r"\b(closed|expired|last date (?:is )?over)\b", nearby, re.I):
                continue

            deadline = extract_explicit_deadline(nearby)
            if deadline != "Not specified":
                try:
                    if datetime.strptime(deadline, "%Y-%m-%d").date() < today:
                        continue
                except ValueError:
                    deadline = "Not specified"

            dedup_key = re.sub(
                r"\b20\d{2}(?:\s*[-–]\s*\d{2,4})?\b|\b(apply|application|open|deadline)\b"
                r"|[^a-z0-9]+",
                " ",
                title.lower(),
            ).strip()
            if not dedup_key or dedup_key in seen:
                continue
            seen.add(dedup_key)
            catalog.append({
                "category": category,
                "title": title,
                "deadline": deadline,
                "source": site["url"],
            })

    order = {"Scholarship": 0, "Grant": 1, "Internship": 2, "Fellowship": 3}
    catalog.sort(key=lambda item: (
        order[item["category"]],
        item["deadline"] == "Not specified",
        item["deadline"],
        item["title"].lower(),
    ))
    return catalog


def send_current_catalog(config: dict) -> int:
    catalog = verify_current_catalog(build_current_catalog(config))
    if not catalog:
        print("Verified catalogue: no authentic dated opportunities found.")
        return 1

    headers = "OpportunityType\tScholarshipTitle\tDeadline\tSource"
    text = headers + "\n" + "\n".join(
        f"{item['category']}\t{item['title']}\t{item['deadline']}\t{item['source']}"
        for item in catalog
    )
    rows = "".join(
        "<tr>"
        f"<td>{html_lib.escape(item['category'])}</td>"
        f"<td>{html_lib.escape(item['title'])}</td>"
        f"<td style='white-space:nowrap'>{html_lib.escape(item['deadline'])}</td>"
        f"<td><a href='{html_lib.escape(item['source'], quote=True)}'>Open</a></td>"
        "</tr>"
        for item in catalog
    )
    html = (
        "<h2>Verified Scholarships, Grants, Internships and Fellowships</h2>"
        "<p>Every link was reachable and the deadline was confirmed on an "
        "official or established provider page.</p>"
        "<table border='1' cellpadding='7' cellspacing='0' "
        "style='border-collapse:collapse;font-family:Arial,sans-serif;font-size:13px'>"
        "<tr><th>OpportunityType</th><th>ScholarshipTitle</th>"
        "<th>Deadline</th><th>Source</th></tr>"
        f"{rows}</table>"
    )
    ok = send_email(
        f"[Watcher] {len(catalog)} verified dated opportunities",
        text,
        html,
    )
    print(f"Verified catalogue: {len(catalog)} item(s) " + ("sent." if ok else "send failed."))
    return 0 if ok else 1


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:80]


def load_config() -> dict:
    with open(CONFIG_FILE, encoding="utf-8") as f:
        config = json.load(f)

    source_file = BASE_DIR / config.get("settings", {}).get("sources_file", "sources.json")
    if source_file.exists():
        with open(source_file, encoding="utf-8-sig") as f:
            raw_sources = json.load(f).get("sources", [])
        known = {site["url"].rstrip("/") for site in config.get("sites", [])}
        for entry in raw_sources:
            site = {"url": entry} if isinstance(entry, str) else dict(entry)
            url = site.get("url", "").strip()
            if not url or url.rstrip("/") in known or site.get("enabled") is False:
                continue
            parsed = urlparse(url)
            path_label = parsed.path.strip("/").split("/")[-1].replace("-", " ")[:45]
            site.setdefault("name", f"{parsed.netloc} — {path_label or 'home'}")
            site["_bulk_source"] = True
            config.setdefault("sites", []).append(site)
            known.add(url.rstrip("/"))
    return config


class _LegacySSLAdapter(requests.adapters.HTTPAdapter):
    """Lets us talk to very old government servers whose SSL is so outdated
    that modern Python refuses the connection by default."""

    def init_poolmanager(self, *args, **kwargs):
        import ssl
        ctx = ssl.create_default_context()
        ctx.options |= 0x4  # OP_LEGACY_SERVER_CONNECT
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


def pdf_bytes_to_html(content: bytes, source_url: str = "") -> str:
    """Extract PDF text and wrap it as HTML for the normal line pipeline."""
    from io import BytesIO
    from pypdf import PdfReader

    pages = [page.extract_text() or "" for page in PdfReader(BytesIO(content)).pages]
    text = "\n".join(pages)
    if not text.strip():
        filename = source_url.rsplit("/", 1)[-1].split("?", 1)[0] or "document.pdf"
        digest = hashlib.sha256(content).hexdigest()
        text = f"New scholarship document announced {datetime.now().year}: {filename} {digest}"
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    title = next(
        (line for line in lines if 8 <= len(line) <= 200 and TOPIC_RE.search(line)),
        next((line for line in lines if 8 <= len(line) <= 200), "PDF notice"),
    )
    return (
        f"<html><head><title>{html_lib.escape(title)}</title></head>"
        f"<body><pre>{html_lib.escape(text)}</pre></body></html>"
    )


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
            content_type = resp.headers.get("content-type", "").lower()
            if "pdf" in content_type or resp.content.startswith(b"%PDF"):
                return pdf_bytes_to_html(resp.content, url)
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
        if _PLAYWRIGHT is None:
            _PLAYWRIGHT = sync_playwright().start()
        try:
            _BROWSER = _PLAYWRIGHT.chromium.launch(headless=True)
        except Exception:
            _PLAYWRIGHT.stop()
            _PLAYWRIGHT = None
            raise

    context = _BROWSER.new_context(user_agent=USER_AGENT, ignore_https_errors=True)
    page = context.new_page()
    try:
        page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
        page.wait_for_timeout(6000)  # let client-side data requests finish
        return page.content()
    finally:
        context.close()


def shutdown_browser() -> None:
    global _BROWSER, _PLAYWRIGHT
    if _BROWSER is not None:
        _BROWSER.close()
        _BROWSER = None
    if _PLAYWRIGHT is not None:
        _PLAYWRIGHT.stop()
        _PLAYWRIGHT = None


# CSS selectors for notice/announcement sections common on .gov.in sites.
# When any of these exist on the page, we read ONLY those sections — they
# contain real announcements (scholarship openings, deadlines, circulars)
# instead of navigation menus and department headings.
_NOTICE_SELECTORS = [
    # explicit id/class labels used across Indian government portals
    "[id*=notice]", "[class*=notice]",
    "[id*=announcement]", "[class*=announcement]",
    "[id*=news]", "[class*=latestnews]", "[class*=latest-news]",
    "[id*=update]", "[class*=update]",
    "[id*=notification]", "[class*=notification]",
    "[id*=marquee]", "marquee",          # scrolling tickers on old .gov.in sites
    "[id*=highlight]", "[class*=highlight]",
    "[id*=circular]", "[class*=circular]",
    # common generic patterns
    ".notice-board", "#noticeBoard", ".scrollnews", ".scroll-news",
    ".what-new", ".whats-new", "#whats-new",
]


def extract_lines(html: str, base_url: str = "",
                  selector: str | None = None) -> tuple[list[str], dict[str, str]]:
    """Turn a page into clean text lines, plus a map of line -> link.

    If the page has notice/announcement sections (or a site-specific selector
    is given), only those sections are read — this avoids scraping navigation
    menus and department headers that generate noise but never real alerts."""
    from urllib.parse import urljoin

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(STRIP_TAGS):
        tag.decompose()

    # Remember the destination of every clickable text on the page (full page,
    # before we narrow the text scope, so links still resolve correctly).
    links: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        text = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
        href = a["href"].strip()
        if len(text) >= 4 and href and not href.lower().startswith("javascript"):
            links.setdefault(text.lower(), urljoin(base_url, href))

    # Narrow to announcement sections if possible.
    scope = None
    if selector:
        scope = soup.select(selector) or None
    if scope is None:
        for sel in _NOTICE_SELECTORS:
            found = soup.select(sel)
            if found:
                scope = found
                break

    text_source = soup  # default: full page
    if scope:
        from bs4 import BeautifulSoup as BS
        combined = BS("".join(str(t) for t in scope), "html.parser")
        # Only use the scoped section if it has enough substance — a tiny hit
        # (e.g. a chatbot widget that happens to have "notice" in its class)
        # should fall back to the full page rather than produce 1-2 junk lines.
        candidate_lines = [
            re.sub(r"\s+", " ", l).strip()
            for l in combined.get_text("\n").splitlines()
            if len(re.sub(r"\s+", " ", l).strip()) >= 10
        ]
        if len(candidate_lines) >= 5:
            text_source = combined

    lines = []
    seen = set()
    for raw in text_source.get_text("\n").splitlines():
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


def looks_like_error_page(lines: list[str]) -> bool:
    """Catch soft 404/blocked pages that incorrectly return HTTP 200."""
    sample = " ".join(lines[:30])
    return bool(re.search(
        r"\b404\b.{0,40}\b(not found|error)\b|\bpage not found\b"
        r"|\baccess denied\b|\brequest blocked\b|\bsite can.t be reached\b",
        sample, re.I,
    ))


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

    # .strip() is critical: GitHub secrets are often pasted with a trailing
    # newline. An unstripped address puts "\n" into the SMTP MAIL FROM, which
    # Gmail rejects with "555 5.5.2 Syntax error" and a phantom empty recipient.
    address = os.environ.get("EMAIL_ADDRESS", "").strip()
    # Google displays app passwords with spaces ("abcd efgh ...") - remove them
    password = os.environ.get("EMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    if not address or not password:
        return False
    # An empty EMAIL_TO (e.g. an unset GitHub secret passed through the
    # workflow) must fall back to self, not produce "RCPT TO:<>".
    to = (os.environ.get("EMAIL_TO") or "").strip() or DEFAULT_EMAIL_TO
    recipients = [t.strip() for t in to.split(",") if t.strip()]
    # Final guard: never hand smtplib an empty recipient list.
    if not recipients:
        recipients = [address]
        to = address
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
            server.sendmail(address, recipients, msg.as_string())
        return True
    except Exception as e:
        print(f"  ! email send failed: {e}")
        return False


def log_alert(text: str) -> None:
    with open(ALERT_LOG, "a", encoding="utf-8") as f:
        f.write(f"\n{'=' * 60}\n{datetime.now().isoformat()}\n{text}\n")


def alert_fingerprint(subject: str, text: str) -> str:
    normalized = re.sub(r"\s+", " ", f"{subject} {text}").strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def event_fingerprint(site_name: str, label: str, line: str, link: str) -> str:
    normalized = re.sub(
        r"\s+", " ", f"event {site_name} {label} {line} {link}"
    ).strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def load_sent_alerts() -> dict[str, str]:
    if not SENT_FILE.exists():
        return {}
    try:
        with open(SENT_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def load_pending_fingerprints() -> set[str]:
    if not PENDING_FILE.exists():
        return set()
    try:
        with open(PENDING_FILE, encoding="utf-8-sig") as f:
            pending = json.load(f)
        return {
            item.get("fingerprint") or alert_fingerprint(item["subject"], item["text"])
            for item in pending
        }
    except (json.JSONDecodeError, OSError, KeyError, TypeError):
        return set()


def remember_alert(fingerprint: str) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    sent = load_sent_alerts()
    sent[fingerprint] = datetime.now(timezone.utc).isoformat()
    with open(SENT_FILE, "w", encoding="utf-8") as f:
        json.dump(sent, f, indent=1)


def queue_alert(subject: str, text: str, html: str | None,
                fingerprint: str) -> None:
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
        "fingerprint": fingerprint,
        "subject": subject,
        "text": text,
        "html": html or "",
    })
    with open(PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump(pending, f, ensure_ascii=False, indent=1)


def alert(text: str, html: str | None = None, subject: str | None = None) -> bool:
    """Handle one finding. Instant mode: send through every configured
    channel now. Digest mode: save it for the evening digest email.
    The log file ALWAYS gets it immediately, so nothing is ever lost."""
    subject = subject or (text.splitlines()[0][:80] if text else "Website alert")
    fingerprint = alert_fingerprint(subject, text)
    with ALERT_LOCK:
        if fingerprint in load_sent_alerts() or fingerprint in load_pending_fingerprints():
            print("  >> duplicate alert skipped")
            return False
        log_alert(text)
        remember_alert(fingerprint)

        if DELIVERY_MODE == "digest":
            queue_alert(subject, text, html, fingerprint)
            print("  >> ALERT saved for evening digest")
            return True

        channels = []
        if send_telegram(text):
            channels.append("telegram")
        if send_email(f"[Watcher] {subject}", text, html):
            channels.append("email")
        where = " + ".join(channels) if channels else "log only - no channel configured"
        print(f"  >> ALERT ({where})")
        return True


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

    lines, links = extract_lines(html, base_url=url, selector=site.get("selector"))
    if not lines:
        digest = hashlib.sha256(html.encode("utf-8", errors="replace")).hexdigest()
        lines = [
            f"New scholarship page update announced {datetime.now().year}: {digest}"
        ]
    if looks_like_error_page(lines):
        return {"status": "error", "name": name, "error": "site returned an error page"}

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
    line_positions = {line.lower(): index for index, line in enumerate(lines)}
    for ln in relevant:
        topic_context = f"{url} {site.get('topic_context', '')}"
        event = classify_line(ln, topic_context)
        if event is None:
            continue
        emoji, label, confidence = event
        index = line_positions.get(ln.lower(), 0)
        title = find_event_title(lines, index, name)
        context = " ".join(lines[index:index + 6])
        deadline = extract_explicit_deadline(context)
        link = links.get(ln.lower()) or find_application_link(links, title, url)
        items.append({
            "emoji": emoji,
            "label": label,
            "confidence": confidence,
            "line": title,
            "event_line": ln,
            "deadline": deadline,
            "link": link,
            "source": url,
        })
    order = {"Application window OPEN": 0, "New scheme announced": 1,
             "Deadline / last date": 2, "Rule / stipend change": 3}
    items.sort(key=lambda it: order.get(it["label"], 9))
    sent_alerts = load_sent_alerts()
    for item in items:
        item["fingerprint"] = event_fingerprint(
            name, item["label"],
            f"{item['line']} {item['event_line']} {item['deadline']}",
            item["link"] or url,
        )
    items = [item for item in items if item["fingerprint"] not in sent_alerts]

    print(f"  changed: {len(added)} new lines, {len(items)} live events")
    if not items:
        return {"status": "ok", "name": name}

    shown = items[:15]
    text, html = format_scholarship_table(shown)

    top = shown[0]
    subject = f"{top['emoji']} {name}: {top['line'][:60]}"
    sent = alert(text, html=html, subject=subject)
    if sent:
        with ALERT_LOCK:
            for item in items:
                remember_alert(item["fingerprint"])
    return {"status": "changed" if sent else "ok", "name": name,
            "new_lines": len(items) if sent else 0}


def main() -> int:
    global DELIVERY_MODE
    parser = argparse.ArgumentParser(description="Website change watcher")
    parser.add_argument("--site", help="only check sites whose name contains this text")
    parser.add_argument("--no-delay", action="store_true", help="skip politeness delay")
    parser.add_argument("--send-digest", action="store_true",
                        help="send the daily digest email of everything found, then exit")
    parser.add_argument("--send-current", action="store_true",
                        help="email all strict active opportunities in current snapshots")
    args = parser.parse_args()

    config = load_config()
    settings = config.get("settings", {})
    DELIVERY_MODE = settings.get("delivery_mode", "instant")

    if args.send_digest:
        return send_digest()
    if args.send_current:
        return send_current_catalog(config)
    sites = [site for site in config.get("sites", []) if site.get("enabled", True)]
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
        browser_sites = [site for site in sites if site.get("renderer") == "browser"]
        http_sites = [site for site in sites if site.get("renderer") != "browser"]
        workers = max(1, int(settings.get("workers", 12)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(check_site, site, settings, args.no_delay): site
                for site in http_sites
            }
            for future in as_completed(futures):
                results.append(future.result())
                if delay:
                    time.sleep(delay / workers)
        for site in browser_sites:
            results.append(check_site(site, settings, args.no_delay))
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
