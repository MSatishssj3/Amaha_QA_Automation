"""Trading-platform adapters. Add a broker = add one module implementing ``Platform``."""

from .base import Platform, PlatformError
from .registry import make_platform

__all__ = ["Platform", "PlatformError", "make_platform"]
