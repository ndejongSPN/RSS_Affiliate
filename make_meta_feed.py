#!/usr/bin/env python3
"""
make_meta_feed.py
=================
Combine the newest post from many RSS/Atom feeds into one RSS 2.0 file.

Extras in this version
----------------------
*   Concurrency (ThreadPoolExecutor)
*   Robust feed discovery
*   XML “repair” pass for malformed feeds
*   Per-feed override file (overrides.ini)
*   On-disk cache of last-good items
*   Structured logging & summary
"""
from __future__ import annotations

import argparse, hashlib, json, logging, re, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional
from urllib.parse import urljoin, urlparse

import configparser
import feedparser              # type: ignore
import requests
from bs4 import BeautifulSoup  # type: ignore
from dateutil import parser as dtparser       # type: ignore
from feedgen.feed import FeedGenerator        # type: ignore
from requests.exceptions import HTTPError
from tenacity import retry, stop_after_attempt, wait_exponential  # type: ignore

# --------------------------------------------------------------------------- #
# Config & logger
# --------------------------------------------------------------------------- #
DEFAULT_WORKERS  = 16
DEFAULT_TIMEOUT  = 10
USER_AGENT       = (
    "MetaFeedBot/1.3 (+https://github.com/<your-repo>;"
    " feed-contact@example.com)"
)
CACHE_FILE       = ".meta_feed_cache.json"
OVERRIDES_FILE   = "overrides.ini"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("meta-feed")

# --------------------------------------------------------------------------- #
# Requests helpers – global Session for TCP reuse
# --------------------------------------------------------------------------- #
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
def get_bytes(url: str, timeout: int) -> bytes:
    resp = SESSION.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content


# --------------------------------------------------------------------------- #
# Feed discovery
# --------------------------------------------------------------------------- #
COMMON_SUFFIXES = ("/feed", "/rss", "/rss.xml", "/atom.xml")
ALT_TYPES       = ["application/rss+xml", "application/atom+xml"]


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


def discover_feed(homepage: str, timeout: int) -> Optional[str]:
    try:
        html = get_bytes(homepage, timeout).decode(errors="ignore")
    except Exception as exc:
        log.debug("fetch %s failed: %s", homepage, exc)
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


