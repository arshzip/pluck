"""YouTube Music / YouTube access: search, metadata, collections, streaming."""

from __future__ import annotations

import re

import yt_dlp
from ytmusicapi import YTMusic

from . import config
from .models import Track, _to_seconds, safe_name

URL_RE = re.compile(r"https?://(www\.|music\.)?(youtube\.com|youtu\.be)/\S+")


def parse_video_id(text: str) -> str | None:
    m = re.search(r"(?:v=|youtu\.be/|shorts/)([\w-]{11})", text)
    return m.group(1) if m else None


def parse_list_id(text: str) -> str | None:
    m = re.search(r"[?&]list=([\w-]+)", text)
    return m.group(1) if m else None


def search(query: str) -> list[Track]:
    """YT Music search: real songs on top, videos underneath."""
    try:
        yt = YTMusic()
        songs = (yt.search(query, filter="songs", limit=5) or [])[:5]
        videos = (yt.search(query, filter="videos", limit=3) or [])[:3]
    except Exception:
        return _fallback_search(query)

    tracks, seen = [], set()
    for bucket, kind in ((songs, "song"), (videos, "video")):
        for r in bucket:
            vid = r.get("videoId")
            if not vid or vid in seen:
                continue
            seen.add(vid)
            thumbs = r.get("thumbnails") or []
            tracks.append(
                Track(
                    vid=vid,
                    title=r.get("title") or "—",
                    artist=(r.get("artists") or [{}])[0].get("name") or r.get("author") or "—",
                    duration=_to_seconds(r.get("duration_seconds") or r.get("duration")),
                    url=f"https://music.youtube.com/watch?v={vid}",
                    kind=kind,
                    album=(r.get("album") or {}).get("name"),
                    album_id=(r.get("album") or {}).get("id"),
                    year=str(r.get("year") or "") or None,
                    cover_url=max(thumbs, key=lambda t: t.get("height") or 0)["url"] if thumbs else None,
                )
            )
    return tracks


def _fallback_search(query: str) -> list[Track]:
    """yt-dlp ytsearch fallback when YT Music search is unreachable."""
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "noplaylist": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch5:{query}", download=False)
    tracks = []
    for entry in info.get("entries") or []:
        if not entry or not entry.get("id"):
            continue
        channel = entry.get("channel") or entry.get("uploader") or "—"
        tracks.append(
            Track(
                vid=entry["id"],
                title=entry.get("title") or "—",
                artist=channel,
                duration=_to_seconds(entry.get("duration")),
                url=f"https://music.youtube.com/watch?v={entry['id']}",
                kind="song" if channel.endswith(" - Topic") else "video",
            )
        )
    return tracks


def fetch_collection(list_id: str) -> dict | None:
    """Album (or playlist) metadata + fully-populated tracks via YT Music."""
    yt = YTMusic()
    try:
        album = yt.get_album(list_id)
    except Exception:
        album = None

    if album and album.get("tracks"):
        artist = (album.get("artists") or [{}])[0].get("name") or "—"
        thumbs = album.get("thumbnails") or []
        cover = max(thumbs, key=lambda t: t.get("height") or 0)["url"] if thumbs else None
        tracks = [
            Track(
                vid=t["videoId"],
                title=t.get("title") or "—",
                artist=(t.get("artists") or [{}])[0].get("name") or artist,
                duration=_to_seconds(t.get("duration_seconds") or t.get("duration")),
                url=f"https://music.youtube.com/watch?v={t['videoId']}",
                kind="song",
                album=album.get("title"),
                year=str(album.get("year") or "") or None,
                track_number=t.get("trackNumber"),
                track_total=len(album["tracks"]),
                cover_url=cover,
                folder=safe_name(f"{artist} - {album.get('title')}"),
            )
            for t in album["tracks"]
            if t.get("videoId")
        ]
        return {"name": album.get("title"), "artist": artist, "year": album.get("year"), "tracks": tracks}

    playlist = yt.get_playlist(list_id.removeprefix("VL"))
    author = (playlist.get("author") or {}).get("name") or "—"
    tracks = []
    for t in playlist.get("tracks") or []:
        if not t.get("videoId"):
            continue  # private / unavailable entries
        thumbs = t.get("thumbnails") or []
        album_ref = t.get("album") or {}
        tracks.append(
            Track(
                vid=t["videoId"],
                title=t.get("title") or "—",
                artist=(t.get("artists") or [{}])[0].get("name") or author,
                duration=_to_seconds(t.get("duration_seconds") or t.get("duration")),
                url=f"https://music.youtube.com/watch?v={t['videoId']}",
                kind="song",
                album=album_ref.get("name"),
                album_id=album_ref.get("id"),
                cover_url=max(thumbs, key=lambda x: x.get("height") or 0)["url"] if thumbs else None,
                folder=safe_name(playlist.get("title") or "playlist"),
            )
        )
    return {"name": playlist.get("title"), "artist": author, "year": None, "tracks": tracks}


