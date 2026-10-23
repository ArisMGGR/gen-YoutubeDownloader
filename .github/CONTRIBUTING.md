# Contributing

Thanks for taking a look. This is a small, single-module tool; changes that
keep it small are the ones most likely to be merged.

## Before you open an issue

Most "it stopped working" reports are an outdated yt-dlp rather than a bug in
this wrapper. YouTube changes things often, and yt-dlp ships fixes quickly:

```bash
pip install -U "yt-dlp[default]"
```

If that fixes it, there is nothing to report here. If it doesn't, please open
a bug report and include your yt-dlp version.

## Development setup

```bash
python -m venv tests/.venv
source tests/.venv/bin/activate        # Windows: tests\.venv\Scripts\activate
pip install -r tests/requirements-dev.txt
```

That is an editable install: the `youtube-downloader` command in the
environment runs the checkout's `youtube_downloader.py`, so an edit is live
without reinstalling. (A plain `pip install .` copies the module into
site-packages instead, and the command would keep running that copy after
every edit.)

`tests/requirements-dev.txt` applies `tests/constraints.txt`, which pins yt-dlp *and* the
lint, type-check, and test tools for development and CI. That way a red build
means this repository broke rather than something upstream changed overnight —
`ruff format --check` in particular changes behaviour between ruff releases,
so an unpinned ruff can turn the build red on its own. Dependabot bumps the
pins weekly and its PR runs the suite against the new versions. End users are
deliberately left unpinned — `pip install .` and `requirements.txt` resolve
yt-dlp freely, because `pip install -U "yt-dlp[default]"` is the documented fix when
downloads break.

## Before you open a pull request

Run the same checks CI runs:

```bash
ruff check .
ruff format --check .
mypy
pytest -q
```

`mypy` takes no arguments: `[tool.mypy]` in `pyproject.toml` names the files to
check and pins the Python version and platform, so the result is the same
wherever you run it. It checks `youtube_downloader.py` strictly — every
function annotated, no bare generics — and the tests loosely, since annotating
every `def test_...` would add noise without catching anything.

CI runs these on Python 3.11 through 3.14 (plus one Windows and one macOS
job, which cover the platform-specific Downloads-folder lookup). The tool
supports Python 3.11, so avoid syntax newer than that — `from __future__
import annotations` is already imported, so modern type-annotation syntax is
fine in annotations.

The floor is 3.11 because yt-dlp deprecated Python 3.10 (which reached end of
life in October 2026) and prints a "Deprecated Feature" warning on every run
there, and it dropped Python 3.9 outright. On 3.9, pip silently resolves to a
long-outdated yt-dlp that YouTube changes have already broken, and
`pip install -U "yt-dlp[default]"` cannot fix it — the same dead end awaits
3.10 once yt-dlp removes it, so claiming support for either would mislead
users.

## Testing notes

The tests run entirely offline. `tests/test_youtube_downloader.py` replaces
`youtube_downloader.yt_dlp` with a stub, so the retry-and-fallback logic can be
driven deterministically without touching the network. Please don't add tests
that download real videos.

If you fix a bug, add a test that fails without your fix — and check that it
does fail, not just that it passes afterwards.

The suite is offline and fast, so there is no reason to skip it locally.

## Audit exceptions

A handful of things in this repository reliably trip linters, scanners, and
code review without being problems — a Windows KNOWNFOLDERID that looks like a
secret, a deliberately unpinned `yt-dlp`, a `requirements.txt` whose only line
is `.`. Each one is written up in
[AUDIT_EXCEPTIONS.md](AUDIT_EXCEPTIONS.md) with the reasoning and a condition
that would make it a real finding again.

If a check fires on something listed there, read the entry rather than
re-investigating. If you are accepting a *new* finding, add an entry — and only
after actually looking into it.

## Code of conduct

Participation is governed by the [Code of Conduct](CODE_OF_CONDUCT.md).

## Scope

This is a thin CLI over yt-dlp. Features that belong in yt-dlp itself are
better contributed there. Please keep in mind the legal disclaimer in the
README: this tool is for personal and educational use.
