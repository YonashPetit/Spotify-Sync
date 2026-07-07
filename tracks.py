"""Track identity persistence and library/playlist associations."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

import db
from isrc_match import normalize_isrc
from models import TrackIdentity
from search_candidates import _normalize_text

_AUDIO_EXTENSIONS = {".m4a", ".mp3", ".flac", ".opus", ".ogg", ".webm"}
_LEGACY_ISRC_STEM_RE = re.compile(r"^([A-Z0-9]{12})(?:_|$)")


def normalize_title(value: str) -> str:
    return _normalize_text(value)


def normalize_artist(value: str) -> str:
    return _normalize_text(value)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_or_create_track(identity: TrackIdentity) -> int:
    conn = db.get_connection()
    row = None
    if identity.spotify_track_id:
        row = conn.execute(
            "SELECT id FROM tracks WHERE spotify_track_id = ?",
            (identity.spotify_track_id,),
        ).fetchone()
    if row is None and identity.youtube_video_id:
        row = conn.execute(
            "SELECT id FROM tracks WHERE youtube_video_id = ?",
            (identity.youtube_video_id,),
        ).fetchone()

    isrc = normalize_isrc(identity.isrc) if identity.isrc else None
    title_norm = normalize_title(identity.title)
    artist_norm = normalize_artist(identity.artist)

    if row is not None:
        track_id = row["id"]
        conn.execute(
            """
            UPDATE tracks SET
              spotify_track_id = COALESCE(spotify_track_id, ?),
              youtube_video_id = COALESCE(youtube_video_id, ?),
              isrc = COALESCE(?, isrc),
              title_norm = ?,
              artist_norm = ?,
              duration_seconds = ?
            WHERE id = ?
            """,
            (
                identity.spotify_track_id,
                identity.youtube_video_id,
                isrc,
                title_norm,
                artist_norm,
                identity.duration_seconds,
                track_id,
            ),
        )
        conn.commit()
        return track_id

    cursor = conn.execute(
        """
        INSERT INTO tracks(
          spotify_track_id, youtube_video_id, isrc,
          title_norm, artist_norm, duration_seconds
        ) VALUES(?, ?, ?, ?, ?, ?)
        """,
        (
            identity.spotify_track_id,
            identity.youtube_video_id,
            isrc,
            title_norm,
            artist_norm,
            identity.duration_seconds,
        ),
    )
    conn.commit()
    return cursor.lastrowid


def get_track_identity(track_id: int) -> TrackIdentity:
    conn = db.get_connection()
    row = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
    if row is None:
        raise LookupError(f"Track id {track_id} does not exist.")
    return TrackIdentity(
        spotify_track_id=row["spotify_track_id"],
        youtube_video_id=row["youtube_video_id"],
        isrc=row["isrc"],
        title=row["title_norm"] or "",
        artist=row["artist_norm"] or "",
        duration_seconds=row["duration_seconds"] or 0,
    )


def link_track_to_library(track_id: int, library_id: int, local_path: str) -> None:
    conn = db.get_connection()
    conn.execute(
        """
        INSERT INTO library_tracks(library_id, track_id, local_path)
        VALUES(?, ?, ?)
        ON CONFLICT(library_id, track_id) DO UPDATE SET local_path = excluded.local_path
        """,
        (library_id, track_id, local_path),
    )
    conn.commit()


def link_track_to_playlist(track_id: int, playlist_id: int) -> None:
    conn = db.get_connection()
    conn.execute(
        """
        INSERT INTO playlist_items(playlist_id, track_id, added_at, removed_at)
        VALUES(?, ?, ?, NULL)
        ON CONFLICT(playlist_id, track_id) DO UPDATE SET removed_at = NULL
        """,
        (playlist_id, track_id, _utc_now()),
    )
    conn.commit()


def get_library_track_path(library_id: int, track_id: int) -> Optional[str]:
    conn = db.get_connection()
    row = conn.execute(
        "SELECT local_path FROM library_tracks WHERE library_id = ? AND track_id = ?",
        (library_id, track_id),
    ).fetchone()
    return row["local_path"] if row else None


def iter_audio_files(directory: Path) -> Iterator[Path]:
    """Yield audio files directly inside *directory* (non-recursive)."""
    if not directory.is_dir():
        return
    for path in directory.iterdir():
        if path.is_file() and path.suffix.lower() in _AUDIO_EXTENSIONS:
            yield path


def read_file_isrc(path: Path) -> Optional[str]:
    """Read ISRC from embedded tags, if present."""
    try:
        if path.suffix.lower() in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4

            audio = MP4(str(path))
            isrc_raw = audio.get("----:com.apple.iTunes:ISRC")
            if isrc_raw:
                value = isrc_raw[0]
                text = value.decode() if isinstance(value, bytes) else str(value)
                return normalize_isrc(text)
        else:
            import mutagen

            audio = mutagen.File(str(path), easy=True)
            if audio and audio.tags:
                isrc_val = audio.tags.get("isrc") or audio.tags.get("ISRC")
                if isrc_val:
                    return normalize_isrc(isrc_val[0])
    except Exception:
        pass
    match = _LEGACY_ISRC_STEM_RE.match(path.stem.upper())
    if match:
        return normalize_isrc(match.group(1))
    return None
