#!/usr/bin/env python3
"""YouTube video downloader CLI built on yt-dlp.

Usage examples (or `python youtube_downloader.py ...` from a checkout):
    youtube-downloader https://www.youtube.com/watch?v=VIDEO_ID
    youtube-downloader URL -q 720
    youtube-downloader URL --audio-only
    youtube-downloader URL -o ~/Videos

By default videos are saved to the system Downloads folder.
"""

from __future__ import annotations

import argparse
import errno
import os
import shutil
import subprocess
import sys
import urllib.parse
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

# The UP036 suppression below is deliberate: this guard is the point - it
# exists so that Python 3.8 to 3.10 gets a clear message instead of a
# confusing failure later. Nothing older reaches it: this file uses 3.8
# syntax (an assignment expression), so 3.7 and below fail to compile it
# before any line runs. test_the_version_guard_is_reachable_on_3_8 keeps
# newer syntax out. Keep the suppression on the `if` line itself and
# do not spell a bare noqa marker anywhere in this comment - ruff parses one
# at the start of a comment as a blanket directive, and a malformed one
# elsewhere as an invalid directive.
if sys.version_info < (3, 11):  # noqa: UP036  # pragma: no cover
    sys.exit(
        "This tool requires Python 3.11 or newer "
        f"(running {sys.version_info.major}.{sys.version_info.minor})."
    )

try:
    import yt_dlp
except ImportError:
    sys.exit("yt-dlp is not installed. Run: pip install -r requirements.txt")


class DownloaderError(Exception):
    """A fatal problem to report as a message, never as a traceback.

    Raised instead of calling sys.exit() inside download(), so the function
    keeps the contract its docstring advertises - return a failure count - for
    anyone importing this module. main() turns it into the same exit status
    and the same message a direct sys.exit() produced.
    """


class _OwnWritePipeError(BrokenPipeError):
    """A closed pipe hit by one of this tool's own writes - and only those.

    Every BrokenPipeError _print() lets out is one of these, so that the
    handlers can tell "the reader of our stdout went away" apart from a
    BrokenPipeError that yt-dlp's networking layer raised because the *site*
    closed its socket. The two share a type and an errno; they do not share a
    meaning. The first is a pipe failure, answered by discarding the pending
    output and exiting 1 without a word. The second is a download failure
    like any other, and treating it as the first left the user with exit
    status 1 and no output at all - not even the "Failed to download" line.
    """


class _OwnWriteEncodeError(UnicodeEncodeError):
    """An output stream that cannot hold a character this tool printed.

    The same split as _OwnWritePipeError, for the same reason. yt-dlp raises
    UnicodeEncodeError too - opening a file whose name the filesystem
    encoding cannot represent, such as a title carrying a lone surrogate, or
    any non-Latin title under a legacy locale - and that one is a download
    failure. Re-raising it as this tool's own write sent the user to
    PYTHONIOENCODING, which cannot help, and abandoned every remaining URL.
    """


def _print(*values: object, **kwargs: Any) -> None:
    """print(), with a closed pipe reported the same way on every platform.

    When the reader of a pipe goes away - `| head`, or a pager quit part-way
    through - POSIX raises BrokenPipeError on the next write. Windows does
    not: a flushed write to a reader-less pipe raises OSError with errno
    EINVAL ("[Errno 22] Invalid argument"), which is not a BrokenPipeError
    and which download()'s per-attempt `except Exception` therefore caught,
    counting a video that had already been saved as a failed download; the
    end-of-URL close() then raised it again outside every handler, and the
    user got a traceback. Every write this tool makes goes through here so
    that the handlers only ever have one exception to name, and that
    exception is _OwnWritePipeError: the handlers act on it, and on nothing
    else, so a BrokenPipeError from a socket yt-dlp was reading is still a
    download failure. EINVAL is mapped only on Windows and only for this
    tool's own writes - an EINVAL raised by yt-dlp, say for an unrepresentable
    filename, still means what it says. A character the stream cannot encode
    is tagged the same way, as _OwnWriteEncodeError.
    """
    try:
        print(*values, **kwargs)
    except BrokenPipeError as exc:
        raise _OwnWritePipeError(*exc.args) from exc
    except UnicodeEncodeError as exc:
        raise _OwnWriteEncodeError(
            exc.encoding, exc.object, exc.start, exc.end, exc.reason
        ) from exc
    except OSError as exc:
        if sys.platform == "win32" and exc.errno == errno.EINVAL:
            raise _OwnWritePipeError(errno.EPIPE, "Broken pipe") from exc
        raise


def version_string() -> str:
    """One line carrying everything a bug report needs.

    The bug report template asks for this tool's version, the yt-dlp version,
    and the Python version, and "it stopped working" is nearly always an outdated
    yt-dlp - so print all three together rather than making someone assemble
    them from three commands.
    """
    try:
        from importlib.metadata import version

        own = version("gen-youtubedownloader")
    except Exception:
        # Running from a source checkout with nothing installed. Not knowing
        # the version is not worth failing over.
        own = "unknown (source checkout)"
    python = (
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    )
    return (
        f"youtube-downloader {own} "
        f"(yt-dlp {yt_dlp.version.__version__}, Python {python})"
    )


# The Windows KNOWNFOLDERID for the Downloads folder. Public, documented
# constant - not a secret.
_DOWNLOADS_FOLDER_GUID = "{374DE290-123F-4565-9164-39C4925E467B}"


def _known_folder_path(guid: str) -> Path | None:
    """Resolve a Windows known folder through SHGetKnownFolderPath.

    This is the authoritative API: it reflects a relocated folder even when
    the registry keys below are stale. Returns None anywhere it is
    unavailable, so callers can fall back.
    """
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        folder_id = GUID()
        if ctypes.windll.ole32.CLSIDFromString(guid, ctypes.byref(folder_id)) != 0:
            return None
        buffer = ctypes.c_wchar_p()
        status = ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(folder_id), 0, None, ctypes.byref(buffer)
        )
        try:
            if status != 0:
                return None
            value = buffer.value
        finally:
            ctypes.windll.ole32.CoTaskMemFree(buffer)
    except Exception:
        # ctypes.windll is missing off Windows, and the call can fail in
        # unusual environments. Either way, fall back to the registry.
        return None
    return Path(value) if value else None


def _windows_downloads_dir() -> Path | None:
    """Ask Windows where the Downloads folder is (it can be relocated)."""
    resolved = _known_folder_path(_DOWNLOADS_FOLDER_GUID)
    if resolved:
        return resolved

    try:
        import winreg
    except ImportError:
        return None

    # "User Shell Folders" holds the authoritative value (with environment
    # variables left unexpanded); "Shell Folders" is a compatibility cache
    # that Windows regenerates and may not carry the Downloads entry.
    key_paths = (
        r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
    )
    for key_path in key_paths:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                value, _ = winreg.QueryValueEx(key, _DOWNLOADS_FOLDER_GUID)
        except OSError:
            continue
        # Same guard the XDG lookup applies, and for the same reason: a
        # relative value - a stale entry, or one naming an environment
        # variable that no longer exists, which expandvars leaves in place -
        # would resolve against the current working directory and scatter
        # downloads wherever the tool happened to be run.
        resolved = _absolute_or_none(str(value).strip())
        if resolved:
            return resolved
    return None


