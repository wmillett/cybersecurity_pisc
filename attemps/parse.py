import argparse
import sys
from pathlib import Path
from urllib.parse import urlparse


def _positive_depth(value: str) -> int:
    try:
        depth = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("depth must be a non-negative integer") from error
    if depth < 0:
        raise argparse.ArgumentTypeError("depth must be a non-negative integer")
    return depth


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    arguments = list(sys.argv[1:] if argv is None else argv)
    for index in range(len(arguments) - 1):
        option = arguments[index]
        next_argument = arguments[index + 1]
        looks_like_url = next_argument.lower().startswith(("http://", "https://"))
        if looks_like_url and option in {"-l", "--level"}:
            arguments[index] = "--level=5"
        elif looks_like_url and option in {"-p", "--path"}:
            arguments[index] = "--path=./data"

    parser = argparse.ArgumentParser(
        prog="spider",
        description="Download images from a web page, optionally following same-site links.",
    )
    parser.add_argument(
        "-r",
        action="store_true",
        help="recursively visit pages on the starting site",
    )
    parser.add_argument(
        "-l",
        "--level",
        type=_positive_depth,
        nargs="?",
        const=5,
        default=None,
        metavar="N",
        help="maximum link depth when using -r (default: 5)",
    )
    parser.add_argument(
        "-p",
        "--path",
        type=Path,
        nargs="?",
        const=Path("./data"),
        default=Path("./data"),
        metavar="PATH",
        help="directory for downloaded images (default: ./data/)",
    )
    parser.add_argument("url", help="starting HTTP or HTTPS URL")
    args = parser.parse_args(arguments)

    try:
        parsed_url = urlparse(args.url)
        parsed_url.port
    except ValueError:
        parser.error("URL has an invalid host or port")
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        parser.error("URL must be an absolute HTTP or HTTPS URL")
    if args.level is not None and not args.r:
        parser.error("-l can only be used together with -r")
    if args.level is None:
        args.level = 5
    return args