# --------------------------------------------------------------------------- #
# Tiny cache helpers
# --------------------------------------------------------------------------- #
def _key(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()


def load_cache() -> dict[str, dict]:
    try:
        return json.loads(Path(CACHE_FILE).read_text())
    except Exception:
        return {}


def save_cache(cache: dict[str, dict]) -> None:
    try:
        Path(CACHE_FILE).write_text(
            json.dumps(cache, default=str, indent=1)
        )
    except Exception as exc:
        log.warning("cannot write cache: %s", exc)


# --------------------------------------------------------------------------- #
# Overrides
# --------------------------------------------------------------------------- #
def load_overrides() -> dict[str, str]:
    cfg = configparser.ConfigParser()
    cfg.read(OVERRIDES_FILE)
    return {
        host: sect.get("feed")
        for host, sect in cfg.items()
        if host != "DEFAULT" and sect.get("feed")
    }


OVERRIDES = load_overrides()


# --------------------------------------------------------------------------- #
# Feed parsing with repair pass
# --------------------------------------------------------------------------- #
RE_BOM = re.compile(rb"^\xef\xbb\xbf")
RE_MISSING_LT = re.compile(br"^\?\s*xml")

def _repair_xml(raw: bytes) -> bytes:
    """Fix common trivial malformations."""
    raw = RE_BOM.sub(b"", raw, count=1)          # strip UTF-8 BOM
    if RE_MISSING_LT.match(raw):                 # '<?xml' but missing '<'
        raw = b"<" + raw
    return raw


def parse_feed(url: str, timeout: int):
    try:
        raw = get_bytes(url, timeout)
    except HTTPError as e:
        if e.response.status_code == 403:
            # Second-chance fetch with polite delay + Accept-Language header
            time.sleep(2)
            SESSION.headers["Accept-Language"] = "en-US,en;q=0.8"
            raw = get_bytes(url, timeout)
        else:
            raise

    parsed = feedparser.parse(raw)
    if parsed.entries:
        return parsed

    # Try repair
    repaired = feedparser.parse(_repair_xml(raw))
    return repaired


def validate_feed(url: str, timeout: int) -> bool:
    try:
        return bool(parse_feed(url, timeout).entries)
    except Exception:
        return False


def get_latest_item(feed_url: str, timeout: int) -> Optional[dict]:
    parsed = parse_feed(feed_url, timeout)
    if not parsed.entries:
        return None
    e = parsed.entries[0]

    if hasattr(e, "published_parsed") and e.published_parsed:
        published = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
    elif getattr(e, "published", None):
        try:
            published = dtparser.parse(e.published).astimezone(timezone.utc)
        except (ValueError, TypeError):
            published = datetime.now(timezone.utc)
    else:
        published = datetime.now(timezone.utc)

    return {
        "title":        e.get("title", "Untitled"),
        "link":         e.get("link"),
        "description":  e.get("summary", ""),
        "published":    published,
    }


# --------------------------------------------------------------------------- #
# Core collection
# --------------------------------------------------------------------------- #
def collect_items(
    sites: Iterable[str],
    *,
    workers: int,
    timeout: int,
    discover: bool,
    cache: dict[str, dict],
) -> List[dict]:
    items: List[dict] = []

    def process(line: str):
        url = normalize_url(line)
        if not url:
            return None

        # Override?
        host = urlparse(url).hostname or ""
        feed_url = OVERRIDES.get(host)

        if not feed_url:
            # If the line already looks like a feed, keep it
            if any(url.endswith(ext) for ext in (".rss", ".xml", "/feed", "/rss", "/atom.xml")):
                feed_url = url
            elif discover:
                feed_url = discover_feed(url, timeout) or url
            else:
                feed_url = url    # trust the user-supplied URL

        try:
            if validate_feed(feed_url, timeout):
                itm = get_latest_item(feed_url, timeout)
                if itm:
                    itm["source"] = friendly_name(url)
                    cache[_key(feed_url)] = itm
                    log.info("✔ %s – %s", itm["source"], itm["title"][:60])
                    return itm
        except Exception as exc:
            log.debug("%s – %s", feed_url, exc)

        # fallback to cached entry, if any
        stale = cache.get(_key(feed_url))
        if stale:
            log.info("⚠ using cached entry for %s", friendly_name(url))
            return stale
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


# --------------------------------------------------------------------------- #
# Feed generation
# --------------------------------------------------------------------------- #
def build_feed(items: List[dict], output: Path):
    fg = FeedGenerator()
    fg.title("State-Policy Think-Tank Digest")
    fg.link(href="https://<replace-with-your-domain>/rss.xml", rel="self")
    fg.description("Latest posts from free-market policy institutes across the U.S.")
    fg.language("en")

    for itm in sorted(items, key=lambda x: x["published"], reverse=True):
        fe = fg.add_entry()
        fe.title(f"[{itm['source']}] {itm['title']}")
        fe.link(href=itm["link"])
        fe.description(itm["description"])
        fe.pubDate(itm["published"])

    fg.rss_file(output)
    log.info("wrote %s (%d items)", output, len(items))


# --------------------------------------------------------------------------- #
# CLI & main
# --------------------------------------------------------------------------- #
def main(argv: List[str] | None = None):
    ap = argparse.ArgumentParser(description="Aggregate newest post from many feeds.")
    ap.add_argument("-i", "--input", default="sites.txt")
    ap.add_argument("-o", "--output", default="rss.xml")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    ap.add_argument("--discover", action="store_true", help="try homepage discovery")
    ap.add_argument("--verbose",  action="store_true", help="DEBUG logging")
    args = ap.parse_args(argv)

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    site_file = Path(args.input)
    if not site_file.is_file():
        log.error("sites file %s not found", site_file)
        sys.exit(1)

    sites = [
        ln for ln in site_file.read_text().splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    log.info("processing %d sites with %d workers", len(sites), args.workers)

    cache = load_cache()

    items = collect_items(
        sites,
        workers=args.workers,
        timeout=args.timeout,
        discover=args.discover,
        cache=cache,
    )

    save_cache(cache)

    fresh = sum(1 for i in items if _key(i["link"]) not in cache)
    cached = len(items) - fresh
    failed = len(sites) - len(items)

    build_feed(items, Path(args.output))

    log.info(
        "run complete → fresh: %d | cached: %d | failed: %d",
        fresh, cached, failed,
    )


if __name__ == "__main__":
    main()
