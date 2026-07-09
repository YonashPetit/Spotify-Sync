"""Shared dataclasses and type aliases for the sync CLI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

DuplicatePolicy = Literal["skip", "ask", "replace", "keep_both"]

ProcessStatus = Literal[
    "downloaded",
    "skipped_duplicate",
    "skipped_blacklisted",
    "needs_user_choice",
    "failed",
    "already_present",
    "adopted",
]


@dataclass
class DuplicateConfig:
    check_isrc: bool = True
    check_metadata: bool = False
    check_audio: bool = False
    metadata_threshold: float = 90.0
    audio_duplicate_threshold: float = 0.95
    audio_review_threshold: float = 0.85
    duplicate_policy: DuplicatePolicy = "skip"

    def as_dict(self) -> dict:
        return {
            "check_isrc": self.check_isrc,
            "check_metadata": self.check_metadata,
            "check_audio": self.check_audio,
            "metadata_threshold": self.metadata_threshold,
            "audio_duplicate_threshold": self.audio_duplicate_threshold,
            "audio_review_threshold": self.audio_review_threshold,
            "duplicate_policy": self.duplicate_policy,
        }


@dataclass
class TrackIdentity:
    spotify_track_id: Optional[str]
    youtube_video_id: Optional[str]
    isrc: Optional[str]
    title: str
    artist: str
    duration_seconds: int

    def as_dict(self, track_id: Optional[int] = None) -> dict:
        data = {
            "spotify_track_id": self.spotify_track_id,
            "youtube_video_id": self.youtube_video_id,
            "isrc": self.isrc,
            "title": self.title,
            "artist": self.artist,
            "duration_seconds": self.duration_seconds,
        }
        if track_id is not None:
            data = {"track_id": track_id, **data}
        return data


@dataclass
class DuplicateResult:
    existing_track_id: int
    existing_local_path: str
    method: Literal["isrc", "audio", "path"]
    confidence: Literal["exact", "high", "review"]
    score: Optional[float]

    def as_dict(self) -> dict:
        return {
            "method": self.method,
            "confidence": self.confidence,
            "score": self.score,
            "existing_track_id": self.existing_track_id,
            "existing_local_path": self.existing_local_path,
        }


@dataclass
class ProcessResult:
    status: ProcessStatus
    track_id: Optional[int]
    track: Optional[TrackIdentity]
    local_path: Optional[str]
    duplicate: Optional[DuplicateResult] = None
    message: Optional[str] = None
    request_id: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "track": self.track.as_dict(self.track_id) if self.track else None,
            "local_path": self.local_path,
            "duplicate": self.duplicate.as_dict() if self.duplicate else None,
            "message": self.message,
        }
