"""Tracked playlist CRUD and per-playlist duplicate configuration."""

from __future__ import annotations

from typing import Literal, Optional

import db
from models import DuplicateConfig


class PlaylistNotFoundError(LookupError):
    pass


def add_playlist(
    *,
    source: Literal["spotify", "youtube"],
    external_id: str,
    library_id: int,
    name: Optional[str],
    config: Optional[DuplicateConfig] = None,
) -> tuple[int, bool]:
    """Return (playlist_id, created)."""
    conn = db.get_connection()
    existing = conn.execute(
        "SELECT id FROM playlists WHERE library_id = ? AND source = ? "
        "AND external_id = ?",
        (library_id, source, external_id),
    ).fetchone()
    if existing:
        if name:
            conn.execute(
                "UPDATE playlists SET name = ? WHERE id = ?", (name, existing["id"])
            )
            conn.commit()
        return existing["id"], False

    cfg = config or DuplicateConfig()
    cursor = conn.execute(
        """
        INSERT INTO playlists(
          library_id, source, external_id, name, enabled, duplicate_policy,
          check_isrc, check_metadata, check_audio,
          metadata_threshold, audio_duplicate_threshold, audio_review_threshold
        ) VALUES(?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            library_id,
            source,
            external_id,
            name,
            cfg.duplicate_policy,
            int(cfg.check_isrc),
            int(cfg.check_metadata),
            int(cfg.check_audio),
            cfg.metadata_threshold,
            cfg.audio_duplicate_threshold,
            cfg.audio_review_threshold,
        ),
    )
    conn.commit()
    return cursor.lastrowid, True


def _row_to_dict(row) -> dict:
    return {
        "playlist_id": row["id"],
        "library_id": row["library_id"],
        "source": row["source"],
        "external_id": row["external_id"],
        "name": row["name"],
        "enabled": bool(row["enabled"]),
        "config": DuplicateConfig(
            check_isrc=bool(row["check_isrc"]),
            check_metadata=bool(row["check_metadata"]),
            check_audio=bool(row["check_audio"]),
            metadata_threshold=row["metadata_threshold"],
            audio_duplicate_threshold=row["audio_duplicate_threshold"],
            audio_review_threshold=row["audio_review_threshold"],
            duplicate_policy=row["duplicate_policy"],
        ).as_dict(),
    }


def get_playlist(playlist_id: int) -> dict:
    conn = db.get_connection()
    row = conn.execute(
        "SELECT * FROM playlists WHERE id = ?", (playlist_id,)
    ).fetchone()
    if row is None:
        raise PlaylistNotFoundError(f"Playlist id {playlist_id} does not exist.")
    return _row_to_dict(row)


def list_playlists() -> list[dict]:
    conn = db.get_connection()
    rows = conn.execute("SELECT * FROM playlists ORDER BY id").fetchall()
    return [_row_to_dict(row) for row in rows]


def remove_playlist(playlist_id: int) -> None:
    """Stop tracking a playlist. Does not delete downloaded files on disk."""
    get_playlist(playlist_id)  # raises if missing

    conn = db.get_connection()
    conn.execute("DELETE FROM playlist_items WHERE playlist_id = ?", (playlist_id,))
    conn.execute("DELETE FROM blacklist WHERE playlist_id = ?", (playlist_id,))
    conn.execute(
        "DELETE FROM pending_decisions WHERE playlist_id = ?", (playlist_id,)
    )
    conn.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
    conn.commit()


def set_playlist_enabled(playlist_id: int, enabled: bool) -> dict:
    """Enable or disable tracking for a playlist without removing it."""
    playlist = get_playlist(playlist_id)  # raises if missing
    conn = db.get_connection()
    conn.execute(
        "UPDATE playlists SET enabled = ? WHERE id = ?",
        (1 if enabled else 0, playlist_id),
    )
    conn.commit()
    playlist["enabled"] = enabled
    return playlist


def playlist_duplicate_config(playlist_id: int) -> DuplicateConfig:
    conn = db.get_connection()
    row = conn.execute(
        "SELECT * FROM playlists WHERE id = ?", (playlist_id,)
    ).fetchone()
    if row is None:
        raise PlaylistNotFoundError(f"Playlist id {playlist_id} does not exist.")
    return DuplicateConfig(
        check_isrc=bool(row["check_isrc"]),
        check_metadata=bool(row["check_metadata"]),
        check_audio=bool(row["check_audio"]),
        metadata_threshold=row["metadata_threshold"],
        audio_duplicate_threshold=row["audio_duplicate_threshold"],
        audio_review_threshold=row["audio_review_threshold"],
        duplicate_policy=row["duplicate_policy"],
    )