def fetch_metadata(vid: str) -> dict:
    """Full metadata; music.youtube.com gives real album + square cover art."""
    last_err: Exception | None = None
    for host in ("music.youtube.com", "www.youtube.com"):
        try:
            opts = {"quiet": True, "no_warnings": True, "noplaylist": True}
            with new_ydl(opts) as ydl:
                info = ydl.extract_info(f"https://{host}/watch?v={vid}", download=False)
            if info:
                return info
        except Exception as err:  # try the next host
            last_err = err
    raise RuntimeError(f"could not fetch metadata: {last_err}")


def apply_session(opts: dict) -> None:
    """Attach the saved session; premium streams need it + the web client."""
    if config.SESSION_BROWSER:
        opts["cookiesfrombrowser"] = (config.SESSION_BROWSER,)
        opts["extractor_args"] = {"youtube": {"player_client": ["web"]}}
        return
    if config.SESSION_COOKIES:
        opts["extractor_args"] = {"youtube": {"player_client": ["web"]}}


def new_ydl(opts: dict) -> yt_dlp.YoutubeDL:
    """YoutubeDL with the session attached — jar must be set post-init
    (yt-dlp ignores the cookiejar param)."""
    apply_session(opts)
    ydl = yt_dlp.YoutubeDL(opts)
    if config.SESSION_COOKIES and not config.SESSION_BROWSER:
        ydl.cookiejar = config.session_jar()
    return ydl


def download_audio(vid: str, tmp, progress_cb) -> tuple:
    """Download the highest-quality audio-only stream; return (path, format)."""
    from pathlib import Path

    base = {
        "outtmpl": str(Path(tmp) / "audio.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "progress_hooks": [progress_cb],
    }
    # premium-quality streams only surface on the music host
    host = "music.youtube.com" if config.SESSION_COOKIES or config.SESSION_BROWSER else "www.youtube.com"
    url = f"https://{host}/watch?v={vid}"

    # probe first: bestaudio's abr sort loses streams that don't report abr
    with new_ydl({**base, "skip_download": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    audio = [
        f
        for f in info.get("formats") or []
        if f.get("acodec") not in (None, "none")
    ]
    if not audio:
        raise RuntimeError("no audio formats available for this track")

    def quality(f: dict) -> float:
        return max(f.get("abr") or 0, f.get("tbr") or 0)

    # prefer the target codec when present — an AAC-to-AAC stream copy beats a
    # nominal-higher opus that would have to be transcoded anyway
    pool = audio
    if config.FORMAT == "aac":
        aac = [f for f in audio if str(f.get("acodec", "")).startswith("mp4a")]
        if aac:
            pool = aac
    best = max(pool, key=quality)
    # fall back to bestaudio/best if the specific format ID is temporarily unavailable
    # across extractor client responses
    format_selector = f"{best['format_id']}/bestaudio/best"
    with new_ydl({**base, "format": format_selector}) as ydl:
        ydl.download([url])

    files = sorted(Path(tmp).glob("audio.*"))
    if not files:
        raise RuntimeError("download produced no audio")
    return files[0], best