def _home_or_none() -> Path | None:
    """Path.home(), or None where no home directory can be determined.

    Path.home() raises RuntimeError - not OSError - when neither HOME nor the
    Windows profile variables are set: a systemd unit, a cron job with a
    stripped environment, a slim container. Every caller here is resolving a
    *default*, so None lets them fall through instead of taking the exception
    out through main(), whose handlers name specific exception types and
    RuntimeError is not among them.
    """
    try:
        return Path.home()
    except RuntimeError:
        return None


def _absolute_or_none(raw: str, *, expand: bool = True) -> Path | None:
    """Expand a configured path, keeping it only if it came out absolute.

    The XDG basedir spec says a relative value must be treated as unset. Left
    unchecked, one would be resolved against the current working directory,
    so the tool would quietly write downloads wherever it happened to be run.

    An empty value needs no branch of its own: it expands to Path("."), which
    the absoluteness check below already rejects like any other relative one.

    `expand=False` takes the value literally - no "$VAR", no "~". That is
    what a single-quoted user-dirs.dirs value means to the shell that sources
    the file, so a literal "$HOME/Videos" is then relative and falls back,
    the same way the desktop's own reader (which ignores the line) does.

    A value carrying a NUL byte names no path at all, and mkdir() would
    refuse it with ValueError - not OSError - which no handler in this module
    names. Treat it as unset like any other unusable value.
    """
    if "\0" in raw:
        return None
    try:
        path = Path(os.path.expandvars(raw)).expanduser() if expand else Path(raw)
    except RuntimeError:
        # expanduser() raises RuntimeError - not OSError - when it cannot
        # resolve a "~": no home directory is set, or "~someone" names a user
        # with no passwd entry. A configured value we cannot expand tells us
        # nothing, so treat it exactly like a relative one and fall back.
        return None
    return path if path.is_absolute() else None


def _unquoted_word(raw: str) -> str:
    """The first shell word of an unquoted value, with its escapes applied.

    Whitespace ends the assignment, exactly as it does after a quoted value:
    reading on took "$HOME/Downloads trailing" as one directory name, which
    mkdir() then created, while the shell and GLib both left the entry unset.
    A backslash escapes the character after it, so "My\\ Videos" stays one
    word.

    A "#" opens a comment only where a word opens, so here only as the first
    character - and needs no branch of its own: a word that starts with "#"
    is relative, so the caller's absoluteness check already discards it.
    Cutting at every "#" instead took "$HOME/Vids#1" down to "$HOME/Vids": a
    different directory, which mkdir() then created and downloaded into,
    while xdg-user-dirs itself reads the whole name.
    """
    word: list[str] = []
    index = 0
    while index < len(raw):
        char = raw[index]
        if char == "\\" and index + 1 < len(raw):
            word.append(raw[index + 1])
            index += 2
            continue
        if char.isspace():
            break
        word.append(char)
        index += 1
    return "".join(word)


def _config_value(raw: str) -> tuple[str, bool]:
    """Take the value out of one user-dirs.dirs assignment.

    Returns the value and whether the shell would expand it: True for an
    unquoted or double-quoted value, False for a single-quoted one.

    The file is a shell fragment, so the value is normally double-quoted and
    may be followed by a comment. Stripping quotes alone left a trailing
    "# note" attached to the path, which is still absolute on POSIX - so it
    was accepted and mkdir() silently created a directory named after the
    comment.

    The reference reader of this file is not the shell but GLib's
    load_user_special_dirs_from_string(), which sits behind every GTK file
    chooser and behind xdg-user-dir(1): it requires a double-quoted value,
    skips whitespace around the "=", expands only a leading "$HOME", takes the
    value up to the *last* double quote on the line, and lets a later line
    override an earlier one. The file is a shell fragment by design, though,
    so on top of what GLib reads this parser follows shell rules for the
    shapes GLib rejects: a quoted section ends at its closing quote, inside
    double quotes a backslash escapes a quote or another backslash, a
    single-quoted value is taken literally with nothing expanded, whatever
    follows the closing quote *immediately* is part of the same word and is
    concatenated on ("$HOME/a"extra is $HOME/aextra), whitespace ends the
    assignment, "#" is a comment opener only at the start of a word, and an
    unquoted value is accepted. Everything xdg-user-dirs-update writes reads
    the same both ways; the two only differ on hand-edited lines GLib would
    ignore outright, or - for text after the closing quote - would read to
    the last quote instead. Anything looser than this silently resolves a
    different directory than the desktop does.

    Three shell rules are knowingly not modelled, because no path a person
    would configure needs them: a backslash-escaped "$" inside double quotes
    or in an unquoted value (the "$" would be expanded again downstream), a
    tail after a single-quoted section that itself carries a "$" (the whole
    word is taken literally), and a quoted section that starts part-way
    through an unquoted value (the quote is kept as a character).
    """
    raw = raw.strip()
    quote = raw[:1]
    if quote not in ('"', "'"):
        return _unquoted_word(raw), True
    expand = quote == '"'
    value: list[str] = []
    index = 1
    while index < len(raw):
        char = raw[index]
        if char == quote:
            break
        if expand and char == "\\" and raw[index + 1 : index + 2] in ('"', "\\"):
            value.append(raw[index + 1])
            index += 2
            continue
        value.append(char)
        index += 1
    else:
        # An unterminated quote is malformed either way; take the rest and let
        # the absoluteness check decide.
        return "".join(value), expand
    rest = raw[index + 1 :]
    if rest and not rest[0].isspace():
        # Still inside the same word, so the "#" rule does not apply to what
        # comes next: end the value at whitespace rather than at a "#".
        value.append(rest.split()[0])
    return "".join(value), expand


