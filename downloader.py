"""Download entry points that wrap the existing search/download pipeline."""

from __future__ import annotations

from pathlib import Path

from download_audio import build_track_filename, download_audio
from models import TrackIdentity
from search_candidates import run_pipeline
from sources.youtube_source import parse_video_id, watch_url


class DownloadError(RuntimeError):
    pass


def download_spotify_track(
    identity: TrackIdentity,
    *,
    save_directory: Path,
    spotify_url: str,
    enable_chromaprint: bool = False,
    enable_embedding: bool = False,
) -> Path:
    """
    Run the full matching pipeline for a Spotify track.

    With chromaprint/embedding disabled the pipeline downloads the top heap
    candidate when no ISRC hit exists; otherwise the best audio match wins,
    falling back to the heap top.
    """
    result = run_pipeline(
        spotify_url,
        save_directory=save_directory,
        enable_chromaprint=enable_chromaprint,
        enable_embedding=enable_embedding,
    )

    if result.downloaded_path is not None:
        return result.downloaded_path

    top = result.best_candidate
    if top is not None:
        filename_base = build_track_filename(identity.title, save_directory)
        return download_audio(
            top.watch_url(),
            save_directory,
            filename_base=filename_base,
        )

    raise DownloadError(
        f"No download candidate found for {identity.artist!r} - {identity.title!r}."
    )


def download_youtube_track(
    identity: TrackIdentity,
    *,
    save_directory: Path,
    youtube_url: str,
) -> Path:
    """Direct download via download_audio() using the video URL."""
    video_id = identity.youtube_video_id or parse_video_id(youtube_url)
    filename_base = build_track_filename(identity.title, save_directory)
    return download_audio(
        watch_url(video_id),
        save_directory,
        filename_base=filename_base,
    )
