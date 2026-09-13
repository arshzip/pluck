# pluck

### Download high quality music from YouTube Music.

![pluck](assets/preview.gif)

#### Search or paste a link. Tracks, albums, videos (audio only). Get 256 kbps AAC audio files with album art + proper tagging.


> [!NOTE]
> While `pluck` works without YT Premium (or without a YT account), 256kbps AAC streams are only available to Premium subscribers. In case a YT Premium session cookie isn't supplied, `pluck` will fall back to 160kbps Opus streams and transcode them.

## Why not just use yt-dlp?
YT Music's catalog splits into two halves: music videos on youtube.com (which yt-dlp), and dedicated streaming-only tracks reserved for Premium accounts. `pluck` exists to support and prioritize dedicated streaming-only tracks which are natively higher-quality AAC. This also avoids a lossy Opus→AAC/mp3 re-encode. Not to mention music videos often have intros/interludes which dedicated streaming tracks don't.

## Install

    uv tool install git+https://github.com/arshzip/pluck

or from a clone: `uv tool install --editable .` — both put `pluck` on PATH.
Needs ffmpeg.

## Setup auth for YT Premium
On first launch, `pluck` will prompt you to either paste a cookie or launch a temporary Chrome profile where you can sign in for `pluck` to automatically fetch and store your cookie.

You can also refresh the cookie by running `pluck login`.

## Usage

- paste a `music.youtube.com` link to a track or an album (several at once works too)
- search and pick from the top results
- paste the path to a `.txt` file with links
- files are named Artist - Title.m4a and saved to your `download_dir` (`~/Downloads/Pluck` by default)

## Config
- `~/.pluck/config.json` stores your auth cookie and download_dir (where downloads are saved).

## Notes
depends on yt-dlp + ytmusicapi + ffmpeg + mutagen.
