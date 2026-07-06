"""Track identity persistence and library/playlist associations."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import db
from isrc_match import normalize_isrc
from models import TrackIdentity
from search_candidates import _normalize_text


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
