"""Terminal UI: palette, console, banner, tables, formatting."""

from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from . import config
from .models import Track

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


def banner() -> None:
    art = Text()
    art.append("⏵⏵ ", style=CYAN)
    art.append(config.APP, style=f"bold {MAGENTA}")
    art.append("  ·  youtube music → tagged audio", style=MUTED)
    home = str(Path.home())
    console.print(
        Panel(
            art,
            subtitle=f"[{DIM}]saves to {str(config.DOWNLOAD_DIR).replace(home, '~', 1)}[/]",
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
