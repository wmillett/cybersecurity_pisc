import argparse
import hashlib
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import URLError
from urllib.parse import unquote, urldefrag, urljoin, urlparse
from urllib.request import Request, urlopen

from attemps.parse import parse_args


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp"}
TIMEOUT_SECONDS = 15


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.images: list[str] = []
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag in {"img", "source"}:
            src = attributes.get("src")
            if src:
                self.images.append(src)
            srcset = attributes.get("srcset")
            if srcset:
                self.images.extend(
                    candidate.strip().split()[0]
                    for candidate in srcset.split(",")
                    if candidate.strip()
                )
        elif tag == "a":
            href = attributes.get("href")
            if href:
                self.links.append(href)


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlparse(url)
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port


def _is_image(url: str) -> bool:
    return Path(urlparse(url).path).suffix.lower() in IMAGE_EXTENSIONS


def _image_filename(url: str, used_names: dict[str, str]) -> str:
    name = Path(unquote(urlparse(url).path)).name
    if not name:
        name = "image"
    previous_url = used_names.get(name)
    if previous_url is not None and previous_url != url:
        suffix = Path(name).suffix
        stem = Path(name).stem
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]
        name = f"{stem}-{digest}{suffix}"
    used_names[name] = url
    return name


def _download_image(
    url: str, output_dir: Path, used_names: dict[str, str]
) -> bool:
    filename = _image_filename(url, used_names)
    request = Request(url, headers={"User-Agent": "Spider/1.0"})
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            data = response.read()
        destination = output_dir / filename
        destination.write_bytes(data)
    except (OSError, TimeoutError, URLError) as error:
        print(f"spider: could not download {url}: {error}", file=sys.stderr)
        return False
    print(f"Downloaded {url} -> {destination}")
    return True


def _fetch_page(url: str) -> PageParser | None:
    request = Request(url, headers={"User-Agent": "Spider/1.0"})
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            if response.headers.get_content_type() not in {
                "text/html",
                "application/xhtml+xml",
            }:
                print(f"spider: skipping non-HTML page {url}", file=sys.stderr)
                return PageParser()
            content = response.read()
    except (OSError, TimeoutError, URLError) as error:
        print(f"spider: could not fetch {url}: {error}", file=sys.stderr)
        return None

    parser = PageParser()
    parser.feed(content.decode("utf-8", errors="replace"))
    return parser


def crawl(url: str, recursive: bool, max_depth: int, output_dir: Path) -> bool:
    site_origin = _origin(url)
    pending = [(url, 0)]
    visited: set[str] = set()
    used_names: dict[str, str] = {}
    successful = True

    while pending:
        page_url, depth = pending.pop(0)
        page_url = urldefrag(page_url).url
        if page_url in visited:
            continue
        visited.add(page_url)

        page = _fetch_page(page_url)
        if page is None:
            successful = False
            continue

        for image_ref in page.images:
            image_url = urldefrag(urljoin(page_url, image_ref)).url
            if urlparse(image_url).scheme not in {"http", "https"}:
                continue
            if _is_image(image_url):
                if not _download_image(image_url, output_dir, used_names):
                    successful = False

        if recursive and depth < max_depth:
            for link in page.links:
                next_url = urldefrag(urljoin(page_url, link)).url
                parsed = urlparse(next_url)
                if (
                    parsed.scheme in {"http", "https"}
                    and _origin(next_url) == site_origin
                    and not _is_image(next_url)
                    and next_url not in visited
                ):
                    pending.append((next_url, depth + 1))

    return successful


def main(argv: list[str] | None = None) -> int:
    args: argparse.Namespace = parse_args(argv)
    try:
        args.path.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        print(f"spider: cannot create output directory {args.path}: {error}", file=sys.stderr)
        return 1
    return 0 if crawl(args.url, args.r, args.level, args.path) else 1


if __name__ == "__main__":
    raise SystemExit(main())
