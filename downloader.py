"""Download entry points that wrap the existing search/download pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from download_audio import build_track_filename, download_audio
from matching_settings import load_matching_settings
from models import TrackIdentity
from search_candidates import run_pipeline
from sources.youtube_source import parse_video_id, watch_url


class DownloadError(RuntimeError):
    pass


def _no_download_message(
    identity: TrackIdentity,
    *,
    candidate_count: int,
    audio_enabled: bool,
    metadata_fallback: bool,
    audio_match_notes: list[str] | None = None,
) -> str:
    label = f"{identity.artist!r} - {identity.title!r}"
    if candidate_count == 0:
        return (
            f"No download candidate found for {label}: "
            "no YouTube results met the metadata score threshold."
        )
    if audio_enabled and not metadata_fallback:
        message = (
            f"No download candidate found for {label}: "
            f"chromaprint/embedding found no confident match among "
            f"{candidate_count} metadata-ranked candidate(s). "
            "Enable comparison metadata fallback or adjust audio matching settings."
        )
        if audio_match_notes:
            message += " " + " ".join(audio_match_notes)
        return message
    return f"No download candidate found for {label}."


@dataclass
class DownloadOutcome:
    path: Path
    method: str
    certainty: float | None = None
    video_id: str | None = None


def download_spotify_track(
    identity: TrackIdentity,
    *,
    save_directory: Path,
    spotify_url: str,
    enable_chromaprint: bool | None = None,
    enable_embedding: bool | None = None,
) -> DownloadOutcome:
    """
    Run the full matching pipeline for a Spotify track.

    With chromaprint/embedding disabled the pipeline downloads the top heap
    candidate when no ISRC hit exists. With audio matching enabled, a
    metadata-ranked fallback is used only when ``comparison_metadata_fallback``
    is on and chromaprint/embedding do not find a satisfactory match.
    """
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
    result = run_pipeline(
        spotify_url,
        save_directory=save_directory,
        enable_chromaprint=use_chromaprint,
        enable_embedding=use_embedding,
    )

    if result.downloaded_path is not None:
        return DownloadOutcome(
            path=result.downloaded_path,
            method=result.match_method or "unknown",
            certainty=result.audio_match_certainty,
            video_id=result.matched_video_id,
        )

    audio_enabled = use_chromaprint or use_embedding
    allow_metadata_fallback = (
        not audio_enabled or global_settings.comparison_metadata_fallback
    )
    top = result.best_candidate
    if top is not None and allow_metadata_fallback:
        try:
            filename_base = build_track_filename(identity.title, save_directory)
            downloaded_path = download_audio(
                top.watch_url(),
                save_directory,
                filename_base=filename_base,
            )
        except Exception as exc:
            raise DownloadError(
                f"Metadata fallback download failed for "
                f"{identity.artist!r} - {identity.title!r}: {exc}"
            ) from exc
        return DownloadOutcome(
            path=downloaded_path,
            method="heap_top",
            certainty=None,
            video_id=top.video_id,
        )

    raise DownloadError(
        _no_download_message(
            identity,
            candidate_count=len(result.candidate_heap),
            audio_enabled=audio_enabled,
            metadata_fallback=allow_metadata_fallback,
            audio_match_notes=result.audio_match_notes,
        )
    )


def download_youtube_track(
    identity: TrackIdentity,
    *,
    save_directory: Path,
    youtube_url: str,
) -> DownloadOutcome:
    """Direct download via download_audio() using the video URL."""
    video_id = identity.youtube_video_id or parse_video_id(youtube_url)
    filename_base = build_track_filename(identity.title, save_directory)
    downloaded_path = download_audio(
        watch_url(video_id),
        save_directory,
        filename_base=filename_base,
    )
    return DownloadOutcome(
        path=downloaded_path,
        method="youtube_direct",
        certainty=None,
        video_id=video_id,
    )
