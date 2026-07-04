"""Extract and compare ISRC codes from YouTube / yt-dlp metadata."""

from __future__ import annotations

import re
from typing import Any, Optional

# CC-XXX-YY-NNNNN (with or without hyphens)
_ISRC_PATTERN = re.compile(
    r"\b([A-Z]{2}[A-Z0-9]{3}[- ]?\d{2}[- ]?\d{5})\b",
    re.IGNORECASE,
)


def normalize_isrc(isrc: str) -> str:
    """Normalize ISRC to uppercase without separators."""
    return re.sub(r"[^A-Z0-9]", "", isrc.upper())


def extract_isrc_from_ytdlp_info(info: dict[str, Any]) -> Optional[str]:
    """
    Attempt to read an ISRC from yt-dlp extract_info output.

    Checks structured fields first, then scans description text.
    """
    direct = info.get("isrc")
    if isinstance(direct, str) and direct.strip():
        return normalize_isrc(direct)

    external_ids = info.get("external_ids") or {}
    if isinstance(external_ids, dict):
        nested = external_ids.get("isrc")
        if isinstance(nested, str) and nested.strip():
            return normalize_isrc(nested)

    for text_field in ("description", "synopsis", "comment"):
        value = info.get(text_field)
        if isinstance(value, str):
            parsed = parse_isrc_from_text(value)
            if parsed:
                return parsed

    return None


def parse_isrc_from_text(text: str) -> Optional[str]:
    """Find the first ISRC-like code in free text."""
    match = _ISRC_PATTERN.search(text)
    if not match:
        return None
    return normalize_isrc(match.group(1))


def is_direct_isrc_match(spotify_isrc: str, candidate_isrc: Optional[str]) -> bool:
    """Return True when both ISRCs are present and identical."""
    if not spotify_isrc or not candidate_isrc:
        return False
    return normalize_isrc(spotify_isrc) == normalize_isrc(candidate_isrc)
