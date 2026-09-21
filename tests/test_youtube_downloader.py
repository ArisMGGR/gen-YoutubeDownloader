"""Tests for the youtube_downloader CLI.

Everything here runs offline: the yt-dlp entry point is replaced with a stub
so the retry ladder can be driven deterministically.
"""

from __future__ import annotations

import io
import sys
import types
from pathlib import Path

import pytest
import yt_dlp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import youtube_downloader as yd

DownloadError = yt_dlp.utils.DownloadError

# The retry ladder only walks for YouTube hosts (see _is_youtube_url), so the
# tests that exercise it must name one. Everything else can keep a bare "URL".
YT = "https://www.youtube.com/watch?v=abc"


# --------------------------------------------------------------------------
# build_format_selector
# --------------------------------------------------------------------------


def test_audio_only_ignores_quality_and_ffmpeg():
    assert yd.build_format_selector("1080", True, True) == "bestaudio/best"
    assert yd.build_format_selector("best", True, False) == "bestaudio/best"


def test_no_ffmpeg_selectors_are_single_file():
    # yt-dlp's bare "best" already means "contains both video and audio",
    # which is what the no-ffmpeg path needs.
    assert yd.build_format_selector("best", False, False) == "best"
    assert yd.build_format_selector("720", False, False) == "best[height<=720]/best"


def test_ffmpeg_selector_merges_streams():
    assert yd.build_format_selector("best", False, True) == "bestvideo+bestaudio/best"


def test_ffmpeg_capped_selector_has_an_uncapped_fallback():
    """A cap no format satisfies must degrade, not fail outright."""
    selector = yd.build_format_selector("360", False, True)
    assert selector.startswith("bestvideo[height<=360]+bestaudio")
    assert selector.endswith("/bestvideo+bestaudio/best")


@pytest.mark.parametrize(
    "quality", ["best", "2160", "1440", "1080", "720", "480", "360"]
)
@pytest.mark.parametrize("have_ffmpeg", [True, False])
def test_selectors_resolve_against_real_formats(quality, have_ffmpeg):
    """Every selector the CLI can emit must match a real-world format list."""
    formats = [
        {
            "format_id": "video-1080",
            "ext": "mp4",
            "height": 1080,
            "vcodec": "avc1",
            "acodec": "none",
            "url": "u",
            "protocol": "https",
        },
        {
            "format_id": "audio",
            "ext": "m4a",
            "vcodec": "none",
            "acodec": "mp4a",
            "url": "u",
            "protocol": "https",
        },
        {
            "format_id": "prog-720",
            "ext": "mp4",
            "height": 720,
            "vcodec": "avc1",
            "acodec": "mp4a",
            "url": "u",
            "protocol": "https",
        },
    ]
    selector = yd.build_format_selector(quality, False, have_ffmpeg)
    ydl = yt_dlp.YoutubeDL({"quiet": True, "simulate": True})
    matched = list(
        ydl.build_format_selector(selector)(
            {"formats": formats, "incomplete_formats": False}
        )
    )
    assert matched, f"selector {selector!r} matched nothing"


# --------------------------------------------------------------------------
# is_client_retryable_error
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "ERROR: [youtube] abc: Video unavailable",
        "ERROR: unable to download video data: HTTP Error 403: Forbidden",
        "ERROR: [youtube] abc: This content isn't available on this app",
    ],
)
def test_client_retryable_errors(message):
    assert yd.is_client_retryable_error(Exception(message))


@pytest.mark.parametrize(
    "message",
    [
        # No client switch fixes a format-selection failure, even though the
        # text contains "not available". Nothing special-cases this message:
        # it stays out of the ladder only because every retryable phrase is a
        # whole sentence, so this is the case that fails if one is loosened.
        "ERROR: [youtube] xyz: Requested format is not available",
        "ERROR: Unable to download webpage: <urlopen error name resolution>",
        "ERROR: [youtube] abc: Sign in to confirm your age",
        # "403" inside a video id must not look like an HTTP 403.
        "ERROR: [youtube] q403abc: Private video. Sign in if you have access",
        # Regression: yt-dlp puts the video title in its errors, so a bare
        # "forbidden" phrase sent unrelated failures round the whole ladder.
        "ERROR: [youtube] abc: 'Forbidden Planet (1956)' is private",
        "ERROR: [youtube] abc: Sign in to confirm you are not a bot. Forbidden",
    ],
)
def test_non_retryable_errors(message):
    assert not yd.is_client_retryable_error(Exception(message))


# --------------------------------------------------------------------------
# download() retry ladder
# --------------------------------------------------------------------------


def _install_fake_ydl(monkeypatch, behaviour, calls):
    """Replace yt_dlp with a stub whose download() defers to `behaviour`."""

    class FakeYoutubeDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def download(self, urls):
            extractor_args = self.opts.get("extractor_args")
            client = (
                extractor_args["youtube"]["player_client"][0]
                if extractor_args
                else None
            )
            calls.append(client)
            # The return value is ignored by download(): with ignoreerrors
            # unset, the real yt-dlp raises rather than returning non-zero.
            return behaviour(client)

    monkeypatch.setattr(
        yd,
        "yt_dlp",
        types.SimpleNamespace(YoutubeDL=FakeYoutubeDL, utils=yt_dlp.utils),
    )


def test_retries_every_fallback_client_then_reports_failure(tmp_path, monkeypatch):
    calls = []

    def always_403(client):
        raise DownloadError("HTTP Error 403: Forbidden")

    _install_fake_ydl(monkeypatch, always_403, calls)
    assert yd.download([YT], tmp_path) == 1
    assert calls == [None, *yd.FALLBACK_CLIENTS]


def test_stops_after_first_attempt_on_non_retryable_error(tmp_path, monkeypatch):
    calls = []

    def network_down(client):
        raise DownloadError("Unable to download webpage: name resolution failed")

    _install_fake_ydl(monkeypatch, network_down, calls)
    assert yd.download(["URL"], tmp_path) == 1
    assert calls == [None]


def test_success_on_a_later_client_clears_the_error(tmp_path, monkeypatch):
    calls = []

    def works_on_android(client):
        if client != "android":
            raise DownloadError("Video unavailable")

    _install_fake_ydl(monkeypatch, works_on_android, calls)
    assert yd.download([YT], tmp_path) == 0
    assert calls == [None, "android"]


def test_forced_client_is_not_retried(tmp_path, monkeypatch):
    calls = []

    def always_403(client):
        raise DownloadError("HTTP Error 403: Forbidden")

    _install_fake_ydl(monkeypatch, always_403, calls)
    assert yd.download(["URL"], tmp_path, client="tv") == 1
    assert calls == ["tv"]


def test_a_retryable_error_on_another_host_is_not_retried(
    tmp_path, monkeypatch, capsys
):
    """Regression: the client ladder walked for every host.

    extractor_args["youtube"] means nothing to any other extractor, so a 403
    from Vimeo re-ran the identical request three more times behind a notice
    about YouTube player clients. One attempt, no notice, one failure.
    """
    calls = []

    def always_403(client):
        raise DownloadError("HTTP Error 403: Forbidden")

    _install_fake_ydl(monkeypatch, always_403, calls)
    assert yd.download(["https://vimeo.com/12345"], tmp_path) == 1
    assert calls == [None]
    assert "retrying with alternate player clients" not in capsys.readouterr().err


def test_a_forced_client_is_still_passed_for_another_host(tmp_path, monkeypatch):
    """-c is the user's explicit choice, so it is honoured wherever it goes."""
    calls = []
    _install_fake_ydl(monkeypatch, lambda client: 0, calls)
    assert yd.download(["https://vimeo.com/12345"], tmp_path, client="tv") == 0
    assert calls == ["tv"]


def test_unexpected_exception_does_not_abort_the_batch(tmp_path, monkeypatch):
    """A non-DownloadError must fail one URL, not the whole run."""
    calls = []

    def blows_up_on_first_url(client):
        if len(calls) == 1:
            raise OSError(28, "No space left on device")

    _install_fake_ydl(monkeypatch, blows_up_on_first_url, calls)
    assert yd.download(["URL1", "URL2"], tmp_path) == 1
    # URL1 failed once without retrying; URL2 still ran.
    assert calls == [None, None]


def test_unwritable_output_dir_reports_cleanly(tmp_path, monkeypatch):
    calls = []
    _install_fake_ydl(monkeypatch, lambda client: None, calls)

    existing_file = tmp_path / "not-a-directory"
    existing_file.write_text("", encoding="utf-8")

    with pytest.raises(yd.DownloaderError) as excinfo:
        yd.download(["URL"], existing_file)
    assert "not-a-directory" in str(excinfo.value)
    assert calls == []


def test_invalid_quality_is_rejected_before_downloading(tmp_path, monkeypatch):
    """A direct caller gets a named error, not a bare ValueError from int()."""
    calls = []
    _install_fake_ydl(monkeypatch, lambda client: None, calls)

    with pytest.raises(yd.DownloaderError) as excinfo:
        yd.download(["URL"], tmp_path, quality="huge")
    assert "Invalid quality" in str(excinfo.value)
    assert calls == []


# --------------------------------------------------------------------------
# Downloads-directory resolution
# --------------------------------------------------------------------------


def test_xdg_env_var_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DOWNLOAD_DIR", str(tmp_path / "Elsewhere"))
    assert yd._xdg_downloads_dir() == tmp_path / "Elsewhere"


def test_xdg_user_dirs_is_parsed(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_DOWNLOAD_DIR", raising=False)
    monkeypatch.setattr(yd.shutil, "which", lambda name: None)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "user-dirs.dirs").write_text(
        'XDG_DESKTOP_DIR="$HOME/Desktop"\nXDG_DOWNLOAD_DIR="$HOME/Downloads"\n',
        encoding="utf-8",
    )
    assert yd._xdg_downloads_dir() == tmp_path / "home" / "Downloads"


def _stub_xdg_user_dir(monkeypatch, tmp_path, *, stdout=None, exc=None):
    """Point _xdg_downloads_dir() at a fake xdg-user-dir binary.

    Every other test in this file stubs shutil.which to None, which skips the
    subprocess branch entirely - so this helper is what puts it under test.
    """
    monkeypatch.delenv("XDG_DOWNLOAD_DIR", raising=False)
    monkeypatch.setattr(yd.shutil, "which", lambda name: "/usr/bin/xdg-user-dir")
    # Nothing should reach the user-dirs.dirs fallback unless the command
    # declined to answer, so point it at a directory that does not exist.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "nonexistent"))

    def fake_run(cmd, **kwargs):
        assert cmd == ["/usr/bin/xdg-user-dir", "DOWNLOAD"]
        # The call must stay shell-free and bounded: no shell, a timeout, and
        # check=True. shell= is left at its default rather than passed, so
        # its absence from kwargs is what is asserted.
        assert "shell" not in kwargs
        assert kwargs["timeout"] == 5
        assert kwargs["check"] is True
        if exc is not None:
            raise exc
        return types.SimpleNamespace(stdout=stdout, returncode=0)

    monkeypatch.setattr(yd.subprocess, "run", fake_run)


def test_xdg_user_dir_command_is_used(monkeypatch, tmp_path):
    """The happy path: the command answers, and its answer wins."""
    _stub_xdg_user_dir(monkeypatch, tmp_path, stdout=str(tmp_path / "Downloads") + "\n")
    assert yd._xdg_downloads_dir() == tmp_path / "Downloads"


def test_xdg_user_dir_echoing_home_is_rejected(monkeypatch, tmp_path):
    """xdg-user-dir echoes $HOME when the entry is unset - that is not an answer.

    Taking it at face value would drop downloads directly into the home
    directory instead of falling through to the user-dirs.dirs lookup.
    """
    home = tmp_path / "home"
    monkeypatch.setattr(yd.Path, "home", staticmethod(lambda: home))
    _stub_xdg_user_dir(monkeypatch, tmp_path, stdout=str(home) + "\n")
    assert yd._xdg_downloads_dir() is None


def test_xdg_user_dir_relative_answer_is_rejected(monkeypatch, tmp_path):
    """A relative reply would resolve against the CWD, so it must be ignored."""
    _stub_xdg_user_dir(monkeypatch, tmp_path, stdout="relative/Downloads\n")
    assert yd._xdg_downloads_dir() is None


@pytest.mark.parametrize(
    "exc",
    [
        OSError("not executable"),
        yd.subprocess.TimeoutExpired(cmd="xdg-user-dir", timeout=5),
        yd.subprocess.CalledProcessError(returncode=1, cmd="xdg-user-dir"),
    ],
)
def test_xdg_user_dir_failure_falls_through_to_user_dirs(monkeypatch, tmp_path, exc):
    """A command that fails must not abort the lookup - it has a fallback."""
    _stub_xdg_user_dir(monkeypatch, tmp_path, exc=exc)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "user-dirs.dirs").write_text(
        'XDG_DOWNLOAD_DIR="$HOME/FromFile"\n', encoding="utf-8"
    )
    assert yd._xdg_downloads_dir() == tmp_path / "home" / "FromFile"


