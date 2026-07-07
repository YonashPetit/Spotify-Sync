"""Library (target download directory) management."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import db
from settings import get_selected_library_id


class LibraryNotFoundError(LookupError):
    pass


def sanitize_dir_name(name: str) -> str:
    """Make a playlist name safe for use as a directory name."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    cleaned = cleaned.strip(" .")
    return cleaned or "playlist"


def get_or_create_library(path: str, name: Optional[str] = None) -> int:
    conn = db.get_connection()
    resolved = str(Path(path).expanduser().resolve())
    row = conn.execute(
        "SELECT id, name FROM libraries WHERE path = ?", (resolved,)
    ).fetchone()
    if row:
        if name and row["name"] != name:
            conn.execute(
                "UPDATE libraries SET name = ? WHERE id = ?", (name, row["id"])
            )
            conn.commit()
        return row["id"]

    cursor = conn.execute(
        "INSERT INTO libraries(name, path) VALUES(?, ?)", (name, resolved)
    )
    conn.commit()
    Path(resolved).mkdir(parents=True, exist_ok=True)
    return cursor.lastrowid


def find_library_by_path(path: str) -> Optional[int]:
    conn = db.get_connection()
    resolved = str(Path(path).expanduser().resolve())
    row = conn.execute(
        "SELECT id FROM libraries WHERE path = ?", (resolved,)
    ).fetchone()
    return row["id"] if row else None


def find_library_by_name(name: str) -> Optional[int]:
    conn = db.get_connection()
    row = conn.execute("SELECT id FROM libraries WHERE name = ?", (name,)).fetchone()
    return row["id"] if row else None


def resolve_library(
    *, dest: Optional[str] = None, library_name: Optional[str] = None
) -> int:
    """dest > library_name > selected_library_id. Raise if none."""
    if dest:
        return get_or_create_library(dest)
    if library_name:
        library_id = find_library_by_name(library_name)
        if library_id is None:
            raise LibraryNotFoundError(f"No library named {library_name!r}.")
        return library_id
    selected = get_selected_library_id()
    if selected is None:
        raise LibraryNotFoundError(
            "No library selected. Run set-download-path / select-download-path "
            "or pass --dest."
        )
    return selected


def get_library_row(library_id: int) -> dict:
    conn = db.get_connection()
    row = conn.execute(
        "SELECT id, name, path FROM libraries WHERE id = ?", (library_id,)
    ).fetchone()
    if row is None:
        raise LibraryNotFoundError(f"Library id {library_id} does not exist.")
    return {"library_id": row["id"], "name": row["name"], "path": row["path"]}


def get_library_path(library_id: int) -> Path:
    return Path(get_library_row(library_id)["path"])


def playlist_dir(library_id: int, playlist_name: str, external_id: str) -> Path:
    safe_name = sanitize_dir_name(playlist_name)
    return get_library_path(library_id) / f"{safe_name} [{external_id}]"
