#!/usr/bin/env python3
"""spider - download images from a website, optionally following links.

Usage: ./spider [-rlp] URL
  -r        recursively visit pages of the starting site
  -r -l [N] maximum depth of the recursion (default: 5)
  -p [PATH] directory where images are saved (default: ./data/)
"""

import argparse
import hashlib
import http.client
import re
import shutil
import sys
import time
from collections import deque
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote, urldefrag, urljoin, urlparse, urlsplit, urlunsplit
from urllib.request import Request, urlopen

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp"}
HTML_TYPES = {"text/html", "application/xhtml+xml"}
DEFAULT_DEPTH = 5
DEFAULT_PATH = Path("./data")
HISTORY_FILE = Path(__file__).resolve().parent / "path_history.txt"
TIMEOUT_SECONDS = 15
REQUEST_DELAY = 0.05  # seconds between requests to avoid overloading the server
USER_AGENT = "Mozilla/5.0 (Spider/1.0; educational project)"
CSS_URL = re.compile(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", re.I)


# ---------------------------------------------------------------------------
# 1. Argument parsing
# ---------------------------------------------------------------------------
def _non_negative_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("depth must be a non-negative integer") from error
    if number < 0:
        raise argparse.ArgumentTypeError("depth must be a non-negative integer")
    return number


def _looks_like_url(value: str) -> bool:
    return value.lower().startswith(("http://", "https://"))


def _fill_optional_values(arguments: list[str]) -> list[str]:
    """Make `-l` / `-p` work without a value when directly followed by the URL.

    The subject writes `-l [N]` and `-p [PATH]`: the value is optional, so
    `./spider -r -l URL` must not swallow the URL as the value of -l.
    """
    fixed: list[str] = []
    for index, argument in enumerate(arguments):
        followed_by_url = index + 1 < len(arguments) and _looks_like_url(arguments[index + 1])
        if followed_by_url and argument in {"-l", "--level"}:
            fixed.append(f"--level={DEFAULT_DEPTH}")
        elif followed_by_url and argument in {"-p", "--path"}:
            fixed.append(f"--path={DEFAULT_PATH}")
        elif followed_by_url and re.fullmatch(r"-[rlp]{2,}", argument) and argument[-1] in "lp":
            # bundled flags such as -rl or -rp
            fixed.append(argument[:-1])
            fixed.append(f"--level={DEFAULT_DEPTH}" if argument[-1] == "l" else f"--path={DEFAULT_PATH}")
        else:
            fixed.append(argument)
    return fixed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    arguments = _fill_optional_values(list(sys.argv[1:] if argv is None else argv))

    parser = argparse.ArgumentParser(
        prog="spider",
        usage="./spider [-rlp] URL",
        description="Download images from a web page, optionally following same-site links.",
    )
    parser.add_argument("-r", action="store_true",
                        help="recursively visit pages on the starting site")
    parser.add_argument("-l", "--level", type=_non_negative_int, nargs="?",
                        const=DEFAULT_DEPTH, default=None, metavar="N",
                        help="maximum link depth when using -r (default: 5)")
    parser.add_argument("-p", "--path", type=Path, nargs="?",
                        const=DEFAULT_PATH, default=DEFAULT_PATH, metavar="PATH",
                        help="directory for downloaded images (default: ./data/)")
    parser.add_argument("url", help="starting HTTP or HTTPS URL")
    args = parser.parse_args(arguments)

    try:
        parsed = urlparse(args.url)
        parsed.port  # raises ValueError on an invalid port
    except ValueError:
        parser.error("URL has an invalid host or port")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        parser.error("URL must be an absolute HTTP or HTTPS URL")
    if args.level is not None and not args.r:
        parser.error("-l can only be used together with -r")
    if args.level is None:
        args.level = DEFAULT_DEPTH
    return args


# ---------------------------------------------------------------------------
# 2. URL helpers
# ---------------------------------------------------------------------------
def _normalize(url: str) -> str:
    """Percent-encode spaces / non-ASCII characters like a browser would."""
    parts = urlsplit(url)
    netloc = parts.netloc
    try:
        netloc.encode("ascii")
    except UnicodeEncodeError:  # internationalised domain name
        netloc = (parts.hostname or "").encode("idna").decode("ascii")
        if parts.port:
            netloc += f":{parts.port}"
    path = quote(parts.path, safe="/%:@!$&'()*+,;=~") or "/"
    query = quote(parts.query, safe="=&%:@/?!$'()*+,;~")
    return urlunsplit((parts.scheme, netloc, path, query, ""))


def _resolve(base: str, reference: str) -> str | None:
    """Absolute, normalized, fragment-free http(s) URL, or None if unusable."""
    try:
        absolute = urldefrag(urljoin(base, reference.strip())).url
        if urlparse(absolute).scheme not in {"http", "https"}:
            return None
        return _normalize(absolute)
    except (ValueError, UnicodeError):
        return None


def _site(url: str) -> tuple[str, int | None]:
    """Host identity used for the same-site check (scheme ignored so that an
    http -> https upgrade does not stop the crawl)."""
    parsed = urlparse(url)
    return (parsed.hostname or "").lower(), parsed.port


def _is_image(url: str) -> bool:
    return Path(urlparse(url).path).suffix.lower() in IMAGE_EXTENSIONS


def _image_filename(url: str, used_names: dict[str, str]) -> str:
    name = Path(unquote(urlparse(url).path)).name
    if name in {"", ".", ".."}:
        name = "image"
    name = name.replace("\x00", "")
    path = Path(name)
    if len(name) > 150:  # keep filesystem-friendly length, preserve extension
        name = path.stem[:140] + path.suffix
        path = Path(name)
    previous = used_names.get(name)
    if previous is not None and previous != url:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]
        name = f"{path.stem}-{digest}{path.suffix}"
    used_names[name] = url
    return name