def test_xdg_key_match_is_exact(monkeypatch, tmp_path):
    """A longer key sharing the prefix must not shadow the real entry.

    Two things this test has to get right, or it cannot fail:

    The decoy's spelling has to follow the platform, the way
    test_registry_environment_variables_are_expanded already does. A decoy
    written "/wrong/path" is not absolute under Windows path semantics, so
    _absolute_or_none() discarded it whatever the key check did, and the test
    passed for a reason unrelated to the exact-key behaviour it is named for.

    The decoy also has to come *last*. The parser takes the last resolvable
    entry, so a decoy placed first is overwritten by the real key even when
    the prefix wrongly matches, and the mistake is invisible again.
    """
    decoy = "C:/wrong/path" if sys.platform == "win32" else "/wrong/path"
    monkeypatch.delenv("XDG_DOWNLOAD_DIR", raising=False)
    monkeypatch.setattr(yd.shutil, "which", lambda name: None)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "user-dirs.dirs").write_text(
        f'XDG_DOWNLOAD_DIR="$HOME/Right"\nXDG_DOWNLOAD_DIR_EXTRA="{decoy}"\n',
        encoding="utf-8",
    )
    assert yd._xdg_downloads_dir() == tmp_path / "home" / "Right"


@pytest.mark.parametrize(
    "entry",
    [
        'XDG_DOWNLOAD_DIR = "$HOME/Spaced"',
        '\tXDG_DOWNLOAD_DIR\t=\t"$HOME/Spaced"',
    ],
)
def test_whitespace_around_the_equals_sign_is_allowed(monkeypatch, tmp_path, entry):
    """Regression: a hand-edited "KEY = value" line was silently skipped.

    GLib's reader of this file - behind every GTK file chooser and behind
    xdg-user-dir(1) - skips spaces and tabs on both sides of the "=", so the
    desktop honoured the line while this tool fell back to ~/Downloads with
    nothing printed to explain the difference.
    """
    monkeypatch.delenv("XDG_DOWNLOAD_DIR", raising=False)
    monkeypatch.setattr(yd.shutil, "which", lambda name: None)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "user-dirs.dirs").write_text(entry + "\n", encoding="utf-8")
    assert yd._xdg_downloads_dir() == tmp_path / "home" / "Spaced"


def test_missing_user_dirs_file_returns_none(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_DOWNLOAD_DIR", raising=False)
    monkeypatch.setattr(yd.shutil, "which", lambda name: None)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "nonexistent"))
    assert yd._xdg_downloads_dir() is None


def test_an_undecodable_user_dirs_file_is_ignored_rather_than_raising(
    monkeypatch, tmp_path
):
    """Regression: UnicodeDecodeError walked straight past `except OSError`.

    A home directory named in a legacy locale encoding puts a non-UTF-8 byte in
    user-dirs.dirs. UnicodeDecodeError is a ValueError, not an OSError, so it
    escaped _xdg_downloads_dir(), _run() and main() - none of whose handlers
    names it - and reached the user as a traceback before any download was
    attempted. An undecodable file
    says nothing, so it has to fall back like an unreadable one.
    """
    monkeypatch.delenv("XDG_DOWNLOAD_DIR", raising=False)
    monkeypatch.setattr(yd.shutil, "which", lambda name: None)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / "user-dirs.dirs").write_bytes(
        b'XDG_DOWNLOAD_DIR="$HOME/T\xe9l\xe9chargements"\n'
    )
    assert yd._xdg_downloads_dir() is None


def test_an_undecodable_xdg_user_dir_reply_is_ignored_rather_than_raising(
    monkeypatch, tmp_path
):
    """The same gap on the sibling path, which decodes with the locale codec.

    subprocess.run(text=True) decodes the command's stdout, so an undecodable
    byte raises there too - and neither OSError nor SubprocessError covers it.
    """
    _stub_xdg_user_dir(
        monkeypatch,
        tmp_path,
        exc=UnicodeDecodeError("utf-8", b"\xe9", 0, 1, "invalid start byte"),
    )
    assert yd._xdg_downloads_dir() is None


def test_last_user_dirs_entry_wins(monkeypatch, tmp_path):
    """Regression: the file is sourced, so a later line overrides an earlier.

    Returning on the first resolvable entry sent downloads to the superseded
    directory while every other XDG consumer used the current one.
    """
    monkeypatch.delenv("XDG_DOWNLOAD_DIR", raising=False)
    monkeypatch.setattr(yd.shutil, "which", lambda name: None)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "user-dirs.dirs").write_text(
        'XDG_DOWNLOAD_DIR="$HOME/Old"\nXDG_DOWNLOAD_DIR="$HOME/New"\n',
        encoding="utf-8",
    )
    assert yd._xdg_downloads_dir() == tmp_path / "home" / "New"


@pytest.mark.parametrize(
    "entry",
    [
        # Quoted, the shape xdg-user-dirs-update writes: the value ends at the
        # closing quote, so the comment is never part of it.
        'XDG_DOWNLOAD_DIR="$HOME/Downloads" # where my videos go',
        # Unquoted, which a hand-edited file can carry: nothing marks the end
        # of the value but the comment itself.
        "XDG_DOWNLOAD_DIR=$HOME/Downloads # where my videos go",
    ],
)
def test_a_trailing_comment_is_not_part_of_the_path(monkeypatch, tmp_path, entry):
    """Regression: stripping quotes alone left "# note" attached to the path.

    On POSIX the result is still absolute, so it was accepted and mkdir()
    silently created and downloaded into a directory named after the comment.
    Both quoting styles are covered because they take different branches.
    """
    monkeypatch.delenv("XDG_DOWNLOAD_DIR", raising=False)
    monkeypatch.setattr(yd.shutil, "which", lambda name: None)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "user-dirs.dirs").write_text(entry + "\n", encoding="utf-8")
    assert yd._xdg_downloads_dir() == tmp_path / "home" / "Downloads"


def test_a_directory_name_containing_a_hash_survives(monkeypatch, tmp_path):
    """Cutting at "#" unconditionally would corrupt a legitimate name."""
    monkeypatch.delenv("XDG_DOWNLOAD_DIR", raising=False)
    monkeypatch.setattr(yd.shutil, "which", lambda name: None)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "user-dirs.dirs").write_text(
        'XDG_DOWNLOAD_DIR="$HOME/Vids #1"\n', encoding="utf-8"
    )
    assert yd._xdg_downloads_dir() == tmp_path / "home" / "Vids #1"


def test_an_unquoted_hash_inside_a_name_is_kept(monkeypatch, tmp_path):
    """Regression: a "#" opens a comment only at the start of a word.

    Cutting the value at every "#" took "$HOME/Vids#1" down to "$HOME/Vids" - a
    different directory, which mkdir() then created and downloaded into, while
    the shell that sources this file reads the whole name. The quoted form of
    the same name was already covered; this is the unquoted half.
    """
    monkeypatch.delenv("XDG_DOWNLOAD_DIR", raising=False)
    monkeypatch.setattr(yd.shutil, "which", lambda name: None)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "user-dirs.dirs").write_text(
        "XDG_DOWNLOAD_DIR=$HOME/Vids#1\n", encoding="utf-8"
    )
    assert yd._xdg_downloads_dir() == tmp_path / "home" / "Vids#1"


def test_text_after_a_closing_quote_joins_the_value(monkeypatch, tmp_path):
    """The shell concatenates it: "$HOME/Down"loads is $HOME/Downloads.

    Stopping at the closing quote silently resolved the shorter path instead.
    """
    monkeypatch.delenv("XDG_DOWNLOAD_DIR", raising=False)
    monkeypatch.setattr(yd.shutil, "which", lambda name: None)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "user-dirs.dirs").write_text(
        'XDG_DOWNLOAD_DIR="$HOME/Down"loads\n', encoding="utf-8"
    )
    assert yd._xdg_downloads_dir() == tmp_path / "home" / "Downloads"


def test_a_word_after_a_quoted_value_is_not_part_of_it(monkeypatch, tmp_path):
    """Whitespace ends the assignment, so what follows is not the path.

    The other half of the concatenation rule above: joining unconditionally
    would append it.
    """
    monkeypatch.delenv("XDG_DOWNLOAD_DIR", raising=False)
    monkeypatch.setattr(yd.shutil, "which", lambda name: None)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "user-dirs.dirs").write_text(
        'XDG_DOWNLOAD_DIR="$HOME/Downloads" trailing\n', encoding="utf-8"
    )
    assert yd._xdg_downloads_dir() == tmp_path / "home" / "Downloads"


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        # Single quotes: the shell expands nothing inside them, so "$HOME" is
        # four literal characters and the path is relative - unset, exactly
        # what the desktop's reader (which rejects single quotes) concludes.
        ("XDG_DOWNLOAD_DIR='$HOME/Videos'", None),
        # A single-quoted literal that is already absolute is a real answer.
        ("XDG_DOWNLOAD_DIR='{abs_lit}'", "abs_lit"),
        # Inside double quotes a backslash escapes a quote, and the closing
        # quote is the first *unescaped* one - not the first one.
        ('XDG_DOWNLOAD_DIR="$HOME/My \\"Vids\\""', 'My "Vids"'),
        # ...and a backslash escapes a backslash.
        ('XDG_DOWNLOAD_DIR="$HOME/a\\\\b"', "a\\b"),
    ],
)
def test_shell_quoting_rules_are_followed(monkeypatch, tmp_path, entry, expected):
    """Regression: single quotes expanded "$HOME", and an escaped quote ended it.

    The docstring on _config_value() promises shell rules for the shapes
    GLib rejects, and single quotes are one of those shapes. Treating them
    like double quotes sent this tool to ~/Videos for a line the desktop
    ignores; a backslash-escaped quote in a name cut the value short and
    mangled the rest.
    """
    home = tmp_path / "home"
    abs_lit = str(tmp_path / "Literal")
    monkeypatch.delenv("XDG_DOWNLOAD_DIR", raising=False)
    monkeypatch.setattr(yd.shutil, "which", lambda name: None)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(home))
    (tmp_path / "user-dirs.dirs").write_text(
        entry.format(abs_lit=abs_lit) + "\n", encoding="utf-8"
    )
    if expected is None:
        assert yd._xdg_downloads_dir() is None
    elif expected == "abs_lit":
        assert yd._xdg_downloads_dir() == Path(abs_lit)
    else:
        assert yd._xdg_downloads_dir() == home / expected


def test_a_byte_order_mark_does_not_hide_the_first_entry(monkeypatch, tmp_path):
    """Regression: a BOM survived utf-8 decoding and defeated startswith().

    The entry was then silently ignored in favour of ~/Downloads - but only
    when it was the file's first line, which made the failure look arbitrary.
    """
    monkeypatch.delenv("XDG_DOWNLOAD_DIR", raising=False)
    monkeypatch.setattr(yd.shutil, "which", lambda name: None)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "user-dirs.dirs").write_bytes(
        b'\xef\xbb\xbfXDG_DOWNLOAD_DIR="$HOME/Downloads"\n'
    )
    assert yd._xdg_downloads_dir() == tmp_path / "home" / "Downloads"


def test_default_download_dir_falls_back_to_home(monkeypatch, tmp_path):
    monkeypatch.setattr(yd.sys, "platform", "linux")
    monkeypatch.setattr(yd, "_xdg_downloads_dir", lambda: None)
    monkeypatch.setattr(yd.Path, "home", staticmethod(lambda: tmp_path))
    assert yd.default_download_dir() == tmp_path / "Downloads"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="on Windows this resolves to the real Downloads folder",
)
def test_known_folder_path_is_none_off_windows():
    assert yd._known_folder_path(yd._DOWNLOADS_FOLDER_GUID) is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only code path")
def test_known_folder_path_honours_its_contract_on_windows():
    """The ctypes path only ever runs on Windows, so only Windows can check it.

    "None or absolute" alone was satisfied by None, so this passed whether the
    lookup worked or failed silently - and off Windows the sibling test asserts
    the result *is* None, so between them nothing could fail if
    _known_folder_path() stopped resolving. That is the one code path that
    follows a relocated Downloads folder ahead of the possibly-stale registry,
    which is behaviour the README promises. SHGetKnownFolderPath is documented
    to succeed for an interactive user, so require an answer as well as the
    shape of one.
    """
    resolved = yd._known_folder_path(yd._DOWNLOADS_FOLDER_GUID)
    assert resolved is not None, "SHGetKnownFolderPath returned no Downloads path"
    assert resolved.is_absolute()


