"""Audio similarity fallback — chromaprint / embeddings (not yet implemented)."""

from __future__ import annotations

from pathlib import Path
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from search_candidates import RankedCandidate
    from get_content import TrackInfo


def resolve_by_audio_similarity(
    track: TrackInfo,
    candidates: list[RankedCandidate],
    save_directory: str | Path,
) -> Optional[Path]:
    """
    Pick the best candidate using chromaprint and/or embedding similarity.

    Planned for when no ISRC direct match is found among searched candidates.
    """
    raise NotImplementedError(
        "Audio similarity resolution (chromaprint / embeddings) is not implemented yet."
    )
