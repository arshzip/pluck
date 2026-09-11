"""Interactive front-end: menus, prompts, download orchestration."""

from __future__ import annotations

import argparse
import re
import shutil
import tempfile
from pathlib import Path

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

from . import config, login
from .audio import transcode
from .cover import fetch_cover
from .models import Track, safe_name
from .tags import tag_file
from .ui import (
    ACCENT,
    CYAN,
    DIM,
    GREEN,
    MUTED,
    ORANGE,
    RED,
    TEAL,
    TEXT,
    YELLOW,
    banner,
    console,
    fmt_duration,
    fmt_total,
    print_auth_hint,
    show_results,
)
from .youtube import (
    URL_RE,
    download_audio,
    fetch_collection,
    fetch_metadata,
    parse_list_id,
    parse_video_id,
    search,
)


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
        folder = config.DOWNLOAD_DIR / track.folder
        stem = f"{track.track_number:02d} - {title}" if track.track_number else (
            title if artist.casefold() in title.casefold() else f"{artist} - {title}"
        )
    else:
        folder = config.DOWNLOAD_DIR
        stem = title if artist.casefold() in title.casefold() else f"{artist} - {title}"
    dest = folder / f"{safe_name(stem)[:120]}.{config.EXT[config.FORMAT]}"

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
        with tempfile.TemporaryDirectory(prefix=f"{config.APP}-") as tmp_str:
            tmp = Path(tmp_str)
            with progress:
                src, fmt = download_audio(track.vid, tmp, hook)

            with console.status(f"[{MUTED}]fetching artwork", spinner="dots", spinner_style=ACCENT):
                cover = fetch_cover(info, track.vid)

            source_is_aac = str(fmt.get("acodec", "")).startswith("mp4a")
            copy_ok = source_is_aac and config.FORMAT == "aac"
            bar_label = (
                "copying aac · bit-perfect"
                if copy_ok
                else f"transcoding {config.FORMAT} · {config.BITRATE} kbps"
            )
            out = tmp / f"out.{config.EXT[config.FORMAT]}"
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
            f"[{CYAN}]{config.FORMAT} {config.BITRATE}[/{CYAN}] kbps · {dest}[/{MUTED}]"
        )
    return float(info.get("duration") or 0.0)


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

    from .config import looks_like_youtube_session, parse_cookie_header, parse_netscape, save_session

    cookies = parse_netscape(text) if "\t" in text else parse_cookie_header(text)
    if not looks_like_youtube_session(cookies):
        console.print(f"  [{RED}]✗[/] that paste doesn't look like a signed-in youtube session")
        return False
    save_session(cookies)
    console.print(
        f"  [{GREEN}]✓[/] session saved to [{TEXT}]{config.CONFIG_FILE}[/] [{MUTED}]({len(cookies)} cookies)[/]"
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
                login.cmd_login()
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
    default = Path.home() / "Downloads" / "Pluck"
    try:
        answer = Prompt.ask(
            f"\n[bold {ACCENT}]save location[/] [{DIM}](enter for ~/Downloads/{config.APP.capitalize()})[/]",
            default="",
            show_default=False,
        ).strip()
    except EOFError:
        answer = ""
    if answer:
        p = Path(answer).expanduser()
        config.DOWNLOAD_DIR = p if p.is_absolute() else Path.cwd() / p
        console.print(f"  [{MUTED}]rips will land in {config.DOWNLOAD_DIR}[/{MUTED}]")
    cfg = config.load_config()  # merge — a session may have been saved just before
    cfg["download_dir"] = str(config.DOWNLOAD_DIR)
    config.save_config(cfg)


def main() -> None:
    ap = argparse.ArgumentParser(
        prog=config.APP,
        description="search YouTube, pick a track, save a tagged audio file",
    )
    ap.add_argument("query", nargs="*", help="optional seed query, or a YouTube/YT Music URL")
    ap.add_argument(
        "-b", "--bitrate",
        default=config.BITRATE,
        choices=["128", "192", "256", "320"],
        help="audio bitrate in kbps (default 256)",
    )
    ap.add_argument(
        "-f", "--format",
        default=config.FORMAT,
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
    config.BITRATE = args.bitrate
    config.FORMAT = args.format
    fresh = not config.CONFIG_FILE.is_file()
    if args.cookies:
        p = Path(args.cookies).expanduser()
        if p.is_file():
            from .config import parse_netscape

            config.SESSION_COOKIES = parse_netscape(p.read_text(errors="ignore")) or None
        else:
            config.SESSION_BROWSER = args.cookies
    else:
        config.load_session()

    cfg = config.load_config()
    if cfg.get("download_dir"):
        config.DOWNLOAD_DIR = Path(cfg["download_dir"]).expanduser()

    if args.query[:1] == ["login"]:
        banner()
        try:
            login.cmd_login()
        except KeyboardInterrupt:
            console.print(f"\n  [{YELLOW}]cancelled[/] [{DIM}]— chrome left open[/]\n")
        return

    banner()
    if not shutil.which("ffmpeg"):
        console.print(f"[{RED}]✗ ffmpeg not found[/] — install with [bold]brew install ffmpeg[/]")
        raise SystemExit(1)
    saved = 0
    total_secs = 0.0
    if config.SESSION_COOKIES or config.SESSION_BROWSER:
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
    config.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

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
    console.print(
        f"\n[{DIM}]{config.APP} out · [{ACCENT}]{saved}[/{ACCENT}] {noun} · "
        f"[{ORANGE}]{fmt_total(total_secs) if total_secs else '0:00'}[/{ORANGE}] "
        f"of music in {str(config.DOWNLOAD_DIR).replace(str(Path.home()), '~', 1)}[/{DIM}]\n"
    )
