"""The package names its own build, and says so honestly when it cannot."""

from __future__ import annotations

import importlib
import importlib.metadata

import corecycler


def test_an_installed_build_reports_its_metadata_version(monkeypatch):
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "9.9.9+gabc1234")
    try:
        importlib.reload(corecycler)
        assert corecycler.__version__ == "9.9.9+gabc1234"
    finally:
        monkeypatch.undo()
        importlib.reload(corecycler)


def test_an_uninstalled_checkout_reports_unknown(monkeypatch):
    def missing(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", missing)
    try:
        importlib.reload(corecycler)
        assert corecycler.__version__ == "0+unknown"
    finally:
        monkeypatch.undo()
        importlib.reload(corecycler)
