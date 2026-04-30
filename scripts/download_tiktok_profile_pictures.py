#!/usr/bin/env python3
"""
Download actual TikTok profile pictures for a list of usernames.

Why this script exists:
- yt-dlp is great for videos and thumbnails, but not for TikTok profile avatars.
- TikTok often embeds avatar URLs inside page JSON.
- This script fetches the profile page, extracts the best avatar URL,
  downloads it, and stores metadata for archival.

Usage:
    python scripts/download_tiktok_profile_pictures.py \
        --input users.txt \
        --output-dir data/profile_pictures \
        --cookies cookies.txt
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import mimetypes
import pathlib
import re
import sys
import time
from typing import Any
from urllib.parse import urlparse

import requests


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def load_usernames(path: pathlib.Path) -> list[str]:
    """
    Read usernames from a text file.

    Supported input lines:
    - username
    - @username
    - full TikTok profile URL

    Blank lines and comments (# ...) are ignored.
    """
    usernames: list[str] = []

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        if line.startswith("http://") or line.startswith("https://"):
            parsed = urlparse(line)
            username = parsed.path.strip("/").lstrip("@")
        else:
            username = line.lstrip("@")

        if username:
            usernames.append(username)

    # Remove duplicates while keeping order.
    seen = set()
    unique = []
    for u in usernames:
        if u not in seen:
            seen.add(u)
            unique.append(u)

    return unique


def load_cookies(session: requests.Session, cookie_file: pathlib.Path | None) -> None:
    """
    Load Netscape-style cookie file into the session if present.

    Why optional?
    Some TikTok pages may be restricted or rate-limited when accessed anonymously.
    Cookies can improve reliability in GitHub Actions.
    """
    if not cookie_file or not cookie_file.exists():
        return

    for line in cookie_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        parts = line.split("\t")
        if len(parts) != 7:
            continue

        domain, _flag, path, secure, expires, name, value = parts
        session.cookies.set(
            name=name,
            value=value,
            domain=domain,
            path=path,
            secure=(secure.upper() == "TRUE"),
        )


def extract_json_blocks(page_html: str) -> list[Any]:
    """
    Extract likely TikTok JSON blobs from HTML.

    TikTok commonly stores profile metadata inside script tags such as:
    - SIGI_STATE
    - __UNIVERSAL_DATA_FOR_REHYDRATION__

    We parse both because TikTok changes internals over time.
    """
    patterns = [
        r'<script[^>]+id="SIGI_STATE"[^>]*>(.*?)</script>',
        r'<script[^>]+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
    ]

    blocks = []
    for pattern in patterns:
        match = re.search(pattern, page_html, re.DOTALL)
        if not match:
            continue

        raw_json = html.unescape(match.group(1))
        try:
            blocks.append(json.loads(raw_json))
        except json.JSONDecodeError:
            continue

    return blocks


def find_best_avatar(data: Any) -> str | None:
    """
    Recursively search for avatar URLs in TikTok JSON.

    Why recursive?
    TikTok's internal structure is not stable, so hardcoding one JSON path
    is brittle. Recursive search gives better long-term resilience.
    """
    candidates: list[tuple[int, str]] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                key_lower = str(key).lower()

                if isinstance(value, str) and value.startswith("http"):
                    if "avatar" in key_lower:
                        score = 0
                        if "larger" in key_lower or "large" in key_lower:
                            score = 30
                        elif "medium" in key_lower:
                            score = 20
                        elif "thumb" in key_lower:
                            score = 10
                        else:
                            score = 5
                        candidates.append((score, value))

                walk(value)

        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(data)

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def download_binary(session: requests.Session, url: str) -> tuple[bytes, str]:
    """
    Download a file and return (content, content_type).
    """
    response = session.get(url, headers=DEFAULT_HEADERS, timeout=30)
    response.raise_for_status()
    return response.content, response.headers.get("Content-Type", "")


def guess_extension(content_type: str, url: str) -> str:
    """
    Determine the file extension safely.

    Why not trust only the URL?
    CDN URLs may omit or hide the real extension. Content-Type is often safer.
    """
    ctype = content_type.split(";")[0].strip()
    ext = mimetypes.guess_extension(ctype) if ctype else None

    if ext:
        return ".jpg" if ext == ".jpe" else ext

    parsed_path = urlparse(url).path.lower()
    for candidate in (".jpg", ".jpeg", ".png", ".webp"):
        if parsed_path.endswith(candidate):
            return ".jpg" if candidate == ".jpeg" else candidate

    return ".jpg"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch_profile_picture(
    session: requests.Session,
    username: str,
    output_dir: pathlib.Path,
) -> dict[str, Any]:
    """
    Fetch profile page, extract avatar URL, download image, and write metadata.

    Returns a metadata dict describing the result.
    """
    profile_url = f"https://www.tiktok.com/@{username}"
    response = session.get(profile_url, headers=DEFAULT_HEADERS, timeout=30)
    response.raise_for_status()

    avatar_url = None
    for block in extract_json_blocks(response.text):
        avatar_url = find_best_avatar(block)
        if avatar_url:
            break

    result: dict[str, Any] = {
        "username": username,
        "profile_url": profile_url,
        "success": False,
    }

    if not avatar_url:
        result["error"] = "Avatar URL not found in page JSON."
        return result

    image_bytes, content_type = download_binary(session, avatar_url)
    extension = guess_extension(content_type, avatar_url)

    user_dir = output_dir / username
    user_dir.mkdir(parents=True, exist_ok=True)

    image_path = user_dir / f"avatar{extension}"
    metadata_path = user_dir / "metadata.json"

    image_path.write_bytes(image_bytes)

    metadata = {
        "username": username,
        "profile_url": profile_url,
        "avatar_url": avatar_url,
        "content_type": content_type,
        "sha256": sha256_bytes(image_bytes),
        "image_file": image_path.name,
        "downloaded_at_unix": int(time.time()),
    }

    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    result.update(
        {
            "success": True,
            "avatar_url": avatar_url,
            "image_path": str(image_path),
            "metadata_path": str(metadata_path),
        }
    )
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download TikTok profile pictures for usernames."
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Path to a text file containing TikTok usernames or profile URLs.",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory where profile pictures and metadata will be stored.",
    )
    parser.add_argument(
        "--cookies",
        required=False,
        help="Optional Netscape-format cookies.txt file.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    input_path = pathlib.Path(args.input)
    output_dir = pathlib.Path(args.output_dir)
    cookie_path = pathlib.Path(args.cookies) if args.cookies else None

    if not input_path.exists():
        print(f"Input file not found: {input_path}", file=sys.stderr)
        return 1

    output_dir.mkdir(parents=True, exist_ok=True)

    usernames = load_usernames(input_path)
    if not usernames:
        print("No usernames found in input file.", file=sys.stderr)
        return 1

    session = requests.Session()
    load_cookies(session, cookie_path)

    failures = 0

    for username in usernames:
        print(f"Processing @{username} ...")
        try:
            result = fetch_profile_picture(session, username, output_dir)
            if result["success"]:
                print(f"  Saved: {result['image_path']}")
            else:
                failures += 1
                print(f"  Failed: {result.get('error', 'Unknown error')}")
        except requests.HTTPError as exc:
            failures += 1
            print(f"  HTTP error for @{username}: {exc}")
        except Exception as exc:
            failures += 1
            print(f"  Unexpected error for @{username}: {exc}")

        # Be polite and reduce chance of rate limiting.
        time.sleep(2)

    if failures == len(usernames):
        print("All downloads failed.", file=sys.stderr)
        return 1

    print(f"Done. Success: {len(usernames) - failures}, Failures: {failures}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
