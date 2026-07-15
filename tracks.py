"""Track identity persistence and library/playlist associations."""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

import db
from isrc_match import normalize_isrc
from models import TrackIdentity
from search_candidates import _normalize_text

_AUDIO_EXTENSIONS = {".m4a", ".mp3", ".flac", ".opus", ".ogg", ".webm"}
_LEGACY_ISRC_STEM_RE = re.compile(r"^([A-Z0-9]{12})(?:_|$)")
_DUPLICATE_STEM_SUFFIX = re.compile(r"\s\(\d+\)$")


@dataclass(frozen=True)
class FileTrackMetadata:
    title: str
    artist: str
    isrc: Optional[str]
    duration_seconds: int = 0


def _prepare_normalize(value: str) -> str:
    return unicodedata.normalize("NFC", value.strip())


def normalize_title(value: str) -> str:
    return _normalize_text(_prepare_normalize(value))


def normalize_artist(value: str) -> str:
    return _normalize_text(_prepare_normalize(value))


def existing_track_id_for_identity(identity: TrackIdentity) -> Optional[int]:
    """Return an existing ``tracks.id`` for this identity, if any."""
    conn = db.get_connection()
    if identity.spotify_track_id:
        row = conn.execute(
            "SELECT id FROM tracks WHERE spotify_track_id = ?",
            (identity.spotify_track_id,),
        ).fetchone()
        if row is not None:
            return int(row["id"])
    if identity.youtube_video_id:
        row = conn.execute(
            "SELECT id FROM tracks WHERE youtube_video_id = ?",
            (identity.youtube_video_id,),
        ).fetchone()
        if row is not None:
            return int(row["id"])
    if identity.isrc:
        isrc = normalize_isrc(identity.isrc)
        row = conn.execute(
            "SELECT id FROM tracks WHERE isrc = ?",
            (isrc,),
        ).fetchone()
        if row is not None:
            return int(row["id"])
    return None


def list_unlinked_tracks_for_library(library_id: int) -> list[dict]:
    """Tracks with no ``library_tracks`` row for *library_id*."""
    conn = db.get_connection()
    rows = conn.execute(
        """
        SELECT
          t.id AS track_id,
          t.spotify_track_id,
          t.youtube_video_id,
          t.isrc,
          t.title_norm,
          t.artist_norm,
          t.duration_seconds
        FROM tracks t
        WHERE NOT EXISTS (
            SELECT 1 FROM library_tracks lt
            WHERE lt.library_id = ? AND lt.track_id = t.id
        )
        """,
        (library_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def update_track(track_id: int, identity: TrackIdentity) -> None:
    """Write identity fields onto an existing ``tracks`` row."""
    conn = db.get_connection()
    isrc = normalize_isrc(identity.isrc) if identity.isrc else None
    duration = identity.duration_seconds if identity.duration_seconds > 0 else None
    conn.execute(
        """
        UPDATE tracks SET
          spotify_track_id = COALESCE(spotify_track_id, ?),
          youtube_video_id = COALESCE(youtube_video_id, ?),
          isrc = COALESCE(?, isrc),
          title_norm = ?,
          artist_norm = ?,
          duration_seconds = COALESCE(?, duration_seconds)
        WHERE id = ?
        """,
        (
            identity.spotify_track_id,
            identity.youtube_video_id,
            isrc,
            normalize_title(identity.title),
            normalize_artist(identity.artist),
            duration,
            track_id,
        ),
    )
    conn.commit()


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
    if row is None and isrc:
        row = conn.execute(
            "SELECT id FROM tracks WHERE isrc = ?",
            (isrc,),
        ).fetchone()

    title_norm = normalize_title(identity.title)
    artist_norm = normalize_artist(identity.artist)

    if row is not None:
        track_id = int(row["id"])
        update_track(track_id, identity)
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


def unlink_track_from_library(library_id: int, track_id: int) -> None:
    conn = db.get_connection()
    conn.execute(
        "DELETE FROM library_tracks WHERE library_id = ? AND track_id = ?",
        (library_id, track_id),
    )
    conn.commit()


def list_playlist_member_tracks(playlist_id: int) -> list[dict]:
    conn = db.get_connection()
    rows = conn.execute(
        """
        SELECT
          t.id AS track_id,
          t.spotify_track_id,
          t.youtube_video_id,
          t.isrc,
          t.title_norm,
          t.artist_norm,
          t.duration_seconds
        FROM playlist_items pi
        JOIN tracks t ON t.id = pi.track_id
        WHERE pi.playlist_id = ? AND pi.removed_at IS NULL
        """,
        (playlist_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def normalize_path_key(path: Path | str) -> str:
    """Case-insensitive path key for orphan / linked comparisons."""
    resolved = Path(path).expanduser().resolve()
    key = str(resolved)
    if os.name == "nt":
        return key.casefold()
    return key


def linked_path_keys_for_library(library_id: int) -> set[str]:
    conn = db.get_connection()
    return {
        normalize_path_key(row["local_path"])
        for row in conn.execute(
            "SELECT local_path FROM library_tracks WHERE library_id = ?",
            (library_id,),
        ).fetchall()
        if row["local_path"]
    }


def linked_paths_for_library(library_id: int) -> set[Path]:
    conn = db.get_connection()
    return {
        Path(row["local_path"]).resolve()
        for row in conn.execute(
            "SELECT local_path FROM library_tracks WHERE library_id = ?",
            (library_id,),
        ).fetchall()
    }


def read_file_track_metadata(path: Path) -> FileTrackMetadata:
    title = ""
    artist = ""
    duration_seconds = 0
    try:
        if path.suffix.lower() in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4

            audio = MP4(str(path))
            names = audio.get("\xa9nam")
            if names:
                title = str(names[0])
            artists = audio.get("\xa9ART")
            if artists:
                artist = str(artists[0])
            if audio.info and getattr(audio.info, "length", None):
                duration_seconds = int(audio.info.length)
        else:
            import mutagen

            audio = mutagen.File(str(path), easy=True)
            if audio and audio.tags:
                title_val = audio.tags.get("title")
                if title_val:
                    title = title_val[0]
                artist_val = audio.tags.get("artist")
                if artist_val:
                    artist = artist_val[0]
            if audio and audio.info and getattr(audio.info, "length", None):
                duration_seconds = int(audio.info.length)
    except Exception:
        pass

    if not title.strip():
        stem = _DUPLICATE_STEM_SUFFIX.sub("", path.stem).strip() or path.stem
        title = unicodedata.normalize("NFC", stem)
    return FileTrackMetadata(
        title=title.strip(),
        artist=artist.strip(),
        isrc=read_file_isrc(path),
        duration_seconds=max(0, duration_seconds),
    )


def track_identity_from_member_row(row: dict) -> TrackIdentity:
    return TrackIdentity(
        spotify_track_id=row.get("spotify_track_id"),
        youtube_video_id=row.get("youtube_video_id"),
        isrc=row.get("isrc"),
        title=row.get("title_norm") or "",
        artist=row.get("artist_norm") or "",
        duration_seconds=int(row.get("duration_seconds") or 0),
    )


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
