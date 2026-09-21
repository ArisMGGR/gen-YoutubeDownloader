# Security Policy

## Supported versions

This project is a single-file CLI with no release branches. Only the latest
commit on `main` is supported.

## Reporting a vulnerability

Please **do not** open a public issue for a security problem.

Use GitHub's private vulnerability reporting instead: go to the
[Security tab](https://github.com/ArisMGGR/gen-YoutubeDownloader/security)
and choose **Report a vulnerability**. That opens a private channel with the
maintainer.

Please include what an attacker could do, the steps to reproduce it, and the
versions of this tool, yt-dlp, and Python you tested against.

## Scope

This tool takes URLs from the command line and passes them to
[yt-dlp](https://github.com/yt-dlp/yt-dlp), which performs the actual network
and media handling. Vulnerabilities in yt-dlp itself should be reported to
that project. Issues in *this* repository's own code — argument handling,
filesystem paths, subprocess use, and the player-client fallback logic — are
in scope here.
