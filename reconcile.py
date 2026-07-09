"""Reconcile playlist folders with database state before sync."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import db
from isrc_match import normalize_isrc
from models import ProcessResult, TrackIdentity
from output import print_human
from tracks import (
    FileTrackMetadata,
    iter_audio_files,
    link_track_to_library,
    link_track_to_playlist,
    linked_paths_for_library,
    list_playlist_member_tracks,
    normalize_artist,
    normalize_title,
    read_file_track_metadata,
    track_identity_from_member_row,
    unlink_track_from_library,
)


@dataclass
class ReconcileReport:
    missing_links_cleared: int = 0
    orphans_adopted: int = 0
    orphans_unmatched: int = 0
    results: list[ProcessResult] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "missing_links_cleared": self.missing_links_cleared,
            "orphans_adopted": self.orphans_adopted,
            "orphans_unmatched": self.orphans_unmatched,
            "results": [result.as_dict() for result in self.results],
        }


def track_file_present(
    library_id: int,
    track_id: int,
    save_directory: Path,
) -> bool:
    """True when the library DB points at an existing file in *save_directory*."""
    conn = db.get_connection()
    row = conn.execute(
        "SELECT local_path FROM library_tracks WHERE library_id = ? AND track_id = ?",
        (library_id, track_id),
    ).fetchone()
    if row is None:
        return False
    recorded = Path(row["local_path"])
    return (
        recorded.exists()
        and recorded.parent.resolve() == save_directory.resolve()
    )


def reconcile_missing_playlist_files(
    *,
    playlist_id: int,
    library_id: int,
    save_directory: Path,
) -> int:
    """
    Clear stale ``library_tracks`` rows when the recorded file is missing
    from the playlist folder so sync can re-download the track.
    """
    conn = db.get_connection()
    rows = conn.execute(
        """
        SELECT pi.track_id, lt.local_path
        FROM playlist_items pi
        LEFT JOIN library_tracks lt
          ON lt.library_id = ? AND lt.track_id = pi.track_id
        WHERE pi.playlist_id = ? AND pi.removed_at IS NULL
        """,
        (library_id, playlist_id),
    ).fetchall()

    cleared = 0
    for row in rows:
        local_path = row["local_path"]
        if not local_path:
            continue
        recorded = Path(local_path)
        if recorded.exists() and recorded.parent.resolve() == save_directory.resolve():
            continue
        unlink_track_from_library(library_id, row["track_id"])
        cleared += 1

    if cleared:
        print_human(
            f"Cleared {cleared} stale database link(s) for missing playlist file(s)."
        )
    return cleared


def _match_file_to_playlist_track(
    metadata: FileTrackMetadata,
    members: list[dict],
) -> Optional[int]:
    if metadata.isrc:
        target = normalize_isrc(metadata.isrc)
        isrc_matches = [
            member
            for member in members
            if member.get("isrc") and normalize_isrc(member["isrc"]) == target
        ]
        if len(isrc_matches) == 1:
            return int(isrc_matches[0]["track_id"])
        if len(isrc_matches) > 1:
            return None

    title_norm = normalize_title(metadata.title) if metadata.title else ""
    if not title_norm:
        return None

    artist_norm = normalize_artist(metadata.artist) if metadata.artist else ""
    title_matches = [
        member for member in members if member.get("title_norm") == title_norm
    ]
    if artist_norm:
        title_matches = [
            member
            for member in title_matches
            if member.get("artist_norm") == artist_norm
        ]
    if len(title_matches) == 1:
        return int(title_matches[0]["track_id"])
    return None


def list_orphan_audio_files(library_id: int, save_directory: Path) -> list[Path]:
    linked = linked_paths_for_library(library_id)
    return [
        path
        for path in iter_audio_files(save_directory)
        if path.resolve() not in linked
    ]


def adopt_orphan_playlist_files(
    *,
    playlist_id: int,
    library_id: int,
    save_directory: Path,
) -> ReconcileReport:
    """
    Link orphan audio files in the playlist folder when metadata matches a
    playlist track (ISRC first, then normalized title/artist).
    """
    report = ReconcileReport()
    members = list_playlist_member_tracks(playlist_id)
    if not members:
        return report

    members_by_id = {int(member["track_id"]): member for member in members}
    orphans = list_orphan_audio_files(library_id, save_directory)

    for path in orphans:
        metadata = read_file_track_metadata(path)
        track_id = _match_file_to_playlist_track(metadata, members)
        if track_id is None:
            report.orphans_unmatched += 1
            continue

        if track_file_present(library_id, track_id, save_directory):
            report.orphans_unmatched += 1
            continue

        member = members_by_id.get(track_id)
        if member is None:
            report.orphans_unmatched += 1
            continue

        identity = track_identity_from_member_row(member)
        if metadata.title:
            identity = TrackIdentity(
                spotify_track_id=identity.spotify_track_id,
                youtube_video_id=identity.youtube_video_id,
                isrc=identity.isrc or metadata.isrc,
                title=metadata.title,
                artist=metadata.artist or identity.artist,
                duration_seconds=identity.duration_seconds,
            )

        link_track_to_library(track_id, library_id, str(path.resolve()))
        link_track_to_playlist(track_id, playlist_id)

        result = ProcessResult(
            status="adopted",
            track_id=track_id,
            track=identity,
            local_path=str(path.resolve()),
            message="Adopted orphan file from playlist folder.",
        )
        report.results.append(result)
        report.orphans_adopted += 1
        print_human(
            f"Adopted orphan file '{path.name}' for '{identity.title}'."
        )

    if report.orphans_unmatched:
        print_human(
            f"Left {report.orphans_unmatched} unmatched orphan file(s) in the folder."
        )
    return report
