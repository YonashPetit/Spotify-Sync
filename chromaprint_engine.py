"""Chromaprint fingerprinting pipelines for acoustid_api and local_scan strategies."""

from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
from typing import Optional

from audio_segments import _require_ffmpeg, get_youtube_stream_url
from get_content import get_spotify_preview_url
from matching_settings import get_chromaprint_strategy

SPOTIFY_PREVIEW_SECONDS = 30.0
YOUTUBE_ACOUSTID_SECONDS = 60.0

_FPCALC_DURATION_RE = re.compile(r"^DURATION=(\d+(?:\.\d+)?)\s*$", re.MULTILINE)
_FPCALC_FINGERPRINT_RE = re.compile(r"^FINGERPRINT=(.+)\s*$", re.MULTILINE | re.DOTALL)


def _require_fpcalc() -> str:
    fpcalc = shutil.which("fpcalc")
    if not fpcalc:
        raise EnvironmentError(
            "fpcalc is required for chromaprint matching. "
            "Install Chromaprint and ensure fpcalc is on your PATH."
        )
    return fpcalc


def _acoustid_api_key() -> str:
    api_key = os.environ.get("ACOUSTID_API_KEY", "").strip()
    if not api_key:
        raise EnvironmentError(
            "Set ACOUSTID_API_KEY for the acoustid_api chromaprint strategy. "
            "Register free at https://acoustid.org/new-application"
        )
    return api_key


def _parse_fpcalc_output(stdout: str) -> tuple[float, str]:
    duration_match = _FPCALC_DURATION_RE.search(stdout)
    fingerprint_match = _FPCALC_FINGERPRINT_RE.search(stdout)
    if not duration_match or not fingerprint_match:
        raise ValueError(f"Unexpected fpcalc output: {stdout[:500]!r}")
    return float(duration_match.group(1)), fingerprint_match.group(1).strip()