def _fake_winreg(monkeypatch, values, *, failing_keys=()):
    """Point _windows_downloads_dir()'s registry branch at a fake hive.

    The known-folder API answers first on any real Windows host, so without
    stubbing it to None this branch is unreachable and nothing here can fail.

    `values` maps a key path to the Downloads value it holds; `failing_keys`
    names key paths whose open raises OSError, the way a missing key does.
    """
    monkeypatch.setattr(yd, "_known_folder_path", lambda guid: None)

    class FakeKey:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    def open_key(root, path):
        if path in failing_keys or path not in values:
            raise OSError(2, "The system cannot find the file specified")
        return FakeKey(path)

    def query_value(key, name):
        assert name == yd._DOWNLOADS_FOLDER_GUID
        return values[key.path], 1

    monkeypatch.setitem(
        sys.modules,
        "winreg",
        types.SimpleNamespace(
            HKEY_CURRENT_USER=0, OpenKey=open_key, QueryValueEx=query_value
        ),
    )


_USER_SHELL_FOLDERS = (
    r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
)
_SHELL_FOLDERS = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders"


def test_registry_downloads_value_is_used(monkeypatch, tmp_path):
    """The authoritative key answers, and its answer wins."""
    _fake_winreg(monkeypatch, {_USER_SHELL_FOLDERS: str(tmp_path / "Elsewhere")})
    assert yd._windows_downloads_dir() == tmp_path / "Elsewhere"


def test_registry_environment_variables_are_expanded(monkeypatch, tmp_path):
    """ "User Shell Folders" stores the value with %VARS% left unexpanded.

    The spelling has to follow the platform. os.path.expandvars() is
    ntpath.expandvars() on Windows, which reads %VAR%, and posixpath's
    everywhere else, which reads only $VAR and leaves a %VAR% literal in
    place - a relative path that _absolute_or_none() then rejects. Hard-coding
    the Windows spelling therefore failed this test on Linux and macOS, with a
    message naming neither the platform nor the cause.
    """
    monkeypatch.setenv("TESTPROFILE", str(tmp_path))
    raw = (
        r"%TESTPROFILE%\Downloads"
        if sys.platform == "win32"
        else "$TESTPROFILE/Downloads"
    )
    _fake_winreg(monkeypatch, {_USER_SHELL_FOLDERS: raw})
    assert yd._windows_downloads_dir() == tmp_path / "Downloads"


@pytest.mark.parametrize(
    "value",
    [
        # An entry naming a variable that no longer exists: expandvars leaves
        # it in place, and what is left is a relative path.
        r"%NO_SUCH_VARIABLE%\Downloads",
        r"relative\Downloads",
        "",
        "   ",
    ],
)
def test_relative_registry_value_is_rejected(monkeypatch, value):
    """Regression: an unusable registry value must not become the output dir.

    Left unchecked it resolves against the current working directory, so the
    tool writes downloads wherever it happened to be run - the same silent
    failure _absolute_or_none() exists to prevent on the XDG side. The XDG
    lookup guarded against it and this one did not.
    """
    _fake_winreg(monkeypatch, {_USER_SHELL_FOLDERS: value})
    assert yd._windows_downloads_dir() is None


def test_registry_falls_through_to_the_compatibility_key(monkeypatch, tmp_path):
    """ "User Shell Folders" may not carry the entry; "Shell Folders" is next."""
    _fake_winreg(
        monkeypatch,
        {_SHELL_FOLDERS: str(tmp_path / "FromCache")},
        failing_keys=(_USER_SHELL_FOLDERS,),
    )
    assert yd._windows_downloads_dir() == tmp_path / "FromCache"


def test_registry_with_no_usable_key_returns_none(monkeypatch):
    _fake_winreg(monkeypatch, {})
    assert yd._windows_downloads_dir() is None


def test_windows_downloads_dir_without_winreg_returns_none(monkeypatch):
    """Off Windows there is no winreg to import, so the lookup gives up."""
    monkeypatch.setattr(yd, "_known_folder_path", lambda guid: None)
    monkeypatch.setitem(sys.modules, "winreg", None)  # makes `import winreg` fail
    assert yd._windows_downloads_dir() is None


def test_default_download_dir_is_always_absolute():
    """Whichever branch wins, the resolved directory must be usable as-is.

    This is the one assertion that exercises the real platform-specific
    lookup on every OS in the CI matrix.
    """
    resolved = yd.default_download_dir()
    assert resolved.is_absolute()
    assert str(resolved) not in ("", ".")


# --------------------------------------------------------------------------
# ProgressReporter
# --------------------------------------------------------------------------


def test_progress_hook_tolerates_a_missing_status():
    yd.ProgressReporter().hook({})  # must not raise


def test_progress_hook_renders_progress_and_closes_on_finish(capsys):
    reporter = yd.ProgressReporter()
    reporter.hook({"status": "downloading", "_percent_str": " 42.0%"})
    reporter.hook({"status": "finished", "filename": "video.f399.mp4"})
    captured = capsys.readouterr()
    assert "42.0%" in captured.out
    assert captured.out.endswith("\n")
    # The progress line lives on stdout, so that is where its terminator has
    # to go; a newline on stderr only looks right on a shared terminal.
    assert captured.err == ""


def test_progress_hook_never_names_a_file(capsys):
    """Regression: this hook fires per stream and knows only temp names.

    Announcing from here printed "Title [id].f399.mp4" and
    "Title [id].f251.webm" - two files ffmpeg deletes when it merges them -
    and never printed the .mp4 the user was left with.
    """
    reporter = yd.ProgressReporter()
    reporter.hook({"status": "finished", "filename": "Title [id].f399.mp4"})
    reporter.hook({"status": "finished", "filename": "Title [id].f251.webm"})
    assert "Downloaded" not in capsys.readouterr().out


def test_progress_line_is_closed_before_later_output(capsys):
    """A redrawn progress line must not be written over by what follows."""
    reporter = yd.ProgressReporter()
    reporter.hook({"status": "downloading", "_percent_str": " 42.0%"})
    reporter.close()
    captured = capsys.readouterr()
    assert captured.out.endswith("\n")


def test_closing_without_an_open_line_prints_nothing(capsys):
    """close() is idempotent, so callers never have to track line state."""
    reporter = yd.ProgressReporter()
    reporter.close()
    reporter.hook({"status": "downloading", "_percent_str": " 42.0%"})
    reporter.close()
    reporter.close()
    assert capsys.readouterr().out.count("\n") == 1


def test_announce_hook_names_the_final_file(capsys):
    """The one line that names a file must name the one left on disk."""
    reporter = yd.ProgressReporter()
    reporter.hook({"status": "downloading", "_percent_str": " 42.0%"})
    reporter.announce_hook(
        {
            "status": "finished",
            "postprocessor": "MoveFiles",
            "info_dict": {"filepath": "/home/u/Downloads/Title [id].mp4"},
        }
    )
    captured = capsys.readouterr()
    assert "Downloaded: /home/u/Downloads/Title [id].mp4" in captured.out
    # The progress line is closed first, so the announcement gets its own line.
    # Asserted against the raw buffer: splitlines() treats a bare "\r" as a
    # line terminator and consumes it, so a per-line check for a leading "\r"
    # can never fail and pinned nothing at all.
    assert captured.out.split("Downloaded:")[0].endswith("\n")


@pytest.mark.parametrize(
    "event",
    [
        # Every postprocessor reports "started" as well; only MoveFiles has
        # run late enough for filepath to be the merged, converted result.
        {
            "status": "started",
            "postprocessor": "MoveFiles",
            "info_dict": {"filepath": "x"},
        },
        {
            "status": "finished",
            "postprocessor": "FFmpegMerger",
            "info_dict": {"filepath": "Title [id].temp.mp4"},
        },
        {
            "status": "finished",
            "postprocessor": "FFmpegExtractAudio",
            "info_dict": {"filepath": "Title [id].webm"},
        },
        # Defensive: nothing to name.
        {"status": "finished", "postprocessor": "MoveFiles", "info_dict": {}},
        {"status": "finished", "postprocessor": "MoveFiles"},
        {},
    ],
)
def test_announce_hook_ignores_everything_else(event, capsys):
    yd.ProgressReporter().announce_hook(event)
    assert capsys.readouterr().out == ""


def test_move_files_key_matches_yt_dlp(capsys):
    """announce_hook() gates on an undocumented yt-dlp internal - pin it here.

    "MoveFiles" is MoveFilesAfterDownloadPP.pp_key(), not a documented API. If
    upstream ever renames it, the hook stops matching and the one line naming
    the downloaded file silently disappears - with every other test in this
    file still green, because they all build the event payload by hand. This
    asserts against yt-dlp itself, so that rename fails the build instead.
    """
    from yt_dlp.postprocessor import MoveFilesAfterDownloadPP

    key = MoveFilesAfterDownloadPP.pp_key()
    reporter = yd.ProgressReporter()
    reporter.announce_hook(
        {"status": "finished", "postprocessor": key, "info_dict": {"filepath": "f.mp4"}}
    )
    assert "Downloaded: f.mp4" in capsys.readouterr().out


def test_error_status_closes_the_line(capsys):
    reporter = yd.ProgressReporter()
    reporter.hook({"status": "downloading", "_percent_str": " 42.0%"})
    reporter.hook({"status": "error"})
    captured = capsys.readouterr()
    assert captured.out.endswith("\n")
    assert captured.err == ""


# --------------------------------------------------------------------------
# main() argument handling
# --------------------------------------------------------------------------


def _pretend_terminal(monkeypatch, *streams):
    """Make the current stdout and stderr (or `streams`) report isatty() True.

    main() refuses to run unless both are a terminal, and pytest's capture
    objects are not one - so every test that drives main() past argument
    parsing has to claim a terminal first, on the very objects sys.stdout and
    sys.stderr hold at the time.
    """
    for stream in streams or (sys.stdout, sys.stderr):
        monkeypatch.setattr(stream, "isatty", lambda: True, raising=False)


