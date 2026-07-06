"""Directory-scoped duplicate detection (ISRC -> metadata -> audio)."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Literal, Optional

import db
from isrc_match import normalize_isrc
from models import DuplicateConfig, DuplicatePolicy, DuplicateResult, TrackIdentity
from search_candidates import _normalize_text, _token_overlap

_DURATION_PREFILTER_SECONDS = 5


def _library_rows(library_id: int) -> list:
    conn = db.get_connection()
    return conn.execute(
        """
        SELECT t.id AS track_id, t.isrc, t.title_norm, t.artist_norm,
               t.duration_seconds, lt.local_path
        FROM library_tracks lt
        JOIN tracks t ON t.id = lt.track_id
        WHERE lt.library_id = ?
        """,
        (library_id,),
    ).fetchall()


def _title_similarity(left: str, right: str) -> float:
    left_norm = _normalize_text(left)
    right_norm = _normalize_text(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    if left_norm in right_norm or right_norm in left_norm:
        return 0.9
    return _token_overlap(left_norm, right_norm)


def _duration_similarity(left: int, right: int) -> float:
    diff = abs(left - right)
    if diff <= 2:
        return 1.0
    if diff <= _DURATION_PREFILTER_SECONDS:
        return 0.8
    return 0.0


def _metadata_score(identity: TrackIdentity, row) -> float:
    """0-100 similarity score from title, artist, and duration."""
    title_score = _title_similarity(identity.title, row["title_norm"] or "")
    artist_score = _title_similarity(identity.artist, row["artist_norm"] or "")
    duration_score = _duration_similarity(
        identity.duration_seconds, row["duration_seconds"] or 0
    )
    return (title_score * 45 + artist_score * 45 + duration_score * 10)


def _audio_similarity_to_existing(
    identity: TrackIdentity, existing_path: Path
) -> Optional[float]:
    """
    Chromaprint fingerprint compare between the Spotify preview middle clip
    and the middle clip of an existing local file. Best-effort: returns None
    when previews/tools are unavailable.
    """
    if not identity.spotify_track_id or not existing_path.exists():
        return None
    try:
        from audio_segments import (
            extract_middle_segment,
            prepare_spotify_preview_middle_clip,
        )
        from audio_similarity import (
            CHROMAPRINT_MIDDLE_SECONDS,
            _fingerprint_similarity,
        )
        from get_content import get_spotify_preview_url

        preview_url = get_spotify_preview_url(identity.spotify_track_id)
        if not preview_url:
            return None

        with tempfile.TemporaryDirectory(prefix="spotify_sync_dup_") as tmp:
            temp_dir = Path(tmp)
            reference = prepare_spotify_preview_middle_clip(
                preview_url,
                window_seconds=CHROMAPRINT_MIDDLE_SECONDS,
                output_path=temp_dir / "reference.wav",
            )
            existing_clip = extract_middle_segment(
                existing_path,
                temp_dir / "existing.wav",
                duration_seconds=float(identity.duration_seconds),
                window_seconds=CHROMAPRINT_MIDDLE_SECONDS,
            )
            return _fingerprint_similarity(reference, existing_clip)
    except Exception:
        return None


def find_duplicate_in_library(
    library_id: int,
    identity: TrackIdentity,
    config: DuplicateConfig,
) -> Optional[DuplicateResult]:
    """Check only tracks already present in this library. Cheap checks first."""
    rows = _library_rows(library_id)
    if not rows:
        return None

    if config.check_isrc and identity.isrc:
        target = normalize_isrc(identity.isrc)
        for row in rows:
            if row["isrc"] and normalize_isrc(row["isrc"]) == target:
                return DuplicateResult(
                    existing_track_id=row["track_id"],
                    existing_local_path=row["local_path"],
                    method="isrc",
                    confidence="exact",
                    score=None,
                )

    duration_matches = [
        row
        for row in rows
        if abs((row["duration_seconds"] or 0) - identity.duration_seconds)
        <= _DURATION_PREFILTER_SECONDS
    ]

    if config.check_metadata:
        best_row = None
        best_score = 0.0
        for row in duration_matches:
            score = _metadata_score(identity, row)
            if score > best_score:
                best_score = score
                best_row = row
        if best_row is not None and best_score >= config.metadata_threshold:
            return DuplicateResult(
                existing_track_id=best_row["track_id"],
                existing_local_path=best_row["local_path"],
                method="metadata",
                confidence="high",
                score=round(best_score, 2),
            )

    if config.check_audio:
        for row in duration_matches:
            similarity = _audio_similarity_to_existing(
                identity, Path(row["local_path"])
            )
            if similarity is None:
                continue
            if similarity >= config.audio_duplicate_threshold:
                return DuplicateResult(
                    existing_track_id=row["track_id"],
                    existing_local_path=row["local_path"],
                    method="audio",
                    confidence="high",
                    score=round(similarity, 4),
                )
            if similarity >= config.audio_review_threshold:
                return DuplicateResult(
                    existing_track_id=row["track_id"],
                    existing_local_path=row["local_path"],
                    method="audio",
                    confidence="review",
                    score=round(similarity, 4),
                )

    return None


def apply_duplicate_policy(
    result: DuplicateResult,
    policy: DuplicatePolicy,
    *,
    json_mode: bool,
) -> Literal["skip", "proceed", "needs_user_choice"]:
    if policy == "skip":
        return "skip"
    if policy in ("replace", "keep_both"):
        return "proceed"
    # policy == "ask"
    if json_mode:
        return "needs_user_choice"
    from output import prompt_duplicate_choice

    choice = prompt_duplicate_choice()
    if choice == "skip":
        return "skip"
    return "proceed"
