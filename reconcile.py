"""Reconcile playlist folders with database state before sync."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import db
from isrc_match import normalize_isrc
from models import ProcessResult, TrackIdentity
from output import print_human, song_title
from tracks import (
    FileTrackMetadata,
    get_or_create_track,
    iter_audio_files,
    link_track_to_library,
    link_track_to_playlist,
    linked_path_keys_for_library,
    list_playlist_member_tracks,
    list_unlinked_tracks_for_library,
    normalize_artist,
    normalize_path_key,
    normalize_title,
    read_file_track_metadata,
    track_identity_from_member_row,
    unlink_track_from_library,
    update_track,
)

_DUPLICATE_STEM_SUFFIX = re.compile(r"\s\(\d+\)$")

@dataclass
class ReconcileReport:
    missing_links_cleared: int = 0
    cleared_track_labels: list[str] = field(default_factory=list)
    orphans_adopted: int = 0
    orphans_unmatched: int = 0
    results: list[ProcessResult] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "missing_links_cleared": self.missing_links_cleared,
            "cleared_track_labels": list(self.cleared_track_labels),
            "orphans_adopted": self.orphans_adopted,
            "orphans_unmatched": self.orphans_unmatched,
            "results": [result.as_dict() for result in self.results],
        }


def _cleared_track_label(title_norm: Optional[str], artist_norm: Optional[str]) -> str:
    title = song_title(title_norm or "")
    artist = (artist_norm or "").strip()
    if artist:
        return f"{title} by {artist}"
    return title


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
) -> tuple[int, list[str]]:
    """
    Clear stale ``library_tracks`` rows when the recorded file is missing
    from the playlist folder so sync can re-download the track.

    Returns ``(cleared_count, cleared_track_labels)``.
    """
    conn = db.get_connection()
    rows = conn.execute(
        """
        SELECT pi.track_id, lt.local_path, t.title_norm, t.artist_norm
        FROM playlist_items pi
        JOIN tracks t ON t.id = pi.track_id
        LEFT JOIN library_tracks lt
          ON lt.library_id = ? AND lt.track_id = pi.track_id
        WHERE pi.playlist_id = ? AND pi.removed_at IS NULL
        """,
        (library_id, playlist_id),
    ).fetchall()

    cleared = 0
    cleared_labels: list[str] = []
    for row in rows:
        local_path = row["local_path"]
        if not local_path:
            continue
        recorded = Path(local_path)
        if recorded.exists() and recorded.parent.resolve() == save_directory.resolve():
            continue
        unlink_track_from_library(library_id, row["track_id"])
        cleared += 1
        cleared_labels.append(
            _cleared_track_label(row["title_norm"], row["artist_norm"])
        )

    if cleared:
        names = ", ".join(repr(label) for label in cleared_labels)
        print_human(
            f"Cleared {cleared} stale database link(s) for missing playlist file(s): "
            f"{names}."
        )
    return cleared, cleared_labels


def _catalog_entry_from_identity(identity: TrackIdentity) -> dict:
    from tracks import existing_track_id_for_identity

    return {
        "track_id": existing_track_id_for_identity(identity),
        "spotify_track_id": identity.spotify_track_id,
        "youtube_video_id": identity.youtube_video_id,
        "isrc": identity.isrc,
        "title_norm": normalize_title(identity.title),
        "artist_norm": normalize_artist(identity.artist),
        "duration_seconds": identity.duration_seconds,
        "_identity": identity,
    }


def _catalog_entry_from_member_row(row: dict) -> dict:
    entry = dict(row)
    entry["_identity"] = track_identity_from_member_row(row)
    return entry


def _catalog_dedupe_key(identity: TrackIdentity) -> str:
    if identity.spotify_track_id:
        return f"spotify:{identity.spotify_track_id}"
    if identity.youtube_video_id:
        return f"youtube:{identity.youtube_video_id}"
    if identity.isrc:
        return f"isrc:{normalize_isrc(identity.isrc)}"
    return f"title:{normalize_title(identity.title)}|{normalize_artist(identity.artist)}"


def load_adoption_catalog(playlist: dict, library_id: int) -> list[dict]:
    """
    All tracks an orphan may belong to: live playlist source plus any DB rows
    not yet linked in this library (failed imports, partial syncs).
    """
    from exportify import is_exportify_playlist

    entries: list[dict] = []
    seen: set[str] = set()

    def add_identity(identity: TrackIdentity) -> None:
        key = _catalog_dedupe_key(identity)
        if key in seen:
            return
        seen.add(key)
        entries.append(_catalog_entry_from_identity(identity))

    def add_member_row(row: dict) -> None:
        identity = track_identity_from_member_row(row)
        key = _catalog_dedupe_key(identity)
        if key in seen:
            return
        seen.add(key)
        entries.append(_catalog_entry_from_member_row(row))

    playlist_id = int(playlist["playlist_id"])

    if is_exportify_playlist(playlist):
        for row in list_playlist_member_tracks(playlist_id):
            add_member_row(row)
        for row in list_unlinked_tracks_for_library(library_id):
            add_member_row(row)
        return entries

    if playlist["source"] == "spotify":
        from sources import spotify_source

        for identity in spotify_source.iter_playlist_track_identities(
            playlist["external_id"]
        ):
            add_identity(identity)
    else:
        from sources import youtube_source

        playlist_url = youtube_source.playlist_url(playlist["external_id"])
        for batch in youtube_source.iter_playlist_video_batches(playlist_url):
            for identity in batch:
                add_identity(identity)

    for row in list_playlist_member_tracks(playlist_id):
        add_member_row(row)
    for row in list_unlinked_tracks_for_library(library_id):
        add_member_row(row)
    return entries


def _base_identity_from_entry(entry: dict) -> TrackIdentity:
    identity = entry.get("_identity")
    if isinstance(identity, TrackIdentity):
        return identity
    return track_identity_from_member_row(entry)


def _title_from_filename(path: Path) -> str:
    """Derive a title from the filename stem, stripping duplicate suffixes."""
    stem = _DUPLICATE_STEM_SUFFIX.sub("", path.stem)
    return stem.strip()


def _match_file_by_isrc(
    metadata: FileTrackMetadata,
    catalog: list[dict],
    *,
    path: Optional[Path] = None,
) -> Optional[dict]:
    """Return the unique catalog entry with a matching ISRC, if any."""
    file_isrc = metadata.isrc
    if not file_isrc and path is not None:
        from tracks import read_file_isrc

        file_isrc = read_file_isrc(path)
    if not file_isrc:
        return None
    target = normalize_isrc(file_isrc)
    if not target:
        return None

    matches = [
        entry
        for entry in catalog
        if entry.get("isrc") and normalize_isrc(entry["isrc"]) == target
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return None

    title_candidates = _title_candidates(metadata, path)
    for title_norm in title_candidates:
        if not title_norm:
            continue
        title_matches = [
            entry
            for entry in catalog
            if not entry.get("isrc") and entry.get("title_norm") == title_norm
        ]
        if len(title_matches) == 1:
            return title_matches[0]
    return None


def _title_candidates(
    metadata: FileTrackMetadata, path: Optional[Path] = None
) -> list[str]:
    candidates: list[str] = []
    if metadata.title:
        candidates.append(normalize_title(metadata.title))
    if path is not None:
        from_stem = normalize_title(_title_from_filename(path))
        if from_stem and from_stem not in candidates:
            candidates.append(from_stem)
    return candidates


def _match_file_by_youtube_id(path: Path, catalog: list[dict]) -> Optional[dict]:
    stem = path.stem
    matches = [
        entry
        for entry in catalog
        if entry.get("youtube_video_id")
        and (
            stem == entry["youtube_video_id"]
            or stem.endswith(f"_{entry['youtube_video_id']}")
        )
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def _match_file_by_title_artist(
    metadata: FileTrackMetadata,
    catalog: list[dict],
    *,
    path: Optional[Path] = None,
) -> Optional[dict]:
    """Fallback match on normalized title (+ artist when needed to disambiguate)."""
    title_candidates = _title_candidates(metadata, path)
    if not title_candidates:
        return None

    artist_norm = normalize_artist(metadata.artist) if metadata.artist else ""

    for title_norm in title_candidates:
        title_matches = [
            entry for entry in catalog if entry.get("title_norm") == title_norm
        ]
        if not title_matches:
            continue
        if len(title_matches) == 1:
            return title_matches[0]
        if artist_norm:
            artist_matches = [
                entry
                for entry in title_matches
                if entry.get("artist_norm") == artist_norm
            ]
            if len(artist_matches) == 1:
                return artist_matches[0]
    return None


def _match_file_to_catalog_entry(
    path: Path,
    metadata: FileTrackMetadata,
    catalog: list[dict],
) -> tuple[Optional[dict], Optional[str]]:
    """
    Match an orphan file to a playlist catalog entry.

    Priority: ISRC, YouTube id filename, then normalized title/artist.
    """
    entry = _match_file_by_isrc(metadata, catalog, path=path)
    if entry is not None:
        return entry, "isrc"

    entry = _match_file_by_youtube_id(path, catalog)
    if entry is not None:
        return entry, "path"

    entry = _match_file_by_title_artist(metadata, catalog, path=path)
    if entry is not None:
        return entry, "metadata"
    return None, None


def _identity_for_adopted_file(
    entry: dict,
    metadata: FileTrackMetadata,
    *,
    prefer_file_metadata: bool,
) -> TrackIdentity:
    """Build the identity to persist (playlist source + file tags)."""
    base = _base_identity_from_entry(entry)
    if prefer_file_metadata:
        return TrackIdentity(
            spotify_track_id=base.spotify_track_id,
            youtube_video_id=base.youtube_video_id,
            isrc=metadata.isrc or base.isrc,
            title=metadata.title or base.title,
            artist=metadata.artist or base.artist,
            duration_seconds=(
                metadata.duration_seconds
                if metadata.duration_seconds > 0
                else base.duration_seconds
            ),
        )
    return TrackIdentity(
        spotify_track_id=base.spotify_track_id,
        youtube_video_id=base.youtube_video_id,
        isrc=base.isrc or metadata.isrc,
        title=metadata.title or base.title,
        artist=metadata.artist or base.artist,
        duration_seconds=(
            metadata.duration_seconds
            if metadata.duration_seconds > 0
            else base.duration_seconds
        ),
    )


def _identity_from_file_metadata(metadata: FileTrackMetadata) -> TrackIdentity:
    """Build a TrackIdentity solely from local file tags / filename."""
    return TrackIdentity(
        spotify_track_id=None,
        youtube_video_id=None,
        isrc=metadata.isrc,
        title=metadata.title or "Unknown",
        artist=metadata.artist or "",
        duration_seconds=max(0, metadata.duration_seconds),
    )


def list_orphan_audio_files(library_id: int, save_directory: Path) -> list[Path]:
    linked = linked_path_keys_for_library(library_id)
    return [
        path
        for path in iter_audio_files(save_directory)
        if normalize_path_key(path) not in linked
    ]


def adopt_orphan_playlist_files(
    *,
    playlist_id: int,
    library_id: int,
    save_directory: Path,
    playlist: Optional[dict] = None,
) -> ReconcileReport:
    """
    Adopt audio files that exist in the playlist folder but are not linked in the
    database.

    Orphans are local-only files (not necessarily on Spotify / YouTube). Each is
    registered like a normal playlist add:
    ``tracks`` → ``library_tracks`` → ``playlist_items``.

    If the file's ISRC uniquely matches an existing catalog/DB track, that
    identity is reused so duplicates are not created; otherwise a new track row
    is created from the file's tags/filename.
    """
    import playlists as playlists_mod

    report = ReconcileReport()
    orphans = list_orphan_audio_files(library_id, save_directory)
    if not orphans:
        return report

    if playlist is None:
        playlist = playlists_mod.get_playlist(playlist_id)
    playlist = dict(playlist)
    playlist["playlist_id"] = playlist_id

    catalog: list[dict] = []
    try:
        catalog = load_adoption_catalog(playlist, library_id)
    except Exception:
        catalog = [
            _catalog_entry_from_member_row(row)
            for row in list_playlist_member_tracks(playlist_id)
        ]

    skipped_duplicates = 0

    for path in orphans:
        metadata = read_file_track_metadata(path)
        if not metadata.title.strip():
            report.orphans_unmatched += 1
            print_human(
                f"Could not adopt orphan file '{path.name}': missing title."
            )
            continue

        entry, method = _match_file_to_catalog_entry(path, metadata, catalog)
        if entry is not None and method is not None:
            identity = _identity_for_adopted_file(
                entry,
                metadata,
                prefer_file_metadata=True,
            )
            adopt_method = method
        else:
            # Local-only orphan: create a fresh track from file metadata.
            identity = _identity_from_file_metadata(metadata)
            adopt_method = "local_file"

        track_id = get_or_create_track(identity)
        update_track(track_id, identity)

        if track_file_present(library_id, track_id, save_directory):
            skipped_duplicates += 1
            print_human(
                f"Skipped duplicate orphan '{path.name}' "
                f"(track already linked: {identity.title!r})."
            )
            continue

        resolved_path = str(path.resolve())
        link_track_to_library(track_id, library_id, resolved_path)
        link_track_to_playlist(track_id, playlist_id)

        method_label = {
            "isrc": "ISRC match",
            "path": "YouTube id filename",
            "metadata": "title/artist match",
            "local_file": "local file metadata",
        }.get(adopt_method, adopt_method)

        result = ProcessResult(
            status="adopted",
            track_id=track_id,
            track=identity,
            local_path=resolved_path,
            message=f"Adopted orphan file via {method_label}.",
        )
        report.results.append(result)
        report.orphans_adopted += 1
        print_human(
            f"Adopted orphan file '{path.name}' as '{identity.title}' "
            f"({method_label})."
        )

    if report.orphans_unmatched:
        print_human(
            f"Left {report.orphans_unmatched} orphan file(s) that could not be adopted."
        )
    if skipped_duplicates:
        print_human(
            f"Skipped {skipped_duplicates} duplicate orphan file(s) "
            "(matching tracks already linked)."
        )
    return report
