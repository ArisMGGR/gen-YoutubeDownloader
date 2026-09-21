"""Test-session setup: keep test runs from writing outside tests/.

The tool caches are pointed at tests/ from pyproject.toml (pytest's
`cache_dir`, mypy's `cache_dir`, ruff's `cache-dir`). Bytecode is the one
thing no config option redirects: importing youtube_downloader from the
repository root would write a __pycache__/ next to it. pytest loads this file
before it imports any test module, so switching bytecode writing off here
covers that import. The cost is recompiling one module per run, which is
not measurable.
"""

import sys

sys.dont_write_bytecode = True
