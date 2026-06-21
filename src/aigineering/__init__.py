"""Aigineering — Agent Runtime with structural hallucination containment."""

from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("aigineering")
except PackageNotFoundError:
    __version__ = "0.0.0-dev"
