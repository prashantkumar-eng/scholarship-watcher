"""Deep-scan seed sites and email current, dated opportunities."""

import argparse
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import watcher

BASE_DIR = Path(__file__).resolve().parent
RESULT_FILE = BASE_DIR / "verified_opportunities.json"
SENT_FILE = BASE_DIR / "state" / "deep-scan-sent.json"
ESTABLISHED_DOMAINS = {
    "buddy4study.com", "britishcouncil.in", "chevening.org", "daad.de",
    "dakshanapublic.s3.ap-southeast-1.amazonaws.com", "fastweb.com",
    "fulbrightonline.org", "icgeb.org", "lawctopus.com", "opportunitydesk.org",
    "scholarshipportal.com", "scholarships.com", "scholarships360.org",
    "scholarships.gov.in", "studyinjapan.go.jp", "twas.org",
}
LINK_HINT_RE = re.compile(
    r"scholar|intern|fellow|grant|financial.?aid|student.?fund|apply.?now",
    re.I,
)


def all_seed_urls() -> list[str]:
    sources = json.loads(
        (BASE_DIR / "sources.json").read_text(encoding="utf-8-sig")
    ).get("sources", [])
    return list(dict.fromkeys(
        item if isinstance(item, str) else item["url"] for item in sources
    ))


def fetch_html(url: str, timeout: int, max_bytes: int = 2_000_000) -> str:
    with requests.get(
        url,
        headers={"User-Agent": watcher.USER_AGENT, "Accept-Language": "en-IN,en;q=0.9"},
        timeout=timeout,
        allow_redirects=True,
        stream=True,
    ) as response:
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").lower()
        if not any(kind in content_type for kind in ("html", "text", "pdf")):
            raise ValueError(f"unsupported content type: {content_type or 'unknown'}")
        chunks = []
        size = 0
        download_limit = 10_000_000 if "pdf" in content_type else max_bytes
        for chunk in response.iter_content(65_536):
            chunks.append(chunk)
            size += len(chunk)
            if size >= download_limit:
                break
        content = b"".join(chunks)
        if "pdf" in content_type or content.startswith(b"%PDF"):
            return watcher.pdf_bytes_to_html(content, url)
        encoding = response.encoding or "utf-8"
        return content.decode(encoding, errors="replace")


def authenticity(url: str) -> str | None:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    if watcher.is_authentic_source(url):
        return "Official provider"
    if any(host == domain or host.endswith(f".{domain}") for domain in ESTABLISHED_DOMAINS):
        return "Established scholarship source"
    return None


def page_title(soup: BeautifulSoup) -> str:
    candidates = [
        soup.find("meta", property="og:title"),
        soup.find("h1"),
        soup.find("title"),
    ]
    for tag in candidates:
        if not tag:
            continue
        value = tag.get("content", "") if tag.name == "meta" else tag.get_text(" ", strip=True)
        value = re.sub(r"\s+", " ", value).strip()
        if value:
            return value[:200]
    return ""


def analyze_page(url: str, html: str) -> dict | None:
    trust = authenticity(url)
    if not trust:
        return None
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    title = page_title(soup)
    category = watcher.opportunity_category(title)
    if not category or not watcher.is_catalog_title(title) or watcher.is_old_cycle(title):
        return None

    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    deadline = watcher.extract_explicit_deadline(text)
    if deadline == "Not specified":
        return None
    try:
        deadline_date = datetime.strptime(deadline, "%Y-%m-%d").date()
    except ValueError:
        return None
    if deadline_date < datetime.now().date():
        return None
    if re.search(r"\b(applications? closed|expired|deadline has passed)\b", text, re.I):
        return None
    if not watcher.page_confirms_item(
        {"title": title, "deadline": deadline}, text
    ):
        return None
    return {
        "category": category,
        "title": title,
        "deadline": deadline,
        "url": url,
        "verification": trust,
    }


def discover_links(base_url: str, html: str, limit: int) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    scored: list[tuple[int, str]] = []
    seen = set()
    for anchor in soup.find_all("a", href=True):
        text = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True))
        href = urljoin(base_url, anchor["href"]).split("#", 1)[0]
        parsed = urlparse(href)
        if parsed.scheme not in {"http", "https"} or href in seen:
            continue
        score = int(bool(LINK_HINT_RE.search(text))) * 2
        score += int(bool(LINK_HINT_RE.search(parsed.path)))
        score += int(bool(re.search(r"\b20(2[6-9]|[3-9]\d)\b", text)))
        if score < 2:
            continue
        seen.add(href)
        scored.append((score, href))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [url for _, url in scored[:limit]]


def fetch_many(urls: list[str], workers: int, timeout: int) -> dict[str, str]:
    pages = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_html, url, timeout): url for url in urls}
        for number, future in enumerate(as_completed(futures), 1):
            url = futures[future]
            try:
                pages[url] = future.result()
            except Exception:
                pass
            if number % 100 == 0 or number == len(urls):
                print(f"Fetched {number}/{len(urls)}; usable {len(pages)}", flush=True)
    return pages


