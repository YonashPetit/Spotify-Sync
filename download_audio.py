"""Download audio-only streams via yt-dlp."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yt_dlp


def download_audio(
    url: str,
    save_directory: str | Path,
    *,
    filename_base: str,
) -> Path:
    """
    Download the best available audio for *url* into *save_directory*.

    Returns the path to the downloaded audio file.
    """
    save_directory = Path(save_directory)
    save_directory.mkdir(parents=True, exist_ok=True)

    outtmpl = str(save_directory / f"{filename_base}.%(ext)s")
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "m4a",
            }
        ],
        "keepvideo": False,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info is None:
            raise RuntimeError(f"Failed to download audio from {url}")

        requested = info.get("requested_downloads")
        if requested:
            return Path(requested[0]["filepath"])

        ext = info.get("ext") or "m4a"
        candidate = save_directory / f"{filename_base}.{ext}"
        if candidate.exists():
            return candidate

        # Post-processor may change extension (e.g. webm -> m4a).
        for path in save_directory.glob(f"{filename_base}.*"):
            if path.is_file():
                return path

    raise FileNotFoundError(
        f"Download completed but no file found for {filename_base} in {save_directory}"
    )


def build_audio_filename(spotify_isrc: str, video_id: str) -> str:
    """Stable filename stem for a direct ISRC-confirmed download."""
    return f"{normalize_filename_part(spotify_isrc)}_{video_id}"


def normalize_filename_part(value: str) -> str:
    return "".join(char for char in value if char.isalnum() or char in ("-", "_"))
