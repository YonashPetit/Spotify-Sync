"""Directory-scoped duplicate detection (non-recursive folder scan)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, Optional

from chromaprint_engine import (
    compare_fingerprints,
    fingerprint_local_file,
    fingerprint_spotify_preview,
)
from isrc_match import normalize_isrc
from matching_settings import get_chromaprint_strategy, load_matching_settings
from models import DuplicateConfig, DuplicatePolicy, DuplicateResult, TrackIdentity
from tracks import iter_audio_files, read_file_isrc

_LEGACY_ISRC_STEM_RE = re.compile(r"^([A-Z0-9]{12})(?:_|$)")


def _isrc_from_filename(path: Path) -> Optional[str]:
    match = _LEGACY_ISRC_STEM_RE.match(path.stem.upper())
    return normalize_isrc(match.group(1)) if match else None


def _file_matches_isrc(path: Path, target_isrc: str) -> bool:
    file_isrc = read_file_isrc(path) or _isrc_from_filename(path)
    return bool(file_isrc and normalize_isrc(file_isrc) == target_isrc)


def _file_matches_youtube_id(path: Path, video_id: str) -> bool:
    stem = path.stem
    return stem == video_id or stem.endswith(f"_{video_id}")


def _chromaprint_similarity_to_existing(
    identity: TrackIdentity, existing_path: Path
) -> Optional[float]:
    if not identity.spotify_track_id or not existing_path.exists():
        return None
    try:
        global_settings = load_matching_settings()
        strategy = get_chromaprint_strategy()
        spotify_fp = fingerprint_spotify_preview(identity.spotify_track_id)
        local_fp = fingerprint_local_file(str(existing_path))
        score, _matched = compare_fingerprints(
            spotify_fp,
            local_fp,
            strategy=strategy,
            threshold=global_settings.chromaprint_match_certainty,
        )
        return score
    except Exception:
        return None


def _embedding_similarity_to_existing(
    identity: TrackIdentity, existing_path: Path
) -> Optional[float]:
    if not identity.spotify_track_id or not existing_path.exists():
        return None
    try:
        import tempfile

        from audio_segments import (
            extract_middle_segment,
            prepare_spotify_preview_middle_clip,
        )
        from audio_similarity import EMBEDDING_MIDDLE_SECONDS, embedding_similarity
        from get_content import get_spotify_preview_url

        preview_url = get_spotify_preview_url(identity.spotify_track_id)
        if not preview_url:
            return None

        with tempfile.TemporaryDirectory(prefix="spotify_sync_dup_emb_") as tmp:
            temp_dir = Path(tmp)
            reference = prepare_spotify_preview_middle_clip(
                preview_url,
                window_seconds=EMBEDDING_MIDDLE_SECONDS,
                output_path=temp_dir / "reference.wav",
            )
            existing_clip = extract_middle_segment(
                existing_path,
                temp_dir / "existing.wav",
                duration_seconds=float(identity.duration_seconds),
                window_seconds=EMBEDDING_MIDDLE_SECONDS,
            )
            return embedding_similarity(reference, existing_clip)
    except Exception:
        return None


def _audio_similarity_to_existing(
    identity: TrackIdentity, existing_path: Path
) -> Optional[float]:
    """Best similarity from enabled duplicate-phase audio matchers."""
    global_settings = load_matching_settings()
    scores: list[float] = []
    if global_settings.duplicate_chromaprint:
        score = _chromaprint_similarity_to_existing(identity, existing_path)
        if score is not None:
            scores.append(score)
    if global_settings.duplicate_embedding:
        score = _embedding_similarity_to_existing(identity, existing_path)
        if score is not None:
            scores.append(score)
    return max(scores) if scores else None


def find_duplicate_in_directory(
    directory: Path,
    identity: TrackIdentity,
    config: DuplicateConfig,
    *,
    track_id: int = 0,
) -> Optional[DuplicateResult]:
    """
    Scan *directory* only (non-recursive) for an existing copy of *identity*.

    Uses ISRC (tags or legacy filename), YouTube id filename patterns, and
    optional chromaprint / embedding similarity (global toggles).
    """
    if not directory.is_dir():
        return None

    global_settings = load_matching_settings()

    audio_files = list(iter_audio_files(directory))
    if not audio_files:
        return None

    if config.check_isrc and identity.isrc:
        target = normalize_isrc(identity.isrc)
        for path in audio_files:
            if _file_matches_isrc(path, target):
                return DuplicateResult(
                    existing_track_id=track_id,
                    existing_local_path=str(path),
                    method="isrc",
                    confidence="exact",
                    score=None,
                )

    if identity.youtube_video_id:
        for path in audio_files:
            if _file_matches_youtube_id(path, identity.youtube_video_id):
                return DuplicateResult(
                    existing_track_id=track_id,
                    existing_local_path=str(path),
                    method="path",
                    confidence="exact",
                    score=None,
                )

    dup_threshold = global_settings.audio_duplicate_threshold
    review_threshold = global_settings.audio_review_threshold

    if global_settings.duplicate_audio_enabled():
        for path in audio_files:
            similarity = _audio_similarity_to_existing(identity, path)
            if similarity is None:
                continue
            if similarity >= dup_threshold:
                return DuplicateResult(
                    existing_track_id=track_id,
                    existing_local_path=str(path),
                    method="audio",
                    confidence="high",
                    score=round(similarity, 4),
                )
            if similarity >= review_threshold:
                return DuplicateResult(
                    existing_track_id=track_id,
                    existing_local_path=str(path),
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
    if json_mode:
        return "needs_user_choice"
    from output import prompt_duplicate_choice

    choice = prompt_duplicate_choice()
    if choice == "skip":
        return "skip"
    return "proceed"
