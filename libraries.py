"""Library (target download directory) management."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import db
import settings
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


def playlist_folder_name(playlist_name: str, external_id: str) -> str:
    """Directory name: original playlist name + id, e.g. ``My Mix [37i9dQZF1DX]``."""
    safe_name = sanitize_dir_name(playlist_name)
    return f"{safe_name} [{external_id}]"


def playlist_dir(library_id: int, playlist_name: str, external_id: str) -> Path:
    return get_library_path(library_id) / playlist_folder_name(
        playlist_name, external_id
    )


def list_libraries() -> list[dict]:
    conn = db.get_connection()
    rows = conn.execute(
        "SELECT id, name, path FROM libraries ORDER BY id"
    ).fetchall()
    return [
        {"library_id": row["id"], "name": row["name"], "path": row["path"]}
        for row in rows
    ]


def delete_library(library_id: int) -> dict:
    """
    Remove a registered library from the DB.

    Does not delete audio files on disk. Clears the selected library if it
    matches. Removes tracked playlists and library track rows for this library.
    """
    get_library_row(library_id)  # raises if missing

    from playlists import remove_playlist

    conn = db.get_connection()
    playlist_rows = conn.execute(
        "SELECT id FROM playlists WHERE library_id = ?", (library_id,)
    ).fetchall()
    playlist_ids = [row["id"] for row in playlist_rows]

    for playlist_id in playlist_ids:
        remove_playlist(playlist_id)

    conn.execute("DELETE FROM library_tracks WHERE library_id = ?", (library_id,))
    conn.execute("DELETE FROM pending_decisions WHERE library_id = ?", (library_id,))
    conn.execute("DELETE FROM libraries WHERE id = ?", (library_id,))
    conn.commit()

    was_selected = settings.get_selected_library_id() == library_id
    if was_selected:
        settings.clear_selected_library_id()

    return {
        "library_id": library_id,
        "was_selected": was_selected,
        "playlists_removed": len(playlist_ids),
    }