def _run_main(monkeypatch, argv):
    """Run main() with download() stubbed out, returning its keyword args."""
    _pretend_terminal(monkeypatch)
    captured = {}

    def fake_download(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(yd, "download", fake_download)
    monkeypatch.setattr(sys, "argv", ["youtube_downloader.py", *argv])
    yd.main()
    return captured


def test_output_tilde_is_expanded(monkeypatch, tmp_path):
    """A quoted "~/..." must reach the home directory, not a literal "~" dir."""
    # Path.expanduser() goes through os.path.expanduser, which reads the
    # environment rather than Path.home(); USERPROFILE is what it consults on
    # Windows, HOME everywhere else.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    captured = _run_main(monkeypatch, ["URL", "-o", "~/Videos"])
    assert captured["output_dir"] == tmp_path / "Videos"
    assert "~" not in str(captured["output_dir"])


def test_explicit_output_dir_is_passed_through(monkeypatch, tmp_path):
    captured = _run_main(monkeypatch, ["URL", "-o", str(tmp_path / "Here")])
    assert captured["output_dir"] == tmp_path / "Here"


def test_default_output_dir_is_resolved_lazily(monkeypatch, tmp_path):
    """Without -o, main() falls back to the system Downloads folder."""
    monkeypatch.setattr(yd, "default_download_dir", lambda: tmp_path / "Downloads")
    captured = _run_main(monkeypatch, ["URL"])
    assert captured["output_dir"] == tmp_path / "Downloads"


def test_empty_output_dir_is_rejected(monkeypatch, capsys):
    """Regression: `-o ""` used to parse to Path(".") and land in the CWD.

    An unset shell variable expanded into `-o "$OUTDIR"` is the usual way to
    produce one. argparse turned it into Path("."), which expanduser() leaves
    alone and mkdir(exist_ok=True) accepts, so the download went to the
    working directory and the "Downloaded:" line named it relatively - nothing
    told the user where it had gone.
    """
    monkeypatch.setattr(sys, "argv", ["youtube-downloader", "URL", "-o", ""])
    with pytest.raises(SystemExit) as excinfo:
        yd.main()
    assert excinfo.value.code == 2
    assert "expected a directory, got an empty value" in capsys.readouterr().err


def test_whitespace_only_output_dir_is_rejected(monkeypatch, capsys):
    """A value that is all spaces names nothing either, so it is refused too."""
    monkeypatch.setattr(sys, "argv", ["youtube-downloader", "URL", "-o", "   "])
    with pytest.raises(SystemExit) as excinfo:
        yd.main()
    assert excinfo.value.code == 2
    assert "expected a directory" in capsys.readouterr().err


def test_explicit_dot_output_dir_is_still_accepted(monkeypatch):
    """The guard must refuse an empty value without refusing a deliberate `.`."""
    captured = _run_main(monkeypatch, ["URL", "-o", "."])
    assert captured["output_dir"] == Path(".")


def test_output_dir_value_is_stripped(monkeypatch, tmp_path):
    """Regression: the guard tested raw.strip() but returned Path(raw).

    `-o " ~/Videos"` - a stray space inside the quotes - passed the emptiness
    guard and produced Path(" ~/Videos"), which expanduser() leaves alone
    because it does not begin with "~", and which is relative. The download
    landed in a directory literally named " ~" under the working directory,
    the very outcome the guard exists to prevent.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    captured = _run_main(monkeypatch, ["URL", "-o", " ~/Videos "])
    assert captured["output_dir"] == tmp_path / "Videos"
    assert "~" not in str(captured["output_dir"])


def test_an_unresolvable_tilde_in_output_is_a_message_not_a_traceback(monkeypatch):
    """Regression: expanduser()'s RuntimeError escaped _run() and main().

    Path.expanduser() raises RuntimeError - not OSError - when it cannot
    resolve the "~". On POSIX "-o ~someone" naming a user with no passwd entry
    is the easy way to get one. None of main()'s handlers names RuntimeError,
    so it reached the user as a traceback before any download was attempted.
    """

    def refuses(self):
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(yd.Path, "expanduser", refuses)
    monkeypatch.setattr(sys, "argv", ["youtube-downloader", "URL", "-o", "~nobody/v"])
    _pretend_terminal(monkeypatch)
    with pytest.raises(SystemExit) as excinfo:
        yd.main()
    assert "Cannot expand '~'" in str(excinfo.value)
    assert excinfo.value.code != 0


def test_no_home_directory_is_a_message_not_a_traceback(monkeypatch):
    """The same gap on the default path, which calls Path.home().

    A systemd unit, a cron job with a stripped environment or a slim container
    can leave no home variable set at all, and then even a plain run with no
    flags tracebacked out of default_download_dir().
    """

    def no_home():
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(yd.sys, "platform", "linux")
    monkeypatch.setattr(yd, "_xdg_downloads_dir", lambda: None)
    monkeypatch.setattr(yd.Path, "home", staticmethod(no_home))
    monkeypatch.setattr(sys, "argv", ["youtube-downloader", "URL"])
    _pretend_terminal(monkeypatch)
    with pytest.raises(SystemExit) as excinfo:
        yd.main()
    assert "Cannot determine your home directory" in str(excinfo.value)
    assert excinfo.value.code != 0


def test_an_unresolvable_home_makes_a_configured_value_unset(monkeypatch):
    """_absolute_or_none() must fall back rather than raise, like its callers.

    It is reached with values out of the registry and out of user-dirs.dirs,
    both of which can carry a "~", and both of which are only ever advisory.
    """

    def no_home(self):
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(yd.Path, "expanduser", no_home)
    assert yd._absolute_or_none("~/Downloads") is None


def test_quality_with_audio_only_warns(monkeypatch, capsys):
    _run_main(monkeypatch, ["URL", "-a", "-q", "720"])
    assert "--quality is ignored with --audio-only" in capsys.readouterr().err


def test_quality_without_audio_only_does_not_warn(monkeypatch, capsys):
    _run_main(monkeypatch, ["URL", "-q", "720"])
    assert "--quality is ignored" not in capsys.readouterr().err


def test_failures_exit_nonzero(monkeypatch):
    monkeypatch.setattr(yd, "download", lambda **kwargs: 2)
    monkeypatch.setattr(sys, "argv", ["youtube_downloader.py", "URL1", "URL2"])
    _pretend_terminal(monkeypatch)
    with pytest.raises(SystemExit) as excinfo:
        yd.main()
    assert "2 download(s) failed" in str(excinfo.value)


# --------------------------------------------------------------------------
# -c/--client selection
# --------------------------------------------------------------------------


def test_web_client_is_selectable(monkeypatch):
    """The web client is selectable though it is not in the fallback ladder."""
    assert "web" not in yd.FALLBACK_CLIENTS
    assert "web" in yd.SELECTABLE_CLIENTS
    assert _run_main(monkeypatch, ["URL", "-c", "web"])["client"] == "web"


def test_every_fallback_client_is_selectable(monkeypatch):
    for client in yd.FALLBACK_CLIENTS:
        assert _run_main(monkeypatch, ["URL", "-c", client])["client"] == client


def test_unknown_client_is_rejected(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["youtube_downloader.py", "URL", "-c", "ios"])
    with pytest.raises(SystemExit):
        yd.main()


# --------------------------------------------------------------------------
# A download that raises nothing is a success
# --------------------------------------------------------------------------


def test_a_download_that_raises_nothing_is_a_success(tmp_path, monkeypatch):
    """yt-dlp's return code is not consulted: it raises, or it succeeded.

    YoutubeDL.trouble() sets its non-zero return code only on the path where
    `ignoreerrors` is set, after the raise it otherwise takes - and this
    tool never sets it. A branch that treated a non-zero return as a failure
    could therefore never fire and was removed.
    """
    calls = []
    _install_fake_ydl(monkeypatch, lambda client: 1, calls)
    assert yd.download(["URL"], tmp_path) == 0
    assert calls == [None]


# --------------------------------------------------------------------------
# -q cap warning
# --------------------------------------------------------------------------


def test_cap_warner_fires_once_when_the_cap_is_exceeded(capsys):
    hook = yd.make_height_cap_warner(360, "URL")
    hook({"status": "finished", "info_dict": {"height": 1080}})
    hook({"status": "finished", "info_dict": {"height": 1080}})
    err = capsys.readouterr().err
    assert "no format at or below 360p" in err
    assert "downloaded 1080p" in err
    assert err.count("Warning") == 1, "must warn once, not once per stream"


def test_cap_warning_names_the_video_it_applies_to(capsys):
    """In a batch, an unattributed warning does not say which video overran."""
    hook = yd.make_height_cap_warner(360, "https://example.invalid/watch?v=abc")
    hook({"status": "finished", "info_dict": {"height": 1080}})
    assert "https://example.invalid/watch?v=abc" in capsys.readouterr().err


def test_cap_warner_is_silent_within_the_cap(capsys):
    hook = yd.make_height_cap_warner(1080, "URL")
    hook({"status": "finished", "info_dict": {"height": 720}})
    hook({"status": "finished", "info_dict": {"height": 1080}})
    # An audio stream carries no height and must not trip the warning.
    hook({"status": "finished", "info_dict": {"height": None}})
    hook({"status": "finished"})
    hook({"status": "downloading", "info_dict": {"height": 2160}})
    assert capsys.readouterr().err == ""


def test_cap_warner_is_registered_only_for_a_numeric_quality(tmp_path, monkeypatch):
    # audio_only below needs ffmpeg to be present, or download() exits early.
    monkeypatch.setattr(yd.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    seen = {}
    calls = []
    _install_fake_ydl(monkeypatch, lambda client: 0, calls)
    real = yd.yt_dlp.YoutubeDL

    def spy(opts):
        seen["hooks"] = opts["progress_hooks"]
        return real(opts)

    monkeypatch.setattr(yd.yt_dlp, "YoutubeDL", spy)

    yd.download(["URL"], tmp_path, quality="720")
    assert len(seen["hooks"]) == 2

    yd.download(["URL"], tmp_path, quality="best")
    assert len(seen["hooks"]) == 1

    # Height means nothing in audio-only mode.
    yd.download(["URL"], tmp_path, quality="720", audio_only=True)
    assert len(seen["hooks"]) == 1


def test_cap_warning_fires_for_every_url_in_a_batch(tmp_path, monkeypatch, capsys):
    """Regression: one shared warner latched and silenced every later video.

    The warner stops after it fires, so building it once per run meant only
    the first over-cap download in a batch was reported - every video after
    it came back larger than asked for with no warning at all.
    """
    monkeypatch.setattr(yd.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    calls = []

    class OverCapYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def download(self, urls):
            calls.append(urls[0])
            for hook in self.opts["progress_hooks"]:
                hook(
                    {
                        "status": "finished",
                        "filename": urls[0],
                        "info_dict": {"height": 1080},
                    }
                )
            return 0

    monkeypatch.setattr(
        yd,
        "yt_dlp",
        types.SimpleNamespace(YoutubeDL=OverCapYDL, utils=yt_dlp.utils),
    )

    assert yd.download(["URL1", "URL2", "URL3"], tmp_path, quality="360") == 0
    # Each URL is attempted once: none of them failed, so the ladder is not
    # walked and no video is retried.
    assert calls == ["URL1", "URL2", "URL3"]
    err = capsys.readouterr().err
    assert err.count("no format at or below 360p") == 3
    for url in ("URL1", "URL2", "URL3"):
        assert url in err


def test_failure_message_starts_on_its_own_line(tmp_path, monkeypatch, capsys):
    """yt-dlp does not signal "error" on every path, so close the line here."""
    calls = []

    def fails_midway(client):
        raise DownloadError("Unable to download webpage: name resolution failed")

    class ProgressThenFailYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def download(self, urls):
            calls.append(urls[0])
            for hook in self.opts["progress_hooks"]:
                hook({"status": "downloading", "_percent_str": " 42.0%"})
            return fails_midway(None)

    monkeypatch.setattr(
        yd,
        "yt_dlp",
        types.SimpleNamespace(YoutubeDL=ProgressThenFailYDL, utils=yt_dlp.utils),
    )

    assert yd.download(["URL"], tmp_path) == 1
    # A name-resolution failure is not client-specific, so it is attempted
    # once rather than sent round the ladder.
    assert calls == ["URL"]
    captured = capsys.readouterr()
    assert "42.0%" in captured.out
    assert captured.out.endswith("\n"), "progress line was left open"
    assert "Failed to download URL" in captured.err


def test_retry_notice_starts_on_its_own_line(tmp_path, monkeypatch, capsys):
    calls = []

    class ProgressThen403YDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def download(self, urls):
            calls.append(urls[0])
            for hook in self.opts["progress_hooks"]:
                hook({"status": "downloading", "_percent_str": " 42.0%"})
            raise DownloadError("HTTP Error 403: Forbidden")

    monkeypatch.setattr(
        yd,
        "yt_dlp",
        types.SimpleNamespace(YoutubeDL=ProgressThen403YDL, utils=yt_dlp.utils),
    )

    assert yd.download([YT], tmp_path) == 1
    # A 403 *is* client-specific, so the whole ladder is walked: the default
    # client plus every fallback, each one re-opening a progress line.
    assert calls == [YT] * (1 + len(yd.FALLBACK_CLIENTS))
    captured = capsys.readouterr()
    assert "retrying with alternate player clients" in captured.err
    # After the "Fetching:" banner, exactly two newlines on stdout: one closing
    # the progress line *before* the retry notice is printed, and one from the
    # end-of-URL close. Checking only that stdout ends in "\n" pinned nothing
    # here - the end-of-URL close supplies that whether or not the retry-path
    # close exists, and removing the retry-path close left the suite green.
    after_banner = captured.out.split(f"Fetching: {YT}\n", 1)[1]
    assert after_banner.count("\n") == 2, "retry notice was not given its own line"


# --------------------------------------------------------------------------
# audio-only and subtitle option wiring
# --------------------------------------------------------------------------


def test_audio_only_without_ffmpeg_reports_install_help(tmp_path, monkeypatch):
    monkeypatch.setattr(yd.shutil, "which", lambda name: None)
    with pytest.raises(yd.DownloaderError) as excinfo:
        yd.download(["URL"], tmp_path, audio_only=True)
    message = str(excinfo.value)
    assert "requires ffmpeg" in message
    for hint in ("winget install ffmpeg", "apt install ffmpeg", "brew install ffmpeg"):
        assert hint in message


def test_setup_errors_reach_the_user_as_a_message_not_a_traceback(monkeypatch):
    """main() must turn DownloaderError into the exit sys.exit() used to give.

    download() no longer calls sys.exit() itself, so that the failure count in
    its docstring stays true for an importing caller. The CLI behaviour has to
    be identical: the message, and a non-zero status.
    """

    def refuses(**kwargs):
        raise yd.DownloaderError("Cannot use output directory 'x': denied")

    monkeypatch.setattr(yd, "download", refuses)
    monkeypatch.setattr(sys, "argv", ["youtube-downloader", "URL"])
    _pretend_terminal(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        yd.main()
    assert str(excinfo.value) == "Cannot use output directory 'x': denied"
    assert excinfo.value.code != 0


def test_no_ffmpeg_warns_but_still_downloads(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(yd.shutil, "which", lambda name: None)
    calls = []
    _install_fake_ydl(monkeypatch, lambda client: 0, calls)
    assert yd.download(["URL"], tmp_path) == 0
    assert "ffmpeg not found" in capsys.readouterr().err


def _captured_opts(tmp_path, monkeypatch, **kwargs):
    """Run download() against the stub and return the opts yt-dlp was given."""
    monkeypatch.setattr(yd.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    seen = {}
    calls = []
    _install_fake_ydl(monkeypatch, lambda client: 0, calls)
    real = yd.yt_dlp.YoutubeDL

    def spy(opts):
        seen.update(opts)
        return real(opts)

    monkeypatch.setattr(yd.yt_dlp, "YoutubeDL", spy)
    yd.download(["URL"], tmp_path, **kwargs)
    return seen


def test_audio_only_wires_the_mp3_postprocessor(tmp_path, monkeypatch):
    opts = _captured_opts(tmp_path, monkeypatch, audio_only=True)
    assert opts["postprocessors"] == [
        {
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }
    ]
    # Nothing to merge when there is no video stream.
    assert opts["merge_output_format"] is None


def test_video_mode_has_no_postprocessor_and_merges_to_mp4(tmp_path, monkeypatch):
    opts = _captured_opts(tmp_path, monkeypatch)
    assert "postprocessors" not in opts
    assert opts["merge_output_format"] == "mp4"


def test_outtmpl_writes_into_the_requested_output_directory(tmp_path, monkeypatch):
    """The one line joining -o and the Downloads lookup to where files land.

    Nothing else in the suite asserted on outtmpl, so a change that dropped
    output_dir from the template - sending every download to the working
    directory - or that hard-coded a different directory entirely passed with
    the suite fully green. The README's promise that files are saved to the
    Downloads folder as "Title [video-id].EXT" rests on this option alone.
    """
    opts = _captured_opts(tmp_path, monkeypatch)
    assert opts["outtmpl"] == str(tmp_path / "%(title)s [%(id)s].%(ext)s")


def test_a_percent_sign_in_the_output_directory_is_not_expanded(tmp_path, monkeypatch):
    """Regression: yt-dlp expands the whole template, directory included.

    A directory named "100%(x)s" - or, in PowerShell, which does not expand
    %...%, one typed as "%USERNAME%" - was created by mkdir() and then
    written around: yt-dlp expanded the name and saved the file somewhere
    download() never checked. "%%" is what yt-dlp hands back as a literal.
    """
    target = tmp_path / "100%(title)s"
    opts = _captured_opts(target, monkeypatch)
    assert opts["outtmpl"].startswith(str(tmp_path / "100%%(title)s"))
    # Through the real template engine: the file lands inside the directory.
    ydl = yt_dlp.YoutubeDL({"outtmpl": opts["outtmpl"], "quiet": True})
    prepared = ydl.prepare_filename({"id": "abc", "title": "T", "ext": "mp4"})
    assert prepared == str(target / "T [abc].mp4")


def test_a_dollar_variable_in_the_output_directory_is_refused(tmp_path, monkeypatch):
    """Regression: "$NAME" in the directory was expanded by yt-dlp.

    There is no escape for it - yt-dlp's "$$" protects only the first "$" -
    so the file was saved wherever the variable pointed while mkdir() had
    created the directory the user named. Refused up front, before mkdir(),
    and only when expansion would actually change the path.
    """
    monkeypatch.setenv("CLIPS", str(tmp_path / "elsewhere"))
    monkeypatch.setattr(yd.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    calls = []
    _install_fake_ydl(monkeypatch, lambda client: 0, calls)
    target = tmp_path / "$CLIPS"

    with pytest.raises(yd.DownloaderError) as excinfo:
        yd.download(["URL"], target)
    message = str(excinfo.value)
    assert str(target) in message
    assert str(tmp_path / "elsewhere") in message
    assert calls == []
    assert not target.exists(), "refused directory must not be created"


def test_a_dollar_that_expands_to_nothing_in_the_output_directory_is_kept(
    tmp_path, monkeypatch
):
    """ "$5 clips" names a variable that does not exist, so it is a plain name."""
    monkeypatch.delenv("5", raising=False)
    opts = _captured_opts(tmp_path / "$5 clips", monkeypatch)
    assert opts["outtmpl"].startswith(str(tmp_path / "$5 clips"))


def test_subtitles_flag_wires_the_subtitle_options(tmp_path, monkeypatch):
    opts = _captured_opts(tmp_path, monkeypatch, subtitles=True)
    assert opts["writesubtitles"] is True
    assert opts["writeautomaticsub"] is True
    assert opts["subtitleslangs"] == ["en"]


def test_subtitles_are_off_by_default(tmp_path, monkeypatch):
    opts = _captured_opts(tmp_path, monkeypatch)
    assert "writesubtitles" not in opts


def test_announce_hook_is_wired_as_a_postprocessor_hook(tmp_path, monkeypatch):
    """Without this registration nothing names the finished file at all."""
    opts = _captured_opts(tmp_path, monkeypatch)
    assert len(opts["postprocessor_hooks"]) == 1
    announce = opts["postprocessor_hooks"][0]
    assert announce.__func__ is yd.ProgressReporter.announce_hook
    # Identity, not just the name: every ProgressReporter has a bound method
    # called "announce_hook", so a hook bound to some *other* reporter passes a
    # name check while close() consults an open-line state that belongs to
    # nothing - and the announcement lands on top of the progress line.
    assert announce.__self__ is opts["progress_hooks"][0].__self__


def test_each_url_announces_the_file_it_produced(tmp_path, monkeypatch, capsys):
    """End to end over the option wiring: one final name per video, no temps."""
    monkeypatch.setattr(yd.shutil, "which", lambda name: "/usr/bin/ffmpeg")

    class MergingYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def download(self, urls):
            stem = urls[0]
            # yt-dlp fetches each stream to its own temporary name...
            for temp in (f"{stem}.f399.mp4", f"{stem}.f251.webm"):
                for hook in self.opts["progress_hooks"]:
                    hook({"status": "downloading", "_percent_str": " 50.0%"})
                    hook({"status": "finished", "filename": temp})
            # ...then merges them and moves the result into place.
            for hook in self.opts["postprocessor_hooks"]:
                hook(
                    {
                        "status": "finished",
                        "postprocessor": "MoveFiles",
                        "info_dict": {"filepath": f"{stem}.mp4"},
                    }
                )
            return 0

    monkeypatch.setattr(
        yd,
        "yt_dlp",
        types.SimpleNamespace(YoutubeDL=MergingYDL, utils=yt_dlp.utils),
    )

    assert yd.download(["A", "B"], tmp_path) == 0
    out = capsys.readouterr().out
    assert "Downloaded: A.mp4" in out
    assert "Downloaded: B.mp4" in out
    assert out.count("Downloaded:") == 2
    # The merged-away temporary files must never be presented as results.
    for temp in (".f399.mp4", ".f251.webm"):
        assert temp not in out


# --------------------------------------------------------------------------
# Ctrl-C handling
# --------------------------------------------------------------------------


def test_ctrl_c_is_caught_by_main_not_the_dunder_main_guard(monkeypatch, capsys):
    """The console script calls main() directly, bypassing __main__.

    So the handler has to live inside main(); a guard under `if __name__ ==
    "__main__"` would leave the installed `youtube-downloader` command
    dumping a traceback on Ctrl-C.
    """

    def interrupted(**kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(yd, "download", interrupted)
    monkeypatch.setattr(sys, "argv", ["youtube-downloader", "URL"])
    _pretend_terminal(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        yd.main()
    assert "Cancelled." in str(excinfo.value)


# --------------------------------------------------------------------------
# --version
# --------------------------------------------------------------------------


def test_version_string_carries_all_three_versions():
    """One line has to answer everything the bug template asks for."""
    text = yd.version_string()
    assert text.startswith("youtube-downloader ")
    assert f"yt-dlp {yt_dlp.version.__version__}" in text
    assert f"Python {sys.version_info.major}.{sys.version_info.minor}" in text


def test_version_string_survives_an_uninstalled_checkout(monkeypatch):
    """Running from a clone with nothing installed must not blow up."""
    import importlib.metadata

    def no_distribution(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", no_distribution)
    text = yd.version_string()
    assert "source checkout" in text
    # The other two are still known, and are the ones that actually matter.
    assert f"yt-dlp {yt_dlp.version.__version__}" in text


@pytest.mark.parametrize("flag", ["-V", "--version"])
def test_version_flag_prints_and_exits_zero(monkeypatch, capsys, flag):
    monkeypatch.setattr(sys, "argv", ["youtube-downloader", flag])
    with pytest.raises(SystemExit) as excinfo:
        yd.main()
    assert excinfo.value.code == 0
    assert "youtube-downloader" in capsys.readouterr().out


def test_version_flag_needs_no_url(monkeypatch):
    """argparse would otherwise reject it for the missing required urls."""
    monkeypatch.setattr(sys, "argv", ["youtube-downloader", "--version"])
    with pytest.raises(SystemExit) as excinfo:
        yd.main()
    assert excinfo.value.code == 0


def test_main_is_the_declared_console_entry_point():
    """[project.scripts] points at main(), so main() must be the wrapper."""
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    assert 'youtube-downloader = "youtube_downloader:main"' in text


# --------------------------------------------------------------------------
# Non-absolute XDG values must be ignored
# --------------------------------------------------------------------------


def test_relative_xdg_env_var_is_ignored(monkeypatch, tmp_path):
    """A relative value would resolve against the CWD, not the user's home."""
    monkeypatch.setenv("XDG_DOWNLOAD_DIR", "relative/Downloads")
    monkeypatch.setattr(yd.shutil, "which", lambda name: None)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "nonexistent"))
    assert yd._xdg_downloads_dir() is None


