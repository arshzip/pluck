#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "mutagen>=1.47",
#   "requests>=2.31",
#   "rich>=13.7",
#   "yt-dlp>=2024.12.13",
#   "yt-dlp-ejs",
#   "bgutil-ytdlp-pot-provider",
#   "ytmusicapi>=1.8",
#   "websocket-client>=1.8",
# ]
# ///
"""
pluck — pull music off YouTube Music as tagged audio files.

Interactive: search → results (songs first, videos labeled) → pick a number →
saved to ~/Downloads/Pluck. Default output is 256 kbps AAC (.m4a) with tags and
embedded album art; `-f mp3` for classic MP3 with ID3v2.3 tags.

Also speaks links: paste YouTube/YT Music URLs (several at once is fine), point
it at a .txt file full of links, or rip a whole album with `a3` at the picker
(album of result 3) or by pasting an album/playlist URL.
"""

from __future__ import annotations

import argparse
import http.cookiejar as http_cookiejar
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import requests
import yt_dlp
from yt_dlp.cookies import YoutubeDLCookieJar
from ytmusicapi import YTMusic
from mutagen.id3 import APIC, COMM, ID3, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TRCK, WOAS
from mutagen.mp4 import MP4, MP4Cover
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

APP = "pluck"
CONFIG_DIR = Path.home() / ".pluck"
CONFIG_FILE = CONFIG_DIR / "config.json"
DOWNLOAD_DIR = Path.home() / "Downloads" / "Pluck"
BITRATE = "256"  # kbps — override with --bitrate
FORMAT = "aac"  # aac → .m4a (default) or mp3 — override with --format
EXT = {"aac": "m4a", "mp3": "mp3"}
SESSION_COOKIES: list[dict] | None = None  # youtube session, from config.json
SESSION_BROWSER: str | None = None  # -c firefox style: browser holding a session

# tokyonight-moon palette
TEXT = "#c8d3f5"
MUTED = "#828bb8"
DIM = "#545c7e"
BORDER = "#3b4261"
ACCENT = "#82aaff"  # blue
CYAN = "#86e1fc"
TEAL = "#4fd6be"
MAGENTA = "#c099ff"
ORANGE = "#ff966c"
YELLOW = "#ffc777"
GREEN = "#c3e88d"
RED = "#ff757f"

console = Console(
    theme=Theme(
        {
            "bar.back": BORDER,
            "bar.finished": ACCENT,
            "bar.pulse": CYAN,
            "progress.percentage": ORANGE,
            "progress.remaining": MUTED,
            "progress.elapsed": MUTED,
            "progress.speed": MUTED,
            "progress.data.speed": MUTED,
            "progress.filesize": MUTED,
            "progress.filesize.total": MUTED,
            "progress.download": TEXT,
            "progress.data.download": TEXT,
            "progress.spinner": ACCENT,
        }
    )
)

URL_RE = re.compile(r"https?://(www\.|music\.)?(youtube\.com|youtu\.be)/\S+")


# ── model ────────────────────────────────────────────────────────────────────


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


# ── youtube (yt-dlp, same approach as onthespot) ────────────────────────────


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


def album_flow(list_id: str) -> tuple[int, float]:
    """Show an album/playlist tracklist and rip the picked tracks."""
    with console.status(f"[{MUTED}]fetching collection", spinner="dots", spinner_style=ACCENT):
        try:
            coll = fetch_collection(list_id)
        except Exception as err:
            console.print(f"  [{RED}]✗[/] couldn't fetch that album/playlist: {err}")
            return 0, 0.0
    if not coll or not coll["tracks"]:
        console.print(f"  [{RED}]✗[/] no tracks found")
        return 0, 0.0

    console.print()
    artist_line = f" · {coll['artist']}" if coll["artist"] and coll["artist"] != "—" else ""
    year_line = f" · {coll['year']}" if coll.get("year") else ""
    console.print(
        f"[bold {TEXT}]{coll['name']}[/][{MUTED}]{artist_line}{year_line} · {len(coll['tracks'])} tracks[/{MUTED}]"
    )
    show_results(coll["tracks"])

    while True:
        choice = Prompt.ask(
            f"\n[bold {ACCENT}]download[/] [{DIM}](a all · 1,3-5 · b back · q quit)[/]"
        ).strip().lower()
        if choice in {"b", "back"}:
            return 0, 0.0
        if choice in {"q", "quit"}:
            raise KeyboardInterrupt
        picks = (
            list(range(1, len(coll["tracks"]) + 1))
            if choice in {"a", "all"}
            else parse_picks(choice, len(coll["tracks"]))
        )
        if picks:
            break
        console.print(f"  [{DIM}]nothing picked — try a, or 1,3-5[/]")

    saved, secs = 0, 0.0
    for i, ix in enumerate(picks, 1):
        console.print(f"\n[{DIM}]{i}/{len(picks)}[/{DIM}]")
        dur = download_track(coll["tracks"][ix - 1])
        if dur is not None:
            saved += 1
            secs += dur
    console.print(f"\n  [{GREEN}]◆[/] [{MUTED}]collection done · {saved}/{len(picks)} saved[/{MUTED}]")
    return saved, secs


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


