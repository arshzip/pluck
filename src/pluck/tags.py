"""Tag writing: ID3v2.3 for MP3, iTunes-style atoms for M4A."""

from __future__ import annotations

from pathlib import Path

from mutagen.id3 import APIC, COMM, ID3, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TRCK, WOAS
from mutagen.mp4 import MP4, MP4Cover

from .cover import _mime


def tag_file(path: Path, info: dict, url: str, cover: bytes | None) -> None:
    """Dispatch on container: ID3v2.3 for MP3, iTunes-style atoms for M4A."""
    if path.suffix == ".mp3":
        _tag_id3(path, info, url, cover)
    else:
        _tag_mp4(path, info, url, cover)


def _common(info: dict) -> tuple[str, str, str, str | None, list]:
    title = info.get("title") or "Unknown title"
    artist = (info.get("channel") or info.get("uploader") or "Unknown artist").removesuffix(" - Topic")
    album = info.get("album") or info.get("title") or "Unknown album"
    year = info.get("release_year") or (info.get("upload_date") or "")[:4] or None
    return title, artist, album, year, info.get("genres") or []


def _tag_id3(path: Path, info: dict, url: str, cover: bytes | None) -> None:
    title, artist, album, year, genres = _common(info)

    tags = ID3()
    tags.add(TIT2(encoding=3, text=title))
    tags.add(TPE1(encoding=3, text=artist))
    tags.add(TPE2(encoding=3, text=artist))
    tags.add(TALB(encoding=3, text=album))
    if year:
        tags.add(TDRC(encoding=3, text=str(year)))
    if genres:
        tags.add(TCON(encoding=3, text=genres[0]))
    if info.get("track_number"):
        n, total = info["track_number"], info.get("track_total")
        tags.add(TRCK(encoding=3, text=f"{n}/{total}" if total else str(n)))
    tags.add(COMM(encoding=3, lang="eng", desc="source", text=url))
    tags.add(WOAS(url=url))
    if cover:
        tags.add(
            APIC(
                encoding=3,
                mime=_mime(cover),
                type=3,  # front cover
                desc="Cover",
                data=cover,
            )
        )
    tags.save(path, v2_version=3)


def _tag_mp4(path: Path, info: dict, url: str, cover: bytes | None) -> None:
    title, artist, album, year, genres = _common(info)

    tags = MP4(path)
    tags["\xa9nam"] = [title]
    tags["\xa9ART"] = [artist]
    tags["aART"] = [artist]
    tags["\xa9alb"] = [album]
    if year:
        tags["\xa9day"] = [str(year)]
    if genres:
        tags["\xa9gen"] = [genres[0]]
    if info.get("track_number"):
        tags["trkn"] = [(int(info["track_number"]), int(info.get("track_total") or 0))]
    tags["\xa9cmt"] = [url]
    if cover:
        kind = MP4Cover.FORMAT_PNG if _mime(cover) == "image/png" else MP4Cover.FORMAT_JPEG
        tags["covr"] = [MP4Cover(cover, imageformat=kind)]
    tags.save()
