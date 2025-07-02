#!/usr/bin/env python3
"""make_meta_feed.py – hardened, malformed-XML-aware (2025-07-02)
-----------------------------------------------------------------
Creates a **meta-RSS 2.0 feed (rss.xml)** that holds the *most recent item* from
each URL listed in ``sites.txt`` (or another file via ``-i``).

### What this version handles
* Cloudflare / WordFence blocks – uses a full Chrome UA and automatic retries.
* Feeds that drop the opening "<" in the XML declaration (Cascade, KPI, Platte,
  etc.) – a repair pass adds it back before parsing.
* Optional homepage discovery (``--discover``) if you supply bare domain names.
* Verbose diagnostics with ``--verbose``.

Python 3.9+ required.  Add to *requirements.txt*:
```
feedparser
feedgen
beautifulsoup4
python-dateutil
requests
tenacity
```
"""
from __future__ import annotations

import argparse
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional
from urllib.parse import urljoin, urlparse

import feedparser  # type: ignore
import requests
from bs4 import BeautifulSoup  # type: ignore
from dateutil import parser as dtparser  # type: ignore
from feedgen.feed import FeedGenerator  # type: ignore
from tenacity import retry, stop_after_attempt, wait_exponential  # type: ignore

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEFAULT_WORKERS = 16
DEFAULT_TIMEOUT = 10  # seconds
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/rss+xml, application/atom+xml;q=0.9, */*;q=0.1",
    "Accept-Encoding": "gzip, deflate, br",
}
COMMON_SUFFIXES = ("/feed", "/rss", "/rss.xml", "/atom.xml")
ALT_TYPES = ["application/rss+xml", "application/atom+xml"]

log = logging.getLogger("meta-feed")

# ---------------------------------------------------------------------------
# Networking helpers
# ---------------------------------------------------------------------------
SESSION = requests.Session()
SESSION.headers.update(HEADERS)
retry_req = retry(
    stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8)
)


@retry_req
def get_bytes(url: str, timeout: int = DEFAULT_TIMEOUT) -> bytes:
    resp = SESSION.get(url, timeout=timeout, allow_redirects=True)
    resp.raise_for_status()
    return resp.content


# ---------------------------------------------------------------------------
# Feed utilities
# ---------------------------------------------------------------------------

def normalize_url(raw: str) -> Optional[str]:
    raw = raw.split("#", 1)[0].strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "https://" + raw
    return raw


def friendly_name(url: str) -> str:
    host = urlparse(url).hostname or url
    return host.removeprefix("www.").split(".")[0].replace("-", " ").title()


# ---------------------------- feed discovery ------------------------------

def discover_feed(homepage: str, timeout: int) -> Optional[str]:
    try:
        html = get_bytes(homepage, timeout).decode(errors="ignore")
    except Exception as exc:
        log.debug("%s – homepage fetch failed: %s", homepage, exc)
        return None

    soup = BeautifulSoup(html, "html.parser")
    link = soup.find("link", rel="alternate", type=ALT_TYPES)
    if link and link.get("href"):
        return urljoin(homepage, link["href"])

    for suf in COMMON_SUFFIXES:
        guess = urljoin(homepage.rstrip("/") + "/", suf.lstrip("/"))
        if validate_feed(guess, timeout):
            return guess
    return None


# ---------------------------- parsing wrapper -----------------------------

# Repair helper for bad XML preambles ("?xml" missing opening bracket).

def _repair_xml(raw: bytes) -> bytes:
    trimmed = raw.lstrip()  # drop BOM / leading whitespace
    if trimmed.startswith(b"?xml"):
        trimmed = b"<" + trimmed
    return trimmed


def parse_feed(url: str, timeout: int):
    """Download *url* and return a parsed feed or raise an error."""
    try:
        raw = get_bytes(url, timeout)
        for attempt in (raw, _repair_xml(raw)):
            parsed = feedparser.parse(attempt)
            if parsed.entries:
                return parsed
        raise ValueError("no entries in downloaded bytes")
    except Exception as primary_exc:
        log.debug("%s – requests path failed: %s", url, primary_exc)
        parsed = feedparser.parse(url, request_headers=HEADERS)
        if parsed.entries:
            return parsed
        raise  # propagate failure


def validate_feed(url: str, timeout: int) -> bool:
    try:
        return bool(parse_feed(url, timeout).entries)
    except Exception as exc:
        log.debug("%s – validation failed: %s", url, exc)
        return False


def latest_item(feed_url: str, timeout: int) -> Optional[dict]:
    try:
        parsed = parse_feed(feed_url, timeout)
    except Exception as exc:
        log.debug("%s – parse failed: %s", feed_url, exc)
        return None

    e = parsed.entries[0]
    if getattr(e, "published_parsed", None):
        published = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
    else:
        pub_raw = e.get("published") or e.get("updated") or ""
        try:
            published = dtparser.parse(pub_raw).astimezone(timezone.utc)
        except Exception:
            published = datetime.now(timezone.utc)

    return {
        "title": e.get("title", "Untitled"),
        "link": e.get("link"),
        "description": e.get("summary", ""),
        "published": published,
    }


# ---------------------------------------------------------------------------
# Concurrent crawler
# ---------------------------------------------------------------------------

def collect_items(sites: Iterable[str], *, workers: int, timeout: int, discover: bool) -> List[dict]:
    items: List[dict] = []

    def worker(site: str):
        url = normalize_url(site)
        if not url:
            return None

        looks_like_feed = any(url.endswith(ext) for ext in COMMON_SUFFIXES + (".xml", ".rss"))
        feed_url = url if looks_like_feed or not discover else discover_feed(url, timeout) or url

        if validate_feed(feed_url, timeout):
            itm = latest_item(feed_url, timeout)
            if itm:
                itm["source"] = friendly_name(url)
                log.info("✔ %s – %s", itm["source"], itm["title"][:60])
                return itm
        return None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(worker, s): s for s in sites}
        for fut in as_completed(futures):
            try:
                itm = fut.result()
                if itm:
                    items.append(itm)
            except Exception as exc:
                log.warning("%s – worker crashed: %s", futures[fut], exc)

    return items


# ---------------------------------------------------------------------------
# Feed builder
# ---------------------------------------------------------------------------

def build_feed(items: List[dict], output: Path):
    fg = FeedGenerator()
    fg.title("Meta-Feed Digest")
    fg.link(href="https://example.com/rss.xml", rel="self")
    fg.description("Newest article from each member-feed in sites.txt")
    fg.language("en")

    for itm in sorted(items, key=lambda x: x["published"], reverse=True):
        fe = fg.add_entry()
        fe.title(f"[{itm['source']}] {itm['title']}")
        fe.link(href=itm["link"])
        fe.description(itm["description"])
        fe.pubDate(itm["published"])

    fg.rss_file(output)
    log.info("wrote %s (%d items)", output, len(items))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(description="Aggregate newest post from many feeds.")
    ap.add_argument("-i", "--input", default="sites.txt", help="path to list (default: sites.txt)")
    ap.add_argument("-o", "--output", default="rss.xml", help="result file (default: rss.xml)")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="thread pool size")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="HTTP timeout per request")
    ap.add_argument("--discover", action="store_true", help="try homepage discovery as fallback")
    ap.add_argument("--verbose", action="store_true", help="DEBUG logging")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    site_file = Path(args.input)
    if not site_file.is_file():
        log.error("sites file %s not found", site_file)
        sys.exit(1)

    # strip blank lines and comments that start with “#”
    sites = [
        ln for ln in site_file.read_text().splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]

    log.info("processing %d sites with %d workers", len(sites), args.workers)

    items = collect_items(
        sites,
        workers=args.workers,
        timeout=args.timeout,
        discover=args.discover,
    )

    if not items:
        log.warning("No items found – writing empty feed so CI artefact exists")
        build_feed([], Path(args.output))
        return

    build_feed(items, Path(args.output))


if __name__ == "__main__":
    main()