from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("corecycler")
except PackageNotFoundError:
    # A source checkout that was never installed: nothing on sys.path carries
    # dist-info, so the build cannot name itself.
    __version__ = "0+unknown"
