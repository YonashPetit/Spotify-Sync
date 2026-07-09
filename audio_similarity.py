"""Chromaprint (AcoustID) and embedding audio matching fallbacks."""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import numpy as np

from audio_segments import prepare_candidate_middle_clip, prepare_spotify_preview_middle_clip
from download_audio import build_track_filename, download_audio
from get_content import get_spotify_preview_url
from matching_settings import load_matching_settings

if TYPE_CHECKING:
    from get_content import TrackInfo
    from search_candidates import RankedCandidate

# --- toggles and tunables ---
ENABLE_CHROMAPRINT_MATCH = True
ENABLE_EMBEDDING_MATCH = True

# Middle segment length (seconds). Always centered on the track / preview.
CHROMAPRINT_MIDDLE_SECONDS = 20.0  # usable range ~10–30
EMBEDDING_MIDDLE_SECONDS = 20.0

# Stop immediately and download when certainty >= this value (0–1).
AUDIO_MATCH_CERTAINTY = 0.90

# If the first candidate is below certainty, try at most this many total.
MAX_AUDIO_MATCH_ATTEMPTS = 3

# Minimum embedding cosine similarity treated as a "very close" match.
EMBEDDING_MATCH_THRESHOLD = 0.90


@dataclass
class AudioMatchResult:
    matched: bool
    certainty: float
    method: str
    video_id: str
    downloaded_path: Optional[Path] = None


def _clamp_chromaprint_window(seconds: float) -> float:
    return max(10.0, min(30.0, seconds))


def _normalize_text(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^\w\s]", " ", value)
    return " ".join(value.split())


def _metadata_matches_spotify(
    track: TrackInfo,
    result_title: str,
    result_artist: str,
) -> bool:
    title, primary_artist, featured_artists, *_ = track
    expected_title = _normalize_text(title)
    expected_artists = [_normalize_text(primary_artist)]
    expected_artists.extend(_normalize_text(a) for a in featured_artists)

    result_title_norm = _normalize_text(result_title or "")
    result_artist_norm = _normalize_text(result_artist or "")

    title_ok = (
        expected_title in result_title_norm
        or result_title_norm in expected_title
        or _token_overlap(expected_title, result_title_norm) >= 0.6
    )
    artist_ok = any(
        expected in result_artist_norm or result_artist_norm in expected
        for expected in expected_artists
        if expected
    )
    return title_ok and artist_ok


def _token_overlap(left: str, right: str) -> float:
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _acoustid_api_key() -> str:
    api_key = os.environ.get("ACOUSTID_API_KEY", "").strip()
    if not api_key:
        raise EnvironmentError(
            "Set ACOUSTID_API_KEY for chromaprint database lookup. "
            "Register free at https://acoustid.org/new-application"
        )
    return api_key


def _fingerprint_similarity(reference: Path, candidate: Path) -> float:
    """Direct chromaprint comparison between two local clips (0–1)."""
    import acoustid

    if not acoustid.have_chromaprint:
        return 0.0

    ref_pair = acoustid.fingerprint_file(str(reference))
    cand_pair = acoustid.fingerprint_file(str(candidate))
    return max(0.0, min(1.0, float(acoustid.compare_fingerprints(ref_pair, cand_pair))))


def _acoustid_lookup_certainty(clip_path: Path, track: TrackInfo) -> float:
    """Look up a clip in the AcoustID database and score against Spotify metadata."""
    import acoustid

    api_key = _acoustid_api_key()

    try:
        results = acoustid.match(api_key, str(clip_path), meta="tracks")
    except (acoustid.AcoustidError, acoustid.NoBackendError):
        return 0.0

    best = 0.0
    for score, _recording_id, title, artist in results:
        if _metadata_matches_spotify(track, title or "", artist or ""):
            best = max(best, float(score))
    return best


