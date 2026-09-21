"""Stress test backend registry."""

from __future__ import annotations

import importlib
import pkgutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from corecycler.engine.backends.base import StressBackend

BACKEND_REGISTRY: dict[str, type[StressBackend]] = {}


def register_backend(name: str):
    """Decorator to register a backend class by display name."""

    def decorator(cls):
        BACKEND_REGISTRY[name] = cls
        return cls

    return decorator


def get_backend(name: str) -> StressBackend:
    """Instantiate a backend by display name. Raises KeyError if unknown."""
    return BACKEND_REGISTRY[name]()


def available_backends() -> list[str]:
    """Return display names of all registered backends."""
    return list(BACKEND_REGISTRY)


def load_all() -> None:
    """Discover backend modules; their decorators populate the registry."""
    for module in pkgutil.iter_modules(__path__):
        if module.name != "base" and not module.name.startswith("_"):
            importlib.import_module(f"{__name__}.{module.name}")


load_all()
