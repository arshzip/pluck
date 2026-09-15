"""Premium login: isolated Chrome + CDP cookie grab (no keychain)."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import requests

from . import config
from .ui import ACCENT, GREEN, MUTED, RED, TEXT, console

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
    profile = config.CONFIG_DIR / "chrome-profile"
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
    config.save_session(
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

    console.print(f"  [{GREEN}]✓[/] session saved to [{TEXT}]{config.CONFIG_FILE}[/]")
    console.print(f"  [{MUTED}]pluck uses it automatically from now on[/{MUTED}]")
