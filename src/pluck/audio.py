"""ffmpeg transcode / stream-copy stage."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from . import config


def _parse_hms(stamp: str) -> float | None:
    try:
        hours, mins, secs = stamp.split(":")
        return int(hours) * 3600 + int(mins) * 60 + float(secs)
    except ValueError:
        return None


def transcode(src: Path, dest: Path, duration: float | None, progress_cb, copy_ok: bool = False) -> None:
    """Re-encode (or losslessly remux) to the target, streaming progress via callback."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found — install with `brew install ffmpeg`")
    if copy_ok:
        codec_args = ["-c:a", "copy"]
    else:
        codec_args = (
            ["-c:a", "aac", "-b:a", f"{config.BITRATE}k"]
            if config.FORMAT == "aac"
            else ["-c:a", "libmp3lame", "-b:a", f"{config.BITRATE}k"]
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
