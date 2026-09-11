"""Core data model + filename helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Track:
    vid: str
    title: str
    artist: str
    duration: float | None
    url: str
    kind: str = "video"  # "song" | "video"
    album: str | None = None
    album_id: str | None = None
    year: str | None = None
    track_number: int | None = None
    track_total: int | None = None
    cover_url: str | None = None
    folder: str | None = None  # rip into ~/Downloads/Pluck/<folder>/


def _to_seconds(value) -> float | None:
    """Accept int seconds or a 'm:ss' string."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        parts = [int(p) for p in str(value).split(":")]
    except ValueError:
        return None
    while len(parts) < 3:
        parts.insert(0, 0)
    hours, mins, secs = parts
    return hours * 3600 + mins * 60 + secs


def safe_name(text: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "—", text).strip()
