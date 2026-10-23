# YouTube Downloader

A simple command-line tool for downloading YouTube videos and audio, built on [yt-dlp](https://github.com/yt-dlp/yt-dlp).

> ## ⚠️ Disclaimer
>
> This tool is provided for personal and educational use only. Downloading
> videos may violate [YouTube's Terms of Service](https://www.youtube.com/t/terms),
> which generally prohibit downloading content except where YouTube explicitly
> allows it. You are responsible for how you use this tool — only download
> content you own or have the right to download, and respect copyright law.
> The author accepts no liability for misuse.

## Requirements

- Python 3.10+ (yt-dlp itself no longer supports 3.9)
- [ffmpeg](https://ffmpeg.org/) — *optional but recommended*. Without it, downloads still work but are limited to single-file formats (usually 720p max), and audio-only MP3 mode is unavailable.
- [Deno](https://deno.com/) — *needed for full YouTube support*. yt-dlp runs YouTube's player JavaScript in it to unlock most formats; without it, yt-dlp falls back to a client that offers fewer of them, and the tool prints yt-dlp's warning saying so. The challenge-solver scripts Deno runs come with the install below (yt-dlp's `default` extra).

## Installation

```bash
pip install .
```

That pulls in yt-dlp (with its `default` extra) and adds a `youtube-downloader` command you can run from
anywhere. Running `pip install -r requirements.txt` from the repository root
does the same thing - the requirements files delegate to `pyproject.toml`,
which is the single source of truth for dependencies.

One project, a few different spellings, so nothing below is a typo: the
repository is `gen-YoutubeDownloader`, it installs as the distribution
`gen-youtubedownloader`, the command it puts on your PATH is
`youtube-downloader`, and the module you can run directly from a checkout is
`youtube_downloader.py`.

Install ffmpeg if you don't have it:

- **Ubuntu/Debian:** `sudo apt install ffmpeg`
- **macOS:** `brew install ffmpeg`
- **Windows:** `winget install ffmpeg` or download from [ffmpeg.org](https://ffmpeg.org/download.html)

On Windows, open a **new** terminal after installing ffmpeg so it's picked up on your PATH. If ffmpeg isn't found, the tool automatically falls back to lower-quality single-file downloads and prints a warning.

Install Deno the same way:

- **Any platform:** `pip install deno`, in the same environment as this tool
- **Windows:** `winget install DenoLand.Deno`
- **macOS:** `brew install deno`
- **Linux:** `curl -fsSL https://deno.land/install.sh | sh`, or your distribution's package

## Usage

Download a video at the best available quality:

```bash
youtube-downloader https://www.youtube.com/watch?v=VIDEO_ID
```

Working from a checkout without installing? Every example below works the
same way as `python youtube_downloader.py ...`.

The tool must be run in an interactive terminal: if stdout or stderr is piped
or redirected it refuses to start (exit status 2). `--help` and `--version`
still work when piped.

### Options

| Option | Description | Default |
|---|---|---|
| `-q`, `--quality` | Preferred max resolution: `best`, `2160`, `1440`, `1080`, `720`, `480`, `360`. Ignored with `--audio-only` | `best` |
| `-a`, `--audio-only` | Download audio only, converted to MP3 | off |
| `-o`, `--output` | Output directory | system Downloads folder |
| `-s`, `--subtitles` | Also download English subtitles | off |
| `-c`, `--client` | Force a specific YouTube player client — one of `android`, `web_safari`, `tv`, `web` | auto |
| `-V`, `--version` | Print this tool's version, plus the yt-dlp and Python versions | — |

### Examples

```bash
# Download at 720p max
youtube-downloader URL -q 720

# Audio only (MP3)
youtube-downloader URL --audio-only

# Several videos at once, with subtitles
youtube-downloader URL1 URL2 URL3 -s
```

Each finished video prints the path it was saved to.

Videos are saved to your system Downloads folder (e.g. `C:\Users\you\Downloads` on
Windows, `~/Downloads` on macOS and Linux) as `Title [video-id].EXT`. With ffmpeg
installed that extension is `.mp4`; `--audio-only` produces `.mp3`, and without
ffmpeg you get whatever single-file format YouTube offers (often `.webm`). If you
have moved your Downloads folder, the tool follows it: on Windows it reads the
location Explorer records, and on Linux it honours your XDG `user-dirs.dirs`
setting. Pass `-o` to save somewhere else.

### One video at a time

This tool downloads single videos. A normal watch URL is fine even when it
carries a playlist id — `watch?v=VIDEO_ID&list=...` downloads just that video
and ignores the rest of the list.

URLs that name a *collection* rather than a video are refused before anything
is downloaded, with a message saying which kind you passed:

```console
$ youtube-downloader https://www.youtube.com/@handle
This tool downloads one video at a time, and this URL points somewhere else:
  https://www.youtube.com/@handle
    -> a channel
Pass a video URL instead, e.g. https://www.youtube.com/watch?v=VIDEO_ID
```

That covers playlist URLs, channel URLs (`/@handle`, `/channel/...`, `/c/...`,
`/user/...`), search results, and feeds. A channel's own *live* page
(`/@handle/live`) is the exception: it names the one video currently streaming
there, so it is accepted. If you pass several URLs at once, every rejected one
is listed in a single message, so you fix them all in one go.

### A note on `-q`

`-q` is a *preferred* maximum, not a hard one. If no format satisfies the cap —
say you ask for `-q 360` on a video whose smallest format is 720p — the tool
downloads the best available format rather than failing outright (the
highest resolution on offer, not the one nearest the cap), and prints a
warning naming the video and the height it actually got. Failing would be the
less useful behaviour; doing it silently would be the worse one. When you pass
several URLs, each video that overruns the cap gets its own warning.

### "This video is not available" / "HTTP Error 403: Forbidden" errors

Some videos (for example ones flagged as *made for kids*) are only served to
certain YouTube player clients, and YouTube sometimes rejects the media
download (403) for one client while accepting it for another. On these
errors the tool automatically retries with alternate clients (`android`,
`web_safari`, `tv`) before giving up. You can also force a specific client
with `-c android` if you already know which one works. The first, un-forced
attempt lets yt-dlp use its own default set of clients; `-c web` narrows that
to the plain web client alone, which helps when working out exactly which
client a particular video needs.

If these errors start appearing on many videos, YouTube has likely changed
something on their side — update yt-dlp first: `pip install -U "yt-dlp[default]"`.
Keep the `[default]` part: it updates the challenge-solver scripts along with
yt-dlp, and a newer yt-dlp refuses the old ones.

## Reporting a problem

`-V` prints everything a bug report needs in one line:

```console
$ youtube-downloader --version
youtube-downloader X.Y.Z (yt-dlp YYYY.MM.DD, Python 3.X.Y)
```

Check the yt-dlp version there against the latest release before filing —
most "it stopped working" reports are an outdated yt-dlp rather than a bug
in this wrapper.

## Development

```bash
pip install -r tests/requirements-dev.txt
ruff check .
ruff format --check .
mypy
pytest -q
```

These are exactly the checks CI runs. `mypy` needs no arguments — the files it
checks, the Python version, and the target platform all come from
`[tool.mypy]` in `pyproject.toml`.

The tests run offline — the yt-dlp entry point is stubbed out, so no video is
ever fetched. See [CONTRIBUTING.md](.github/CONTRIBUTING.md) for details.

## Contributing

Bug reports and pull requests are welcome — see
[CONTRIBUTING.md](.github/CONTRIBUTING.md) for the development setup and the checks to
run before opening one. Participation is governed by the
[Code of Conduct](.github/CODE_OF_CONDUCT.md). To report a security problem privately, see
[SECURITY.md](.github/SECURITY.md).

## License

Released under the [MIT License](LICENSE).
