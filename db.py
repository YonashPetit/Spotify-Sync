"""SQLite state database: connection handling, schema, migrations."""

from __future__ import annotations

import sqlite3
from typing import Optional

from app_paths import get_db_path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS libraries (
  id INTEGER PRIMARY KEY,
  name TEXT,
  path TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS playlists (
  id INTEGER PRIMARY KEY,
  library_id INTEGER NOT NULL REFERENCES libraries(id),
  source TEXT NOT NULL CHECK(source IN ('spotify', 'youtube')),
  external_id TEXT NOT NULL,
  name TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  duplicate_policy TEXT NOT NULL DEFAULT 'skip',
  check_isrc INTEGER NOT NULL DEFAULT 1,
  check_metadata INTEGER NOT NULL DEFAULT 0,
  check_audio INTEGER NOT NULL DEFAULT 0,
  metadata_threshold REAL NOT NULL DEFAULT 90.0,
  audio_duplicate_threshold REAL NOT NULL DEFAULT 0.95,
  audio_review_threshold REAL NOT NULL DEFAULT 0.85,
  UNIQUE(library_id, source, external_id)
);

CREATE TABLE IF NOT EXISTS tracks (
  id INTEGER PRIMARY KEY,
  spotify_track_id TEXT,
  youtube_video_id TEXT,
  isrc TEXT,
  title_norm TEXT,
  artist_norm TEXT,
  duration_seconds INTEGER,
  fingerprint BLOB,
  UNIQUE(spotify_track_id),
  UNIQUE(youtube_video_id)
);

CREATE TABLE IF NOT EXISTS library_tracks (
  id INTEGER PRIMARY KEY,
  library_id INTEGER NOT NULL REFERENCES libraries(id),
  track_id INTEGER NOT NULL REFERENCES tracks(id),
  local_path TEXT NOT NULL,
  UNIQUE(library_id, track_id),
  UNIQUE(library_id, local_path)
);

CREATE TABLE IF NOT EXISTS playlist_items (
  id INTEGER PRIMARY KEY,
  playlist_id INTEGER NOT NULL REFERENCES playlists(id),
  track_id INTEGER NOT NULL REFERENCES tracks(id),
  added_at TEXT NOT NULL,
  removed_at TEXT,
  UNIQUE(playlist_id, track_id)
);

CREATE TABLE IF NOT EXISTS blacklist (
  id INTEGER PRIMARY KEY,
  track_id INTEGER NOT NULL REFERENCES tracks(id),
  playlist_id INTEGER REFERENCES playlists(id),
  reason TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(track_id, playlist_id)
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_decisions (
  request_id TEXT PRIMARY KEY,
  track_id INTEGER NOT NULL REFERENCES tracks(id),
  playlist_id INTEGER REFERENCES playlists(id),
  library_id INTEGER NOT NULL REFERENCES libraries(id),
  save_directory TEXT NOT NULL,
  source_url TEXT,
  existing_track_id INTEGER,
  existing_local_path TEXT,
  method TEXT,
  confidence TEXT,
  score REAL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tracks_isrc ON tracks(isrc);
CREATE INDEX IF NOT EXISTS idx_library_tracks_library ON library_tracks(library_id);
CREATE INDEX IF NOT EXISTS idx_blacklist_track ON blacklist(track_id);
CREATE INDEX IF NOT EXISTS idx_playlist_items_playlist ON playlist_items(playlist_id);
"""

_conn: Optional[sqlite3.Connection] = None


def connect() -> sqlite3.Connection:
    """Open a new connection to the state database."""
    conn = sqlite3.connect(str(get_db_path()))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def get_connection() -> sqlite3.Connection:
    """Shared process-wide connection with schema applied."""
    global _conn
    if _conn is None:
        _conn = connect()
        migrate(_conn)
    return _conn


def init_db() -> None:
    get_connection()


def reset_connection() -> None:
    """Close the shared connection (used by tests when switching DB paths)."""
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None
