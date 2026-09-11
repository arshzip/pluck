"""Album art: pick the best available cover and download it."""

from __future__ import annotations

import re

import requests


def best_cover_url(info: dict, vid: str) -> str | None:
    """Prefer the square album art YouTube Music exposes on googleusercontent.com."""
    squares = [
        t
        for t in info.get("thumbnails") or []
        if "googleusercontent.com" in (t.get("url") or "")
    ]
    if squares:
        def area(t: dict) -> int:
            w, h = t.get("width") or 0, t.get("height") or 0
            return w * h

        return max(squares, key=area)["url"]
    return f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg"


def _upscale(url: str) -> str:
    """googleusercontent art is dynamic — ask for a 1200px square instead of 544."""
    if "googleusercontent.com" in url and "=" in url:
        return re.sub(r"=.*$", "=w1200-h1200", url)
    return url


def fetch_cover(info: dict, vid: str) -> bytes | None:
    candidates = [
        _upscale(best_cover_url(info, vid) or ""),
        f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg",
        f"https://i.ytimg.com/vi/{vid}/sddefault.jpg",
        f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
    ]
    for url in candidates:
        if not url:
            continue
        try:
            resp = requests.get(url, timeout=15)
            if resp.ok and resp.content[:2] in (b"\xff\xd8", b"\x89P"):
                return resp.content
        except requests.RequestException:
            continue
    return None


def _mime(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return "image/webp"
