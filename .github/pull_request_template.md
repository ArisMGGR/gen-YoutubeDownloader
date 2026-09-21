<!--
Thanks for sending a patch. This is a thin CLI over yt-dlp, so changes that
keep it small are the ones most likely to be merged - see CONTRIBUTING.md.
-->

## What this changes

<!-- What the tool does differently afterwards, from a user's point of view. -->

## Why

<!-- The situation you hit. Link an issue if there is one. -->

## Checks

- [ ] `ruff check .`
- [ ] `ruff format --check .`
- [ ] `mypy`
- [ ] `pytest -q`
- [ ] Bug fix only: a test that fails without the fix, and I checked that it
      does fail, not just that it passes afterwards.
- [ ] User-visible change: an entry in `CHANGELOG.md` under `Unreleased`.
      (Internal refactors, test-only changes, and pin bumps do not need one.)

## Scope

- [ ] This is about the CLI wrapper, not about yt-dlp's extraction or download
      behaviour. Anything to do with how videos are extracted, which formats
      exist, or how downloads are performed belongs upstream in yt-dlp.
- [ ] No test in this PR downloads a real video. The suite runs offline.