def test_relative_user_dirs_entry_is_ignored(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_DOWNLOAD_DIR", raising=False)
    monkeypatch.setattr(yd.shutil, "which", lambda name: None)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / "user-dirs.dirs").write_text(
        'XDG_DOWNLOAD_DIR="Downloads"\n', encoding="utf-8"
    )
    assert yd._xdg_downloads_dir() is None


def test_relative_xdg_config_home_falls_back_to_dot_config(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_DOWNLOAD_DIR", raising=False)
    monkeypatch.setattr(yd.shutil, "which", lambda name: None)
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/config")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(yd.Path, "home", staticmethod(lambda: tmp_path))
    (tmp_path / ".config").mkdir()
    (tmp_path / ".config" / "user-dirs.dirs").write_text(
        'XDG_DOWNLOAD_DIR="$HOME/FromDotConfig"\n', encoding="utf-8"
    )
    assert yd._xdg_downloads_dir() == tmp_path / "FromDotConfig"


@pytest.mark.parametrize("value", ["", "   ", "relative/path"])
def test_absolute_or_none_rejects_unusable_values(value):
    assert yd._absolute_or_none(value.strip()) is None


def test_absolute_or_none_expands_vars_and_user(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("SOMEWHERE", str(tmp_path / "var"))
    assert yd._absolute_or_none("$SOMEWHERE/x") == tmp_path / "var" / "x"
    assert yd._absolute_or_none("~/y") == tmp_path / "y"


# --------------------------------------------------------------------------
# Single-video URL enforcement
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        # noplaylist does cover this shape, so it stays allowed.
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123",
        "https://youtu.be/dQw4w9WgXcQ",
        # Pasted without a scheme, which puts the host in urlparse's .path.
        "youtube.com/watch?v=abc",
        "https://m.youtube.com/watch?v=abc",
        "https://music.youtube.com/watch?v=abc",
        "https://www.youtube.com/shorts/abc123",
        "https://www.youtube.com/embed/abc123",
        "https://www.youtube.com/live/abc123",
        "https://www.youtube.com/v/abc123",
        "https://www.youtube-nocookie.com/embed/abc123",
        # Regression: these are all shapes yt-dlp's YouTube extractor accepts
        # as exactly one video, and each was refused as "not a single video".
        "https://www.youtube.com/e/abc123",
        "https://www.youtube.com/?v=abc123",
        "https://www.youtube.com/watch_popup?v=abc123",
        "https://www.youtube.com/clip/UgkxSomeClipId",
        # Regression: a channel's own "/live" page is the one video currently
        # streaming there - yt-dlp's tab extractor redirects to it - and each
        # of these was refused as "a channel".
        "https://www.youtube.com/@handle/live",
        "https://www.youtube.com/channel/UCabc/live",
        "https://www.youtube.com/c/SomeName/live",
        "https://www.youtube.com/user/SomeName/live",
        # Not YouTube: yt-dlp supports thousands of sites and this tool has no
        # business classifying their URL shapes.
        "https://vimeo.com/12345",
        "https://example.com/some/playlist?list=x",
    ],
)
def test_single_video_urls_are_accepted(url):
    assert yd.single_video_problem(url) is None


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.youtube.com/playlist?list=PL123", "a playlist"),
        ("https://www.youtube.com/@handle", "a channel"),
        ("https://www.youtube.com/@handle/videos", "a channel"),
        # Only the channel's own live page is a video; a deeper path that
        # happens to end in the word is still the channel.
        ("https://www.youtube.com/@handle/streams/live", "a channel"),
        ("https://www.youtube.com/channel/UCabc/videos/live", "a channel"),
        ("youtube.com/@handle", "a channel"),
        ("https://www.youtube.com/channel/UCabc", "a channel"),
        ("https://www.youtube.com/c/SomeName", "a channel"),
        ("https://www.youtube.com/user/SomeName", "a channel"),
        # A watch URL carrying only a list id: yt-dlp redirects it to the
        # playlist rather than downloading one video.
        ("https://www.youtube.com/watch?list=PL123", "a playlist"),
        ("https://www.youtube.com/results?search_query=cats", "a search results page"),
        ("https://www.youtube.com/feed/subscriptions", "a feed"),
        # The one /embed/ URL that is a playlist rather than a video.
        ("https://www.youtube.com/embed/videoseries?list=PL123", "a playlist"),
        ("https://www.youtube.com/e/videoseries?list=PL123", "a playlist"),
        ("https://youtu.be/abc/extra", "not a single video"),
        # The bare root is a video only when it carries "v=" (tested above).
        ("https://www.youtube.com/", "not a single video"),
        ("https://www.youtube.com/watch_popup", "not a single video"),
    ],
)
def test_multi_video_urls_are_named_and_rejected(url, expected):
    """Regression: noplaylist does not cover any of these.

    InfoExtractor._yes_playlist() returns early when a URL carries no video
    id and never reads the noplaylist option, so every URL here previously
    enumerated and downloaded every video it named.
    """
    assert yd.single_video_problem(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        # urlparse raises ValueError("Invalid IPv6 URL") on an unbalanced "[",
        # which a mangled paste produces easily enough.
        "http://[::1",
        "https://[",
        "https://www.youtube.com:80@[/watch?v=abc",
    ],
)
def test_a_malformed_url_is_not_classified_rather_than_raising(url):
    """Regression: urlparse's ValueError escaped as a traceback.

    single_video_problem() classifies URLs; it is not the URL validator. A
    malformed one used to take urlparse's ValueError straight out through
    download() - a bare ValueError for an importing caller, and a full
    traceback for the CLI user, which is exactly what DownloaderError and the
    handlers in main() exist to prevent. Declining to classify is the right
    answer: yt-dlp reports the bad URL in its own words.
    """
    assert yd.single_video_problem(url) is None


