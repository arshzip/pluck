"""Paths, settings and the saved session (~/.pluck/config.json)."""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from yt_dlp.cookies import YoutubeDLCookieJar

APP = "pluck"
CONFIG_DIR = Path.home() / ".pluck"
CONFIG_FILE = CONFIG_DIR / "config.json"
DOWNLOAD_DIR = Path.home() / "Downloads" / "Pluck"
BITRATE = "256"  # kbps — override with --bitrate
FORMAT = "aac"  # aac → .m4a (default) or mp3 — override with --format
EXT = {"aac": "m4a", "mp3": "mp3"}
SESSION_COOKIES: list[dict] | None = None  # youtube session, from config.json
SESSION_BROWSER: str | None = None  # -c firefox style: browser holding a session


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


def save_session(cookies: list[dict]) -> None:
    global SESSION_COOKIES
    SESSION_COOKIES = cookies
    cfg = load_config()
    cfg.setdefault("session", {})
    cfg["session"]["cookies"] = cookies
    cfg["session"]["saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    save_config(cfg)


def session_jar() -> YoutubeDLCookieJar | None:
    if not SESSION_COOKIES:
        return None
    jar = YoutubeDLCookieJar()
    for c in SESSION_COOKIES:
        domain = c.get("domain") or ""
        expires = c.get("expires")
        jar.set_cookie(
            http_cookiejar_cookie(
                c.get("name", ""),
                c.get("value", ""),
                domain,
                c.get("path", "/"),
                bool(c.get("secure")),
                expires or None,
                expires in (None, 0),
            )
        )
    return jar


def http_cookiejar_cookie(name, value, domain, path, secure, expires, discard):
    import http.cookiejar

    return http.cookiejar.Cookie(
        0,
        name,
        value,
        None,
        False,
        domain,
        domain.startswith("."),
        domain.startswith("."),
        path,
        True,
        secure,
        expires,
        discard,
        None,
        None,
        {},
    )
