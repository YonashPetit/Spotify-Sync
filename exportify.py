"""Parse Exportify CSV exports and drive playlist import."""

from __future__ import annotations

import csv
import hashlib
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from get_content import parse_spotify_track_id
from job_control import SyncStopped, check_stop, clear_stop
from libraries import sanitize_dir_name
from metadata import TrackMetadata, save_playlist_cover
from models import ProcessResult, TrackIdentity
from output import print_human

_EXPORTIFY_EXTERNAL_PREFIX = "exportify:"


@dataclass(frozen=True)
class ExportifyRow:
    track_uri: str
    track_name: str
    artist_names: str
    album_name: str
    album_release_date: str
    album_image_url: str
    disc_number: Optional[int]
    track_number: Optional[int]
    duration_ms: Optional[int]
    isrc: Optional[str]
    explicit: bool
    popularity: Optional[int]
    added_at: Optional[str]

    @property
    def spotify_track_id(self) -> Optional[str]:
        if not self.track_uri.strip():
            return None
        try:
            return parse_spotify_track_id(self.track_uri)
        except ValueError:
            return None


def _unescape_exportify_field(value: str) -> str:
    return value.replace("\\,", ",").strip()


def _parse_int(value: str) -> Optional[int]:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _parse_bool(value: str) -> bool:
    return (value or "").strip().lower() in {"true", "1", "yes"}


def parse_exportify_csv(path: Path) -> list[ExportifyRow]:
    """Read an Exportify-exported CSV file and return rows in file order."""
    csv_path = Path(path).expanduser()
    if not csv_path.is_file():
        raise FileNotFoundError(f"Exportify CSV not found: {csv_path}")

    rows: list[ExportifyRow] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"Exportify CSV is empty: {csv_path}")

        required = {"Track URI", "Track Name", "Artist Name(s)"}
        missing = required - set(reader.fieldnames)
        if missing:
            raise ValueError(
                f"Exportify CSV missing required column(s): {', '.join(sorted(missing))}"
            )

        for line_number, raw in enumerate(reader, start=2):
            track_uri = (raw.get("Track URI") or "").strip()
            track_name = _unescape_exportify_field(raw.get("Track Name") or "")
            artist_names = _unescape_exportify_field(raw.get("Artist Name(s)") or "")
            if not track_uri and not track_name:
                continue
            if not track_name:
                print_human(
                    f"Skipping Exportify CSV line {line_number}: missing track name."
                )
                continue
            rows.append(
                ExportifyRow(
                    track_uri=track_uri,
                    track_name=track_name,
                    artist_names=artist_names,
                    album_name=_unescape_exportify_field(raw.get("Album Name") or ""),
                    album_release_date=(raw.get("Album Release Date") or "").strip(),
                    album_image_url=(raw.get("Album Image URL") or "").strip(),
                    disc_number=_parse_int(raw.get("Disc Number") or ""),
                    track_number=_parse_int(raw.get("Track Number") or ""),
                    duration_ms=_parse_int(raw.get("Track Duration (ms)") or ""),
                    isrc=(raw.get("ISRC") or "").strip() or None,
                    explicit=_parse_bool(raw.get("Explicit") or ""),
                    popularity=_parse_int(raw.get("Popularity") or ""),
                    added_at=(raw.get("Added At") or "").strip() or None,
                )
            )

    if not rows:
        raise ValueError(f"Exportify CSV contains no tracks: {csv_path}")
    return rows