def _run_fpcalc_on_stream(
    stream_url: str,
    *,
    duration_limit: Optional[float] = None,
    raw: bool = True,
) -> tuple[float, str]:
    """Pipe ffmpeg WAV output into fpcalc and return (duration, fingerprint text)."""
    ffmpeg = _require_ffmpeg()
    fpcalc = _require_fpcalc()

    ffmpeg_cmd = [ffmpeg, "-hide_banner", "-loglevel", "error"]
    if duration_limit is not None:
        ffmpeg_cmd.extend(["-t", f"{duration_limit:.3f}"])
    ffmpeg_cmd.extend(["-i", stream_url, "-vn", "-ac", "1", "-ar", "44100", "-f", "wav", "-"])

    fpcalc_cmd = [fpcalc]
    if raw:
        fpcalc_cmd.append("-raw")
    fpcalc_cmd.append("-")

    ffmpeg_proc = subprocess.Popen(
        ffmpeg_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    fpcalc_proc = subprocess.Popen(
        fpcalc_cmd,
        stdin=ffmpeg_proc.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if ffmpeg_proc.stdout is not None:
        ffmpeg_proc.stdout.close()

    stdout, stderr = fpcalc_proc.communicate()
    ffmpeg_stderr = ffmpeg_proc.communicate()[1]
    if fpcalc_proc.returncode != 0:
        detail = (stderr or ffmpeg_stderr or b"").decode(errors="replace").strip()
        raise RuntimeError(f"fpcalc stream fingerprint failed: {detail}")

    return _parse_fpcalc_output(stdout)


def _run_fpcalc_on_file(path: str, *, raw: bool = True) -> tuple[float, str]:
    """Run fpcalc directly on a local audio file."""
    fpcalc = _require_fpcalc()
    cmd = [fpcalc]
    if raw:
        cmd.append("-raw")
    cmd.append(path)
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"fpcalc failed for {path}: {(result.stderr or result.stdout).strip()}"
        )
    return _parse_fpcalc_output(result.stdout)


def _raw_fingerprint_to_ints(raw_fingerprint: str) -> list[int]:
    return [int(part) for part in raw_fingerprint.split(",") if part.strip()]


def _ints_to_ctypes(values: list[int]):
    return (ctypes.c_int * len(values))(*values)


def _compare_raw_segments(left: list[int], right: list[int]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    try:
        import acoustid
    except ImportError:
        return 0.0
    if not acoustid.have_chromaprint:
        return 0.0
    import chromaprint

    left_arr = _ints_to_ctypes(left)
    right_arr = _ints_to_ctypes(right)
    score = chromaprint.chromaprint_compare(
        left_arr, right_arr, len(left), len(right)
    )
    maxval = chromaprint.chromaprint_get_item_duration(0) * min(len(left), len(right))
    if maxval <= 0:
        return 0.0
    return max(0.0, min(1.0, score / float(maxval)))


def sliding_window_similarity(short_raw: str, long_raw: str) -> float:
    """Cross-correlate a short raw fingerprint across a longer one (0–1)."""
    short = _raw_fingerprint_to_ints(short_raw)
    long_fp = _raw_fingerprint_to_ints(long_raw)
    if not short or not long_fp or len(short) > len(long_fp):
        return 0.0

    window = len(short)
    best = 0.0
    for offset in range(len(long_fp) - window + 1):
        score = _compare_raw_segments(short, long_fp[offset : offset + window])
        best = max(best, score)
    return best


def _encode_fingerprint_for_api(raw_fingerprint: str) -> str:
    ints = _raw_fingerprint_to_ints(raw_fingerprint)
    if not ints:
        raise ValueError("Empty raw fingerprint")
    import chromaprint

    fp_array = _ints_to_ctypes(ints)
    encoded = chromaprint.chromaprint_encode_fingerprint(fp_array, len(ints), 1)
    if isinstance(encoded, bytes):
        return encoded.decode("ascii")
    return str(encoded)


def _acoustid_lookup_recording_scores(
    duration: float,
    raw_fingerprint: str,
) -> dict[str, float]:
    """Return {recording_id: score} from AcoustID lookup (meta=recordingids)."""
    api_key = _acoustid_api_key()
    fingerprint = _encode_fingerprint_for_api(raw_fingerprint)
    payload = urllib.parse.urlencode(
        {
            "client": api_key,
            "duration": str(int(round(duration))),
            "fingerprint": fingerprint,
            "meta": "recordingids",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        "https://api.acoustid.org/v2/lookup",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = json.loads(response.read().decode("utf-8"))

    if body.get("status") != "ok":
        message = body.get("error", {}).get("message", "AcoustID lookup failed")
        raise RuntimeError(message)

    scores: dict[str, float] = {}
    for result in body.get("results") or []:
        score = float(result.get("score") or 0.0)
        for recording in result.get("recordings") or []:
            recording_id = recording.get("id")
            if recording_id:
                scores[recording_id] = max(scores.get(recording_id, 0.0), score)
    return scores


def _acoustid_recording_match_score(
    reference: tuple[float, str],
    candidate: tuple[float, str],
) -> float:
    ref_scores = _acoustid_lookup_recording_scores(reference[0], reference[1])
    cand_scores = _acoustid_lookup_recording_scores(candidate[0], candidate[1])
    if not ref_scores or not cand_scores:
        return 0.0
    best = 0.0
    for recording_id, ref_score in ref_scores.items():
        if recording_id in cand_scores:
            best = max(best, min(ref_score, cand_scores[recording_id]))
    return best


def fingerprint_spotify_preview(spotify_link: str) -> tuple[float, str]:
    preview_url = get_spotify_preview_url(spotify_link)
    if not preview_url:
        raise ValueError(
            "Spotify preview URL unavailable — chromaprint matching requires a 30s preview."
        )
    return _run_fpcalc_on_stream(
        preview_url,
        duration_limit=SPOTIFY_PREVIEW_SECONDS,
        raw=True,
    )


def fingerprint_youtube_candidate(
    watch_url: str,
    *,
    strategy: Optional[str] = None,
) -> tuple[float, str]:
    stream_url, _duration = get_youtube_stream_url(watch_url)
    active = strategy or get_chromaprint_strategy()
    if active == "acoustid_api":
        return _run_fpcalc_on_stream(
            stream_url,
            duration_limit=YOUTUBE_ACOUSTID_SECONDS,
            raw=True,
        )
    return _run_fpcalc_on_stream(stream_url, duration_limit=None, raw=True)


def fingerprint_local_file(path: str) -> tuple[float, str]:
    return _run_fpcalc_on_file(str(path), raw=True)


def compare_fingerprints(
    reference: tuple[float, str],
    candidate: tuple[float, str],
    *,
    strategy: Optional[str] = None,
    threshold: float = 0.90,
) -> tuple[float, bool]:
    """Compare fingerprints using the active strategy; returns (score, matched)."""
    active = strategy or get_chromaprint_strategy()
    if active == "acoustid_api":
        score = _acoustid_recording_match_score(reference, candidate)
    else:
        score = sliding_window_similarity(reference[1], candidate[1])
    return score, score >= threshold
