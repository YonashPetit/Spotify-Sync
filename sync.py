"""Track processing and playlist sync orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import db
import libraries
import playlists as playlists_mod
from blacklist import is_blacklisted
from downloader import download_spotify_track, download_youtube_track
from duplicates import apply_duplicate_policy, find_duplicate_in_directory
from metadata import tag_downloaded_file
from models import DuplicateConfig, DuplicateResult, ProcessResult, TrackIdentity
from sources import spotify_source, youtube_source
from tracks import (
    get_library_track_path,
    get_or_create_track,
    link_track_to_library,
    link_track_to_playlist,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class SyncReport:
    playlist_id: int
    results: list[ProcessResult] = field(default_factory=list)
    total_items_seen: int = 0

    @property
    def summary(self) -> dict:
        return summarize_results(self.results, self.total_items_seen)


def create_pending_decision(
    *,
    track_id: int,
    playlist_id: Optional[int],
    library_id: int,
    save_directory: Path,
    source_url: Optional[str],
    duplicate: DuplicateResult,
) -> str:
    """Persist a needs_user_choice request for later resolve-duplicate."""
    conn = db.get_connection()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    request_id = f"dup-{stamp}-{track_id}"
    conn.execute(
        """
        INSERT OR REPLACE INTO pending_decisions(
          request_id, track_id, playlist_id, library_id, save_directory,
          source_url, existing_track_id, existing_local_path, method,
          confidence, score, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            request_id,
            track_id,
            playlist_id,
            library_id,
            str(save_directory),
            source_url,
            duplicate.existing_track_id,
            duplicate.existing_local_path,
            duplicate.method,
            duplicate.confidence,
            duplicate.score,
            _utc_now(),
        ),
    )
    conn.commit()
    return request_id


def get_pending_decision(request_id: str) -> Optional[dict]:
    conn = db.get_connection()
    row = conn.execute(
        "SELECT * FROM pending_decisions WHERE request_id = ?", (request_id,)
    ).fetchone()
    return dict(row) if row else None


def delete_pending_decision(request_id: str) -> None:
    conn = db.get_connection()
    conn.execute(
        "DELETE FROM pending_decisions WHERE request_id = ?", (request_id,)
    )
    conn.commit()


def download_track(
    identity: TrackIdentity,
    *,
    save_directory: Path,
    source_url: Optional[str] = None,
) -> Path:
    if identity.spotify_track_id:
        url = source_url or spotify_source.track_url(identity.spotify_track_id)
        local_path = download_spotify_track(
            identity, save_directory=save_directory, spotify_url=url
        )
    else:
        url = source_url or youtube_source.watch_url(identity.youtube_video_id)
        local_path = download_youtube_track(
            identity, save_directory=save_directory, youtube_url=url
        )

    tag_downloaded_file(local_path, identity)
    return local_path


def finalize_downloaded_track(
    *,
    track_id: int,
    identity: TrackIdentity,
    local_path: Path,
    library_id: int,
    playlist_id: Optional[int],
    duplicate: Optional[DuplicateResult] = None,
) -> ProcessResult:
    """Record DB rows for a downloaded file."""
    link_track_to_library(track_id, library_id, str(local_path))
    if playlist_id is not None:
        link_track_to_playlist(track_id, playlist_id)

    return ProcessResult(
        status="downloaded",
        track_id=track_id,
        track=identity,
        local_path=str(local_path),
        duplicate=duplicate,
    )


def process_track_for_playlist(
    *,
    playlist_id: Optional[int],
    identity: TrackIdentity,
    save_directory: Path,
    library_id: int,
    config: DuplicateConfig,
    json_mode: bool,
    source_url: Optional[str] = None,
) -> ProcessResult:
    track_id = get_or_create_track(identity)

    if is_blacklisted(track_id, playlist_id):
        return ProcessResult(
            status="skipped_blacklisted",
            track_id=track_id,
            track=identity,
            local_path=None,
            message="Track is blacklisted.",
        )

    duplicate = None
    recorded_path = get_library_track_path(library_id, track_id)
    if recorded_path is not None:
        recorded = Path(recorded_path)
        if (
            recorded.exists()
            and recorded.parent.resolve() == save_directory.resolve()
        ):
            if playlist_id is not None:
                link_track_to_playlist(track_id, playlist_id)
            return ProcessResult(
                status="already_present",
                track_id=track_id,
                track=identity,
                local_path=str(recorded),
                message="Track already present in target directory.",
            )

        duplicate = find_duplicate_in_directory(
            save_directory,
            identity,
            config,
            track_id=track_id,
        )

    if duplicate is not None:
        action = apply_duplicate_policy(
            duplicate, config.duplicate_policy, json_mode=json_mode
        )
        if action == "skip":
            return ProcessResult(
                status="skipped_duplicate",
                track_id=track_id,
                track=identity,
                local_path=None,
                duplicate=duplicate,
                message="Duplicate found in target directory.",
            )
        if action == "needs_user_choice":
            request_id = create_pending_decision(
                track_id=track_id,
                playlist_id=playlist_id,
                library_id=library_id,
                save_directory=save_directory,
                source_url=source_url,
                duplicate=duplicate,
            )
            return ProcessResult(
                status="needs_user_choice",
                track_id=track_id,
                track=identity,
                local_path=None,
                duplicate=duplicate,
                message="Potential duplicate in target directory.",
                request_id=request_id,
            )
        if config.duplicate_policy == "replace":
            _remove_existing_track(library_id, duplicate)

    try:
        local_path = download_track(
            identity, save_directory=save_directory, source_url=source_url
        )
    except Exception as exc:  # noqa: BLE001 - report failure per track
        return ProcessResult(
            status="failed",
            track_id=track_id,
            track=identity,
            local_path=None,
            duplicate=duplicate,
            message=f"Download failed: {exc}",
        )

    return finalize_downloaded_track(
        track_id=track_id,
        identity=identity,
        local_path=local_path,
        library_id=library_id,
        playlist_id=playlist_id,
        duplicate=duplicate,
    )