def _xdg_downloads_dir() -> Path | None:
    """Ask the XDG user-dirs config where the Downloads folder is.

    The home directory itself is never an answer, from any of the three
    sources. xdg-user-dirs disables an entry by pointing it at $HOME, and
    xdg-user-dir(1) echoes $HOME for an entry that is unset - either way the
    user has not named a Downloads folder, and saving straight into $HOME
    is not what they asked for. With no home to compare against, an absolute
    value is taken at face value.
    """
    home = _home_or_none()

    def answer(resolved: Path | None) -> Path | None:
        return resolved if resolved and resolved != home else None

    resolved = answer(_absolute_or_none(os.environ.get("XDG_DOWNLOAD_DIR", "").strip()))
    if resolved:
        return resolved

    xdg_user_dir = shutil.which("xdg-user-dir")
    if xdg_user_dir:
        try:
            result = subprocess.run(
                [xdg_user_dir, "DOWNLOAD"],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
        # UnicodeDecodeError is listed because text=True decodes the reply with
        # the locale codec, and a home directory named in a legacy encoding
        # makes that raise. It is a ValueError, so neither OSError nor
        # SubprocessError covers it - see the same note on the read below.
        except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
            pass
        else:
            resolved = answer(_absolute_or_none(result.stdout.strip()))
            if resolved:
                return resolved

    config_home = _absolute_or_none(os.environ.get("XDG_CONFIG_HOME", "").strip())
    config_dir = config_home or (home / ".config" if home else None)
    if config_dir is None:
        return None
    user_dirs = config_dir / "user-dirs.dirs"
    # utf-8-sig rather than utf-8: a byte-order mark would otherwise survive
    # into the first line, where it defeats the startswith() test below and
    # silently hides an entry that is only ever the file's first line.
    try:
        lines = user_dirs.read_text(encoding="utf-8-sig").splitlines()
    # A user-dirs.dirs naming a home directory in a legacy locale encoding is
    # not valid UTF-8, and UnicodeDecodeError is a ValueError rather than an
    # OSError - so on its own the OSError clause let it out of this function,
    # past _run(), and past main(), which handles only KeyboardInterrupt,
    # BrokenPipeError, UnicodeEncodeError and DownloaderError (a decode error
    # is not an encode error). The user got a traceback before any
    # download was attempted. An undecodable file tells us nothing, so treat it
    # the same way as an unreadable one and fall back to ~/Downloads.
    except (OSError, UnicodeDecodeError):
        return None
    # The file is a shell fragment that xdg-user-dirs sources, so a later
    # assignment overrides an earlier one. Keep scanning and take the last
    # resolvable entry rather than returning on the first: stopping early sent
    # downloads to the superseded directory while every other XDG consumer
    # used the current one, with nothing printed to explain the difference.
    found: Path | None = None
    for line in lines:
        # Split at the first "=" and compare the whole key, so that a longer
        # key merely starting with XDG_DOWNLOAD_DIR does not match, and so
        # that whitespace around the "=" - which GLib's reader of this file
        # skips, and which a hand-edited file can carry - does not hide the
        # entry: a line the desktop honours must not send this tool to
        # ~/Downloads instead, silently.
        key, sep, raw = line.partition("=")
        if not sep or key.strip() != "XDG_DOWNLOAD_DIR":
            continue
        # Entries are usually written as "$HOME/Downloads".
        value, expand = _config_value(raw)
        resolved = _absolute_or_none(value, expand=expand)
        if resolved:
            found = resolved
    # Compared after the scan, not per line: a later "$HOME" disables the
    # entry, so it must override an earlier real directory, not be skipped.
    return answer(found)


def default_download_dir() -> Path:
    """The system Downloads folder, falling back to ~/Downloads.

    Raises DownloaderError when no platform lookup answered *and* no home
    directory can be determined - there is then nowhere sensible to put a
    download, and -o is the only way forward. Raised rather than allowed to
    surface as Path.home()'s RuntimeError, which reached the user as a
    traceback before anything was attempted.
    """
    if sys.platform == "win32":
        resolved = _windows_downloads_dir()
    elif sys.platform == "darwin":
        resolved = None  # macOS always keeps it at ~/Downloads.
    else:
        resolved = _xdg_downloads_dir()
    if resolved:
        return resolved
    home = _home_or_none()
    if home is None:
        raise DownloaderError(
            "Cannot determine your home directory, so there is no default "
            "download location. Set HOME (or USERPROFILE on Windows), or "
            "name a directory with -o."
        )
    return home / "Downloads"


def build_format_selector(quality: str, audio_only: bool, have_ffmpeg: bool) -> str:
    """Translate CLI quality options into a yt-dlp format selector."""
    if audio_only:
        return "bestaudio/best"
    if not have_ffmpeg:
        # Without ffmpeg we can't merge separate video/audio streams. yt-dlp's
        # "best" already means "the best format containing both video and
        # audio", which is exactly the single-file formats we need here.
        if quality == "best":
            return "best"
        return f"best[height<={quality}]/best"
    if quality == "best":
        return "bestvideo+bestaudio/best"
    # Numeric quality, e.g. 1080 -> cap the video height at that value, then
    # fall back to an uncapped format rather than failing outright when no
    # format satisfies the cap.
    return (
        f"bestvideo[height<={quality}]+bestaudio"
        f"/best[height<={quality}]"
        f"/bestvideo+bestaudio/best"
    )


class ProgressReporter:
    """Renders yt-dlp's progress and tracks whether a line is left open.

    The progress line is redrawn in place with a carriage return and no
    newline, so whatever is printed next - an error, a warning, the banner
    for the following URL - lands on top of it unless the line is closed
    first. close() does that, and is idempotent so callers never have to
    track whether there was anything to close.
    """

    def __init__(self) -> None:
        self._line_open = False

    def hook(self, d: dict[str, Any]) -> None:
        """Progress hook: renders the live line, announces nothing.

        The "finished" event here fires once per *downloaded stream*, not
        once per video, and carries that stream's own temporary filename -
        see announce_hook() for why naming a file from it is wrong.
        """
        status = d.get("status")
        if status == "downloading":
            percent = d.get("_percent_str", "?").strip()
            speed = d.get("_speed_str", "?").strip()
            eta = d.get("_eta_str", "?").strip()
            _print(f"\r  {percent} at {speed}, ETA {eta}   ", end="", flush=True)
            self._line_open = True
        elif status in ("finished", "error"):
            self.close()

    def announce_hook(self, d: dict[str, Any]) -> None:
        """Postprocessor hook: names the file the user actually ended up with.

        This has to come from a postprocessor rather than from the progress
        hook. Whenever ffmpeg merges separate video and audio streams - the
        default for every quality - yt-dlp downloads each one to a per-format
        temporary name ("Title [id].f399.mp4", "Title [id].f251.webm") and
        merges them afterwards, firing a progress "finished" event per stream.
        Reporting those names announced two files that no longer exist by the
        time the run ends, and never named the merged result at all. The same
        applies to --audio-only, where the downloaded .webm is replaced by the
        extracted .mp3.

        MoveFiles is the last postprocessor to touch a video, so its
        info_dict carries the final path - the merge and the audio extraction
        have both already been applied.
        """
        if d.get("status") != "finished" or d.get("postprocessor") != "MoveFiles":
            return
        filepath = (d.get("info_dict") or {}).get("filepath")
        if filepath:
            self.close()
            _print(f"Downloaded: {filepath}")

    def close(self) -> None:
        """End an in-progress line so later output starts on its own."""
        if self._line_open:
            # The unterminated line is on stdout, so that is where the
            # newline belongs. Sending it to stderr only looks right while
            # both streams happen to share a terminal.
            _print(flush=True)
            self._line_open = False


class _YtDlpLogger:
    """Where yt-dlp's own console output goes: its warnings, and nothing else.

    "quiet" and "no_warnings" are not the switches they look like. With no
    logger set, YoutubeDL.trouble() still writes every "ERROR: ..." line to
    stderr before it raises DownloadError - from inside ydl.download(), before
    the except clause in download() can close the progress line - so the error
    landed appended to the live line, and then appeared a second time in this
    tool's own "Failed to download ..." message, which carries the same text.
    A logger routes all three of yt-dlp's channels (to_screen, warnings,
    errors) through this object instead. Three channels, three methods:
    yt-dlp calls debug(), warning() and error() on the logger it is given, and
    nothing else. error() drops its message, because every failure is still
    reported exactly once, by download(), from the exception yt-dlp raised;
    debug() drops to_screen()'s chatter and yt-dlp's own progress line.

    warning() is the one channel nothing else reports, so it is printed.
    Dropping it hid the sentence that explains a missing format - "No
    supported JavaScript runtime could be found", or formats skipped for want
    of a PO token - and the user got a lower quality, or "Requested format is
    not available", with no cause to act on. "no_warnings" does not stop
    yt-dlp handing warnings to a logger. Each one is printed once per run, on
    stderr, after closing the progress line it would otherwise land on:
    yt-dlp does not pass its only_once flag to a logger, and the retry ladder
    builds a fresh YoutubeDL per attempt, so the same warning otherwise
    repeated for every client tried and every URL.

    `reporter` is the current URL's ProgressReporter; download() sets it.
    """

    def __init__(self) -> None:
        self.reporter: ProgressReporter | None = None
        self._seen: set[str] = set()

    def debug(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        if message in self._seen:
            return
        self._seen.add(message)
        if self.reporter is not None:
            self.reporter.close()
        _print(f"Warning: {message}", file=sys.stderr)

    def error(self, message: str) -> None:
        pass


def make_height_cap_warner(
    max_height: int, url: str
) -> Callable[[dict[str, Any]], None]:
    """A progress hook that warns if -q's cap was exceeded for `url`.

    build_format_selector() deliberately ends its numeric ladders with an
    uncapped tier, so a cap no format satisfies degrades instead of failing
    outright. That is the right trade, but silently handing back a larger
    file than asked for is not - so say so, once per video.

    One of these is built per URL. A single warner shared across a batch
    would latch after the first over-cap download and leave every later
    video's overrun unreported, which is the silence this exists to prevent.
    """
    warned = False

    def hook(d: dict[str, Any]) -> None:
        nonlocal warned
        if warned or d.get("status") != "finished":
            return
        height = (d.get("info_dict") or {}).get("height")
        if height and height > max_height:
            warned = True
            _print(
                f"Warning: {url}: no format at or below {max_height}p was "
                f"available; downloaded {height}p instead.",
                file=sys.stderr,
            )

    return hook


# Player clients to retry with when YouTube claims a video is unavailable
# or rejects the media download. Some videos (e.g. ones flagged "made for
# kids") are served to certain clients only, and media URLs issued for one
# client sometimes return 403 while another client's work fine.
FALLBACK_CLIENTS = ["android", "web_safari", "tv"]

# What -c/--client accepts. Deliberately a superset of the fallback ladder:
# the first attempt forces no client at all, leaving yt-dlp to use its own
# default set (which includes "web" among others), so "web" does not belong
# in the ladder. Pinning it on its own narrows that set to a single client,
# which is useful when working out which client a video actually needs.
SELECTABLE_CLIENTS = [*FALLBACK_CLIENTS, "web"]

# Error fragments that another player client may not hit. These are kept
# specific on purpose: a loose "not available" also matches "Requested
# format is not available", and a loose "403" matches any video id that
# happens to contain those digits.
_RETRYABLE_ERROR_PHRASES = (
    "http error 403",
    # Anchored to the status code rather than spelled bare: yt-dlp puts the
    # video title in its error messages, so a bare "forbidden" also matched
    # any video with that word in its title and sent unrelated failures round
    # the whole client ladder.
    "403: forbidden",
    "video unavailable",
    "this video is not available",
    "not available on this app",
    "content isn't available",
    "content is not available",
)


def is_client_retryable_error(exc: Exception) -> bool:
    """Errors that another player client may not hit.

    There is deliberately no deny-list in front of the phrases above. One
    used to sit here for "Requested format is not available", but every
    phrase in the tuple is a whole sentence that is not a substring of it,
    so the check could never change a result. If a phrase is ever loosened
    back to a bare "not available", test_non_retryable_errors is what
    catches format-selection failures being sent round the ladder.
    """
    msg = str(exc).lower()
    return any(phrase in msg for phrase in _RETRYABLE_ERROR_PHRASES)


# Hosts whose URLs this tool understands well enough to classify. Anything
# else is passed straight through to yt-dlp, which supports thousands of
# sites - narrowing that to YouTube would be a regression, not a fix.
_YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "gaming.youtube.com",
        "youtube-nocookie.com",
    }
)
_YOUTUBE_SHORT_HOSTS = frozenset({"youtu.be"})