def load_config() -> dict:
    if CONFIG_FILE.is_file():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2) + "\n")
    CONFIG_FILE.chmod(0o600)


def parse_netscape(text: str) -> list[dict]:
    cookies = []
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _, path, secure, expires, name, value = parts[:7]
        cookies.append(
            {
                "name": name,
                "value": value,
                "domain": domain,
                "path": path,
                "secure": secure.upper() == "TRUE",
                "expires": int(expires) if expires.isdigit() else 0,
            }
        )
    return cookies


def load_session() -> None:
    """Populate SESSION_COOKIES from config.json, migrating the old layout."""
    global SESSION_COOKIES
    cfg = load_config()
    if (cfg.get("session") or {}).get("cookies"):
        SESSION_COOKIES = cfg["session"]["cookies"]
        return

    cookies: list[dict] = []
    for legacy in (
        CONFIG_DIR / "cookies.txt",
        Path.home() / ".config" / APP / "cookies.txt",
    ):
        if cookies:
            break
        if legacy.is_file():
            cookies = parse_netscape(legacy.read_text(errors="ignore"))
    if not cookies:
        return

    SESSION_COOKIES = cookies
    cfg.setdefault("session", {})["cookies"] = cookies
    save_config(cfg)

    old_dir = Path.home() / ".config" / APP  # pre-config layout
    if old_dir.is_dir():
        old_profile = old_dir / "chrome-profile"
        if old_profile.is_dir():
            shutil.move(str(old_profile), str(CONFIG_DIR / "chrome-profile"))
        shutil.rmtree(old_dir, ignore_errors=True)
    (CONFIG_DIR / "cookies.txt").unlink(missing_ok=True)


def session_jar() -> YoutubeDLCookieJar | None:
    if not SESSION_COOKIES:
        return None
    jar = YoutubeDLCookieJar()
    for c in SESSION_COOKIES:
        domain = c.get("domain") or ""
        expires = c.get("expires")
        jar.set_cookie(
            http_cookiejar.Cookie(
                0,
                c.get("name", ""),
                c.get("value", ""),
                None,
                False,
                domain,
                domain.startswith("."),
                domain.startswith("."),
                c.get("path", "/"),
                True,
                bool(c.get("secure")),
                expires or None,
                expires in (None, 0),
                None,
                None,
                {},
            )
        )
    return jar


def apply_session(opts: dict) -> None:
    """Attach the saved session; premium streams need it + the web client."""
    if SESSION_BROWSER:
        opts["cookiesfrombrowser"] = (SESSION_BROWSER,)
        opts["extractor_args"] = {"youtube": {"player_client": ["web"]}}
        return
    if SESSION_COOKIES:
        opts["extractor_args"] = {"youtube": {"player_client": ["web"]}}


def new_ydl(opts: dict) -> yt_dlp.YoutubeDL:
    """YoutubeDL with the session attached — jar must be set post-init
    (yt-dlp ignores the cookiejar param)."""
    apply_session(opts)
    ydl = yt_dlp.YoutubeDL(opts)
    if SESSION_COOKIES and not SESSION_BROWSER:
        ydl.cookiejar = session_jar()
    return ydl


AUTH_MARKERS = (
    "premium members",
    "sign in to confirm",
    "confirm your age",
    "age-restricted",
    "private video",
    "use --cookies",
)


def print_auth_hint(err: Exception) -> None:
    """Sign-in / age-gated failures get a way out, not just an error."""
    if not any(m in str(err).lower() for m in AUTH_MARKERS):
        return
    console.print(
        f"      [{YELLOW}]hint[/] [{MUTED}]— needs a signed-in session. run [/{MUTED}][bold {ACCENT}]pluck login[/]"
        f" [{MUTED}]to save one, or -c firefox[/]"
    )


