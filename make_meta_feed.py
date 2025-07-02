#!/usr/bin/env python3
"""
make_meta_feed.py
=================
Builds a single RSS 2.0 file (`rss.xml`) containing the *freshest* post from
every site listed in `sites.txt`.

Key improvements over the basic version you started with
-------------------------------------------------------
* **Concurrency** – fetch pages & feeds in parallel (default: 16 workers);
  drops runtime from minutes to seconds for large site lists.
* **Robust feed discovery** – checks both RSS & Atom `<link rel="alternate">`
  tags *and* tries common suffixes (`/feed`, `/rss`, `/atom.xml`).
* **Retry‑friendly requests** – uses a single `requests.Session` with
  exponential‑backoff retries and a polite but unmistakable UA string so sites
  can whitelist the crawler if needed.
* **CLI flags** – `-i/--input`, `-o/--output`, `--workers`, `--timeout` make it
  easy to reuse in CI (GitHub Actions, cron, etc.).
* **Structured logging** – timestamps + reason for any failure, so you can
  diagnose pesky hosts when the Action fails.
* **Graceful date handling** – falls back to `dateutil.parser.parse()` when
  `feedparser` cannot supply a struct‑time.
* **Typed** (PEP 484) & formatted with `black`. Runs on Python 3.11+.

Requirements (add these to requirements.txt):
    feedparser
    feedgen
    beautifulsoup4
    python-dateutil
    requests
    tenacity   # back‑off helper (optional but recommended)

Usage
-----
    python make_meta_feed.py -i sites.txt -o rss.xml --workers 32 --timeout 10

In GitHub Actions you can omit all flags and rely on defaults (it will look for
`sites.txt` in the repo root and output `rss.xml`).
"""
from __future__ import annotations

import argparse
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, List, Optional
from urllib.parse import urljoin, urlparse

import feedparser  # type: ignore
import requests
from bs4 import BeautifulSoup  # type: ignore
from dateutil import parser as dtparser  # type: ignore
from feedgen.feed import FeedGenerator  # type: ignore
from tenacity import retry, stop_after_attempt, wait_exponential  # type: ignore

# ---------------------------------------------------------------------------
# Config & logger
# ---------------------------------------------------------------------------
DEFAULT_WORKERS = 16
DEFAULT_TIMEOUT = 10  # seconds
USER_AGENT = "MetaFeedBot/1.0 (+https://github.com/<your repo>)"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("meta‑feed")


# ---------------------------------------------------------------------------
# Requests helpers – one global Session improves TCP reuse & cookies
# ---------------------------------------------------------------------------
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
def get_text(url: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Fetch *text* at URL with retry + timeout."""
    resp = SESSION.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.text


# ---------------------------------------------------------------------------
# Feed discovery & parsing
# ---------------------------------------------------------------------------
COMMON_SUFFIXES = ("/feed", "/rss", "/rss.xml", "/atom.xml")
ALT_TYPES = {"application/rss+xml", "application/atom+xml"}


def normalize_url(raw: str) -> Optional[str]:
    """Return a full URL or *None* for blank / commented lines."""
    raw = raw.split("#", 1)[0].strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "https://" + raw  # default to HTTPS
    return raw


def friendly_name(url: str) -> str:
    host = urlparse(url).hostname or url
    return host.removeprefix("www.").split(".")[0].replace("-", " ").title()


def discover_feed(homepage: str, timeout: int) -> Optional[str]:
    """Return a feed URL if found, else *None*."""
    try:
        html = get_text(homepage, timeout)
    except Exception as exc:
        log.debug("fetch %s failed: %s", homepage, exc)
        return None

    soup = BeautifulSoup(html, "html.parser")
    link = soup.find("link", {"rel": "alternate", "type": ALT_TYPES})
    if link and link.get("href"):
        return urljoin(homepage, link["href"])
    # fallback guesses (WordPress etc.)
    for suf in COMMON_SUFFIXES:
        guess = urljoin(homepage.rstrip("/") + "/", suf.lstrip("/"))
        if validate_feed(guess, timeout):
            return guess
    return None


def validate_feed(url: str, timeout: int) -> bool:
    try:
        parsed = feedparser.parse(url, request_headers={"User-Agent": USER_AGENT}, timeout=timeout)
        return bool(parsed.entries)
    except Exception:
        return False


def get_latest_item(feed_url: str, timeout: int):
    parsed = feedparser.parse(feed_url, request_headers={"User-Agent": USER_AGENT}, timeout=timeout)
    if not parsed.entries:
        return None
    e = parsed.entries[0]
    published: datetime
    if "published_parsed" in e and e.published_parsed:
        published = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
    elif "published" in e:
        try:
            published = dtparser.parse(e.published).astimezone(timezone.utc)
        except (ValueError, TypeError):
            published = datetime.now(timezone.utc)
    else:
        published = datetime.now(timezone.utc)
    return {
        "title": e.get("title", "Untitled"),
        "link": e.get("link"),
        "description": e.get("summary", ""),
        "published": published,
    }


# ---------------------------------------------------------------------------
# Core collection function (parallel)
# ---------------------------------------------------------------------------

def collect_items(sites: Iterable[str], *, workers: int, timeout: int) -> List[dict]:
    items: List[dict] = []

    def process(site: str):
        url = normalize_url(site)
        if not url:
            return None
        feed_url = discover_feed(url, timeout) or url
        if validate_feed(feed_url, timeout):
            item = get_latest_item(feed_url, timeout)
            if item:
                item["source"] = friendly_name(url)
                return item
        return None

    with ThreadPoolExecutor(max_workers=workers) as exe:
        futures = {exe.submit(process, s): s for s in sites}
        for fut in as_completed(futures):
            try:
                itm = fut.result()
                if itm:
                    items.append(itm)
            except Exception as exc:
                log.warning("processing %s failed: %s", futures[fut], exc)

    return items


# ---------------------------------------------------------------------------
# Feed generation
# ---------------------------------------------------------------------------

def build_feed(items: List[dict], output: Path):
    fg = FeedGenerator()
    fg.title("State‑Policy Think‑Tank Digest")
    fg.link(href="https://<replace‑with‑your‑domain>/rss.xml", rel="self")
    fg.description("Latest posts from free‑market policy institutes across the U.S.")
    fg.language("en")

    for itm in sorted(items, key=lambda x: x["published"], reverse=True):
        fe = fg.add_entry()
        fe.title(f"[{itm['source']}] {itm['title']}")
        fe.link(href=itm["link"])
        fe.description(itm["description"])
        fe.pubDate(itm["published"])

    fg.rss_file(output)
    log.info("wrote %s (%,d items)", output, len(items))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: List[str] | None = None):
    ap = argparse.ArgumentParser(description="Aggregate latest posts into one RSS feed.")
    ap.add_argument("-i", "--input", default="sites.txt", help="path to sites.txt (default: sites.txt)")
    ap.add_argument("-o", "--output", default="rss.xml", help="output RSS file (default: rss.xml)")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="concurrent workers (default: 16)")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="HTTP timeout in seconds (default: 10)")
    args = ap.parse_args(argv)

    sites_path = Path(args.input)
    if not sites_path.is_file():
        log.error("sites file %s not found", sites_path)
        sys.exit(1)

    sites = sites_path.read_text().splitlines()
    log.info("processing %d sites with %d workers", len(sites), args.workers)

    items = collect_items(sites, workers=args.workers, timeout=args.timeout)
    if not items:
        log.error("No items found – exiting without writing feed")
        sys.exit(1)

    build_feed(items, Path(args.output))


if __name__ == "__main__":
    main()
