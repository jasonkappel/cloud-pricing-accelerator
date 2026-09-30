"""The harvester's publish contract, shared so the API builds exactly the artifact the harvester verifies.

The deployed API package carries the ``harvester`` package next to ``app``; a source checkout finds it in
``src/``.
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    from harvester.core import PublicationError, ValidationError
    from harvester.snapshot import (
        build_published_manifest,
        published_header,
        unwrap_approval,
        verify_published_manifest,
    )
except ModuleNotFoundError:
    sys.path.append(str(Path(__file__).resolve().parents[2]))
    from harvester.core import PublicationError, ValidationError
    from harvester.snapshot import (
        build_published_manifest,
        published_header,
        unwrap_approval,
        verify_published_manifest,
    )

__all__ = [
    "PublicationError",
    "ValidationError",
    "build_published_manifest",
    "published_header",
    "unwrap_approval",
    "verify_published_manifest",
]
