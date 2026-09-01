"""Input validation and OSM-dump format detection.

Format detection is filename-based (by suffix) and does not inspect file
contents. Compound OSM suffixes (for example ``.osm.pbf``) are matched before
the bare fallbacks (``.osm`` / ``.pbf``) so a file such as ``italy.osm.pbf`` is
detected as the compound format rather than the bare ``.pbf``.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# Suffixes libosmium can read, cross-checked here as the set of accepted
# input formats.
SUPPORTED_SUFFIXES: set[str] = {
    ".osm",
    ".pbf",
    ".osm.pbf",
    ".osm.xml",
    ".osm.bz2",
    ".osm.gz",
}

# Ordered longest-first so compound suffixes win over the bare fallbacks.
_DETECTION_ORDER: tuple[str, ...] = (
    ".osm.pbf",
    ".osm.xml",
    ".osm.bz2",
    ".osm.gz",
    ".osm",
    ".pbf",
)


class SupportedFormat(Enum):
    """A detected, supported OSM serialization format.

    The value is the (possibly compound) filename suffix used to detect it.
    """

    OSM = ".osm"
    PBF = ".pbf"
    OSM_PBF = ".osm.pbf"
    OSM_XML = ".osm.xml"
    OSM_BZ2 = ".osm.bz2"
    OSM_GZ = ".osm.gz"

    @property
    def suffix(self) -> str:
        """The filename suffix associated with this format."""
        return self.value


class InputError(Exception):
    """Base for fatal input errors; carries an exit-worthy message."""


class FileNotFoundInputError(InputError):
    """The supplied path does not identify an existing file."""


class UnsupportedFormatError(InputError):
    """The supplied file is in an unsupported format."""


def detect_format(path: Path) -> SupportedFormat | None:
    """Detect the OSM format of ``path`` from its filename suffix.

    Compound suffixes are matched before bare ones. Returns ``None`` when no
    supported suffix matches. Detection is case-insensitive on the suffix.
    """
    name = path.name.lower()
    for suffix in _DETECTION_ORDER:
        if name.endswith(suffix):
            return SupportedFormat(suffix)
    return None


def validate_input(path: Path) -> SupportedFormat:
    """Validate an input OSM dump path and return its detected format.

    Raises:
        FileNotFoundInputError: when ``path`` is not an existing file.
        UnsupportedFormatError: when the format is not supported.

    Returns the detected :class:`SupportedFormat` for a valid input.
    """
    if not path.is_file():
        raise FileNotFoundInputError(f"Input file not found: {path}")

    fmt = detect_format(path)
    if fmt is None:
        supported = ", ".join(sorted(SUPPORTED_SUFFIXES))
        raise UnsupportedFormatError(
            f"Unsupported input format for {path}; supported formats: {supported}"
        )

    return fmt