def chromaprint_certainty(
    reference_clip: Path,
    candidate_clip: Path,
    track: TrackInfo,
) -> float:
    """
    Combine AcoustID database lookup on the candidate with direct fingerprint
    similarity against the Spotify reference clip.
    """
    db_score = _acoustid_lookup_certainty(candidate_clip, track)
    try:
        fp_score = _fingerprint_similarity(reference_clip, candidate_clip)
    except Exception:
        fp_score = 0.0
    return max(db_score, fp_score)


def _compute_embedding(path: Path) -> np.ndarray:
    import librosa

    audio, sample_rate = librosa.load(str(path), sr=22050, mono=True)
    mel = librosa.feature.melspectrogram(y=audio, sr=sample_rate, n_mels=128)
    log_mel = librosa.power_to_db(mel, ref=np.max)
    vector = np.mean(log_mel, axis=1)
    norm = np.linalg.norm(vector)
    if norm == 0:
        return vector
    return vector / norm


def embedding_similarity(reference_clip: Path, candidate_clip: Path) -> float:
    """Cosine similarity between mel-spectrogram embeddings (0–1)."""
    ref = _compute_embedding(reference_clip)
    cand = _compute_embedding(candidate_clip)
    return float(np.clip(np.dot(ref, cand), 0.0, 1.0))


def _download_match(
    track: TrackInfo,
    candidate: RankedCandidate,
    save_directory: Path,
) -> Path:
    title, *_ = track
    filename_base = build_track_filename(title, save_directory)
    return download_audio(
        candidate.watch_url(),
        save_directory,
        filename_base=filename_base,
    )


def _prepare_reference_clip(
    spotify_link: str,
    *,
    window_seconds: float,
    temp_dir: Path,
) -> Path:
    preview_url = get_spotify_preview_url(spotify_link)
    if not preview_url:
        raise ValueError(
            "Spotify preview URL unavailable — audio matching requires a 30s preview."
        )
    return prepare_spotify_preview_middle_clip(
        preview_url,
        window_seconds=window_seconds,
        output_path=temp_dir / "spotify_reference.wav",
    )


def _prepare_reference_clip_safe(
    spotify_link: str,
    *,
    window_seconds: float,
    temp_dir: Path,
) -> Optional[Path]:
    """Return the Spotify reference clip, or None if preview/extraction fails."""
    try:
        return _prepare_reference_clip(
            spotify_link, window_seconds=window_seconds, temp_dir=temp_dir
        )
    except Exception:
        return None


def _prepare_candidate_clip_safe(
    candidate: RankedCandidate,
    *,
    window_seconds: float,
    output_path: Path,
) -> Optional[Path]:
    """Return a candidate middle clip, or None if stream extraction fails."""
    try:
        return prepare_candidate_middle_clip(
            candidate.watch_url(),
            duration_seconds=float(candidate.duration or 0),
            window_seconds=window_seconds,
            output_path=output_path,
        )
    except Exception:
        return None


def match_by_chromaprint(
    track: TrackInfo,
    candidates: list[RankedCandidate],
    *,
    spotify_link: str,
    save_directory: Path,
    certainty_threshold: float = AUDIO_MATCH_CERTAINTY,
    max_attempts: int = MAX_AUDIO_MATCH_ATTEMPTS,
    middle_seconds: float = CHROMAPRINT_MIDDLE_SECONDS,
) -> Optional[AudioMatchResult]:
    """
    Compare the middle chromaprint clip against AcoustID (+ fingerprint similarity).

    Checks up to *max_attempts* top candidates. Stops immediately on first match
    with certainty >= *certainty_threshold*.
    """
    if not candidates:
        return None

    window = _clamp_chromaprint_window(middle_seconds)
    attempts = candidates[:max_attempts]

    with tempfile.TemporaryDirectory(prefix="spotify_sync_chromaprint_") as tmp:
        temp_dir = Path(tmp)
        reference_clip = _prepare_reference_clip_safe(
            spotify_link, window_seconds=window, temp_dir=temp_dir
        )
        if reference_clip is None:
            return None

        for index, candidate in enumerate(attempts):
            candidate_clip = _prepare_candidate_clip_safe(
                candidate,
                window_seconds=window,
                output_path=temp_dir / f"candidate_{index}.wav",
            )
            if candidate_clip is None:
                continue

            try:
                certainty = chromaprint_certainty(
                    reference_clip, candidate_clip, track
                )
            except Exception:
                continue

            if certainty >= certainty_threshold:
                try:
                    downloaded = _download_match(track, candidate, save_directory)
                except Exception:
                    continue
                return AudioMatchResult(
                    matched=True,
                    certainty=certainty,
                    method="chromaprint",
                    video_id=candidate.video_id,
                    downloaded_path=downloaded,
                )

    return None


