# pluck

YouTube Music → tagged audio files, from the terminal.

![pluck](assets/preview.gif)

Search or paste a link. Tracks, albums, videos (audio only). With a YT Music
Premium session, rips are bit-perfect copies of the 256 kbps AAC catalog
streams.

## install

    uv tool install git+https://github.com/arshzip/pluck

or from a clone: `uv tool install --editable .` — both put `pluck` on PATH.
Needs ffmpeg.

## premium

Paste cookies, login, or continue signed out. `pluck login`
launches a throwaway Chrome to auth and saves the session to
`~/.pluck/config.json` — the browser profile is kept, so rerunning just
refreshes. Signed in = bit-perfect 256 kbps AAC and premium-only tracks.
Signed out = ~160 kbps opus, transcoded.

`-c firefox` or `-c ~/cookies.txt` for a one-off session.

## usage

- paste a `music.youtube.com` link to a track or an album (several at once works too)
- search and pick from the results
- paste the path to a `.txt` file with links
- `a3` rips the album behind result 3

    -b 128|192|256|320   bitrate (default 256)
    -f aac|mp3           aac → .m4a (default) or mp3 with ID3v2.3

## notes
depends on yt-dlp + ytmusicapi + ffmpeg + mutagen. Thanks to bgutil-ytdlp-pot-provider for the sign in bypass. 
