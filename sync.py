"""Track processing and playlist sync orchestration."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

import db
import libraries
import playlists as playlists_mod
import settings
from blacklist import is_blacklisted
from downloader import download_spotify_track, download_youtube_track
from duplicates import apply_duplicate_policy, find_duplicate_in_directory
from metadata import save_playlist_cover, tag_downloaded_file
from models import DuplicateConfig, DuplicateResult, ProcessResult, TrackIdentity
from output import (
    log_download_retry,
    log_download_start,
    log_process_result,
    print_human,
)
from reconcile import (
    ReconcileReport,
    adopt_orphan_playlist_files,
    reconcile_missing_playlist_files,
    track_file_present,
)
from sources import spotify_source, youtube_source
from sources.spotify_source import PLAYLIST_PAGE_SIZE
from tracks import (
    get_library_track_path,
    get_or_create_track,
    link_track_to_library,
    link_track_to_playlist,
)

# Pause briefly between songs to reduce YouTube / yt-dlp rate-limit hits.
SYNC_SONG_DELAY_SECONDS = 1.0


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def describe_match_reason(method: str, certainty: Optional[float]) -> str:
    label_map = {
        "isrc": "via ISRC match",
        "chromaprint": "via chromaprint match certainty",
        "embedding": "via vector embedding match",
        "heap_top": "via metadata-ranked candidate",
        "youtube_direct": "via direct YouTube URL",
    }
    label = label_map.get(method, f"via {method}")
    if certainty is not None:
        return f"{label} ({certainty:.2f})"
    return label


def _fetch_playlist_metadata(playlist: dict) -> dict:
    if playlist["source"] == "spotify":
        return spotify_source.fetch_playlist_metadata(playlist["external_id"])
    return youtube_source.fetch_playlist_metadata(
        youtube_source.playlist_url(playlist["external_id"])
    )


def _apply_playlist_cover(save_directory: Path, playlist: dict) -> Optional[str]:
    try:
        meta = _fetch_playlist_metadata(playlist)
        return save_playlist_cover(save_directory, meta.get("cover_url"))
    except Exception:
        return None


@dataclass
class SyncReport:
    playlist_id: int
    results: list[ProcessResult] = field(default_factory=list)
    total_items_seen: int = 0
    reconcile: Optional[ReconcileReport] = None

    @property
    def summary(self) -> dict:
        return summarize_results(self.results, self.total_items_seen, self.reconcile)


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
) -> tuple[Path, str, Optional[float]]:
    if identity.spotify_track_id:
        url = source_url or spotify_source.track_url(identity.spotify_track_id)
        outcome = download_spotify_track(
            identity, save_directory=save_directory, spotify_url=url
        )
    else:
        url = source_url or youtube_source.watch_url(identity.youtube_video_id)
        outcome = download_youtube_track(
            identity, save_directory=save_directory, youtube_url=url
        )

    local_path = outcome.path
    tag_downloaded_file(local_path, identity)
    return local_path, outcome.method, outcome.certainty


def finalize_downloaded_track(
    *,
    track_id: int,
    identity: TrackIdentity,
    local_path: Path,
    library_id: int,
    playlist_id: Optional[int],
    duplicate: Optional[DuplicateResult] = None,
    message: Optional[str] = None,
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
        message=message,
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
    retry: bool = False,
    log_events: bool = True,
) -> ProcessResult:
    try:
        return _process_track_for_playlist_impl(
            playlist_id=playlist_id,
            identity=identity,
            save_directory=save_directory,
            library_id=library_id,
            config=config,
            json_mode=json_mode,
            source_url=source_url,
            retry=retry,
            log_events=log_events,
        )
    except Exception as exc:  # noqa: BLE001 - report failure per track
        track_id: Optional[int] = None
        try:
            track_id = get_or_create_track(identity)
        except Exception:
            pass
        result = ProcessResult(
            status="failed",
            track_id=track_id,
            track=identity,
            local_path=None,
            message=f"Processing failed: {exc}",
        )
        if log_events:
            log_process_result(result)
        return result


def _process_track_for_playlist_impl(
    *,
    playlist_id: Optional[int],
    identity: TrackIdentity,
    save_directory: Path,
    library_id: int,
    config: DuplicateConfig,
    json_mode: bool,
    source_url: Optional[str] = None,
    retry: bool = False,
    log_events: bool = True,
) -> ProcessResult:
    track_id = get_or_create_track(identity)

    if is_blacklisted(track_id, playlist_id):
        result = ProcessResult(
            status="skipped_blacklisted",
            track_id=track_id,
            track=identity,
            local_path=None,
            message="Track is blacklisted.",
        )
        if log_events:
            log_process_result(result)
        return result

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
            result = ProcessResult(
                status="already_present",
                track_id=track_id,
                track=identity,
                local_path=str(recorded),
                message="Track already present in target directory.",
            )
            if log_events:
                log_process_result(result)
            return result

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
            result = ProcessResult(
                status="skipped_duplicate",
                track_id=track_id,
                track=identity,
                local_path=None,
                duplicate=duplicate,
                message="Duplicate found in target directory.",
            )
            if log_events:
                log_process_result(result)
            return result
        if action == "needs_user_choice":
            request_id = create_pending_decision(
                track_id=track_id,
                playlist_id=playlist_id,
                library_id=library_id,
                save_directory=save_directory,
                source_url=source_url,
                duplicate=duplicate,
            )
            result = ProcessResult(
                status="needs_user_choice",
                track_id=track_id,
                track=identity,
                local_path=None,
                duplicate=duplicate,
                message="Potential duplicate in target directory.",
                request_id=request_id,
            )
            if log_events:
                log_process_result(result)
            return result
        if config.duplicate_policy == "replace":
            _remove_existing_track(library_id, duplicate)

    if log_events:
        if retry:
            log_download_retry(identity.title)
        else:
            log_download_start(identity.title)

    try:
        local_path, match_method, match_certainty = download_track(
            identity, save_directory=save_directory, source_url=source_url
        )
    except Exception as exc:  # noqa: BLE001 - report failure per track
        result = ProcessResult(
            status="failed",
            track_id=track_id,
            track=identity,
            local_path=None,
            duplicate=duplicate,
            message=f"Download failed: {exc}",
        )
        if log_events:
            log_process_result(result)
        return result

    match_note = describe_match_reason(match_method, match_certainty)

    result = finalize_downloaded_track(
        track_id=track_id,
        identity=identity,
        local_path=local_path,
        library_id=library_id,
        playlist_id=playlist_id,
        duplicate=duplicate,
        message=match_note,
    )
    if log_events:
        log_process_result(result)
    return result


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


def _iter_source_identity_batches(
    playlist: dict,
    *,
    batch_size: int = PLAYLIST_PAGE_SIZE,
) -> Iterator[list[TrackIdentity]]:
    if playlist["source"] == "spotify":
        yield from spotify_source.iter_playlist_track_batches(
            playlist["external_id"],
            batch_size=batch_size,
        )
    else:
        yield from youtube_source.iter_playlist_video_batches(
            youtube_source.playlist_url(playlist["external_id"]),
            batch_size=batch_size,
        )


def _source_url_for_identity(identity: TrackIdentity) -> Optional[str]:
    if identity.spotify_track_id:
        return spotify_source.track_url(identity.spotify_track_id)
    if identity.youtube_video_id:
        return youtube_source.watch_url(identity.youtube_video_id)
    return None


def _pause_between_songs() -> None:
    if SYNC_SONG_DELAY_SECONDS > 0:
        time.sleep(SYNC_SONG_DELAY_SECONDS)


def _failed_sync_result(
    identity: TrackIdentity,
    track_id: Optional[int],
    exc: Exception,
) -> ProcessResult:
    result = ProcessResult(
        status="failed",
        track_id=track_id,
        track=identity,
        local_path=None,
        message=f"Processing failed: {exc}",
    )
    log_process_result(result)
    return result


def summarize_results(
    results: list[ProcessResult],
    total_items_seen: int,
    reconcile: Optional[ReconcileReport] = None,
) -> dict:
    summary = {
        "total_items_seen": total_items_seen,
        "new_items_processed": len(results),
        "downloaded": 0,
        "skipped_duplicate": 0,
        "skipped_blacklisted": 0,
        "needs_user_choice": 0,
        "failed": 0,
        "already_present": 0,
        "adopted": 0,
    }
    for result in results:
        summary[result.status] += 1
    if reconcile is not None:
        summary["reconcile"] = reconcile.as_dict()
    return summary


def sync_playlist(playlist_id: int, *, json_mode: bool = False) -> SyncReport:
    playlist = playlists_mod.get_playlist(playlist_id)
    library_id = playlist["library_id"]
    config = playlists_mod.playlist_duplicate_config(playlist_id)
    save_directory = libraries.playlist_dir(
        library_id, playlist["name"] or playlist["external_id"], playlist["external_id"]
    )
    save_directory.mkdir(parents=True, exist_ok=True)
    cover_path = _apply_playlist_cover(save_directory, playlist)

    cleared, cleared_labels = reconcile_missing_playlist_files(
        playlist_id=playlist_id,
        library_id=library_id,
        save_directory=save_directory,
    )
    reconcile_report = ReconcileReport(
        missing_links_cleared=cleared,
        cleared_track_labels=cleared_labels,
    )
    if settings.get_adopt_orphan_files():
        adopt_report = adopt_orphan_playlist_files(
            playlist_id=playlist_id,
            library_id=library_id,
            save_directory=save_directory,
        )
        reconcile_report.orphans_adopted = adopt_report.orphans_adopted
        reconcile_report.orphans_unmatched = adopt_report.orphans_unmatched
        reconcile_report.results.extend(adopt_report.results)

    conn = db.get_connection()
    active_rows = conn.execute(
        "SELECT track_id FROM playlist_items WHERE playlist_id = ? "
        "AND removed_at IS NULL",
        (playlist_id,),
    ).fetchall()
    previously_active = {row["track_id"] for row in active_rows}

    results: list[ProcessResult] = list(reconcile_report.results)
    seen_track_ids: set[int] = set()
    total_items_seen = 0
    batch_number = 0

    batch_iter = _iter_source_identity_batches(playlist)
    while True:
        try:
            batch = next(batch_iter)
        except StopIteration:
            break
        except Exception as exc:  # noqa: BLE001 - keep partial progress
            print_human(
                f"Playlist pagination stopped early: {exc}. "
                "Completed songs are saved; re-run sync to continue."
            )
            break

        if not batch:
            continue

        batch_number += 1
        batch_start = total_items_seen + 1
        batch_end = total_items_seen + len(batch)
        print_human(
            f"Syncing playlist page {batch_number} "
            f"({len(batch)} track(s), items {batch_start}–{batch_end})…"
        )

        for identity in batch:
            total_items_seen += 1
            track_id: Optional[int] = None
            result: Optional[ProcessResult] = None
            try:
                track_id = get_or_create_track(identity)
                seen_track_ids.add(track_id)

                if not track_file_present(library_id, track_id, save_directory):
                    result = process_track_for_playlist(
                        playlist_id=playlist_id,
                        identity=identity,
                        save_directory=save_directory,
                        library_id=library_id,
                        config=config,
                        json_mode=json_mode,
                        source_url=_source_url_for_identity(identity),
                    )
            except Exception as exc:  # noqa: BLE001 - report failure per track
                result = _failed_sync_result(identity, track_id, exc)

            if result is not None:
                results.append(result)
                _pause_between_songs()

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
        reconcile=reconcile_report,
    )


def sync_all(*, json_mode: bool = False) -> dict[int, SyncReport]:
    all_results: dict[int, SyncReport] = {}
    for playlist in playlists_mod.list_playlists():
        if not playlist["enabled"]:
            continue
        playlist_id = playlist["playlist_id"]
        try:
            all_results[playlist_id] = sync_playlist(
                playlist_id, json_mode=json_mode
            )
        except Exception as exc:  # noqa: BLE001 - continue other playlists
            name = playlist["name"] or playlist["external_id"]
            print_human(
                f"Sync failed for playlist {name!r} (id={playlist_id}): {exc}"
            )
    return all_results