# ── premium login (isolated chrome + CDP, no keychain) ───────────────────────

LOGIN_PORT = 4444


def chrome_binary() -> str | None:
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        shutil.which("google-chrome"),
        shutil.which("chromium"),
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    return None


def cdp_port_live(port: int) -> bool:
    try:
        requests.get(f"http://127.0.0.1:{port}/json/version", timeout=1)
        return True
    except requests.RequestException:
        return False


def cdp_call(ws_url: str, method: str, timeout: float = 5) -> dict:
    import websocket

    ws = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True)
    try:
        ws.send(json.dumps({"id": 1, "method": method}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == 1:
                return msg
    finally:
        ws.close()


def cdp_cookies(port: int) -> list[dict]:
    ver = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=3).json()
    resp = cdp_call(ver["webSocketDebuggerUrl"], "Storage.getCookies")
    if "result" in resp and "cookies" in resp["result"]:
        return resp["result"]["cookies"]
    raise RuntimeError(str(resp.get("error") or "cdp error"))


def _login_pids(profile: Path) -> list[int]:
    try:
        out = subprocess.run(
            ["pgrep", "-f", f"user-data-dir={profile}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return [int(p) for p in out.stdout.split() if p.strip().isdigit()]
    except Exception:
        return []


def _kill_pids(pids: list[int]) -> None:
    def alive(p: int) -> bool:
        try:
            os.kill(p, 0)
            return True
        except OSError:
            return False

    for p in pids:
        try:
            os.kill(p, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.time() + 5
    while time.time() < deadline and any(alive(p) for p in pids):
        time.sleep(0.25)
    for p in pids:
        if alive(p):
            try:
                os.kill(p, signal.SIGKILL)
            except OSError:
                pass
    time.sleep(0.5)


def cmd_login() -> None:
    """Isolated Chrome → user signs in → session saved over CDP (no keychain)."""
    chrome = chrome_binary()
    if not chrome:
        console.print(f"  [{RED}]✗[/] Google Chrome not found in /Applications")
        raise SystemExit(1)
    profile = CONFIG_DIR / "chrome-profile"
    profile.mkdir(parents=True, exist_ok=True)

    def launch() -> None:
        subprocess.Popen(
            [
                chrome,
                f"--user-data-dir={profile}",
                f"--remote-debugging-port={LOGIN_PORT}",
                "--remote-allow-origins=*",
                "--no-first-run",
                "--no-default-browser-check",
                "https://accounts.google.com/signin",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    if not cdp_port_live(LOGIN_PORT):
        # a stale instance without a live port blocks the debug server
        if _login_pids(profile):
            console.print(f"  [{MUTED}]closing a stale login browser…[/{MUTED}]")
            _kill_pids(_login_pids(profile))
        launch()
        deadline = time.time() + 20
        while time.time() < deadline and not cdp_port_live(LOGIN_PORT):
            time.sleep(0.5)
        if not cdp_port_live(LOGIN_PORT):
            console.print(
                f"  [{RED}]✗[/] login browser didn't come up — close any login window "
                f"and run [bold {ACCENT}]pluck login[/] again"
            )
            raise SystemExit(1)

    cookies: list[dict] = []
    signed_in = False
    deadline = time.time() + 120
    errors = 0
    relaunched = False
    with console.status(
        f"[{MUTED}]reading session from the login browser (ctrl-c to cancel)…",
        spinner="dots",
        spinner_style=ACCENT,
    ):
        while time.time() < deadline:
            try:
                cookies = cdp_cookies(LOGIN_PORT)
                errors = 0
            except Exception:
                errors += 1
                if errors == 3 and not relaunched:
                    relaunched = True
                    console.print(
                        f"\n  [{MUTED}]login browser not answering — relaunching it…[/{MUTED}]"
                    )
                    _kill_pids(_login_pids(profile))
                    launch()
                    time.sleep(3)
                elif errors >= 20:  # ~40s of dead port
                    console.print(
                        f"\n  [{RED}]✗[/] lost contact with the login browser — try again"
                    )
                    raise SystemExit(1)
                time.sleep(2)
                continue
            if any(
                c.get("name") == "SAPISID" and "youtube" in (c.get("domain") or "")
                for c in cookies
            ):
                signed_in = True
                break
            time.sleep(2)

    if not signed_in:
        console.print(f"  [{RED}]✗[/] no youtube sign-in detected — chrome stays open, try again")
        raise SystemExit(1)

    keep = [
        c
        for c in cookies
        if "youtube" in (c.get("domain") or "") or "google" in (c.get("domain") or "")
    ]
    save_session(
        [
            {
                "name": c.get("name"),
                "value": c.get("value"),
                "domain": c.get("domain"),
                "path": c.get("path", "/"),
                "secure": bool(c.get("secure")),
                "expires": int(c["expires"]) if isinstance(c.get("expires"), (int, float)) else 0,
            }
            for c in keep
        ]
    )

    try:  # politely close the login browser; its profile persists for refreshes
        ver = requests.get(f"http://127.0.0.1:{LOGIN_PORT}/json/version", timeout=2).json()
        cdp_call(ver["webSocketDebuggerUrl"], "Browser.close")
    except Exception:
        pass

    console.print(f"  [{GREEN}]✓[/] session saved to [{TEXT}]{CONFIG_FILE}[/]")
    console.print(f"  [{MUTED}]pluck uses it automatically from now on[/{MUTED}]")


def save_session(cookies: list[dict]) -> None:
    global SESSION_COOKIES
    SESSION_COOKIES = cookies
    cfg = load_config()
    cfg.setdefault("session", {})
    cfg["session"]["cookies"] = cookies
    cfg["session"]["saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    save_config(cfg)


def parse_cookie_header(text: str) -> list[dict]:
    """'NAME=value; NAME=value' browser header → cookie dicts."""
    text = text.strip()
    if text.lower().startswith("cookie:"):
        text = text[7:]
    cookies = []
    for pair in text.split(";"):
        name, _, value = pair.strip().partition("=")
        if not name:
            continue
        cookies.append(
            {
                "name": name,
                "value": value,
                "domain": ".youtube.com",
                "path": "/",
                "secure": name.startswith("__Secure-"),
                "expires": 0,
            }
        )
    return cookies


def looks_like_youtube_session(cookies: list[dict]) -> bool:
    markers = {"SID", "HSID", "SSID", "SAPISID", "__Secure-1PSID", "__Secure-3PSID"}
    return bool(markers & {c.get("name") for c in cookies})


def paste_cookie_flow() -> bool:
    console.print(f"  [{MUTED}]paste cookies — a cookie header or cookies.txt contents — "
                  f"then enter on an empty line[/]")
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if not line.strip() and lines:
            break
        lines.append(line)
    text = "\n".join(lines).strip()
    if not text:
        return False

    cookies = parse_netscape(text) if "\t" in text else parse_cookie_header(text)
    if not looks_like_youtube_session(cookies):
        console.print(f"  [{RED}]✗[/] that paste doesn't look like a signed-in youtube session")
        return False
    save_session(cookies)
    console.print(
        f"  [{GREEN}]✓[/] session saved to [{TEXT}]{CONFIG_FILE}[/] [{MUTED}]({len(cookies)} cookies)[/]"
    )
    return True


def first_run_menu() -> None:
    console.print(f"  [{DIM}]no saved session — how do you want to proceed?[/]")
    console.print(f"    [{ACCENT}]1[/] [{MUTED}]paste cookies · a youtube cookie header or cookies.txt contents[/]")
    console.print(f"    [{ACCENT}]2[/] [{MUTED}]login · an isolated chrome window opens, sign in once[/]")
    console.print(f"    [{ACCENT}]3[/] [{MUTED}]continue · public tracks, sources cap at ~160 kbps[/]")
    while True:
        try:
            choice = Prompt.ask(f"\n[bold {ACCENT}]choose[/] [{DIM}](1-3)[/]").strip()
        except EOFError:
            return
        if choice == "1":
            if paste_cookie_flow():
                return
        elif choice == "2":
            try:
                cmd_login()
                return
            except SystemExit:
                console.print()
                continue
        elif choice == "3":
            return
        else:
            console.print(f"  [{DIM}]enter 1, 2, or 3[/]")


def ensure_download_dir() -> None:
    """First run: offer a save location. enter → ~/Downloads/Pluck."""
    global DOWNLOAD_DIR
    default = Path.home() / "Downloads" / "Pluck"
    try:
        answer = Prompt.ask(
            f"\n[bold {ACCENT}]save location[/] [{DIM}](enter for ~/Downloads/{APP.capitalize()})[/]",
            default="",
            show_default=False,
        ).strip()
    except EOFError:
        answer = ""
    if answer:
        p = Path(answer).expanduser()
        DOWNLOAD_DIR = p if p.is_absolute() else Path.cwd() / p
        console.print(f"  [{MUTED}]rips will land in {DOWNLOAD_DIR}[/{MUTED}]")
    cfg = load_config()  # merge — a session may have been saved just before
    cfg["download_dir"] = str(DOWNLOAD_DIR)
    save_config(cfg)


def download_audio(vid: str, tmp: Path, progress_cb) -> tuple[Path, dict]:
    """Download the highest-quality audio-only stream; return (path, format)."""
    base = {
        "outtmpl": str(tmp / "audio.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "progress_hooks": [progress_cb],
    }
    # premium-quality streams only surface on the music host
    host = "music.youtube.com" if SESSION_COOKIES or SESSION_BROWSER else "www.youtube.com"
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
    if FORMAT == "aac":
        aac = [f for f in audio if str(f.get("acodec", "")).startswith("mp4a")]
        if aac:
            pool = aac
    best = max(pool, key=quality)
    with new_ydl({**base, "format": best["format_id"]}) as ydl:
        ydl.download([url])

    files = sorted(tmp.glob("audio.*"))
    if not files:
        raise RuntimeError("download produced no audio")
    return files[0], best


def transcode(src: Path, dest: Path, duration: float | None, progress_cb, copy_ok: bool = False) -> None:
    """Re-encode (or losslessly remux) to the target, streaming progress via callback."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found — install with `brew install ffmpeg`")
    if copy_ok:
        codec_args = ["-c:a", "copy"]
    else:
        codec_args = (
            ["-c:a", "aac", "-b:a", f"{BITRATE}k"]
            if FORMAT == "aac"
            else ["-c:a", "libmp3lame", "-b:a", f"{BITRATE}k"]
        )
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(src), "-vn", *codec_args,
        "-progress", "pipe:1", str(dest),
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for line in proc.stdout or []:
        if line.startswith("out_time=") and duration:
            secs = _parse_hms(line.split("=", 1)[1].strip())
            if secs is not None:
                progress_cb(min(secs, duration))
    err = (proc.stderr.read() if proc.stderr else "").strip()
    if proc.wait() != 0:
        detail = err.splitlines()[-1] if err else "unknown error"
        raise RuntimeError(f"ffmpeg failed: {detail}")


def _parse_hms(stamp: str) -> float | None:
    try:
        hours, mins, secs = stamp.split(":")
        return int(hours) * 3600 + int(mins) * 60 + float(secs)
    except ValueError:
        return None


# ── artwork ──────────────────────────────────────────────────────────────────


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


# ── id3 tagging ──────────────────────────────────────────────────────────────


def _mime(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return "image/webp"


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


# ── ui ───────────────────────────────────────────────────────────────────────


def banner() -> None:
    art = Text()
    art.append("⏵⏵ ", style=CYAN)
    art.append(APP, style=f"bold {MAGENTA}")
    art.append("  ·  youtube music → tagged audio", style=MUTED)
    home = str(Path.home())
    console.print(
        Panel(
            art,
            subtitle=f"[{DIM}]saves to {str(DOWNLOAD_DIR).replace(home, '~', 1)}[/]",
            subtitle_align="right",
            border_style=BORDER,
            padding=(0, 2),
        )
    )
    console.print()


def show_results(tracks: list[Track]) -> None:
    table = Table(
        show_header=True,
        header_style=f"bold {DIM}",
        border_style=BORDER,
        pad_edge=False,
        expand=True,
    )
    table.add_column("#", style=f"bold {ACCENT}", width=3, justify="right")
    table.add_column("title", ratio=5, style=TEXT)
    table.add_column("artist", ratio=3, style=MUTED)
    table.add_column("time", style=DIM, justify="right", width=6)
    table.add_column("kind", justify="right", width=6)

    for i, t in enumerate(tracks, 1):
        if t.duration:
            mins, secs = divmod(int(t.duration), 60)
            length = f"{mins}:{secs:02d}"
        else:
            length = "live"
        kind = f"[{TEAL}]song[/]" if t.kind == "song" else f"[{ORANGE}]video[/]"
        table.add_row(str(i), t.title, t.artist, length, kind)

    console.print(table)


def fmt_duration(seconds: float | None) -> str:
    if not seconds:
        return "—"
    mins, secs = divmod(int(seconds), 60)
    return f"{mins}:{secs:02d}"


def fmt_total(seconds: float) -> str:
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    mins, secs = divmod(rem, 60)
    return f"{hours}:{mins:02d}:{secs:02d}" if hours else f"{mins}:{secs:02d}"


def download_track(track: Track) -> float | None:
    """Fetch metadata → download → tag. Returns the track duration, or None."""
    if track.cover_url:  # metadata already complete from YT Music — skip extraction
        info = {
            "title": track.title,
            "channel": track.artist,
            "album": track.album,
            "release_year": track.year,
            "duration": track.duration,
            "thumbnails": [{"url": track.cover_url}],
            "track_number": track.track_number,
            "track_total": track.track_total,
        }
    else:
        with console.status(f"[{MUTED}]fetching metadata", spinner="dots", spinner_style=ACCENT):
            try:
                info = fetch_metadata(track.vid)
            except Exception as err:
                console.print(f"  [{RED}]✗[/] metadata failed: {err}")
                print_auth_hint(err)
                return None

    artist = (info.get("channel") or info.get("uploader") or "Unknown").removesuffix(" - Topic")
    title = info.get("title") or track.title
    # titles on youtube often already lead with the artist — don't duplicate it
    if track.folder:  # album / playlist rip → its own subfolder
        folder = DOWNLOAD_DIR / track.folder
        stem = f"{track.track_number:02d} - {title}" if track.track_number else (
            title if artist.casefold() in title.casefold() else f"{artist} - {title}"
        )
    else:
        folder = DOWNLOAD_DIR
        stem = title if artist.casefold() in title.casefold() else f"{artist} - {title}"
    dest = folder / f"{safe_name(stem)[:120]}.{EXT[FORMAT]}"

    duration = info.get("duration")
    folder.mkdir(parents=True, exist_ok=True)

    progress = Progress(
        SpinnerColumn(style=ACCENT),
        TextColumn(f"[{ACCENT}]downloading"),
        BarColumn(bar_width=None, style=DIM, complete_style=ACCENT, finished_style=ACCENT),
        TaskProgressColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=True,
    )
    task = progress.add_task("download", total=None)

    def hook(d: dict) -> None:
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                progress.update(task, total=total, completed=d.get("downloaded_bytes", 0))
        elif d["status"] == "finished":
            progress.update(task, completed=progress.tasks[task].total or 1)

    copy_ok = False
    fmt: dict = {}
    try:
        with tempfile.TemporaryDirectory(prefix=f"{APP}-") as tmp_str:
            tmp = Path(tmp_str)
            with progress:
                src, fmt = download_audio(track.vid, tmp, hook)

            with console.status(f"[{MUTED}]fetching artwork", spinner="dots", spinner_style=ACCENT):
                cover = fetch_cover(info, track.vid)

            source_is_aac = str(fmt.get("acodec", "")).startswith("mp4a")
            copy_ok = source_is_aac and FORMAT == "aac"
            bar_label = (
                "copying aac · bit-perfect" if copy_ok else f"transcoding {FORMAT} · {BITRATE} kbps"
            )
            out = tmp / f"out.{EXT[FORMAT]}"
            with Progress(
                SpinnerColumn(style=ACCENT),
                TextColumn(f"[{CYAN}]{bar_label}"),
                BarColumn(bar_width=None, style=DIM, complete_style=ACCENT, finished_style=ACCENT),
                TaskProgressColumn(),
                TimeRemainingColumn(),
                console=console,
                transient=True,
            ) as transcode_bar:
                transcode_task = transcode_bar.add_task("transcode", total=duration or None)
                transcode(
                    src,
                    out,
                    duration,
                    lambda done: transcode_bar.update(transcode_task, completed=done),
                    copy_ok=copy_ok,
                )

            shutil.move(str(out), dest)  # overwrites any previous rip
            tag_file(dest, info, track.url, cover)
    except KeyboardInterrupt:
        console.print(f"  [{YELLOW}]cancelled[/]")
        dest.unlink(missing_ok=True)
        return None
    except Exception as err:
        console.print(f"  [{RED}]✗[/] {err}")
        print_auth_hint(err)
        dest.unlink(missing_ok=True)
        return None

    album = info.get("album") or "single"
    year = info.get("release_year") or (info.get("upload_date") or "")[:4] or "----"
    codec = {"opus": "opus", "mp4a.40.2": "aac"}.get(fmt.get("acodec"), fmt.get("acodec"))
    kbps = round(max(fmt.get("abr") or 0, fmt.get("tbr") or 0)) or "?"
    console.print(f"  [{GREEN}]✓[/] [bold {TEXT}]{dest.name}[/]")
    console.print(
        f"    [{MUTED}]{album} · [{ORANGE}]{year}[/{ORANGE}] · {fmt_duration(info.get('duration'))} · "
        f"cover {'embedded' if cover else 'unavailable'}[/{MUTED}]"
    )
    if copy_ok:
        console.print(
            f"    [{MUTED}]{codec or 'audio'} ~[{ORANGE}]{kbps}[/{ORANGE}] kbps → "
            f"[{TEAL}]bit-perfect stream copy[/{TEAL}] · {dest}[/{MUTED}]"
        )
    else:
        console.print(
            f"    [{MUTED}]{codec or 'audio'} ~[{ORANGE}]{kbps}[/{ORANGE}] kbps → "
            f"[{CYAN}]{FORMAT} {BITRATE}[/{CYAN}] kbps · {dest}[/{MUTED}]"
        )
    return float(info.get("duration") or 0.0)


def parse_video_id(text: str) -> str | None:
    m = re.search(r"(?:v=|youtu\.be/|shorts/)([\w-]{11})", text)
    return m.group(1) if m else None


def parse_list_id(text: str) -> str | None:
    m = re.search(r"[?&]list=([\w-]+)", text)
    return m.group(1) if m else None


def parse_picks(spec: str, limit: int) -> list[int]:
    """'1,3-5' → [1, 3, 4, 5], clamped to 1..limit."""
    picks: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            if a.isdigit() and b.isdigit() and 1 <= int(a) <= int(b) <= limit:
                picks.update(range(int(a), int(b) + 1))
        elif part.isdigit() and 1 <= int(part) <= limit:
            picks.add(int(part))
    return sorted(picks)


# ── main loop ────────────────────────────────────────────────────────────────


def prompt_query() -> str:
    try:
        return Prompt.ask(
            f"[bold {ACCENT}]paste link or search[/] [{DIM}](txt · q quit)[/]"
        ).strip()
    except EOFError:
        return "q"


def pick_track(tracks: list[Track]) -> tuple[str, Track | None]:
    """Returns ('track'|'album', track), ('back', None) or ('quit', None)."""
    while True:
        try:
            choice = Prompt.ask(
                f"\n[bold {ACCENT}]pick[/] [{DIM}](1-{len(tracks)} · a# album of # · b back · q quit)[/]"
            ).strip().lower()
        except EOFError:
            return "quit", None
        if choice in {"b", "back"}:
            return "back", None
        if choice in {"q", "quit"}:
            return "quit", None
        if choice.startswith("a") and choice[1:].isdigit():
            idx = int(choice[1:])
            if 1 <= idx <= len(tracks):
                return "album", tracks[idx - 1]
        if choice.isdigit() and 1 <= int(choice) <= len(tracks):
            return "track", tracks[int(choice) - 1]
        console.print(f"  [{DIM}]enter 1-{len(tracks)}, a#, b, or q[/]")


def main() -> None:
    global BITRATE, FORMAT, SESSION_COOKIES, SESSION_BROWSER, DOWNLOAD_DIR
    ap = argparse.ArgumentParser(
        prog=APP,
        description="search YouTube, pick a track, save a tagged audio file",
    )
    ap.add_argument("query", nargs="*", help="optional seed query, or a YouTube/YT Music URL")
    ap.add_argument(
        "-b", "--bitrate",
        default=BITRATE,
        choices=["128", "192", "256", "320"],
        help="audio bitrate in kbps (default 256)",
    )
    ap.add_argument(
        "-f", "--format",
        default=FORMAT,
        choices=["aac", "mp3"],
        help="output codec: aac → .m4a (default) or mp3 → .mp3 with ID3v2.3 tags",
    )
    ap.add_argument(
        "-c", "--cookies",
        default=None,
        metavar="BROWSER|FILE",
        help="browser name (firefox, chrome, safari…) or cookies.txt — reuses your existing youtube session; nothing is launched",
    )
    args = ap.parse_args()
    BITRATE = args.bitrate
    FORMAT = args.format
    fresh = not CONFIG_FILE.is_file()
    if args.cookies:
        p = Path(args.cookies).expanduser()
        if p.is_file():
            SESSION_COOKIES = parse_netscape(p.read_text(errors="ignore")) or None
        else:
            SESSION_BROWSER = args.cookies
    else:
        load_session()

    cfg = load_config()
    if cfg.get("download_dir"):
        DOWNLOAD_DIR = Path(cfg["download_dir"]).expanduser()

    if args.query[:1] == ["login"]:
        banner()
        try:
            cmd_login()
        except KeyboardInterrupt:
            console.print(f"\n  [{YELLOW}]cancelled[/] [{DIM}]— chrome left open[/]\n")
        return

    banner()
    if not shutil.which("ffmpeg"):
        console.print(f"[{RED}]✗ ffmpeg not found[/] — install with [bold]brew install ffmpeg[/]")
        raise SystemExit(1)
    saved = 0
    total_secs = 0.0
    if SESSION_COOKIES or SESSION_BROWSER:
        console.print(
            f"  [{GREEN}]●[/] [{MUTED}]yt music premium session — rips come out "
            f"[/{MUTED}][{TEAL}]bit-perfect 256 kbps aac[/{TEAL}]"
        )
    elif console.is_terminal:
        first_run_menu()
    else:
        console.print(
            f"  [{DIM}]○ signed-out — sources cap at ~160 kbps · run "
            f"[bold {DIM}]pluck login[/{DIM}][{DIM}] for premium quality (see README)[/]"
        )
    if fresh and console.is_terminal:
        ensure_download_dir()
    console.print()
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # optional: a query/URL passed as CLI arg seeds the first search
    pending_query = " ".join(args.query).strip() or None

    try:
        while True:
            query = pending_query or prompt_query()
            pending_query = None

            if not query:
                continue
            if query.lower() in {"q", "quit", "exit"}:
                break

            # links: pasted inline (any count), or from a .txt file
            links = [m.group(0) for m in URL_RE.finditer(query)]
            as_file = Path(query.strip().strip("'\"")).expanduser()
            if not links and as_file.suffix.lower() == ".txt" and as_file.is_file():
                links = [m.group(0) for m in URL_RE.finditer(as_file.read_text(errors="ignore"))]
                console.print(f"\n  [{MUTED}]{len(links)} link(s) from {as_file.name}[/{MUTED}]")
            if links:
                console.print()
                for link in links:
                    vid = parse_video_id(link)
                    list_id = None if vid else parse_list_id(link)
                    if list_id:
                        n_saved, n_secs = album_flow(list_id.removeprefix("VL"))
                        saved += n_saved
                        total_secs += n_secs
                    elif vid:
                        dur = download_track(Track(vid, "…", "…", None, link))
                        if dur is not None:
                            saved += 1
                            total_secs += dur
                    else:
                        console.print(f"  [{RED}]✗[/] couldn't parse a video or album id in {link}")
                console.print()
                continue

            with console.status(
                f"[{MUTED}]searching [/][bold {TEXT}]{query!r}", spinner="dots", spinner_style=ACCENT
            ):
                try:
                    tracks = search(query)
                except Exception as err:
                    console.print(f"  [{RED}]✗[/] search failed: {err}")
                    continue

            if not tracks:
                console.print(f"  [{DIM}]no results[/]")
                continue

            console.print()
            show_results(tracks)
            try:
                action, target = pick_track(tracks)
            except KeyboardInterrupt:
                break
            if action == "back":
                continue
            if action == "quit":
                break

            console.print()
            if action == "album":
                if not target.album_id:
                    console.print(f"  [{RED}]✗[/] that result has no album linked")
                    console.print()
                    continue
                n_saved, n_secs = album_flow(target.album_id)
                saved += n_saved
                total_secs += n_secs
            else:
                dur = download_track(target)
                if dur is not None:
                    saved += 1
                    total_secs += dur
            console.print()
    except KeyboardInterrupt:
        pass

    noun = "track" if saved == 1 else "tracks"
    spent = fmt_total(total_secs) if total_secs else "0:00"
    console.print(
        f"\n[{DIM}]{APP} out · [{ACCENT}]{saved}[/{ACCENT}] {noun} · "
        f"[{ORANGE}]{spent}[/{ORANGE}] of music in ~/Downloads/{APP.capitalize()}[/{DIM}]\n"
    )


if __name__ == "__main__":
    main()