# Path prefixes that address exactly one video by id, e.g. "/shorts/VIDEO_ID".
# "/e/" is the older spelling of "/embed/" that yt-dlp's YouTube extractor
# still lists; "/clip/" is the "Share clip" URL, which names one segment of
# one video and has its own extractor in yt-dlp.
_VIDEO_PATH_PREFIXES = ("/shorts/", "/embed/", "/e/", "/live/", "/v/", "/clip/")

# Paths that carry the video id in the "v" query parameter rather than in the
# path: the usual watch page, the pop-out player, and the bare root, all of
# which yt-dlp's extractor accepts as "/?v=VIDEO_ID" style URLs.
_WATCH_PATHS = ("/watch", "/watch_popup", "/")

# "/embed/videoseries?list=..." is the one /embed/ URL that is a playlist
# rather than a video, so the id segment alone does not settle it.
_EMBED_PLAYLIST_ID = "videoseries"

# Paths that address a channel. A channel URL names a collection - except for
# its "/live" page, which yt-dlp's tab extractor resolves to the one video
# currently streaming on that channel (or reports that the channel is not
# live), and which is the link every channel page offers as "watch live".
_CHANNEL_PATH_PREFIXES = ("/@", "/channel/", "/c/", "/user/")


def _parsed_host(url: str) -> str | None:
    """The lowercased host of `url` without a "www." prefix, or None.

    None means the URL could not be parsed, and callers treat it the way they
    treat any host this module does not recognise: leave it to yt-dlp.
    urlparse raises ValueError on a malformed netloc - an unbalanced "[", the
    usual result of a mangled paste, is the common way to get one - and
    .hostname raises for the same reason. Nothing here is the URL validator,
    so the exception must not escape as a traceback, which is precisely what
    DownloaderError and the handlers in main() exist to prevent.
    """
    try:
        parsed = urllib.parse.urlparse(url)
        # A pasted "youtube.com/@handle" has no scheme, so urlparse puts the
        # host in .path and leaves .hostname empty. Re-parse it as a network
        # location so the host checks see it. The scheme guard keeps this
        # away from yt-dlp's own "ytsearch:..." style prefixes.
        if not parsed.scheme and not parsed.netloc:
            parsed = urllib.parse.urlparse(f"//{url}")
        return (parsed.hostname or "").lower().removeprefix("www.")
    except ValueError:
        return None