# ---------------------------------------------------------------------------
# 3. HTML parsing
# ---------------------------------------------------------------------------
class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.images: list[str] = []
        self.links: list[str] = []

    def _add_srcset(self, srcset: str) -> None:
        # split on a comma followed by whitespace: URLs may contain commas
        # (e.g. .../w_955,h_511,al_c/...)
        for candidate in re.split(r",\s+", srcset.strip()):
            parts = candidate.strip().split()
            if parts:
                self.images.append(parts[0])

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value for key, value in attrs if value}

        if tag in {"img", "source"}:
            for key in ("src", "data-src", "data-original"):
                if key in attributes:
                    self.images.append(attributes[key])
            for key in ("srcset", "data-srcset"):
                if key in attributes:
                    self._add_srcset(attributes[key])
        elif tag == "a" and "href" in attributes:
            self.links.append(attributes["href"])
        elif tag == "meta":
            key = (attributes.get("property") or attributes.get("name") or "").lower()
            if key in {"og:image", "og:image:url", "twitter:image"} and "content" in attributes:
                self.images.append(attributes["content"])

        # inline CSS: style="background-image: url(...)"
        if "style" in attributes:
            self.images.extend(CSS_URL.findall(attributes["style"]))


# ---------------------------------------------------------------------------
# 4. Network
# ---------------------------------------------------------------------------
def _decode(content: bytes, charset: str | None) -> str:
    """Decode with the declared charset, falling back to UTF-8 when the server
    announces one Python does not know (e.g. 'utf-6')."""
    try:
        return content.decode(charset or "utf-8", errors="replace")
    except (LookupError, TypeError):
        return content.decode("utf-8", errors="replace")


def _fetch_page(url: str) -> tuple[PageParser, str] | None:
    """Return (parsed page, final URL after redirects) or None on failure."""
    request = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            final_url = response.geturl()
            if response.headers.get_content_type() not in HTML_TYPES:
                print("    (skipping non-HTML page)")
                return PageParser(), final_url
            charset = response.headers.get_content_charset()
            content = response.read()
    except (OSError, ValueError, http.client.HTTPException) as error:
        print(f"spider: could not fetch {url}: {error}", file=sys.stderr)
        return None

    parser = PageParser()
    parser.feed(_decode(content, charset))
    return parser, final_url


def _download_image(url: str, output_dir: Path, used_names: dict[str, str]) -> bool:
    filename = _image_filename(url, used_names)
    destination = output_dir / filename
    request = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            if response.headers.get_content_type() in HTML_TYPES:
                print(f"    (skipping {url}: server returned a web page, not an image)")
                return True
            with destination.open("wb") as output:
                shutil.copyfileobj(response, output)  # streamed, not loaded in RAM
    except (OSError, ValueError, http.client.HTTPException) as error:
        destination.unlink(missing_ok=True)  # remove partial file
        print(f"spider: could not download {url}: {error}", file=sys.stderr)
        return False
    print(f"    [+] {url} -> {destination}")
    return True


# ---------------------------------------------------------------------------
# 5. Crawler
# ---------------------------------------------------------------------------
def crawl(start_url: str, recursive: bool, max_depth: int, output_dir: Path) -> bool:
    start_url = _normalize(start_url)
    site: tuple[str, int | None] | None = None  # set from the first response
    pending: deque[tuple[str, int]] = deque([(start_url, 0)])
    visited_pages: set[str] = set()
    downloaded: set[str] = set()
    used_names: dict[str, str] = {}
    successful = True

    while pending:
        page_url, depth = pending.popleft()
        if page_url in visited_pages:
            continue
        visited_pages.add(page_url)

        print(f"[depth {depth}] {page_url}")
        time.sleep(REQUEST_DELAY)
        result = _fetch_page(page_url)
        if result is None:
            successful = False
            continue
        page, final_url = result
        if site is None:
            site = _site(final_url)  # follows an initial redirect (http -> https, www, ...)
        visited_pages.add(final_url)

        candidates = list(page.images)
        next_pages: list[str] = []
        for link in page.links:
            link_url = _resolve(final_url, link)
            if link_url is None:
                continue
            if _is_image(link_url):
                candidates.append(link_url)  # <a href="photo.jpg"> full-size image
            elif recursive and depth < max_depth and _site(link_url) == site:
                next_pages.append(link_url)

        for reference in candidates:
            image_url = _resolve(final_url, reference)
            if image_url is None or not _is_image(image_url) or image_url in downloaded:
                continue
            downloaded.add(image_url)
            time.sleep(REQUEST_DELAY)
            if not _download_image(image_url, output_dir, used_names):
                successful = False

        for next_url in next_pages:
            if next_url not in visited_pages:
                pending.append((next_url, depth + 1))

    print(f"\nDone: {len(downloaded)} image(s) from {len(visited_pages)} page(s).")
    return successful


def _record_path(path: Path) -> None:
    """Append `path` to the history file unless it is already listed."""
    entry = str(path.resolve())  # absolute, so ./data and data/ are the same entry
    try:
        known: set[str] = set()
        if HISTORY_FILE.exists():
            known = set(HISTORY_FILE.read_text(encoding="utf-8").splitlines())
        if entry in known:
            return
        with HISTORY_FILE.open("a", encoding="utf-8") as history:
            history.write(entry + "\n")
    except OSError as error:  # a broken history file must never stop the crawl
        print(f"spider: cannot update {HISTORY_FILE}: {error}", file=sys.stderr)

def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        args.path.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        print(f"spider: cannot create output directory {args.path}: {error}", file=sys.stderr)
        return 1
    _record_path(args.path)
    return 0 if crawl(args.url, args.r, args.level, args.path) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
