"""Extract and compare ISRC codes from YouTube / YouTube Music metadata."""

from __future__ import annotations

import json
import re
from typing import Any, Literal, Optional

# CC-XXX-YY-NNNNN (with or without hyphens)
_ISRC_PATTERN = re.compile(
    r"\b([A-Z]{2}[A-Z0-9]{3}[- ]?\d{2}[- ]?\d{5})\b",
    re.IGNORECASE,
)

Source = Literal["youtube_music", "youtube"]


def normalize_isrc(isrc: str) -> str:
    """Normalize ISRC to uppercase without separators."""
    return re.sub(r"[^A-Z0-9]", "", isrc.upper())


def parse_isrc_from_text(text: str) -> Optional[str]:
    """Find the first ISRC-like code in free text."""
    match = _ISRC_PATTERN.search(text)
    if not match:
        return None
    return normalize_isrc(match.group(1))


def extract_isrc_from_ytdlp_info(info: dict[str, Any]) -> Optional[str]:
    """
    Read an ISRC from yt-dlp extract_info output when YouTube exposes it.

    Checks structured fields first, then scans description / synopsis text.
    """
    direct = info.get("isrc")
    if isinstance(direct, str) and direct.strip():
        return normalize_isrc(direct)

    external_ids = info.get("external_ids") or {}
    if isinstance(external_ids, dict):
        nested = external_ids.get("isrc")
        if isinstance(nested, str) and nested.strip():
            return normalize_isrc(nested)

    for text_field in ("description", "synopsis", "comment", "title", "track"):
        value = info.get(text_field)
        if isinstance(value, str):
            parsed = parse_isrc_from_text(value)
            if parsed:
                return parsed

    return None


def extract_isrc_from_ytmusicapi_song(video_id: str) -> Optional[str]:
    """
    YouTube Music does not surface ISRC in normal song metadata, but the full
    API payload is scanned in case it appears in nested fields.
    """
    try:
        from ytmusicapi import YTMusic
    except ImportError:
        return None

    try:
        song = YTMusic().get_song(video_id)
    except Exception:
        return None

    return parse_isrc_from_text(json.dumps(song, default=str))


def extract_isrc_for_video(
    video_id: str,
    source: Source,
    ytdlp_info: dict[str, Any],
) -> Optional[str]:
    """
    Best-effort ISRC extraction: yt-dlp fields first, then YouTube Music API.
    """
    isrc = extract_isrc_from_ytdlp_info(ytdlp_info)
    if isrc:
        return isrc

    if source == "youtube_music":
        return extract_isrc_from_ytmusicapi_song(video_id)

    return None


def lookup_video_id_by_isrc_on_youtube_music(spotify_isrc: str) -> Optional[str]:
    """
    YouTube Music indexes recordings by ISRC internally.

    Searching songs by ISRC returns the matching ``videoId`` when one exists.
    """
    try:
        from ytmusicapi import YTMusic
    except ImportError:
        return None

    normalized = normalize_isrc(spotify_isrc)
    try:
        results = YTMusic().search(normalized, filter="songs")
    except Exception:
        return None

    if not results:
        return None

    video_id = results[0].get("videoId")
    return video_id if video_id else None


def lookup_video_id_by_isrc_on_youtube(
    spotify_isrc: str,
    *,
    search_limit: int = 5,
) -> Optional[str]:
    """
    Fallback: search regular YouTube for the ISRC string and confirm via metadata.
    """
    import yt_dlp

    normalized = normalize_isrc(spotify_isrc)
    search_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
    }
    metadata_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }

    try:
        with yt_dlp.YoutubeDL(search_opts) as ydl:
            info = ydl.extract_info(
                f"ytsearch{search_limit}:{normalized}", download=False
            )
        entries = info.get("entries") or []
    except Exception:
        return None

    with yt_dlp.YoutubeDL(metadata_opts) as ydl:
        for entry in entries:
            video_id = entry.get("id")
            if not video_id:
                continue
            try:
                full = ydl.extract_info(
                    f"https://www.youtube.com/watch?v={video_id}",
                    download=False,
                )
            except Exception:
                continue
            if full and is_direct_isrc_match(
                spotify_isrc, extract_isrc_from_ytdlp_info(full)
            ):
                return video_id

    return None


def find_video_by_isrc_search(spotify_isrc: str) -> Optional[tuple[str, Source]]:
    """
    Resolve a direct ISRC hit via platform search (YouTube Music, then YouTube).
    """
    video_id = lookup_video_id_by_isrc_on_youtube_music(spotify_isrc)
    if video_id:
        return video_id, "youtube_music"

    video_id = lookup_video_id_by_isrc_on_youtube(spotify_isrc)
    if video_id:
        return video_id, "youtube"

    return None


def is_direct_isrc_match(spotify_isrc: str, candidate_isrc: Optional[str]) -> bool:
    """Return True when both ISRCs are present and identical."""
    if not spotify_isrc or not candidate_isrc:
        return False
    return normalize_isrc(spotify_isrc) == normalize_isrc(candidate_isrc)