def _is_youtube_url(url: str) -> bool:
    """Whether `url` is on a host whose player clients this tool can pick.

    The -c ladder reaches yt-dlp as extractor_args["youtube"], which only the
    YouTube extractors read. Walking it for any other host re-ran the identical
    request three more times behind a notice about YouTube player clients.
    """
    host = _parsed_host(url)
    return host in _YOUTUBE_HOSTS or host in _YOUTUBE_SHORT_HOSTS


def _describe_youtube_target(path: str, query: dict[str, list[str]]) -> str:
    """Name what a non-video YouTube URL points at, for the error message.

    Saying "that URL points to a channel" beats "that URL is not a video":
    it tells the user which of the things they might have pasted they
    actually did paste.
    """
    if path == "/playlist" or query.get("list"):
        return "a playlist"
    if path.startswith(_CHANNEL_PATH_PREFIXES):
        return "a channel"
    if path == "/results":
        return "a search results page"
    if path.startswith("/feed/"):
        return "a feed"
    return "not a single video"


def single_video_problem(url: str) -> str | None:
    """Describe why `url` is not one video, or return None if it is fine.

    yt-dlp's "noplaylist" option is not the guard it looks like: it only
    applies when a URL carries *both* a video id and a list id, i.e. a
    "watch?v=...&list=..." URL. InfoExtractor._yes_playlist() returns early
    for anything else and never consults the option, so a bare
    "playlist?list=...", "@handle", or "/results?search_query=..." URL
    enumerated and downloaded every video it named - unbounded, and the exact
    opposite of what this tool documents.

    Only YouTube URLs are classified. A URL on any other host is left alone
    and returns None; yt-dlp handles thousands of sites and this tool has no
    business second-guessing their shapes.
    """
    # A malformed URL is not classified (see _parsed_host): yt-dlp reports the
    # bad URL in its own words, the same way every other host this function
    # deliberately declines to classify is handled.
    host = _parsed_host(url)
    if host is None:
        return None
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme and not parsed.netloc:
        parsed = urllib.parse.urlparse(f"//{url}")

    path = parsed.path.rstrip("/") or "/"

    if host in _YOUTUBE_SHORT_HOSTS:
        # A youtu.be link is "youtu.be/VIDEO_ID" and nothing deeper.
        segments = [segment for segment in path.split("/") if segment]
        if len(segments) == 1:
            return None
        return "not a single video"

    if host not in _YOUTUBE_HOSTS:
        return None

    query = urllib.parse.parse_qs(parsed.query)

    if path in _WATCH_PATHS:
        # "&list=..." alongside "v=..." is fine - noplaylist does cover that
        # case, and it is the one URL shape where it works.
        if query.get("v", [""])[0]:
            return None
        return _describe_youtube_target(path, query)

    if path.startswith(_VIDEO_PATH_PREFIXES):
        # path was rstrip("/")-ed above, so a path matching one of the
        # prefixes always has at least one character after the prefix's own
        # trailing slash - which means split() always yields a third segment
        # and the index below cannot raise. That segment can still be empty,
        # for a doubled slash like "/shorts//abc"; an empty id is not
        # _EMBED_PLAYLIST_ID, so such a URL is passed to yt-dlp to report.
        video_id = path.split("/")[2]
        if video_id != _EMBED_PLAYLIST_ID:
            return None

    if path.startswith(_CHANNEL_PATH_PREFIXES):
        # "/@handle/live", "/channel/ID/live", "/c/Name/live", "/user/Name/live"
        # are one video each: the stream currently live on that channel. The
        # segment count keeps this to the channel's own live page, not to
        # a deeper tab that merely ends in the word.
        segments = [segment for segment in path.split("/") if segment]
        expected = 2 if path.startswith("/@") else 3
        if len(segments) == expected and segments[-1] == "live":
            return None

    return _describe_youtube_target(path, query)


def _broken_pipe_inside(exc: BaseException) -> BrokenPipeError | None:
    """The _OwnWritePipeError a yt-dlp DownloadError may be wrapping, or None.

    A progress hook runs inside YoutubeDL.dl(), and process_info() wraps any
    OSError raised there as UnavailableVideoError, which download()'s wrapper
    then reports as a DownloadError - so a closed pipe hit by this tool's own
    progress write does not reach the `except _OwnWritePipeError` clause in
    download() at all. It arrives as a DownloadError whose exc_info names the
    UnavailableVideoError, whose __context__ is the _OwnWritePipeError. Walk
    that chain so the caller can re-raise the original and let main() handle
    it as the pipe failure it is, rather than counting the URL as failed and
    then tripping over the still-pending stdout buffer at exit.

    Only this tool's own class counts. yt-dlp chains the exception a socket
    raised into the DownloadError it reports the same way - TransportError
    from the OSError, ExtractorError from that - and a site that closes its
    connection mid-request raises a plain BrokenPipeError on POSIX. Matching
    on the base type sent that to main()'s pipe handler, which pointed fd 1
    and fd 2 at devnull and exited 1: no "Failed to download" line, no
    failure count, nothing. A BrokenPipeError of any other origin is left in
    the chain, and the DownloadError is reported like every other one.
    """
    seen: set[int] = set()
    exc_info = getattr(exc, "exc_info", None)
    pending: list[BaseException | None] = [
        exc_info[1] if exc_info else None,
        exc.__cause__,
        exc.__context__,
    ]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, _OwnWritePipeError):
            return current
        pending.extend((current.__cause__, current.__context__))
    return None