def test_a_malformed_url_reaches_yt_dlp_instead_of_tracebacking(tmp_path, monkeypatch):
    """The whole CLI path, not just the classifier: one failure, no traceback."""
    calls = []

    def rejects(client):
        raise DownloadError("ERROR: Unsupported URL: http://[::1")

    _install_fake_ydl(monkeypatch, rejects, calls)
    # Counted as a normal failure, and the batch is not aborted by an escaping
    # ValueError partway through it.
    assert yd.download(["http://[::1"], tmp_path) == 1
    assert calls == [None]


def test_playlist_url_is_refused_before_anything_is_downloaded(tmp_path, monkeypatch):
    calls = []
    _install_fake_ydl(monkeypatch, lambda client: 0, calls)

    with pytest.raises(yd.DownloaderError) as excinfo:
        yd.download(["https://www.youtube.com/playlist?list=PL123"], tmp_path)
    message = str(excinfo.value)
    assert "a playlist" in message
    assert "https://www.youtube.com/playlist?list=PL123" in message
    assert "watch?v=VIDEO_ID" in message
    # Nothing was fetched: this is a usage error, caught like an invalid -q.
    assert calls == []


def test_every_rejected_url_in_a_batch_is_named(tmp_path, monkeypatch):
    """One message listing all of them beats fixing them one run at a time."""
    calls = []
    _install_fake_ydl(monkeypatch, lambda client: 0, calls)

    with pytest.raises(yd.DownloaderError) as excinfo:
        yd.download(
            [
                "https://www.youtube.com/watch?v=ok",
                "https://www.youtube.com/@handle",
                "https://www.youtube.com/results?search_query=cats",
            ],
            tmp_path,
        )
    message = str(excinfo.value)
    assert "a channel" in message
    assert "a search results page" in message
    # A good URL in the same batch is not listed as a problem.
    assert "watch?v=ok\n" not in message
    assert calls == []


def test_playlist_items_backstops_the_url_check(tmp_path, monkeypatch):
    """Anything that still resolves to a playlist is capped at one video.

    single_video_problem() only classifies YouTube URLs, so this is the guard
    for everything else it deliberately waves through.
    """
    opts = _captured_opts(tmp_path, monkeypatch)
    assert opts["playlist_items"] == "1"
    assert opts["noplaylist"] is True


# --------------------------------------------------------------------------
# Who owns the terminal: this tool, not yt-dlp
#
# "quiet" and "no_warnings" do not stop yt-dlp writing to the console. The
# first two tests pin the options that do; the other two drive the real
# yt-dlp code paths that wrote through them, without any network, because a
# stubbed yt_dlp cannot show what the real one prints.
# --------------------------------------------------------------------------


def test_yt_dlp_is_told_not_to_draw_its_own_progress(tmp_path, monkeypatch):
    """Regression: "quiet" does not imply "noprogress" for an API caller.

    Without it yt-dlp drew "[download]  42.0% of ..." on stdout with a carriage
    return and ProgressReporter.hook() redrew a shorter line on top, so the
    terminal alternated between the two and the tail of yt-dlp's line stayed
    visible after this tool's.
    """
    opts = _captured_opts(tmp_path, monkeypatch)
    assert opts["noprogress"] is True


def test_yt_dlp_console_output_is_routed_to_a_silent_logger(
    tmp_path, monkeypatch, capsys
):
    """Regression: yt-dlp's "ERROR: ..." lines reached stderr directly.

    YoutubeDL.trouble() writes the message before it raises, from inside
    ydl.download(), so it landed on the still-open progress line and then
    appeared again in this tool's own "Failed to download ..." message. A
    logger is the only switch that redirects that write.
    """
    opts = _captured_opts(tmp_path, monkeypatch)
    logger = opts["logger"]
    capsys.readouterr()
    # yt-dlp calls exactly these three on the logger it is given. Calling
    # them, not merely finding them, is what pins "silent": a logger whose
    # warning() printed passed a callable() check.
    for method in ("debug", "warning", "error"):
        getattr(logger, method)("[youtube] abc: something yt-dlp would say")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def _real_ydl_with_this_tools_options(tmp_path, monkeypatch, capsys):
    """A real YoutubeDL built from exactly the options download() passes.

    Capturing the options runs download() against the stub, which prints its
    "Fetching:" banner; that output is drained here so the caller sees only
    what the real yt-dlp writes afterwards.
    """
    opts = _captured_opts(tmp_path, monkeypatch)
    capsys.readouterr()
    return yt_dlp.YoutubeDL(opts)


def test_real_yt_dlp_draws_no_progress_line_of_its_own(tmp_path, monkeypatch, capsys):
    """The real downloader, fed one progress event: only this tool's line."""
    from yt_dlp.downloader import get_suitable_downloader

    ydl = _real_ydl_with_this_tools_options(tmp_path, monkeypatch, capsys)
    info = {"protocol": "https", "url": "u", "id": "abc", "title": "T", "ext": "mp4"}
    # Mirrors YoutubeDL.dl(): the downloader is built per file and the
    # YoutubeDL-level hooks are attached to it there.
    downloader = get_suitable_downloader(info, ydl.params)(ydl, ydl.params)
    for hook in ydl._progress_hooks:
        downloader.add_progress_hook(hook)
    downloader._hook_progress(
        {
            "status": "downloading",
            "downloaded_bytes": 2_000_000,
            "total_bytes": 10_000_000,
            "speed": 1_000_000.0,
            "eta": 5,
            "elapsed": 2.0,
            "filename": "x",
        },
        info,
    )
    out = capsys.readouterr().out
    # This tool's line is there, with the fields yt-dlp computes for it...
    assert "20.0%" in out
    assert "ETA" in out
    # ...and yt-dlp's own "[download] ..." rendering is not.
    assert "[download]" not in out


def test_real_yt_dlp_error_report_writes_nothing_to_the_console(
    tmp_path, monkeypatch, capsys
):
    """report_error() is what yt-dlp calls before raising DownloadError."""
    ydl = _real_ydl_with_this_tools_options(tmp_path, monkeypatch, capsys)
    # The other two channels yt-dlp writes through: a warning and an
    # informational line. Neither may reach the console either.
    ydl.report_warning("[youtube] abc: some formats may be missing")
    ydl.to_screen("[info] abc: Downloading 1 format(s)")
    with pytest.raises(DownloadError):
        ydl.report_error("[youtube] abc: Video unavailable")
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


# --------------------------------------------------------------------------
# The terminal guard: no piping, no redirection
# --------------------------------------------------------------------------


def _record_download(monkeypatch):
    calls = []

    def fake_download(**kwargs):
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(yd, "download", fake_download)
    return calls


def test_a_piped_stdout_is_refused_before_anything_is_downloaded(monkeypatch, capsys):
    calls = _record_download(monkeypatch)
    _pretend_terminal(monkeypatch, sys.stderr)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    monkeypatch.setattr(sys, "argv", ["youtube-downloader", "URL"])
    with pytest.raises(SystemExit) as excinfo:
        yd.main()
    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert "must be run directly in a terminal" in captured.err
    assert captured.out == ""
    assert calls == []


def test_a_redirected_stderr_is_refused_even_with_a_terminal_stdout(
    monkeypatch, capsys
):
    calls = _record_download(monkeypatch)
    _pretend_terminal(monkeypatch, sys.stdout)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False, raising=False)
    monkeypatch.setattr(sys, "argv", ["youtube-downloader", "URL"])
    with pytest.raises(SystemExit) as excinfo:
        yd.main()
    assert excinfo.value.code == 2
    assert calls == []


def test_two_terminals_let_the_run_proceed(monkeypatch, capsys):
    calls = _record_download(monkeypatch)
    _pretend_terminal(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["youtube-downloader", "URL"])
    yd.main()  # no SystemExit
    assert len(calls) == 1 and calls[0]["urls"] == ["URL"]
    captured = capsys.readouterr()
    assert "All downloads completed." in captured.out
    assert "must be run directly in a terminal" not in captured.err


def test_help_still_works_when_stdout_is_piped(monkeypatch, capsys):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False, raising=False)
    monkeypatch.setattr(sys, "argv", ["youtube-downloader", "--help"])
    with pytest.raises(SystemExit) as excinfo:
        yd.main()
    assert excinfo.value.code == 0
    captured = capsys.readouterr()
    assert "--audio-only" in captured.out
    assert "must be run directly in a terminal" not in captured.err


def test_importing_the_module_does_not_run_the_guard():
    """The check lives in the entry path, not at module level.

    Proved in a child whose stdout and stderr are pipes - a real non-terminal
    - rather than by asserting on pytest's capture, which `pytest -s` turns
    off and which then made this test fail on any interactive terminal.
    """
    import subprocess

    root = Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.dont_write_bytecode = True; "
            f"sys.path.insert(0, {str(root)!r}); import youtube_downloader",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "must be run directly in a terminal" not in proc.stderr


# --------------------------------------------------------------------------
# --version is assembled lazily
# --------------------------------------------------------------------------


def test_version_string_is_not_built_on_an_ordinary_run(monkeypatch):
    """Regression: argparse's built-in version action took the string eagerly.

    `version=version_string()` ran while the parser was being built, so every
    run - and every argument error - paid for the importlib.metadata lookup.
    """
    calls = []
    monkeypatch.setattr(yd, "version_string", lambda: calls.append(1) or "x")
    _run_main(monkeypatch, ["URL"])
    assert calls == []


def test_version_string_is_not_built_for_help(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["youtube-downloader", "--help"])
    calls = []
    monkeypatch.setattr(yd, "version_string", lambda: calls.append(1) or "x")
    with pytest.raises(SystemExit) as excinfo:
        yd.main()
    assert excinfo.value.code == 0
    assert calls == []
    assert "--audio-only" in capsys.readouterr().out


# --------------------------------------------------------------------------
# Assumptions this wrapper makes about yt-dlp
#
# Most tests in this file either replace youtube_downloader.yt_dlp with a stub
# or exercise a function that never reaches yt-dlp at all, which is what keeps
# the suite offline and fast - but it also means a yt-dlp release that renamed
# something this module hard-codes could ship green and only break for users.
# The two tests below exist for the sole purpose of pinning those names
# against the real yt-dlp, so the weekly Dependabot bump of the
# constraints.txt pin is what surfaces such a rename.
#
# They are not the only tests that touch the real yt-dlp:
# test_selectors_resolve_against_real_formats builds a real YoutubeDL format
# selector, test_move_files_key_matches_yt_dlp imports the real
# MoveFilesAfterDownloadPP, the two test_real_yt_dlp_* tests in the "Who
# owns the terminal" section drive a real YoutubeDL's progress and
# error-reporting paths,
# test_a_percent_sign_in_the_output_directory_is_not_expanded runs the
# output template through a real prepare_filename(), and the closed-pipe
# tests built on _real_ydl_with_a_fake_site() and _spawn_real_download()
# run a real YoutubeDL end to end against a fake site. Those assert about
# this wrapper's behaviour and happen to need yt-dlp to do it; these two
# assert about yt-dlp itself.
# --------------------------------------------------------------------------


def test_the_announce_hook_postprocessor_name_still_exists():
    """announce_hook() keys off the literal postprocessor name "MoveFiles".

    That name is how the hook recognises the one event carrying the final,
    post-merge path. If yt-dlp renames the postprocessor, the hook silently
    stops firing and the "Downloaded: ..." line the README promises simply
    never prints - no error, no failing stub-based test.
    """
    from yt_dlp.postprocessor import MoveFilesAfterDownloadPP

    assert MoveFilesAfterDownloadPP.pp_key() == "MoveFiles"