def match_by_embedding(
    track: TrackInfo,
    candidates: list[RankedCandidate],
    *,
    spotify_link: str,
    save_directory: Path,
    certainty_threshold: float = EMBEDDING_MATCH_THRESHOLD,
    max_attempts: int = MAX_AUDIO_MATCH_ATTEMPTS,
    middle_seconds: float = EMBEDDING_MIDDLE_SECONDS,
) -> Optional[AudioMatchResult]:
    """
    Compare middle-segment mel-spectrogram embeddings (Spotify preview vs candidate).

    Checks up to *max_attempts* top candidates. Stops immediately on first match
    with similarity >= *certainty_threshold*, then downloads the full track.
    """
    if not candidates:
        return None

    attempts = candidates[:max_attempts]

    with tempfile.TemporaryDirectory(prefix="spotify_sync_embedding_") as tmp:
        temp_dir = Path(tmp)
        reference_clip = _prepare_reference_clip_safe(
            spotify_link, window_seconds=middle_seconds, temp_dir=temp_dir
        )
        if reference_clip is None:
            return None

        for index, candidate in enumerate(attempts):
            candidate_clip = _prepare_candidate_clip_safe(
                candidate,
                window_seconds=middle_seconds,
                output_path=temp_dir / f"candidate_{index}.wav",
            )
            if candidate_clip is None:
                continue

            try:
                certainty = embedding_similarity(reference_clip, candidate_clip)
            except Exception:
                continue

            if certainty >= certainty_threshold:
                try:
                    downloaded = _download_match(track, candidate, save_directory)
                except Exception:
                    continue
                return AudioMatchResult(
                    matched=True,
                    certainty=certainty,
                    method="embedding",
                    video_id=candidate.video_id,
                    downloaded_path=downloaded,
                )

    return None


def resolve_by_audio_similarity(
    track: TrackInfo,
    candidates: list[RankedCandidate],
    save_directory: str | Path,
    *,
    spotify_link: str,
    enable_chromaprint: Optional[bool] = None,
    enable_embedding: Optional[bool] = None,
) -> Optional[AudioMatchResult]:
    """
    Run enabled audio matchers in order: chromaprint, then embedding.

    Each method examines up to the configured ``max_audio_match_attempts`` top
    metadata-ranked candidates and stops immediately when the first candidate
    exceeds the configured certainty.
    ``enable_*`` args override the module constants when not None.
    """
    save_directory = Path(save_directory)
    global_settings = load_matching_settings()
    use_chromaprint = (
        global_settings.comparison_chromaprint
        if enable_chromaprint is None
        else enable_chromaprint
    )
    use_embedding = (
        global_settings.comparison_embedding
        if enable_embedding is None
        else enable_embedding
    )
    max_attempts = global_settings.max_audio_match_attempts

    if use_chromaprint:
        result = match_by_chromaprint(
            track,
            candidates,
            spotify_link=spotify_link,
            save_directory=save_directory,
            certainty_threshold=global_settings.chromaprint_match_certainty,
            max_attempts=max_attempts,
        )
        if result is not None:
            return result

    if use_embedding:
        result = match_by_embedding(
            track,
            candidates,
            spotify_link=spotify_link,
            save_directory=save_directory,
            certainty_threshold=global_settings.embedding_match_threshold,
            max_attempts=max_attempts,
        )
        if result is not None:
            return result

    return None