def identity_from_row(row: ExportifyRow) -> TrackIdentity:
    duration_seconds = 0
    if row.duration_ms is not None and row.duration_ms > 0:
        duration_seconds = max(1, row.duration_ms // 1000)
    return TrackIdentity(
        spotify_track_id=row.spotify_track_id,
        youtube_video_id=None,
        isrc=row.isrc,
        title=row.track_name,
        artist=row.artist_names,
        duration_seconds=duration_seconds,
    )


def metadata_from_row(row: ExportifyRow) -> TrackMetadata:
    year: Optional[int] = None
    release_date = row.album_release_date
    if release_date and release_date[:4].isdigit():
        year = int(release_date[:4])
    return TrackMetadata(
        title=row.track_name,
        artist=row.artist_names,
        album=row.album_name,
        year=year,
        track_number=row.track_number,
        isrc=row.isrc,
        cover_url=row.album_image_url or None,
    )


def is_exportify_playlist(playlist: dict) -> bool:
    return str(playlist.get("external_id") or "").startswith(_EXPORTIFY_EXTERNAL_PREFIX)


def external_id_for_csv(csv_path: Path, playlist_name: str) -> str:
    stem = sanitize_dir_name(playlist_name)
    digest = hashlib.sha256(str(csv_path.resolve()).encode("utf-8")).hexdigest()[:10]
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", stem).strip("-").lower() or "playlist"
    return f"{_EXPORTIFY_EXTERNAL_PREFIX}{slug}-{digest}"


def default_playlist_name(csv_path: Path) -> str:
    stem = Path(csv_path).stem.strip()
    return stem or "Exportify Playlist"


def _source_url_for_row(row: ExportifyRow) -> Optional[str]:
    from sources import spotify_source

    if row.spotify_track_id:
        return spotify_source.track_url(row.spotify_track_id)
    return None


def _pause_between_songs() -> None:
    from sync import SYNC_SONG_DELAY_SECONDS

    if SYNC_SONG_DELAY_SECONDS > 0:
        time.sleep(SYNC_SONG_DELAY_SECONDS)


def import_exportify_csv(
    csv_path: Path,
    *,
    library_id: int,
    playlist_id: Optional[int] = None,
    create_playlist_name: Optional[str] = None,
    json_mode: bool = False,
) -> dict:
    """
    Import tracks from an Exportify CSV.

    Provide exactly one of *playlist_id* (add to existing playlist) or
    *create_playlist_name* (create a standalone exportify playlist).
    """
    import libraries
    import playlists as playlists_mod
    import sync as sync_mod

    if bool(playlist_id is not None) == bool(create_playlist_name):
        raise ValueError(
            "Provide exactly one of playlist_id or create_playlist_name."
        )

    rows = parse_exportify_csv(csv_path)
    created_playlist = False
    cover_path: Optional[str] = None

    if create_playlist_name is not None:
        playlist_name = create_playlist_name.strip() or default_playlist_name(csv_path)
        external_id = external_id_for_csv(csv_path, playlist_name)
        playlist_id, created_playlist = playlists_mod.add_playlist(
            source="spotify",
            external_id=external_id,
            library_id=library_id,
            name=playlist_name,
        )
        directory = libraries.playlist_dir(library_id, playlist_name, external_id)
        directory.mkdir(parents=True, exist_ok=True)
        first_cover = next(
            (row.album_image_url for row in rows if row.album_image_url), None
        )
        cover_path = save_playlist_cover(directory, first_cover)
        playlist = playlists_mod.get_playlist(playlist_id)
        save_directory = directory
        config = playlists_mod.playlist_duplicate_config(playlist_id)
        print_human(
            f"{'Created' if created_playlist else 'Using existing'} exportify playlist "
            f"{playlist_name!r} (id={playlist_id}) -> {directory}"
        )
        if cover_path:
            print_human(f"Saved playlist cover to {cover_path}.")
    else:
        assert playlist_id is not None
        playlist = playlists_mod.get_playlist(playlist_id)
        if playlist["library_id"] != library_id:
            raise ValueError(
                f"Playlist id {playlist_id} belongs to library "
                f"{playlist['library_id']}, not {library_id}."
            )
        if not playlist["enabled"]:
            name = playlist["name"] or playlist["external_id"]
            raise ValueError(
                f"Playlist {name!r} (id={playlist_id}) is disabled. "
                "Enable it before importing Exportify tracks."
            )
        save_directory = libraries.playlist_dir(
            library_id,
            playlist["name"] or playlist["external_id"],
            playlist["external_id"],
        )
        save_directory.mkdir(parents=True, exist_ok=True)
        config = playlists_mod.playlist_duplicate_config(playlist_id)
        name = playlist["name"] or playlist["external_id"]
        print_human(
            f"Importing {len(rows)} Exportify track(s) into playlist "
            f"{name!r} (id={playlist_id})."
        )

    results: list[ProcessResult] = []
    clear_stop()
    stopped = False
    for index, row in enumerate(rows, start=1):
        try:
            check_stop("exportify import")
        except SyncStopped as exc:
            stopped = True
            print_human(str(exc))
            break
        identity = identity_from_row(row)
        exportify_meta = metadata_from_row(row)
        print_human(
            f"Exportify import {index}/{len(rows)}: "
            f"{identity.artist!r} - {identity.title!r}"
        )
        result = sync_mod.process_track_for_playlist(
            playlist_id=playlist_id,
            identity=identity,
            save_directory=save_directory,
            library_id=library_id,
            config=config,
            json_mode=json_mode,
            source_url=_source_url_for_row(row),
            exportify_meta=exportify_meta,
        )
        results.append(result)
        _pause_between_songs()

    summary = sync_mod.summarize_results(results, len(rows))
    if stopped:
        print_human("Exportify import interrupted — completed songs are kept.")
    print_human(
        "Import summary: "
        f"{summary['total_items_seen']} seen, "
        f"{summary['downloaded']} downloaded, "
        f"{summary['skipped_duplicate']} duplicate-skipped, "
        f"{summary['skipped_blacklisted']} blacklisted, "
        f"{summary['already_present']} already present, "
        f"{summary['needs_user_choice']} need choice, "
        f"{summary['failed']} failed."
    )

    playlist = playlists_mod.get_playlist(playlist_id)
    return {
        "playlist": playlist,
        "playlist_id": playlist_id,
        "playlist_directory": str(save_directory),
        "created_playlist": created_playlist,
        "cover_path": cover_path,
        "track_count": len(rows),
        "results": [result.as_dict() for result in results],
        "summary": summary,
        "stopped": stopped,
    }
