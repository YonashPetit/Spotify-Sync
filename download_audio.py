"""Download audio-only streams via yt-dlp."""

from __future__ import annotations

import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yt_dlp

_FFMPEG_VERSION_RE = re.compile(r"ffmpeg version (\d+)\.")


def _ffmpeg_works(ffmpeg_path: Path) -> bool:
    """Accept only reasonably modern builds with a matching ffprobe."""
    ffprobe = ffmpeg_path.with_name("ffprobe" + ffmpeg_path.suffix)
    if not ffprobe.exists():
        return False
    try:
        result = subprocess.run(
            [str(ffmpeg_path), "-version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    match = _FFMPEG_VERSION_RE.search(result.stdout or "")
    return match is not None and int(match.group(1)) >= 4


@lru_cache(maxsize=1)
def find_ffmpeg_location() -> Optional[str]:
    """
    Locate a usable ffmpeg directory, skipping stale builds that may shadow a
    working install on PATH. Honors SPOTIFY_SYNC_FFMPEG (dir or binary path).
    """
    override = os.environ.get("SPOTIFY_SYNC_FFMPEG")
    if override:
        override_path = Path(override)
        return str(override_path.parent if override_path.is_file() else override_path)

    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        candidate = Path(entry) / exe
        if candidate.is_file() and _ffmpeg_works(candidate):
            return entry
    return None


def _global_cookies_file() -> Optional[str]:
    """Cookies file configured via the CLI settings store, if any."""
    try:
        from settings import get_cookies_file

        return get_cookies_file()
    except Exception:
        return None


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
        "noprogress": True,
        # Prefer native m4a so ffmpeg only remuxes instead of re-encoding.
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": outtmpl,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "m4a",
            }
        ],
        # Older ffmpeg builds mark the native AAC encoder as experimental.
        "postprocessor_args": {"ffmpegextractaudio": ["-strict", "-2"]},
        "keepvideo": False,
    }
    cookies = _global_cookies_file()
    if cookies:
        ydl_opts["cookiefile"] = cookies
    ffmpeg_location = find_ffmpeg_location()
    if ffmpeg_location:
        ydl_opts["ffmpeg_location"] = ffmpeg_location

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


def build_track_filename(title: str, save_directory: Path) -> str:
    """Filename stem from song title; ISRC lives in embedded metadata."""
    return resolve_unique_filename(save_directory, sanitize_track_filename(title))


def sanitize_track_filename(title: str) -> str:
    """Make a human-readable song title safe as a Windows filename stem."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title)
    cleaned = cleaned.strip(" .")
    return cleaned or "track"


def resolve_unique_filename(directory: Path, stem: str) -> str:
    """Return *stem* or ``stem (2)``, etc. if audio files already exist."""
    directory.mkdir(parents=True, exist_ok=True)
    if not any(directory.glob(f"{stem}.*")):
        return stem
    counter = 2
    while True:
        candidate = f"{stem} ({counter})"
        if not any(directory.glob(f"{candidate}.*")):
            return candidate
        counter += 1


def normalize_filename_part(value: str) -> str:
    return "".join(char for char in value if char.isalnum() or char in ("-", "_"))
