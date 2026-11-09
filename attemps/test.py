#!/usr/bin/env python3
"""spider - recursively download images from a website.

Usage: ./spider [-rlp] URL
  -r        recursively download images found in the URL
  -r -l N   maximum depth level of the recursion (default: 5)
  -p PATH   directory where files are saved (default: ./data/)
"""

import argparse
import hashlib
import os
import sys
from collections import deque
from html.parser import HTMLParser
from urllib.error import URLError, HTTPError
from urllib.parse import urljoin, urlparse, urldefrag
from urllib.request import Request, urlopen
from urllib.parse import quote, urlsplit, urlunsplit
import http.client

EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".bmp")
DEFAULT_DEPTH = 5
DEFAULT_PATH = "./data/"
TIMEOUT = 10
HEADERS = {"User-Agent": "Mozilla/5.0 (spider; educational project)"}


# --------------------------------------------------------------------------
# 1. Argument parsing
# --------------------------------------------------------------------------
def parse_args():
    parser = argparse.ArgumentParser(
        prog="spider",
        usage="./spider [-rlp] URL",
        description="Download all the images from a website.",
    )
    parser.add_argument("-r", action="store_true",
                        help="recursively download images")
    parser.add_argument("-l", metavar="N", type=int, default=None,
                        help="max recursion depth (requires -r, default: 5)")
    parser.add_argument("-p", metavar="PATH", default=DEFAULT_PATH,
                        help="download directory (default: ./data/)")
    parser.add_argument("URL", help="URL to scrape")
    args = parser.parse_args()

    # -l only makes sense together with -r
    if args.l is not None and not args.r:
        parser.error("option -l requires -r")
    if args.l is not None and args.l < 0:
        parser.error("depth (-l) must be a non-negative integer")
    if args.l is None:
        args.l = DEFAULT_DEPTH

    # URL must be http(s)
    parsed = urlparse(args.URL)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        parser.error("URL must start with http:// or https://")

    return args


# --------------------------------------------------------------------------
# 2. HTML parsing (collect <img src> and <a href>)
# --------------------------------------------------------------------------
class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.images = set()
        self.links = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "img":
            if attrs.get("src"):
                self.images.add(attrs["src"])
            # also catch lazy-loaded / responsive images
            if attrs.get("data-src"):
                self.images.add(attrs["data-src"])
            if attrs.get("srcset"):
                for part in attrs["srcset"].split(","):
                    candidate = part.strip().split(" ")[0]
                    if candidate:
                        self.images.add(candidate)
        elif tag == "a" and attrs.get("href"):
            self.links.add(attrs["href"])


# --------------------------------------------------------------------------
# 3. Networking / file helpers
# --------------------------------------------------------------------------
def normalize(url):
    p = urlsplit(url)
    return urlunsplit((
        p.scheme, p.netloc,
        quote(p.path, safe="/%:@!$&'()*+,;=~"),
        quote(p.query, safe="=&%:@/?!$'()*+,;~"),
        "",
    ))

def fetch(url):
    """Return (content_type, bytes) or None on failure."""
    url = normalize(url)
    try:
        with urlopen(Request(url, headers=HEADERS), timeout=TIMEOUT) as resp:
            return resp.headers.get("Content-Type", ""), resp.read()
    except (OSError, ValueError, http.client.HTTPException) as exc:
        print(f"[!] Cannot fetch {url}: {exc}", file=sys.stderr)
        return None


def has_image_extension(url):
    return urlparse(url).path.lower().endswith(EXTENSIONS)


def save_image(url, data, directory):
    name = os.path.basename(urlparse(url).path) or "image"
    path = os.path.join(directory, name)
    if os.path.exists(path):  # avoid overwriting different files with same name
        stem, ext = os.path.splitext(name)
        suffix = hashlib.md5(url.encode()).hexdigest()[:8]
        path = os.path.join(directory, f"{stem}_{suffix}{ext}")
    with open(path, "wb") as f:
        f.write(data)
    return path


# --------------------------------------------------------------------------
# 4. Crawler
# --------------------------------------------------------------------------
def crawl(start_url, recursive, max_depth, directory):
    os.makedirs(directory, exist_ok=True)
    base_host = urlparse(start_url).netloc

    visited_pages = set()
    downloaded = set()
    queue = deque([(start_url, 0)])  # (url, depth)

    while queue:
        url, depth = queue.popleft()
        url, _ = urldefrag(url)
        if url in visited_pages:
            continue
        visited_pages.add(url)

        print(f"[depth {depth}] {url}")
        result = fetch(url)
        if result is None:
            continue
        content_type, body = result
        if "html" not in content_type.lower():
            continue

        parser = PageParser()
        parser.feed(body.decode("utf-8", errors="replace"))

        # download images of this page
        for src in parser.images:
            img_url, _ = urldefrag(urljoin(url, src))
            if img_url in downloaded or not has_image_extension(img_url):
                continue
            downloaded.add(img_url)
            img = fetch(img_url)
            if img is None:
                continue
            path = save_image(img_url, img[1], directory)
            print(f"    [+] {img_url} -> {path}")

        # follow links (same host only) while depth allows it
        if recursive and depth < max_depth:
            for href in parser.links:
                link = urljoin(url, href)
                p = urlparse(link)
                if p.scheme in ("http", "https") and p.netloc == base_host:
                    queue.append((link, depth + 1))

    print(f"\nDone: {len(downloaded)} image(s) found in "
          f"{len(visited_pages)} page(s).")


# --------------------------------------------------------------------------
def main():
    args = parse_args()
    crawl(args.URL, args.r, args.l, args.p)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)

