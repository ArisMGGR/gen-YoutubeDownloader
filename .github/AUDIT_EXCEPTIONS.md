# Audit exceptions

Things in this repository that reliably trip linters, secret scanners, and
code review without being problems. Each entry says what fires, why it is
deliberate, and — the part that matters — the condition that would make it a
real finding again.

If a check fires on something listed here, read the entry rather than
re-investigating. If you are accepting a *new* finding, add an entry, and only
after actually looking into it. An entry with no "when this becomes a real
finding" line is not an entry; it is a dismissal.

## AX-01 — A GUID that looks like a leaked secret

**Fires on:** `youtube_downloader.py`, `_DOWNLOADS_FOLDER_GUID =
"{374DE290-123F-4565-9164-39C4925E467B}"`. Entropy-based secret scanners flag
the shape.

**Why it is fine:** that is the Windows KNOWNFOLDERID for the Downloads
folder. It is a public, documented, Microsoft-assigned constant — the same
value in every Windows installation on earth — and it is passed to
`SHGetKnownFolderPath` to ask the OS where Downloads lives. It grants nothing
and identifies nobody.

**When this becomes a real finding:** if a GUID-shaped or key-shaped literal
appears anywhere in this repository that is *not* this constant. Check the
value against Microsoft's KNOWNFOLDERID list before assuming a new one is
equally harmless.

## AX-02 — `yt-dlp` is deliberately not pinned for users

**Fires on:** dependency scanners and reviewers who expect a lockfile.
`pyproject.toml` declares `yt-dlp[default]>=2025.3.21` — an open upper bound — and
`requirements.txt` inherits it.

**Why it is fine:** YouTube changes break yt-dlp regularly, and the documented
fix is `pip install -U "yt-dlp[default]"`. Pinning users to a snapshot would walk them
straight into the failure mode the README warns about, and there would be no
way out that did not involve editing this repository. CI *is* pinned, in
`tests/constraints.txt`, so a red build means this repository broke rather than
something upstream changing overnight; Dependabot bumps that pin weekly and
its PR runs the full suite against the new version.

**When this becomes a real finding:** if a yt-dlp release ever ships a
breaking API change to something this module reaches into. Today the module
touches exactly three yt-dlp attributes — `yt_dlp.YoutubeDL`,
`yt_dlp.utils.DownloadError` (on which the whole retry ladder pivots) and
`yt_dlp.version.__version__` (which `-V` prints and the bug report template
asks for). It also passes yt-dlp these option names, which yt-dlp silently
ignores if it no longer knows them: `format`, `outtmpl`, `noplaylist`,
`playlist_items`, `quiet`, `no_warnings`, `noprogress`, `logger`,
`merge_output_format`, `postprocessors`, `progress_hooks`,
`postprocessor_hooks`, `extractor_args`, `writesubtitles`,
`writeautomaticsub` and `subtitleslangs`. And it names two postprocessors by
key: `FFmpegExtractAudio`, which `--audio-only` asks for, and `MoveFiles`,
which the announce hook listens for. The floor in `pyproject.toml` would then
need raising. The three tests written specifically to pin these names against
the real yt-dlp (`test_the_announce_hook_postprocessor_name_still_exists`,
`test_the_audio_extraction_postprocessor_key_still_resolves` and
`test_every_configured_player_client_is_known_to_yt_dlp`) are what should
catch such a rename first; `test_move_files_key_matches_yt_dlp`,
`test_selectors_resolve_against_real_formats`, the two `test_real_yt_dlp_*`
tests, `test_a_percent_sign_in_the_output_directory_is_not_expanded` and the
closed-pipe tests built on `_real_ydl_with_a_fake_site()` and
`_spawn_real_download()` also run against the real yt-dlp and would likely
fail alongside them.

It has already happened once, which is why the floor is where it is. The floor
is *not* just "old enough to work" — it is the version that satisfies every
yt-dlp internal this repository touches, tests included. `2025.3.21` is the
first release in which `yt_dlp/extractor/youtube` is a package rather than a
single module, so it is the first in which
`from yt_dlp.extractor.youtube._base import INNERTUBE_CLIENTS` resolves. Raising
this floor is therefore a real change with a real reason, not paperwork: if you
add an import of a yt-dlp internal, check it against the floor and raise the
floor rather than assuming a recent release.

## AX-03 — `requirements.txt` whose only line is `.`

**Fires on:** tooling that expects a requirements file to list packages, and
on reviewers who read it as an empty or broken file.

**Why it is fine:** `pyproject.toml` is the single source of truth for
dependencies. `requirements.txt` contains `.` and `tests/requirements-dev.txt`
contains `-c constraints.txt` plus `-e .[dev]` (editable, so the dev
environment's `youtube-downloader` command tracks the checkout), so both
delegate to it and the two can never drift out of agreement with the package
metadata. Both files
document, in their own comments, that pip resolves the bare `.` against the
current working directory, so they must be run from the repository root; the
`-c` path is resolved relative to `requirements-dev.txt` itself, which is why
both dev files can live together under `tests/`.

**When this becomes a real finding:** if either file ever gains a package name
of its own. At that point there are two sources of truth and they will drift.

## AX-04 — A `noqa` that suppresses the check it is demonstrating

**Fires on:** `ruff`, and on reviewers who read `# noqa` as a shortcut.
`youtube_downloader.py` carries `if sys.version_info < (3, 10):  # noqa: UP036`.

**Why it is fine:** UP036 flags version checks that are outdated relative to
the declared `requires-python`, which is exactly what this line looks like.
The check *is* the point: it exists so that Python 3.8 or 3.9 gets one clear
sentence instead of a confusing failure several imports later. It cannot help
anything older: the module uses an assignment expression, which is 3.8 syntax,
so 3.7 and below fail to compile the file before the guard runs (and pip
refuses to install it there, because of `requires-python`).
`test_the_version_guard_is_reachable_on_3_8` parses the module with the 3.8
grammar, so syntax newer than that cannot slip in and silently move the guard
out of reach for 3.8 and 3.9 as well. Note the comment above it — do not spell a bare `noqa` marker anywhere
in surrounding prose, because ruff parses one at the start of a comment as a
blanket directive.

**When this becomes a real finding:** if the version in the guard and
`requires-python` in `pyproject.toml` ever disagree. They are two spellings of
the same number and nothing enforces that they match.
