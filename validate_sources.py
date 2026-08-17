"""Check bulk sources and optionally remove unreachable or irrelevant pages."""

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from watcher import TOPIC_RE, extract_lines, fetch, looks_like_error_page

BASE_DIR = Path(__file__).resolve().parent
SOURCES_FILE = BASE_DIR / "sources.json"
REPORT_FILE = BASE_DIR / "broken_sources.json"


def check(url: str, timeout: int) -> tuple[str, str]:
    try:
        page = fetch(url, timeout=timeout, retries=1)
        lines, _ = extract_lines(page, base_url=url)
        if not lines:
            return url, "no readable content"
        if looks_like_error_page(lines):
            return url, "error page"
        if not TOPIC_RE.search(f"{url} {' '.join(lines)}"):
            return url, "no scholarship, internship, or fellowship content"
        return url, ""
    except Exception as exc:
        return url, str(exc)[:200]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prune", action="store_true",
                        help="remove failed sources from sources.json")
    parser.add_argument("--workers", type=int, default=24)
    parser.add_argument("--timeout", type=int, default=12)
    args = parser.parse_args()

    data = json.loads(SOURCES_FILE.read_text(encoding="utf-8-sig"))
    sources = list(dict.fromkeys(
        entry if isinstance(entry, str) else entry["url"]
        for entry in data.get("sources", [])
    ))
    healthy: list[str] = []
    broken: list[dict[str, str]] = []

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(check, url, args.timeout): url for url in sources}
        for number, future in enumerate(as_completed(futures), 1):
            url, reason = future.result()
            if reason:
                broken.append({"url": url, "reason": reason})
            else:
                healthy.append(url)
            if number % 50 == 0 or number == len(sources):
                print(f"Checked {number}/{len(sources)}")

    healthy.sort()
    broken.sort(key=lambda item: item["url"])
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "healthy_count": len(healthy),
        "broken_count": len(broken),
        "sources": broken,
    }
    REPORT_FILE.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if args.prune:
        SOURCES_FILE.write_text(
            json.dumps({"sources": healthy}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(f"Healthy: {len(healthy)} | Removed/report: {len(broken)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
