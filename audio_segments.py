"""Extract middle audio segments for fingerprint / embedding comparison."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional
from urllib.request import urlretrieve

import yt_dlp


def middle_segment_start(duration_seconds: float, window_seconds: float) -> float:
    """Return the start time (seconds) of a window centered on the track."""
    if duration_seconds <= 0:
        return 0.0
    window = min(window_seconds, duration_seconds)
    return max(0.0, (duration_seconds - window) / 2.0)


def effective_window(duration_seconds: float, window_seconds: float) -> float:
    if duration_seconds <= 0:
        return window_seconds
    return min(window_seconds, duration_seconds)


def _require_ffmpeg() -> str:
    from download_audio import find_ffmpeg_location

    location = find_ffmpeg_location()
    if location:
        candidate = Path(location) / (
            "ffmpeg.exe" if (Path(location) / "ffmpeg.exe").exists() else "ffmpeg"
        )
        if candidate.exists():
            return str(candidate)

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise EnvironmentError(
            "ffmpeg is required for audio segment extraction. "
            "Install ffmpeg and ensure it is on your PATH."
        )
    return ffmpeg


def extract_middle_segment(
    input_path: Path | str,
    output_path: Path | str,
    *,
    duration_seconds: float,
    window_seconds: float,
) -> Path:
    """Cut the middle *window_seconds* from a local audio file via ffmpeg."""
    ffmpeg = _require_ffmpeg()
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    start = middle_segment_start(duration_seconds, window_seconds)
    length = effective_window(duration_seconds, window_seconds)

    cmd = [
        ffmpeg,
        "-y",
        "-ss",
        f"{start:.3f}",
        "-t",
        f"{length:.3f}",
        "-i",
        str(input_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "44100",
        str(output_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return output_path


def extract_middle_from_stream_url(
    stream_url: str,
    output_path: Path | str,
    *,
    duration_seconds: float,
    window_seconds: float,
) -> Path:
    """Cut the middle segment directly from a remote audio stream URL."""
    ffmpeg = _require_ffmpeg()
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    start = middle_segment_start(duration_seconds, window_seconds)
    length = effective_window(duration_seconds, window_seconds)

    cmd = [
        ffmpeg,
        "-y",
        "-ss",
        f"{start:.3f}",
        "-t",
        f"{length:.3f}",
        "-i",
        stream_url,
        "-vn",
        "-ac",
        "1",
        "-ar",
        "44100",
        str(output_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return output_path


def get_youtube_stream_url(watch_url: str) -> tuple[str, float]:
    """Resolve a direct audio stream URL and duration from a YouTube watch link."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "bestaudio/best",
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(watch_url, download=False)
    if info is None:
        raise ValueError(f"Could not resolve stream for {watch_url}")

    stream_url = info.get("url")
    if not stream_url:
        raise ValueError(f"No stream URL in metadata for {watch_url}")

    duration = float(info.get("duration") or 0)
    return stream_url, duration


def prepare_candidate_middle_clip(
    watch_url: str,
    *,
    duration_seconds: float,
    window_seconds: float,
    output_path: Path | str,
) -> Path:
    """Extract the middle clip from a YouTube / YouTube Music watch URL."""
    stream_url, stream_duration = get_youtube_stream_url(watch_url)
    total_duration = duration_seconds or stream_duration
    return extract_middle_from_stream_url(
        stream_url,
        output_path,
        duration_seconds=total_duration,
        window_seconds=window_seconds,
    )


def prepare_spotify_preview_middle_clip(
    preview_url: str,
    *,
    preview_duration_seconds: float = 30.0,
    window_seconds: float,
    output_path: Path | str,
) -> Path:
    """Download Spotify preview MP3 and extract its middle segment."""
    output_path = Path(output_path)
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        preview_file = Path(tmp.name)

    try:
        urlretrieve(preview_url, preview_file)
        return extract_middle_segment(
            preview_file,
            output_path,
            duration_seconds=preview_duration_seconds,
            window_seconds=window_seconds,
        )
    finally:
        preview_file.unlink(missing_ok=True)