def test_every_configured_player_client_is_known_to_yt_dlp():
    """The client names in SELECTABLE_CLIENTS must be ones yt-dlp accepts.

    These reach yt-dlp as extractor_args["youtube"]["player_client"]. A name
    yt-dlp no longer knows is not rejected loudly, so the retry ladder would
    quietly stop being a fallback at all.
    """
    from yt_dlp.extractor.youtube._base import INNERTUBE_CLIENTS

    unknown = sorted(set(yd.SELECTABLE_CLIENTS) - set(INNERTUBE_CLIENTS))
    assert not unknown, f"yt-dlp no longer knows these player clients: {unknown}"


# --------------------------------------------------------------------------
# Guards that turn a crash into a message
# --------------------------------------------------------------------------


def test_a_non_decimal_quality_is_rejected_as_a_message(tmp_path):
    """Regression: the guard used isdigit(), which int() does not agree with.

    str.isdigit() is True for the Unicode superscripts, which int() then
    refuses - so a direct caller passing one got the bare ValueError several
    statements later that this guard exists to turn into a DownloaderError.
    """
    superscript_two = "\N{SUPERSCRIPT TWO}"
    assert superscript_two.isdigit() and not superscript_two.isdecimal()
    with pytest.raises(yd.DownloaderError, match="Invalid quality"):
        yd.download(["https://youtu.be/abc"], tmp_path, quality=superscript_two)


def test_a_non_ascii_decimal_quality_is_rejected_as_a_message(tmp_path):
    """Regression: isdecimal() alone admitted digits yt-dlp's grammar refuses.

    This is the case that pins the isascii() half of the guard, and it is not
    covered by the superscript above: a superscript is neither ASCII nor
    decimal, so that test passes with either half of the condition broken.
    An Arabic-Indic digit string is isdecimal(), and int() reads it happily,
    so the guard's int()-facing half waves it through - but the raw string is
    also interpolated into the yt-dlp format selector, whose filter grammar
    takes ASCII digits only ("Invalid filter specification 'height<=...'").
    """
    arabic_indic = "١٠٨٠"  # "1080" in Arabic-Indic digits
    assert arabic_indic.isdecimal() and not arabic_indic.isascii()
    assert int(arabic_indic) == 1080
    with pytest.raises(yd.DownloaderError, match="Invalid quality"):
        yd.download(["https://youtu.be/abc"], tmp_path, quality=arabic_indic)


def test_a_closed_pipe_exits_quietly_instead_of_tracebacking(monkeypatch):
    """A reader that goes away mid-run must not produce a traceback.

    Piping a long run into `head`, or quitting the pager part-way through,
    makes the next write raise BrokenPipeError. main() has to catch it:
    letting it escape dumps a traceback over whatever the user was reading.
    """

    def explode():
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(yd, "_run", explode)
    # Both streams substituted: see _TerminalStringIO for why this test must
    # never let main() reach the process's own descriptors.
    _own_streams(monkeypatch)
    with pytest.raises(SystemExit) as excinfo:
        yd.main()
    assert excinfo.value.code == 1


# --------------------------------------------------------------------------
# The output stream itself failing
#
# These drive main() with substituted streams, because that is where the two
# failures live: what this tool writes, not what yt-dlp downloads. Stubbing
# _run() the way the test above does exercises main()'s handlers but cannot see
# the paths where the write fails inside download()'s own retry loop.
# --------------------------------------------------------------------------


class _TerminalStringIO(io.StringIO):
    """A stdout or stderr stand-in that claims to be a terminal.

    Every test that lets a BrokenPipeError reach main() installs one of
    these on *both* streams, and never asks for capsys alongside it. Two
    reasons, both of which only show up under `pytest -s`:

    main() answers a closed pipe by dup2()-ing the process's own fd 1 and
    fd 2 at devnull, guarded by `sys.stdout is sys.__stdout__`. Under the
    default capture that guard is what keeps the redirect away from the test
    run; with capture off it is not, and the rest of the run's output -
    summary included - silently vanished.

    capsys and monkeypatch.setattr(sys, "stdout", ...) also tear down in the
    wrong order for each other: capsys restores and closes its own buffer
    first, then monkeypatch puts that closed buffer back, and pytest itself
    died flushing it. Reading the buffer directly needs neither.
    """

    def isatty(self):
        return True


def _own_streams(monkeypatch, stdout=None):
    """Install `stdout` (default: a fresh _TerminalStringIO) and a fresh
    stderr, returning the stderr buffer."""
    err = _TerminalStringIO()
    monkeypatch.setattr(
        sys, "stdout", stdout if stdout is not None else _TerminalStringIO()
    )
    monkeypatch.setattr(sys, "stderr", err)
    return err


class _AsciiOnlyStream:
    """A stream that refuses non-ASCII and cannot be reconfigured.

    Stands in for whatever main() cannot make tolerant - a stdout replaced by
    another library, or a stream object that is not a TextIOWrapper - so the
    handler in main() is what has to catch the failure.
    """

    encoding = "ascii"

    def __init__(self):
        self.text = ""

    def write(self, text):
        text.encode("ascii")  # raises UnicodeEncodeError, like a real stream
        self.text += text
        return len(text)

    def flush(self):
        pass

    def isatty(self):
        return True


class _ClosesAfter:
    """A stdout that takes a few writes and then behaves like a closed pipe.

    `head` exiting part-way through a run is the real version: the writes that
    already landed succeeded, and the next one raises.
    """

    encoding = "utf-8"

    def __init__(self, allowed):
        self.allowed = allowed
        self.text = ""

    def write(self, text):
        if self.allowed <= 0:
            raise BrokenPipeError(32, "Broken pipe")
        self.allowed -= 1
        self.text += text
        return len(text)

    def flush(self):
        pass

    def isatty(self):
        return True


def _announcing_ydl(monkeypatch, filepath):
    """Stub yt-dlp: reports success and announces `filepath` via MoveFiles."""

    class AnnouncingYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def download(self, urls):
            for hook in self.opts["postprocessor_hooks"]:
                hook(
                    {
                        "status": "finished",
                        "postprocessor": "MoveFiles",
                        "info_dict": {"filepath": filepath},
                    }
                )
            return 0

    monkeypatch.setattr(
        yd,
        "yt_dlp",
        types.SimpleNamespace(YoutubeDL=AnnouncingYDL, utils=yt_dlp.utils),
    )
    monkeypatch.setattr(yd.shutil, "which", lambda name: "/usr/bin/ffmpeg")


def test_an_unencodable_title_is_not_reported_as_a_failed_download(
    monkeypatch, tmp_path
):
    """Regression: a title the stream could not print counted as a failure.

    On Windows stdout is the ANSI code page whenever output is redirected or
    piped rather than attached to the console, and a video title carrying an
    emoji or any CJK character then made the "Downloaded: ..." line raise
    UnicodeEncodeError from inside yt-dlp's postprocessor hook. download()
    recorded that as the URL's failure, so the run printed "1 download(s)
    failed." and exited non-zero over a video that had been saved correctly.
    """
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    _announcing_ydl(monkeypatch, "C:/Downloads/\u30c6\u30b9\u30c8 [id].mp4")
    _pretend_terminal(monkeypatch, stream, sys.stderr)
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "argv", ["youtube-downloader", "URL", "-o", str(tmp_path)])

    yd.main()  # no SystemExit: nothing failed

    stream.flush()
    out = stream.buffer.getvalue().decode("cp1252")
    assert "All downloads completed." in out
    # The line still names the file; only the characters cp1252 cannot hold are
    # escaped, which is what backslashreplace buys over an exception.
    assert "Downloaded:" in out
    assert "[id].mp4" in out


def test_an_unencodable_url_is_a_message_not_a_traceback(monkeypatch, tmp_path):
    """The same failure where the stream cannot be made tolerant at all.

    "Fetching: <url>" is printed outside download()'s try, so before this was
    handled a URL carrying a character the stream could not encode reached the
    user as a full traceback - the outcome DownloaderError and the handlers in
    main() exist to prevent.
    """
    stream = _AsciiOnlyStream()
    calls = []
    _install_fake_ydl(monkeypatch, lambda client: 0, calls)
    _pretend_terminal(monkeypatch, sys.stderr)
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "youtube-downloader",
            "https://www.youtube.com/watch?v=\u30c6\u30b9\u30c8",
            "-o",
            str(tmp_path),
        ],
    )

    with pytest.raises(SystemExit) as excinfo:
        yd.main()
    assert "encoding cannot represent" in str(excinfo.value)
    assert excinfo.value.code != 0
    # It failed on its own output, before yt-dlp was ever asked for anything.
    assert calls == []


def test_the_platforms_own_closed_pipe_error_is_handled(monkeypatch, tmp_path):
    """Regression: Windows raises OSError(EINVAL), not BrokenPipeError.

    The two tests around this one raise BrokenPipeError by hand, so they
    passed on every platform whatever a real closed pipe raised. On Windows a
    flushed write to a pipe whose reader has exited raises "[Errno 22]
    Invalid argument": download()'s per-attempt `except Exception` recorded
    it as a failed download, and the end-of-URL close() then raised it again
    outside every handler - a traceback, exit status 120, over a video that
    had been saved. This closes the read end of a real pipe and lets the OS
    raise whatever it raises - and lets the real YoutubeDL wrap it, since
    the wrapper is what turns it into a DownloadError on the way back up.
    """
    import os

    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    stream = os.fdopen(write_fd, "w", encoding="utf-8")

    events = [{"status": "downloading", "downloaded_bytes": 1, "total_bytes": 3}]
    _real_ydl_with_a_fake_site(monkeypatch, events)
    _pretend_terminal(monkeypatch, stream)
    _own_streams(monkeypatch, stdout=stream)
    monkeypatch.setattr(
        sys, "argv", ["youtube-downloader", "fake://abc", "-o", str(tmp_path)]
    )
    try:
        with pytest.raises(SystemExit) as excinfo:
            yd.main()
    finally:
        # The buffer still holds the failed write, and the real YoutubeDL
        # keeps a reference to the stream, so it would be flushed again when
        # that object is collected. Give it a live pipe to flush into.
        drain_read, drain_write = os.pipe()
        os.dup2(drain_write, write_fd)
        stream.close()
        os.close(drain_write)
        os.close(drain_read)
    assert excinfo.value.code == 1


def _real_ydl_with_a_fake_site(monkeypatch, progress_events):
    """The real YoutubeDL, minus the network: one fake site, one fake fetch.

    The stubs used elsewhere call the hooks themselves, so an exception a
    hook raises reaches download() bare. The real YoutubeDL does not pass it
    on that way: process_info() wraps any OSError out of dl() - and the
    progress hooks run inside dl() - as UnavailableVideoError, which its
    download() wrapper reports as a DownloadError. Only the real class can
    show what a closed pipe looks like from the other side of that wrapper.
    """
    from yt_dlp.downloader.http import HttpFD
    from yt_dlp.extractor.common import InfoExtractor

    class FakeSiteIE(InfoExtractor):
        _VALID_URL = r"fake://(?P<id>\w+)"

        def _real_extract(self, url):
            fmt = {
                "format_id": "f",
                "url": "https://example.invalid/x.mp4",
                "ext": "mp4",
                "protocol": "https",
                "vcodec": "avc1",
                "acodec": "mp4a",
                "height": 720,
            }
            return {"id": self._match_id(url), "title": "T", "formats": [fmt]}

    class RealYDLWithFakeSite(yt_dlp.YoutubeDL):
        def __init__(self, params):
            super().__init__(params, auto_init=False)
            self.add_info_extractor(FakeSiteIE())

    def fake_real_download(self, filename, info_dict):
        for event in progress_events:
            self._hook_progress(
                {"filename": filename, "tmpfilename": filename, **event}, info_dict
            )
        return True

    monkeypatch.setattr(HttpFD, "real_download", fake_real_download)
    monkeypatch.setattr(
        yd,
        "yt_dlp",
        types.SimpleNamespace(YoutubeDL=RealYDLWithFakeSite, utils=yt_dlp.utils),
    )
    monkeypatch.setattr(yd.shutil, "which", lambda name: None)


def test_a_pipe_closed_during_a_real_download_is_not_a_failed_download(
    monkeypatch, tmp_path
):
    """Regression: the closed pipe came back from yt-dlp as a DownloadError.

    With the real YoutubeDL, the BrokenPipeError from the first progress
    write was wrapped as UnavailableVideoError inside dl() and reported as a
    DownloadError, so download() counted the URL as failed, printed "Failed
    to download ...: [Errno 32] Broken pipe", and main() exited through the
    failure-count path with stdout's buffer still pending - the interpreter's
    exit flush then failed a second time, with "Exception ignored while
    flushing sys.stdout" and status 120. The test doubles that raise
    BrokenPipeError straight out of the hook never went through that wrapper
    and passed either way.
    """
    events = [
        {"status": "downloading", "downloaded_bytes": n, "total_bytes": 3}
        for n in range(3)
    ]
    _real_ydl_with_a_fake_site(monkeypatch, events)
    # "Fetching: ..." and its newline are the two writes that succeed; the
    # first progress write is the one that hits the closed pipe.
    err = _own_streams(monkeypatch, stdout=_ClosesAfter(2))
    monkeypatch.setattr(
        sys, "argv", ["youtube-downloader", "fake://abc", "-o", str(tmp_path)]
    )

    with pytest.raises(SystemExit) as excinfo:
        yd.main()
    assert excinfo.value.code == 1
    assert "Failed to download" not in err.getvalue()
    assert "download(s) failed" not in err.getvalue()


