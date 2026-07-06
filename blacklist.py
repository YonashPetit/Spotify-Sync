"""Global and playlist-scoped track blacklist."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import db


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def blacklist_track(
    track_id: int,
    *,
    playlist_id: Optional[int] = None,
    reason: Optional[str] = None,
) -> tuple[int, bool]:
    """Return (blacklist_id, created). created=False if already exists."""
    conn = db.get_connection()
    existing = conn.execute(
        "SELECT id FROM blacklist WHERE track_id = ? AND playlist_id IS ?",
        (track_id, playlist_id),
    ).fetchone()
    if existing:
        return existing["id"], False

    cursor = conn.execute(
        "INSERT INTO blacklist(track_id, playlist_id, reason, created_at) "
        "VALUES(?, ?, ?, ?)",
        (track_id, playlist_id, reason, _utc_now()),
    )
    conn.commit()
    return cursor.lastrowid, True


def is_blacklisted(track_id: int, playlist_id: Optional[int]) -> bool:
    """True if globally blacklisted OR blacklisted for this specific playlist."""
    conn = db.get_connection()
    row = conn.execute(
        "SELECT 1 FROM blacklist WHERE track_id = ? "
        "AND (playlist_id IS NULL OR playlist_id = ?) LIMIT 1",
        (track_id, playlist_id),
    ).fetchone()
    return row is not None


def list_blacklisted(*, playlist_id: Optional[int] = None) -> list[dict]:
    conn = db.get_connection()
    if playlist_id is None:
        rows = conn.execute(
            """
            SELECT b.id AS blacklist_id, b.track_id, b.playlist_id, b.reason,
                   b.created_at, p.name AS playlist_name, t.*
            FROM blacklist b
            JOIN tracks t ON t.id = b.track_id
            LEFT JOIN playlists p ON p.id = b.playlist_id
            ORDER BY b.id
            """
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT b.id AS blacklist_id, b.track_id, b.playlist_id, b.reason,
                   b.created_at, p.name AS playlist_name, t.*
            FROM blacklist b
            JOIN tracks t ON t.id = b.track_id
            LEFT JOIN playlists p ON p.id = b.playlist_id
            WHERE b.playlist_id = ?
            ORDER BY b.id
            """,
            (playlist_id,),
        ).fetchall()

    entries = []
    for row in rows:
        entries.append(
            {
                "blacklist_id": row["blacklist_id"],
                "track": {
                    "track_id": row["track_id"],
                    "spotify_track_id": row["spotify_track_id"],
                    "youtube_video_id": row["youtube_video_id"],
                    "isrc": row["isrc"],
                    "title": row["title_norm"],
                    "artist": row["artist_norm"],
                    "duration_seconds": row["duration_seconds"],
                },
                "scope": "global" if row["playlist_id"] is None else "playlist",
                "playlist_id": row["playlist_id"],
                "playlist_name": row["playlist_name"],
                "reason": row["reason"],
                "created_at": row["created_at"],
            }
        )
    return entries