def download(
    urls: list[str],
    output_dir: Path,
    quality: str = "best",
    audio_only: bool = False,
    subtitles: bool = False,
    client: str | None = None,
) -> int:
    """Download one or more videos. Returns the number of failures.

    A leading "~" in output_dir is expanded, as the CLI does for -o.

    Raises DownloaderError for a problem that stops the run before any
    download is attempted; main() prints it and exits non-zero.
    """
    # The CLI constrains -q to a fixed set of choices, so this only bites a
    # direct caller - who would otherwise get a bare ValueError out of the
    # int() below, several statements away from the argument that caused it.
    #
    # isdecimal() rather than isdigit(): isdigit() is also true for digit
    # characters that int() then refuses - the Unicode superscripts among them -
    # so on its own it would let exactly the ValueError this guard exists to
    # prevent through to the int() that computes cap_height below. Note that
    # the isascii() conjunct below now subsumes that: no ASCII character has
    # isdigit() != isdecimal(), so the two spellings are interchangeable while
    # isascii() stands. isdecimal() is kept as the honest description of what
    # is wanted, not because it is the half doing the work.
    #
    # isascii() on top of that, because int() is the weaker of the two
    # consumers. int() happily reads Arabic-Indic digits, but the value also
    # reaches yt-dlp as text, interpolated into the format selector below,
    # and yt-dlp's filter grammar rejects anything but ASCII digits:
    # "Invalid filter specification 'height<=...'".
    #
    # And int() itself, last: past sys.get_int_max_str_digits() - 4300 digits
    # unless configured otherwise - it refuses even an ASCII decimal string,
    # which was the same bare ValueError again.
    valid = quality == "best" or (quality.isascii() and quality.isdecimal())
    if valid and quality != "best":
        try:
            int(quality)
        except ValueError:
            valid = False
    if not valid:
        raise DownloaderError(
            f"Invalid quality {quality!r}: expected 'best' or a height in "
            f"pixels, e.g. '1080'."
        )

    # Checked for every URL before any of them is fetched, the same way the
    # quality above is: this is a usage error, and finding out about it after
    # three videos have already landed in the output directory helps nobody.
    rejected = [
        (url, problem) for url in urls if (problem := single_video_problem(url))
    ]
    if rejected:
        listed = "\n".join(f"  {url}\n    -> {problem}" for url, problem in rejected)
        raise DownloaderError(
            f"This tool downloads one video at a time, and "
            f"{'these URLs point' if len(rejected) > 1 else 'this URL points'} "
            f"somewhere else:\n"
            f"{listed}\n"
            f"Pass a video URL instead, e.g. "
            f"https://www.youtube.com/watch?v=VIDEO_ID"
        )

    have_ffmpeg = shutil.which("ffmpeg") is not None

    if audio_only and not have_ffmpeg:
        raise DownloaderError(
            "Audio extraction to MP3 requires ffmpeg, which was not found.\n"
            "Install it and make sure it is on your PATH:\n"
            "  Windows:       winget install ffmpeg  (then open a new terminal)\n"
            "  Ubuntu/Debian: sudo apt install ffmpeg\n"
            "  macOS:         brew install ffmpeg"
        )
    if not audio_only and not have_ffmpeg:
        _print(
            "Warning: ffmpeg not found - falling back to single-file formats.\n"
            "Quality may be limited (usually 720p max). Install ffmpeg for the\n"
            "best quality: winget install ffmpeg (Windows) / apt install ffmpeg\n"
            "(Linux) / brew install ffmpeg (macOS), then open a new terminal.\n",
            file=sys.stderr,
        )

    # yt-dlp also runs the template through expanduser(), so a leading "~"
    # left in place was created literally by mkdir() - "./~/Clips" under the
    # working directory - while the file went to $HOME/Clips. main() expands
    # -o before calling, but a direct caller's Path("~/Clips") arrived here
    # untouched. Expanding is idempotent, so doing it again costs nothing.
    output_dir = _expand_output_dir(output_dir)

    # The directory goes into the template as text, and yt-dlp runs the whole
    # template through %-substitution and expandvars(): a directory named
    # "100%(x)s" or "D:\%USERNAME%\vids" (PowerShell does not expand %...%)
    # was created here by mkdir() and then written *around* by yt-dlp, which
    # expanded the name and saved the file somewhere this function never
    # checked. "%" is escaped as "%%", which yt-dlp hands back literally.
    #
    # A "$NAME" in the directory name has no such escape: yt-dlp's own "$$"
    # only protects the first "$", so "$$NAME" still comes back expanded. It
    # is refused instead, but only when expansion would actually change the
    # path - a "$5" with no variable called 5 is left alone. "%" is masked
    # first so that the "%NAME%" case, already handled above, does not trip
    # the comparison on Windows. Masked with a space, not deleted: a space
    # ends a variable name the way the "%" does in yt-dlp's copy, whereas
    # deleting it joined "$CLIPS%x" into "$CLIPSx" - no such variable, so
    # the guard passed while yt-dlp expanded $CLIPS.
    directory = str(output_dir)
    masked = directory.replace("%", " ")
    if os.path.expandvars(masked) != masked:
        raise DownloaderError(
            f"Cannot use output directory '{output_dir}': it contains a "
            f"'$NAME' that yt-dlp would expand to "
            f"'{os.path.expandvars(directory)}' and save the file there "
            f"instead. Rename the directory or unset that variable."
        )

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DownloaderError(
            f"Cannot use output directory '{output_dir}': {exc.strerror or exc}"
        ) from exc
    except ValueError as exc:
        # A NUL byte in the name: mkdir() refuses it with ValueError, not
        # OSError, and a direct caller got that bare instead of the
        # DownloaderError this function promises. Quoted with repr() so the
        # message shows the NUL rather than printing it.
        raise DownloaderError(
            f"Cannot use output directory {directory!r}: {exc}"
        ) from exc

    # A numeric -q is a preferred maximum, not a hard one: the selector falls
    # back to an uncapped tier rather than failing. Warn when it does.
    cap_height = int(quality) if not audio_only and quality != "best" else None

    outtmpl = os.path.join(directory.replace("%", "%%"), "%(title)s [%(id)s].%(ext)s")

    # One logger for the whole run, so a warning repeated by every attempt and
    # every URL is printed once; it is pointed at each URL's reporter below.
    logger = _YtDlpLogger()

    opts = {
        "format": build_format_selector(quality, audio_only, have_ffmpeg),
        "outtmpl": outtmpl,
        "noplaylist": True,
        # Defence in depth behind single_video_problem(). "noplaylist" only
        # covers a "watch?v=...&list=..." URL, and the check above only
        # classifies YouTube URLs - so if anything still resolves to a
        # multi-entry playlist, cap it at one video rather than downloading
        # the whole thing. Ignored outright for a single video.
        "playlist_items": "1",
        # "progress_hooks" and "postprocessor_hooks" are built per URL inside
        # the loop below.
        "quiet": True,
        "no_warnings": True,
        # The second of two guards on yt-dlp's own "[download]  42.0% of ..."
        # line. With a logger set, yt-dlp already sends that line to the
        # logger's debug() - which _YtDlpLogger drops - rather than to
        # stdout. "noprogress" swaps in a printer that draws nothing at all,
        # so the line stays off the terminal even without the logger; "quiet"
        # does not do that for an API caller. Drawn, it would sit under
        # ProgressReporter.hook()'s shorter line and leave its tail showing.
        # The hook still receives _percent_str and friends - report_progress()
        # fills them in before it consults the (now silent) printer.
        "noprogress": True,
        # See _YtDlpLogger: yt-dlp's "ERROR:" lines otherwise reach stderr
        # directly, on top of the open progress line and duplicating the
        # message download() prints from the exception.
        "logger": logger,
        # Merge separate video/audio streams into mp4 (only possible with ffmpeg).
        "merge_output_format": "mp4" if (have_ffmpeg and not audio_only) else None,
    }

    if audio_only:
        opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]

    if subtitles:
        opts.update(
            {
                "writesubtitles": True,
                "writeautomaticsub": True,
                "subtitleslangs": ["en"],
            }
        )

    failures = 0
    for url in urls:
        _print(f"\nFetching: {url}")
        # If the user forced a client, use only that one; otherwise start with
        # yt-dlp's default client and fall back to alternates on availability
        # errors - but only where a player client means anything. The ladder
        # reaches yt-dlp as extractor_args["youtube"], which no other
        # extractor reads, so for any other host a retry could only repeat
        # the first attempt exactly, three times, behind a notice about
        # YouTube player clients.
        if client:
            client_attempts: list[str | None] = [client]
        elif _is_youtube_url(url):
            client_attempts = [None, *FALLBACK_CLIENTS]
        else:
            client_attempts = [None]
        # The cap warner is built per URL because it latches after it fires:
        # sharing one across the batch would report only the first video that
        # overran its cap and silence every later one. The reporter is built
        # here for a different and weaker reason - its open-line state
        # describes a single video - and sharing it would in fact change
        # nothing today, since close() runs unconditionally at the end of
        # every URL below and leaves the state closed either way.
        reporter = ProgressReporter()
        logger.reporter = reporter
        hooks: list[Callable[[dict[str, Any]], None]] = [reporter.hook]
        if cap_height is not None:
            # Registered after the reporter, so the progress line is already
            # closed by the time the warning prints.
            hooks.append(make_height_cap_warner(cap_height, url))
        url_opts = dict(
            opts,
            progress_hooks=hooks,
            postprocessor_hooks=[reporter.announce_hook],
        )

        last_error: Exception | None = None
        for attempt_client in client_attempts:
            attempt_opts = dict(url_opts)
            if attempt_client:
                attempt_opts["extractor_args"] = {
                    "youtube": {"player_client": [attempt_client]}
                }
            try:
                with yt_dlp.YoutubeDL(attempt_opts) as ydl:
                    # The return value is not consulted: with `ignoreerrors`
                    # unset, which it is here, YoutubeDL.trouble() raises
                    # DownloadError before it would ever set the non-zero
                    # code, so download() can only return 0 or raise.
                    ydl.download([url])
            except yt_dlp.utils.DownloadError as exc:
                # A closed pipe hit by the progress hook comes back wrapped in
                # a DownloadError (see _broken_pipe_inside); it is this tool's
                # own write failing, not the download, so hand it to main().
                broken_pipe = _broken_pipe_inside(exc)
                if broken_pipe is not None:
                    raise broken_pipe from exc
                last_error = exc
                # Only client-specific errors (unavailable, 403 on media)
                # are worth retrying on another client; anything else
                # (network down, bad URL) fails for all of them.
                if is_client_retryable_error(exc):
                    # The notice announces the fallbacks; say nothing where
                    # there are none, i.e. off YouTube or with -c forced.
                    if attempt_client is None and len(client_attempts) > 1:
                        reporter.close()
                        _print(
                            "Download rejected - retrying with alternate "
                            "player clients...",
                            file=sys.stderr,
                        )
                    continue
                break
            except (_OwnWritePipeError, _OwnWriteEncodeError):
                # Not download failures at all: both come from this tool's own
                # writes. A reader that went away (piping into head, quitting a
                # pager) and an output stream whose encoding cannot hold a
                # video title were recorded as `last_error` below, so a video
                # that had already been saved was reported as "Failed to
                # download ..." with a non-zero exit. Re-raise instead, so
                # main() handles each the way it says it does. This clause
                # sees them raised from the postprocessor hook; a pipe closed
                # during the download itself arrives as a DownloadError and
                # is unwrapped above. The tool's own classes, not the base
                # BrokenPipeError and UnicodeEncodeError: one of those raised
                # bare by anything else - yt-dlp opening a file whose name the
                # filesystem encoding cannot hold, say - is a failure of that
                # thing, and falls through to be recorded.
                raise
            except Exception as exc:
                # Anything else - a full disk, a permission error on the
                # output file, an unexpected yt-dlp failure - fails the same
                # way for every client. Record it and move on to the next URL
                # instead of aborting the whole batch with a traceback.
                last_error = exc
                break
            else:
                last_error = None
                break
        # yt-dlp does not report an "error" status on every failure path, so
        # a partial progress line can still be open here.
        reporter.close()
        if last_error is not None:
            _print(f"Failed to download {url}: {last_error}", file=sys.stderr)
            failures += 1
    return failures


