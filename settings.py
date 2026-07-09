"""Key/value settings stored in the state database."""

from __future__ import annotations

import os
import sys
from typing import Optional

import db

SELECTED_LIBRARY_KEY = "selected_library_id"
COOKIES_FILE_KEY = "cookies_file"
ADOPT_ORPHAN_FILES_KEY = "adopt_orphan_files"


def get_setting(key: str) -> Optional[str]:
    conn = db.get_connection()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def get_selected_library_id() -> Optional[int]:
    value = get_setting(SELECTED_LIBRARY_KEY)
    return int(value) if value is not None else None


def set_selected_library_id(library_id: int) -> None:
    set_setting(SELECTED_LIBRARY_KEY, str(library_id))


def clear_selected_library_id() -> None:
    conn = db.get_connection()
    conn.execute("DELETE FROM settings WHERE key = ?", (SELECTED_LIBRARY_KEY,))
    conn.commit()


def get_cookies_file() -> Optional[str]:
    return get_setting(COOKIES_FILE_KEY)


def set_cookies_file(path: str) -> None:
    set_setting(COOKIES_FILE_KEY, path)


def get_adopt_orphan_files() -> bool:
    return get_setting(ADOPT_ORPHAN_FILES_KEY) == "1"


def set_adopt_orphan_files(enabled: bool) -> None:
    set_setting(ADOPT_ORPHAN_FILES_KEY, "1" if enabled else "0")


def is_json_mode(argv: Optional[list[str]] = None) -> bool:
    """True if --json in argv or SPOTIFY_SYNC_JSON=1."""
    if argv is None:
        argv = sys.argv[1:]
    if "--json" in argv:
        return True
    return os.environ.get("SPOTIFY_SYNC_JSON") == "1"