def test_a_real_process_exits_1_and_silent_on_a_closed_pipe(tmp_path):
    """The whole process, not just main(): status 1, nothing on stderr.

    Only a real process can show this. After the failed write the data is
    still pending in stdout's buffer, and the interpreter flushes it again at
    shutdown; against a closed pipe that raises a second time and is reported
    as "Exception ignored while flushing sys.stdout" with exit status 120 -
    which is what main() reported before it pointed the descriptor at
    devnull. In-process tests cannot see that flush, and the redirect is
    deliberately skipped wherever sys.stdout has been replaced, as a test
    runner's capture replaces it.
    """
    proc = _spawn_real_download(tmp_path, urls=["fake://abc"], events=200000)
    assert proc.stdout is not None and proc.stderr is not None
    # Read one byte so the child has started writing, then walk away like
    # `head` does: the next write on the child's side hits a closed pipe.
    proc.stdout.read(1)
    proc.stdout.close()
    stderr = proc.stderr.read()
    proc.stderr.close()
    assert proc.wait(timeout=60) == 1, stderr.decode(errors="replace")
    assert stderr == b""


def test_a_real_process_exits_1_when_stderr_is_the_closed_pipe(tmp_path):
    """Regression: only fd 1 was pointed at devnull before the exit.

    `2>&1 | less` closes both streams together, and the write that fails is
    whichever comes next - here the "Failed to download ..." line on stderr.
    main()'s handler ran and called sys.exit(1), but stderr's buffer still
    held the failed line, the shutdown flush raised again, and the process
    exited 120.
    """
    proc = _spawn_real_download(
        tmp_path, urls=["fake://refused"], events=0, stdout_to_devnull=True
    )
    assert proc.stderr is not None
    # The reader of stderr goes away at once; the child's first stderr write
    # after that is the failure line for the refused URL.
    proc.stderr.close()
    assert proc.wait(timeout=60) == 1


def _spawn_real_download(tmp_path, *, urls, events, stdout_to_devnull=False):
    """A child interpreter running main() against the real YoutubeDL.

    The child registers the same fake site the in-process helper does and
    patches HttpFD.real_download to fire `events` progress events per URL, so
    a closed pipe is seen through yt-dlp's own exception wrapping. A URL whose
    id is "refused" fails extraction, so the child prints "Failed to
    download" for it. Bytecode writing is switched off first, the way
    conftest.py does for the test run itself, so the child does not leave a
    __pycache__/ at the repository root.
    """
    import subprocess

    root = Path(__file__).resolve().parent.parent
    child = f"""
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, {str(root)!r})
import types
import youtube_downloader as yd
import yt_dlp
from yt_dlp.downloader.http import HttpFD
from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.utils import ExtractorError

class FakeSiteIE(InfoExtractor):
    _VALID_URL = r"fake://(?P<id>\\w+)"
    def _real_extract(self, url):
        vid = self._match_id(url)
        if vid == "refused":
            raise ExtractorError("refused by the fake site", expected=True)
        fmt = {{"format_id": "f", "url": "https://example.invalid/x.mp4",
                "ext": "mp4", "protocol": "https", "vcodec": "avc1",
                "acodec": "mp4a", "height": 720}}
        return {{"id": vid, "title": "T", "formats": [fmt]}}

class RealYDLWithFakeSite(yt_dlp.YoutubeDL):
    def __init__(self, params):
        super().__init__(params, auto_init=False)
        self.add_info_extractor(FakeSiteIE())

def fake_real_download(self, filename, info_dict):
    for i in range({events!r}):
        self._hook_progress({{"status": "downloading", "downloaded_bytes": i,
                             "total_bytes": {events!r}, "filename": filename,
                             "tmpfilename": filename}}, info_dict)
    return True

HttpFD.real_download = fake_real_download
yd.yt_dlp = types.SimpleNamespace(YoutubeDL=RealYDLWithFakeSite, utils=yt_dlp.utils)
yd.shutil.which = lambda name: "/usr/bin/ffmpeg"
# The child's streams are pipes by construction; claim a terminal so the
# guard in _run() lets the closed-pipe path be exercised.
sys.stdout.isatty = lambda: True
sys.stderr.isatty = lambda: True
sys.argv = ["youtube-downloader", *{urls!r}, "-o", {str(tmp_path)!r}]
yd.main()
"""
    return subprocess.Popen(
        [sys.executable, "-c", child],
        stdout=subprocess.DEVNULL if stdout_to_devnull else subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


@pytest.mark.skipif(sys.platform != "win32", reason="EINVAL is the Windows spelling")
def test_windows_einval_from_this_tools_own_write_is_a_broken_pipe():
    """The mapping itself, in isolation: only EINVAL, only from our write."""
    import errno

    class Refuses:
        def write(self, text):
            raise OSError(errno.EINVAL, "Invalid argument")

        def flush(self):
            pass

    with pytest.raises(BrokenPipeError):
        yd._print("x", file=Refuses())

    class Disk:
        def write(self, text):
            raise OSError(errno.ENOSPC, "No space left on device")

        def flush(self):
            pass

    with pytest.raises(OSError) as excinfo:
        yd._print("x", file=Disk())
    assert not isinstance(excinfo.value, BrokenPipeError)


def test_a_pipe_closed_mid_download_exits_quietly(monkeypatch, tmp_path):
    """Regression: a closed reader was reported as a failed download.

    The BrokenPipeError from a progress line was caught by the per-attempt
    `except Exception`, so quitting a pager part-way through a run printed
    "Failed to download URL: [Errno 32] Broken pipe" and exited with "1
    download(s) failed." over a download that had succeeded - bypassing main()'s
    handler, whose stated purpose is exactly this case.

    Driven through the real YoutubeDL, and closing the pipe on the *second*
    progress write: the first has already opened the progress line, so the
    failure surfaces from the end-of-URL close() rather than from inside
    yt-dlp's wrapper - the other route a closed pipe can take.
    """
    events = [
        {"status": "downloading", "downloaded_bytes": n, "total_bytes": 3}
        for n in range(3)
    ]
    _real_ydl_with_a_fake_site(monkeypatch, events)
    # Four writes: "Fetching: ...", its newline, the first progress line and
    # its empty end="" write. The second progress write hits the closed pipe.
    err = _own_streams(monkeypatch, stdout=_ClosesAfter(4))
    monkeypatch.setattr(
        sys, "argv", ["youtube-downloader", "fake://abc", "-o", str(tmp_path)]
    )

    with pytest.raises(SystemExit) as excinfo:
        yd.main()
    assert excinfo.value.code == 1
    assert "Failed to download" not in err.getvalue()


def test_a_pipe_closed_on_the_announce_write_is_not_a_failed_download(
    monkeypatch, tmp_path
):
    """The third route a closed pipe takes: raw, out of the postprocessor hook.

    The real YoutubeDL wraps an OSError raised inside dl() - which is where
    the progress hooks run - but not one raised from a postprocessor hook, so
    the BrokenPipeError from the "Downloaded: ..." line reaches download()
    bare. Only the `except (BrokenPipeError, UnicodeEncodeError): raise`
    clause stands between it and the per-attempt `except Exception`, and
    until this test existed nothing in the suite raised through that clause:
    deleting it left every test green while the tool reported "1 download(s)
    failed." over a video that had been saved.
    """
    # No progress events: "Fetching: ..." and its newline succeed, and the
    # announcement is the write that hits the closed pipe.
    _real_ydl_with_a_fake_site(monkeypatch, [])
    err = _own_streams(monkeypatch, stdout=_ClosesAfter(2))
    monkeypatch.setattr(
        sys, "argv", ["youtube-downloader", "fake://abc", "-o", str(tmp_path)]
    )

    with pytest.raises(SystemExit) as excinfo:
        yd.main()
    assert excinfo.value.code == 1
    assert "Failed to download" not in err.getvalue()
    assert "download(s) failed" not in err.getvalue()


# --------------------------------------------------------------------------
# A broken pipe that is not ours
#
# The closed-pipe handling above exists for one event: the reader of this
# tool's stdout going away. A BrokenPipeError has a second, unrelated origin -
# the socket yt-dlp is talking to, when the site closes its connection - and
# yt-dlp chains that one into the DownloadError it reports exactly the way it
# chains this tool's own. The two must not be answered the same way.
# --------------------------------------------------------------------------


def _real_ydl_whose_site_drops_the_connection(monkeypatch):
    """The real YoutubeDL and a fake site that fails the way a socket does.

    Mirrors the chain yt-dlp builds for a network failure: the networking
    layer raises TransportError from the OSError (`raise TransportError(
    cause=e) from e` in networking/_urllib.py and _requests.py), and
    _request_webpage() raises ExtractorError(cause=err) inside the except
    that caught it. Every link is reachable from the DownloadError's
    exc_info through __cause__ or __context__, which is the walk
    _broken_pipe_inside() makes.
    """
    from yt_dlp.extractor.common import InfoExtractor
    from yt_dlp.networking.exceptions import TransportError
    from yt_dlp.utils import ExtractorError

    class DroppingSiteIE(InfoExtractor):
        _VALID_URL = r"fake://(?P<id>\w+)"

        def _real_extract(self, url):
            try:
                try:
                    raise BrokenPipeError(32, "Broken pipe")
                except OSError as socket_error:
                    raise TransportError(cause=socket_error) from socket_error
            except TransportError as err:
                # No "from": _request_webpage() does not use one either, so
                # the link is __context__, not __cause__ - and that is the
                # link the walk has to follow.
                raise ExtractorError(  # noqa: B904
                    f"Unable to download webpage: {err}", cause=err
                )

    class RealYDLWithDroppingSite(yt_dlp.YoutubeDL):
        def __init__(self, params):
            super().__init__(params, auto_init=False)
            self.add_info_extractor(DroppingSiteIE())

    monkeypatch.setattr(
        yd,
        "yt_dlp",
        types.SimpleNamespace(YoutubeDL=RealYDLWithDroppingSite, utils=yt_dlp.utils),
    )
    monkeypatch.setattr(yd.shutil, "which", lambda name: "/usr/bin/ffmpeg")


def test_a_pipe_broken_by_the_site_is_a_failed_download_not_a_closed_terminal(
    monkeypatch, tmp_path
):
    """Regression: a socket's BrokenPipeError was taken for the terminal's.

    _broken_pipe_inside() matched any BrokenPipeError in the chain, so a site
    closing its connection was handed to main()'s pipe handler, which pointed
    fd 1 and fd 2 at devnull and exited 1 - with no "Failed to download"
    line, no failure count, and nothing on either stream to say why. It is a
    download failure like any other and has to be reported as one.
    """
    _real_ydl_whose_site_drops_the_connection(monkeypatch)
    err = _own_streams(monkeypatch)
    monkeypatch.setattr(
        sys, "argv", ["youtube-downloader", "fake://abc", "-o", str(tmp_path)]
    )

    with pytest.raises(SystemExit) as excinfo:
        yd.main()
    assert "1 download(s) failed" in str(excinfo.value)
    assert "Failed to download fake://abc" in err.getvalue()
    assert "Broken pipe" in err.getvalue()


def test_only_this_tools_own_pipe_error_is_unwrapped():
    """The unwrapping keys on the class _print() raises, not on the errno.

    Same chain shape both times; only the origin of the innermost exception
    differs. The base BrokenPipeError is what a socket raises and must stay
    in the chain; the subclass is what _print() raises and must come out.
    """

    def chained(innermost):
        try:
            try:
                raise innermost
            except OSError as inner:
                raise RuntimeError("wrapped once") from inner
        except RuntimeError as once:
            try:
                raise ValueError("wrapped twice")
            except ValueError as twice:
                twice.exc_info = (type(once), once, None)
                return twice

    assert yd._broken_pipe_inside(chained(BrokenPipeError(32, "Broken pipe"))) is None

    own = yd._OwnWritePipeError(32, "Broken pipe")
    assert yd._broken_pipe_inside(chained(own)) is own

    # And _print() is where the subclass comes from, so a closed pipe under
    # one of this tool's writes always carries the tag.
    class ClosedPipe:
        def write(self, text):
            raise BrokenPipeError(32, "Broken pipe")

        def flush(self):
            pass

    with pytest.raises(yd._OwnWritePipeError):
        yd._print("x", file=ClosedPipe())