def _remove_existing_track(library_id: int, duplicate: DuplicateResult) -> None:
    """For duplicate_policy=replace: drop the existing file and its rows."""
    conn = db.get_connection()
    existing = Path(duplicate.existing_local_path)
    if existing.exists():
        try:
            existing.unlink()
        except OSError:
            pass
    conn.execute(
        "DELETE FROM library_tracks WHERE library_id = ? AND track_id = ?",
        (library_id, duplicate.existing_track_id),
    )
    conn.commit()


def _iter_source_identities(playlist: dict):
    if playlist["source"] == "spotify":
        yield from spotify_source.iter_playlist_track_identities(
            playlist["external_id"]
        )
    else:
        yield from youtube_source.iter_playlist_video_identities(
            playlist["external_id"]
        )


def summarize_results(results: list[ProcessResult], total_items_seen: int) -> dict:
    summary = {
        "total_items_seen": total_items_seen,
        "new_items_processed": len(results),
        "downloaded": 0,
        "skipped_duplicate": 0,
        "skipped_blacklisted": 0,
        "needs_user_choice": 0,
        "failed": 0,
        "already_present": 0,
    }
    for result in results:
        summary[result.status] += 1
    return summary


def sync_playlist(playlist_id: int, *, json_mode: bool = False) -> SyncReport:
    playlist = playlists_mod.get_playlist(playlist_id)
    library_id = playlist["library_id"]
    config = playlists_mod.playlist_duplicate_config(playlist_id)
    save_directory = libraries.playlist_dir(
        library_id, playlist["name"] or playlist["external_id"], playlist["external_id"]
    )
    save_directory.mkdir(parents=True, exist_ok=True)

    conn = db.get_connection()
    active_rows = conn.execute(
        "SELECT track_id FROM playlist_items WHERE playlist_id = ? "
        "AND removed_at IS NULL",
        (playlist_id,),
    ).fetchall()
    previously_active = {row["track_id"] for row in active_rows}

    results: list[ProcessResult] = []
    seen_track_ids: set[int] = set()
    total_items_seen = 0

    for identity in _iter_source_identities(playlist):
        total_items_seen += 1
        track_id = get_or_create_track(identity)
        seen_track_ids.add(track_id)

        if track_id in previously_active:
            continue

        source_url = None
        if identity.spotify_track_id:
            source_url = spotify_source.track_url(identity.spotify_track_id)
        elif identity.youtube_video_id:
            source_url = youtube_source.watch_url(identity.youtube_video_id)

        result = process_track_for_playlist(
            playlist_id=playlist_id,
            identity=identity,
            save_directory=save_directory,
            library_id=library_id,
            config=config,
            json_mode=json_mode,
            source_url=source_url,
        )
        results.append(result)

    removed = previously_active - seen_track_ids
    if removed:
        now = _utc_now()
        conn.executemany(
            "UPDATE playlist_items SET removed_at = ? "
            "WHERE playlist_id = ? AND track_id = ?",
            [(now, playlist_id, track_id) for track_id in removed],
        )
        conn.commit()

    return SyncReport(
        playlist_id=playlist_id,
        results=results,
        total_items_seen=total_items_seen,
    )


def sync_all(*, json_mode: bool = False) -> dict[int, SyncReport]:
    all_results: dict[int, SyncReport] = {}
    for playlist in playlists_mod.list_playlists():
        if not playlist["enabled"]:
            continue
        all_results[playlist["playlist_id"]] = sync_playlist(
            playlist["playlist_id"], json_mode=json_mode
        )
    return all_results