def deduplicate(items: list[dict]) -> list[dict]:
    best = {}
    for item in items:
        key = re.sub(
            r"\b20\d{2}(?:[-–]\d{2,4})?\b|[^a-z0-9]+",
            " ",
            item["title"].lower(),
        ).strip()
        current = best.get(key)
        if not current or item["verification"] == "Official provider":
            best[key] = item
    return sorted(
        best.values(),
        key=lambda item: (item["category"], item["deadline"], item["title"].lower()),
    )


def format_results(items: list[dict]) -> tuple[str, str]:
    text = "OpportunityType\tTitle\tDeadline\tValidLink\tVerification\n" + "\n".join(
        f"{item['category']}\t{item['title']}\t{item['deadline']}\t"
        f"{item['url']}\t{item['verification']}"
        for item in items
    )
    rows = "".join(
        "<tr>"
        f"<td>{watcher.html_lib.escape(item['category'])}</td>"
        f"<td>{watcher.html_lib.escape(item['title'])}</td>"
        f"<td>{item['deadline']}</td>"
        f"<td><a href='{watcher.html_lib.escape(item['url'], quote=True)}'>Open</a></td>"
        f"<td>{item['verification']}</td>"
        "</tr>"
        for item in items
    )
    html = (
        "<h2>Verified Current Opportunities</h2>"
        "<p>Each page was opened successfully and its future deadline was confirmed.</p>"
        "<table border='1' cellpadding='7' cellspacing='0' "
        "style='border-collapse:collapse;font-family:Arial,sans-serif;font-size:13px'>"
        "<tr><th>Type</th><th>Title</th><th>Deadline</th>"
        "<th>Valid link</th><th>Verification</th></tr>"
        f"{rows}</table>"
    )
    return text, html


def email_results(items: list[dict]) -> bool:
    text, html = format_results(items)
    return watcher.send_email(
        f"[Watcher] {len(items)} deeply verified current opportunities", text, html
    )


def queue_results(items: list[dict]) -> None:
    subject = f"{len(items)} deeply verified current opportunities"
    text, html = format_results(items)
    fingerprint = watcher.alert_fingerprint(subject, text)
    watcher.log_alert(text)
    watcher.queue_alert(subject, text, html, fingerprint)


def unsent_items(items: list[dict]) -> tuple[list[dict], set[str]]:
    sent = set()
    if SENT_FILE.exists():
        try:
            sent = set(json.loads(SENT_FILE.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            sent = set()
    fingerprints = {
        watcher.alert_fingerprint(
            item["title"], f"{item['deadline']} {item['url']}"
        ): item
        for item in items
    }
    return [item for key, item in fingerprints.items() if key not in sent], set(fingerprints)


def remember_items(fingerprints: set[str]) -> None:
    SENT_FILE.parent.mkdir(exist_ok=True)
    existing = set()
    if SENT_FILE.exists():
        try:
            existing = set(json.loads(SENT_FILE.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            existing = set()
    SENT_FILE.write_text(
        json.dumps(sorted(existing | fingerprints), indent=1) + "\n", encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    delivery = parser.add_mutually_exclusive_group()
    delivery.add_argument("--send-email", action="store_true")
    delivery.add_argument("--queue", action="store_true",
                          help="add new verified results to the daily digest")
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--timeout", type=int, default=12)
    parser.add_argument("--links-per-site", type=int, default=5)
    args = parser.parse_args()

    seeds = all_seed_urls()
    print(f"Deep scan: {len(seeds)} seed URL(s)", flush=True)
    seed_pages = fetch_many(seeds, args.workers, args.timeout)

    detail_urls = []
    for url, html in seed_pages.items():
        detail_urls.extend(discover_links(url, html, args.links_per_site))
    detail_urls = list(dict.fromkeys(url for url in detail_urls if url not in seed_pages))
    print(f"Discovered {len(detail_urls)} relevant detail link(s)", flush=True)
    detail_pages = fetch_many(detail_urls, args.workers, args.timeout)

    items = [
        item for url, html in {**seed_pages, **detail_pages}.items()
        if (item := analyze_page(url, html))
    ]
    items = deduplicate(items)
    result = {
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "seed_count": len(seeds),
        "reachable_seed_count": len(seed_pages),
        "detail_count": len(detail_pages),
        "opportunity_count": len(items),
        "opportunities": items,
    }
    RESULT_FILE.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Verified {len(items)} current dated opportunity(s)")
    if args.send_email or args.queue:
        new_items, fingerprints = unsent_items(items)
        if not new_items:
            print("No new verified opportunities.")
            return 0
        if args.queue:
            queue_results(new_items)
            remember_items(fingerprints)
            print(f"Queued {len(new_items)} verified opportunity(s).")
            return 0
        sent = email_results(new_items)
        if sent:
            remember_items(fingerprints)
        print("Email sent." if sent else "Email failed.")
        return 0 if sent else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