def _output_dir(raw: str) -> Path:
    """--output as a Path, refusing a value that names nothing.

    argparse turns `-o ""` - an unset shell variable expanded into
    `-o "$OUTDIR"` is the usual way to produce one - into Path("."), which by
    the time _run() sees it is indistinguishable from a deliberate `-o .`.
    The download then lands in the current working directory and the
    "Downloaded:" line names it relatively, so nothing tells the user where
    it went. That is the same silent failure _absolute_or_none() exists to
    prevent for XDG values, so refuse it here rather than guess.

    Returns Path(raw.strip()) rather than Path(raw), so the string tested and
    the string used are the same one. `-o " ~/Videos"` - a stray space inside the
    quotes - otherwise passed the guard and produced Path(" ~/Videos"), which
    expanduser() leaves alone because it does not begin with "~", and which is
    relative: the download landed in a directory literally named " ~" under
    the working directory, which is the very outcome this guard exists to
    prevent.
    """
    if not raw.strip():
        raise argparse.ArgumentTypeError("expected a directory, got an empty value")
    return Path(raw.strip())


def _expand_output_dir(path: Path) -> Path:
    """Expand a leading "~" in -o, as a message rather than a traceback.

    Called by _run() for -o and again by download() for a direct caller's
    output_dir, since yt-dlp would otherwise expand the "~" itself.

    Path.expanduser() raises RuntimeError - not OSError - when it cannot
    resolve the "~": on POSIX "~someone" naming a user with no passwd entry is
    the easy way to get one, and a bare "~" does it wherever no home directory
    is set. That escaped _run() and main(), which handle only
    KeyboardInterrupt, BrokenPipeError, UnicodeEncodeError and
    DownloaderError, so a mistyped username reached the user as a traceback.
    """
    try:
        return path.expanduser()
    except RuntimeError as exc:
        raise DownloaderError(
            f"Cannot expand '~' in the output directory '{path}': no such "
            f"user, or no home directory is set. Pass a full path to -o "
            f"instead."
        ) from exc


def _require_terminal(prog: str) -> None:
    """Refuse to run unless stdout and stderr are both a real terminal.

    `prog` is the name the refusal gives the tool: the parser's, so that the
    installed `youtube-downloader` command is not told off as a file called
    youtube_downloader.py that the user never ran.

    Called from _run() once the arguments have parsed, so --help, --version
    and argument errors keep working when piped, and never at import time.
    A stream that is missing altogether (pythonw) counts as not a terminal.
    The message goes to stderr under try/except, because stderr itself may be
    the closed pipe. The BrokenPipeError handling further down stays as it
    is: it is the fallback for anything this check does not catch, such as
    a terminal that goes away mid-run.
    """

    def is_terminal(stream: Any) -> bool:
        try:
            return stream is not None and bool(stream.isatty())
        except (AttributeError, ValueError):
            return False

    if is_terminal(sys.stdout) and is_terminal(sys.stderr):
        return
    try:
        sys.stderr.write(
            f"{prog} must be run directly in a terminal "
            "(piping or redirecting output is not supported).\n"
        )
        sys.stderr.flush()
    except (OSError, AttributeError, ValueError):
        pass
    sys.exit(2)


