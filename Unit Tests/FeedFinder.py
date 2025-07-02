#!/usr/bin/env python3
"""
test_feeds.py – quick audit of how many sites in sites.txt publish an RSS/Atom feed

Dependencies:
  pip install requests beautifulsoup4 feedparser
"""

from pathlib import Path
from urllib.parse import urlparse, urljoin
import re, sys
import requests
from bs4 import BeautifulSoup
import feedparser

# -------------- configuration ----------------------------------------------

SITES_FILE = Path("/workspaces/RSS_Affiliate/sites.txt")
TIMEOUT     = 8     # seconds per request
HEADERS     = {"User-Agent": "RSS-Checker/1.0 (+https://example.com)"}

# -------------- helpers -----------------------------------------------------

def normalize(raw: str) -> str | None:
    """Strip comments/whitespace and prepend scheme if missing."""
    raw = raw.split("#", 1)[0].strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    return raw

FEED_HINT_RE = re.compile(r"\.(rss|xml)$|/feed/?$|[?&]format=rss", re.I)

def has_feed_signature(url: str) -> bool:
    """Cheap yes/no without fetching anything."""
    return bool(FEED_HINT_RE.search(url))

def discover_feed(homepage: str) -> str | None:
    """Fetch the HTML, look for <link rel="alternate" type="application/rss+xml">."""
    try:
        html = requests.get(homepage, headers=HEADERS, timeout=TIMEOUT).text
    except Exception:
        return None
    soup = BeautifulSoup(html, "html.parser")
    link = soup.find("link", type="application/rss+xml")
    if link and link.get("href"):
        return urljoin(homepage, link["href"])
    return None

def validate_feed(url: str) -> bool:
    """Run feedparser; consider it valid if we parse >=1 entry."""
    try:
        parsed = feedparser.parse(url)
        return bool(parsed.entries)
    except Exception:
        return False

# -------------- main --------------------------------------------------------

def main():
    if not SITES_FILE.exists():
        sys.exit(f"❌ {SITES_FILE} not found")

    found, total = 0, 0

    for raw in SITES_FILE.read_text().splitlines():
        site = normalize(raw)
        if not site:
            continue
        total += 1
        feed_url = None

        # (a) direct hint
        if has_feed_signature(site) and validate_feed(site):
            feed_url = site

        # (b) <link rel="alternate">
        if not feed_url:
            alt = discover_feed(site)
            if alt and validate_feed(alt):
                feed_url = alt

        # (c) naive WordPress guess
        if not feed_url:
            guess = urljoin(site.rstrip("/") + "/", "feed")
            if guess != site and validate_feed(guess):
                feed_url = guess

        if feed_url:
            found += 1
            print(f"✔  {site:<40} → {feed_url}")
        else:
            print(f"✖  {site}")

    print("\n— summary —")
    print(f"{found} of {total} sites expose a working RSS/Atom feed.")

if __name__ == "__main__":
    main()