class _VersionAction(argparse.Action):
    """--version, assembled only on the run that actually asks for it.

    argparse's built-in "version" action takes its string as an argument, so
    `version=version_string()` was evaluated while the parser was being
    built - meaning every single run, including `--help` and every argument
    error, paid for the importlib.metadata lookup inside version_string().
    Building the string here keeps that cost on the one code path that wants
    it, and makes the comment about deferred work in _run() true.
    """

    def __init__(self, option_strings: Sequence[str], dest: str, **kwargs: Any) -> None:
        super().__init__(option_strings, dest, nargs=0, **kwargs)

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | Sequence[Any] | None,
        option_string: str | None = None,
    ) -> None:
        _print(version_string())
        parser.exit()


def _run() -> None:
    parser = argparse.ArgumentParser(
        description="Download YouTube videos or audio.",
    )
    parser.add_argument(
        "-V",
        "--version",
        action=_VersionAction,
        default=argparse.SUPPRESS,
        help="Show this tool's version, plus the yt-dlp and Python versions",
    )
    parser.add_argument("urls", nargs="+", help="One or more YouTube video URLs")
    parser.add_argument(
        "-q",
        "--quality",
        default="best",
        choices=["best", "2160", "1440", "1080", "720", "480", "360"],
        help="Preferred maximum video resolution (default: best). If no format "
        "fits the cap, the best available is downloaded instead and a "
        "warning is printed. Ignored with --audio-only",
    )
    parser.add_argument(
        "-a",
        "--audio-only",
        action="store_true",
        help="Download audio only and convert to MP3 (requires ffmpeg)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=_output_dir,
        default=None,
        help="Output directory (default: the system Downloads folder)",
    )
    parser.add_argument(
        "-s",
        "--subtitles",
        action="store_true",
        help="Also download English subtitles when available",
    )
    parser.add_argument(
        "-c",
        "--client",
        default=None,
        choices=SELECTABLE_CLIENTS,
        help="Force a specific YouTube player client. By default, alternate "
        "clients are tried automatically if a video is reported unavailable",
    )

    args = parser.parse_args()

    # After parsing, so --help and --version work when piped; before any
    # lookup, download or progress output.
    _require_terminal(parser.prog)

    if args.audio_only and args.quality != "best":
        _print(
            "Warning: --quality is ignored with --audio-only; audio is always "
            "taken at the best available quality.",
            file=sys.stderr,
        )

    # Resolved after parsing so that --help and argument errors don't pay for
    # a registry read or an xdg-user-dir subprocess, and so the help text
    # doesn't print the user's home directory.
    output_dir = (
        _expand_output_dir(args.output)
        if args.output is not None
        else default_download_dir()
    )

    failures = download(
        urls=args.urls,
        output_dir=output_dir,
        quality=args.quality,
        audio_only=args.audio_only,
        subtitles=args.subtitles,
        client=args.client,
    )

    if failures:
        sys.exit(f"\n{failures} download(s) failed.")
    _print("\nAll downloads completed.")


def _make_output_encoding_tolerant() -> None:
    """Let a character the output stream cannot hold escape, not raise.

    Video titles routinely carry an emoji or CJK text, and on Windows stdout is
    the ANSI code page rather than the console's UTF-8 writer whenever output is
    redirected or piped. The "Downloaded: ..." line then raised
    UnicodeEncodeError from inside yt-dlp's postprocessor hook, and download()
    recorded that as the URL's failure - so a video that had been saved
    correctly was reported as a failed download. backslashreplace cannot raise,
    which keeps the line printable and the failure count honest.

    Anything that is not a reconfigurable text stream - a stdout replaced by
    another library, or none at all under pythonw - is left alone; the handler
    in main() is what covers those.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="backslashreplace")
        except (OSError, ValueError):
            pass


def _discard_pending_output() -> None:
    """Point fd 1 and fd 2 at devnull so the exit flush cannot raise.

    A write that failed leaves its data pending in the stream's buffer, and
    the interpreter flushes that buffer again at shutdown. Against a
    genuinely closed pipe that second attempt fails too, and it is reported
    as "Exception ignored while flushing sys.stdout: ..." with exit status
    120 - measured on Windows, and it is the case the Python docs' SIGPIPE
    note describes for POSIX. Exiting with status 1 needs the descriptor
    redirected first, which is the recipe from that note.

    Both streams, because either can be the closed pipe: `2>&1 | less` closes
    them together, and the write that fails is then whichever comes next - a
    progress line on stdout, or a "Failed to download" line on stderr. With
    only fd 1 redirected, a failed stderr write still exited 120.

    dup2 on the descriptor is process-global, so this only touches a stream
    that is still the real process one: under a test runner, or wherever
    something has replaced sys.stdout or sys.stderr, there is no closed pipe
    behind it and nothing to discard.
    """
    # Compared through the original, not the current, stream: mypy widens
    # `sys.stdout is sys.__stdout__` back to Optional, and a substituted
    # stream need not even have a fileno().
    for current, original in (
        (sys.stdout, sys.__stdout__),
        (sys.stderr, sys.__stderr__),
    ):
        if original is None or current is not original:
            continue
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(devnull, original.fileno())
            finally:
                os.close(devnull)
        except (OSError, ValueError):
            # No usable descriptor (pythonw, a closed stream): the flush this
            # guards against cannot happen either.
            pass


def main() -> None:
    """CLI entry point.

    The Ctrl-C and DownloaderError handling lives here rather than under
    `if __name__ == "__main__"`, because the `youtube-downloader` console
    script generated from [project.scripts] calls this function directly - a
    guard down there would leave the installed command, which is the one the
    README tells people to use, dumping a traceback on Ctrl-C.
    """
    _make_output_encoding_tolerant()
    try:
        _run()
    except KeyboardInterrupt:
        sys.exit("\nCancelled.")
    except BrokenPipeError:
        # The reader went away mid-run - piping into `head`, or quitting the
        # pager on a long multi-URL run. Without this the write that failed
        # dumps a traceback over whatever the user was actually reading.
        # (_print() is what makes this the one exception to name: Windows
        # raises OSError(EINVAL) for the same event and it is mapped there.)
        _discard_pending_output()
        sys.exit(1)
    except UnicodeEncodeError:
        # Reachable only where the streams could not be reconfigured above,
        # since backslashreplace cannot raise. Deliberately pure ASCII: it has
        # to go to the stream that has just refused a character.
        sys.exit(
            "Cannot write to this output stream: its encoding cannot represent "
            "the text (a video title, or the URL you passed). Set "
            "PYTHONIOENCODING=utf-8, or send output to a UTF-8 stream."
        )
    except DownloaderError as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()
