"""spotify_sync command-line interface (human + Hermes JSON modes)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

import db
import libraries
import matching_settings as matching_settings_mod
import output
import playlists as playlists_mod
import settings
import status_overview
import sync as sync_mod
from blacklist import blacklist_track, list_blacklisted
from downloader import DownloadError
from libraries import LibraryNotFoundError
from metadata import save_playlist_cover
from models import DuplicateConfig, DuplicateResult, ProcessResult, TrackIdentity
from output import (
    emit_error,
    emit_success,
    log_download_start,
    log_operation_error,
    log_operation_start,
    log_operation_success,
    log_process_result,
    print_human,
)
from playlists import PlaylistNotFoundError
from sources import spotify_source, youtube_source
from tracks import get_or_create_track, get_track_identity

EXIT_OK = 0
EXIT_VALIDATION = 2
EXIT_EXTERNAL = 3

ALLOWED_DUPLICATE_ACTIONS = ["skip", "replace", "keep_both"]


class CliError(Exception):
    def __init__(self, code: str, message: str, exit_code: int = EXIT_VALIDATION):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


# ---------------------------------------------------------------------------
# helpers


def _library_dict(library_id: int) -> dict:
    return libraries.get_library_row(library_id)


def _decision_request(result: ProcessResult, library_id: int,
                      playlist_id: Optional[int]) -> dict:
    return {
        "request_id": result.request_id,
        "allowed_actions": ALLOWED_DUPLICATE_ACTIONS,
        "recommended_action": "skip",
        "context": {"library_id": library_id, "playlist_id": playlist_id},
    }


def _print_result_human(result: ProcessResult) -> None:
    """Legacy detailed line; sync/add-track now use title-based logs in output."""
    from output import log_process_result

    log_process_result(result)


def _print_summary_human(summary: dict) -> None:
    print_human(
        "Summary: "
        f"{summary['total_items_seen']} seen, "
        f"{summary['new_items_processed']} processed, "
        f"{summary['downloaded']} downloaded, "
        f"{summary['skipped_duplicate']} duplicate-skipped, "
        f"{summary['skipped_blacklisted']} blacklisted, "
        f"{summary['already_present']} already present, "
        f"{summary['needs_user_choice']} need choice, "
        f"{summary['failed']} failed."
    )


def _resolve_track_identity(args: argparse.Namespace) -> tuple[TrackIdentity, Optional[str]]:
    """Resolve a track identity + source URL from add-track/blacklist args."""
    try:
        if getattr(args, "spotify_track_url", None):
            identity = spotify_source.fetch_track_identity(args.spotify_track_url)
            return identity, spotify_source.track_url(identity.spotify_track_id)
        if getattr(args, "youtube_url", None):
            identity = youtube_source.fetch_video_identity(args.youtube_url)
            return identity, youtube_source.watch_url(identity.youtube_video_id)
        if getattr(args, "spotify_playlist_url", None):
            if args.index is None:
                raise CliError(
                    "INVALID_ARGUMENT",
                    "--index is required with --spotify-playlist-url.",
                )
            identity = spotify_source.get_playlist_track_by_index(
                args.spotify_playlist_url, args.index
            )
            return identity, spotify_source.track_url(identity.spotify_track_id)
        if getattr(args, "youtube_playlist_url", None):
            if args.index is None:
                raise CliError(
                    "INVALID_ARGUMENT",
                    "--index is required with --youtube-playlist-url.",
                )
            identity = youtube_source.get_playlist_video_by_index(
                args.youtube_playlist_url, args.index
            )
            return identity, youtube_source.watch_url(identity.youtube_video_id)
    except IndexError as exc:
        raise CliError("PLAYLIST_INDEX_OUT_OF_RANGE", str(exc)) from exc
    except ValueError as exc:
        raise CliError("INVALID_ARGUMENT", str(exc)) from exc

    raise CliError(
        "INVALID_ARGUMENT",
        "Provide one of --spotify-track-url, --youtube-url, "
        "--spotify-playlist-url --index N, or --youtube-playlist-url --index N.",
    )


# ---------------------------------------------------------------------------
# command handlers (each returns the JSON `data` dict)


def cmd_login(args: argparse.Namespace) -> dict:
    import spotify_auth

    log_operation_start("login")
    removed: list[str] = []
    print_human(
        "Opening your browser for Spotify authorization. Make sure "
        f"{spotify_auth.redirect_uri()} is registered as a Redirect URI in "
        "your Spotify app settings (developer.spotify.com/dashboard)."
    )
    client, removed = spotify_auth.login_interactive(force=True)
    if removed:
        print_human("Cleared existing Spotify token cache.")
    me = client.me()
    print_human(f"Logged in as {me.get('display_name') or me['id']}.")
    log_operation_success("login")
    return {
        "authenticated": True,
        "user": {"id": me["id"], "display_name": me.get("display_name")},
        "scopes": spotify_auth.OAUTH_SCOPES,
        "token_cache": str(spotify_auth.token_cache_path()),
        "cleared_caches": removed,
    }


def cmd_set_download_path(args: argparse.Namespace) -> dict:
    log_operation_start("set-download-path")
    path = Path(args.path).expanduser()
    existing = libraries.find_library_by_path(str(path))
    library_id = libraries.get_or_create_library(str(path), name=args.name)
    created = existing is None
    if settings.get_selected_library_id() is None:
        settings.set_selected_library_id(library_id)
    library = _library_dict(library_id)
    print_human(
        f"{'Created' if created else 'Found existing'} library "
        f"{library['name'] or library['path']} (id={library_id})."
    )
    log_operation_success("set-download-path")
    return {"library": library, "created": created}


def cmd_select_download_path(args: argparse.Namespace) -> dict:
    log_operation_start("select-download-path")
    if not args.path and not args.name:
        raise CliError("INVALID_ARGUMENT", "Provide --path or --name.")
    if args.path:
        library_id = libraries.find_library_by_path(args.path)
        if library_id is None:
            raise CliError(
                "LIBRARY_NOT_FOUND",
                f"No library registered at path {args.path!r}. "
                "Run set-download-path first.",
            )
    else:
        library_id = libraries.find_library_by_name(args.name)
        if library_id is None:
            raise CliError("LIBRARY_NOT_FOUND", f"No library named {args.name!r}.")
    settings.set_selected_library_id(library_id)
    library = _library_dict(library_id)
    print_human(f"Selected library {library['name'] or library['path']} (id={library_id}).")
    log_operation_success("select-download-path")
    return {"selected_library": library}


def _resolve_library_id(args: argparse.Namespace) -> int:
    """Resolve --library-id, --path, or --name to a library id."""
    provided = sum(
        1
        for value in (args.library_id, args.path, args.name)
        if value is not None
    )
    if provided != 1:
        raise CliError(
            "INVALID_ARGUMENT",
            "Provide exactly one of --library-id, --path, or --name.",
        )
    if args.library_id is not None:
        libraries.get_library_row(args.library_id)
        return args.library_id
    if args.path:
        library_id = libraries.find_library_by_path(args.path)
        if library_id is None:
            raise CliError(
                "LIBRARY_NOT_FOUND",
                f"No library registered at path {args.path!r}.",
            )
        return library_id
    library_id = libraries.find_library_by_name(args.name)
    if library_id is None:
        raise CliError("LIBRARY_NOT_FOUND", f"No library named {args.name!r}.")
    return library_id


def cmd_delete_download_path(args: argparse.Namespace) -> dict:
    log_operation_start("delete-download-path")
    library_id = _resolve_library_id(args)
    library = _library_dict(library_id)
    result = libraries.delete_library(library_id)
    if result["was_selected"]:
        print_human(
            f"Removed library {library['name'] or library['path']} (id={library_id}). "
            "It was the selected library; selection cleared."
        )
    else:
        print_human(
            f"Removed library {library['name'] or library['path']} (id={library_id})."
        )
    if result["playlists_removed"]:
        print_human(
            f"Stopped tracking {result['playlists_removed']} playlist(s) "
            "for this library."
        )
    log_operation_success("delete-download-path")
    return {"library": library, **result}


def cmd_remove_playlist(args: argparse.Namespace) -> dict:
    log_operation_start("remove-playlist")
    playlist = playlists_mod.get_playlist(args.playlist_id)
    playlists_mod.remove_playlist(args.playlist_id)
    print_human(
        f"Stopped tracking {playlist['source']} playlist "
        f"{playlist['name']!r} (id={args.playlist_id}). "
        "Downloaded files were not deleted."
    )
    log_operation_success("remove-playlist")
    return {"playlist": playlist, "removed": True}


def cmd_unset_playlist(args: argparse.Namespace) -> dict:
    log_operation_start("unset-playlist")
    playlist = playlists_mod.set_playlist_enabled(args.playlist_id, False)
    name = playlist["name"] or playlist["external_id"]
    print_human(
        f"Unset tracked playlist {name!r} (id={args.playlist_id}). "
        "sync --all will skip it."
    )
    log_operation_success("unset-playlist")
    return {"playlist": playlist, "unset": True}


def cmd_set_cookies(args: argparse.Namespace) -> dict:
    log_operation_start("set-cookies")
    cookies_path = Path(args.cookies_file).expanduser()
    if not cookies_path.is_file():
        raise CliError(
            "COOKIES_FILE_NOT_FOUND",
            f"Cookies file not found: {cookies_path}",
        )
    settings.set_cookies_file(str(cookies_path.resolve()))
    print_human(f"Cookies file set to {cookies_path.resolve()}.")
    log_operation_success("set-cookies")
    return {"cookies_file": str(cookies_path.resolve()), "scope": "global"}


def cmd_add_playlist(args: argparse.Namespace) -> dict:
    log_operation_start("add-playlist")
    if bool(args.spotify_playlist_url) == bool(args.youtube_playlist_url):
        raise CliError(
            "INVALID_ARGUMENT",
            "Provide exactly one of --spotify-playlist-url or --youtube-playlist-url.",
        )

    library_id = libraries.resolve_library(dest=args.dest, library_name=args.library)

    if args.spotify_playlist_url:
        source = "spotify"
        meta = spotify_source.fetch_playlist_metadata(args.spotify_playlist_url)
    else:
        source = "youtube"
        meta = youtube_source.fetch_playlist_metadata(args.youtube_playlist_url)

    playlist_id, created = playlists_mod.add_playlist(
        source=source,
        external_id=meta["external_id"],
        library_id=library_id,
        name=meta["name"],
    )
    directory = libraries.playlist_dir(library_id, meta["name"], meta["external_id"])
    folder_existed = directory.is_dir()
    directory.mkdir(parents=True, exist_ok=True)
    cover_path = save_playlist_cover(directory, meta.get("cover_url"))

    playlist = playlists_mod.get_playlist(playlist_id)
    folder_note = "using existing folder" if folder_existed else "created folder"
    print_human(
        f"{'Added' if created else 'Already tracking'} {source} playlist "
        f"{meta['name']!r} (id={playlist_id}) -> {directory} ({folder_note})"
    )
    if cover_path:
        print_human(f"Saved playlist cover to {cover_path}.")
    log_operation_success("add-playlist")
    return {
        "playlist": playlist,
        "playlist_directory": str(directory),
        "created": created,
        "folder_existed": folder_existed,
        "cover_path": cover_path,
    }


def cmd_add_track(args: argparse.Namespace, json_mode: bool) -> dict:
    log_operation_start("add-track")
    identity, source_url = _resolve_track_identity(args)

    playlist_id: Optional[int] = None
    config = DuplicateConfig()
    if args.from_playlist is not None:
        playlist = playlists_mod.get_playlist(args.from_playlist)
        playlist_id = playlist["playlist_id"]
        config = playlists_mod.playlist_duplicate_config(playlist_id)
        library_id = (
            libraries.get_or_create_library(args.dest)
            if args.dest
            else playlist["library_id"]
        )
        save_directory = libraries.playlist_dir(
            library_id,
            playlist["name"] or playlist["external_id"],
            playlist["external_id"],
        )
    else:
        library_id = libraries.resolve_library(dest=args.dest, library_name=None)
        save_directory = libraries.get_library_path(library_id)

    result = sync_mod.process_track_for_playlist(
        playlist_id=playlist_id,
        identity=identity,
        save_directory=save_directory,
        library_id=library_id,
        config=config,
        json_mode=json_mode,
        source_url=source_url,
    )

    data: dict = {"result": result.as_dict()}
    if result.status == "needs_user_choice":
        data["decision_request"] = _decision_request(result, library_id, playlist_id)
    if result.status == "failed":
        raise CliError(
            "DOWNLOAD_FAILED", result.message or "Download failed.", EXIT_EXTERNAL
        )
    log_operation_success("add-track")
    return data


def cmd_list_playlists(args: argparse.Namespace) -> dict:
    log_operation_start("list-playlists")
    playlists = playlists_mod.list_playlists()
    if not playlists:
        print_human("No playlists are being tracked.")
    for playlist in playlists:
        name = playlist["name"] or playlist["external_id"]
        status = "enabled" if playlist["enabled"] else "disabled"
        print_human(
            f"[{playlist['playlist_id']}] {playlist['source']}: {name!r} "
            f"({status}, library_id={playlist['library_id']}, "
            f"external_id={playlist['external_id']})"
        )
    log_operation_success("list-playlists")
    return {"count": len(playlists), "playlists": playlists}


def _print_matching_toggles(config: matching_settings_mod.MatchingSettings) -> None:
    print_human(
        "Duplicate phase: "
        f"chromaprint={_toggle_label(config.duplicate_chromaprint)}, "
        f"embedding={_toggle_label(config.duplicate_embedding)}"
    )
    print_human(
        "Comparison phase: "
        f"chromaprint={_toggle_label(config.comparison_chromaprint)}, "
        f"embedding={_toggle_label(config.comparison_embedding)}"
    )


def _toggle_label(value: bool) -> str:
    return "on" if value else "off"


def _collect_toggle_updates(args: argparse.Namespace, fields: list[str]) -> dict:
    updates: dict = {}
    for field in fields:
        value = getattr(args, field, None)
        if value is not None:
            updates[field] = matching_settings_mod.parse_toggle(value)
    return updates


def cmd_set_audio_matching(args: argparse.Namespace) -> dict:
    log_operation_start("set-audio-matching")
    updates = _collect_toggle_updates(
        args,
        [
            "duplicate_chromaprint",
            "duplicate_embedding",
            "comparison_chromaprint",
            "comparison_embedding",
        ],
    )
    if not updates:
        raise CliError(
            "INVALID_ARGUMENT",
            "Provide at least one toggle: "
            "--duplicate-chromaprint, --duplicate-embedding, "
            "--comparison-chromaprint, or --comparison-embedding (on/off).",
        )
    try:
        config = matching_settings_mod.update_matching_settings(**updates)
    except ValueError as exc:
        raise CliError("INVALID_ARGUMENT", str(exc)) from exc
    _print_matching_toggles(config)
    log_operation_success("set-audio-matching")
    return {"matching_settings": config.as_dict()}


def cmd_set_thresholds(args: argparse.Namespace) -> dict:
    log_operation_start("set-thresholds")
    updates: dict = {}
    for field in (
        "metadata_minimum_rating",
        "audio_duplicate_threshold",
        "audio_review_threshold",
        "chromaprint_match_certainty",
        "embedding_match_threshold",
    ):
        value = getattr(args, field, None)
        if value is not None:
            updates[field] = float(value)
    if not updates:
        raise CliError(
            "INVALID_ARGUMENT",
            "Provide at least one threshold flag "
            "(e.g. --metadata-minimum-rating, --audio-duplicate-threshold).",
        )
    try:
        config = matching_settings_mod.update_matching_settings(**updates)
    except ValueError as exc:
        raise CliError("INVALID_ARGUMENT", str(exc)) from exc
    for key, value in updates.items():
        print_human(f"Set {key} to {value}.")
    log_operation_success("set-thresholds")
    return {"matching_settings": config.as_dict(), "updated": updates}


def cmd_set_scoring_weights(args: argparse.Namespace) -> dict:
    log_operation_start("set-scoring-weights")
    field_map = {
        "artist": "weight_artist",
        "title": "weight_title",
        "duration": "weight_duration",
        "official_channel": "weight_official_channel",
        "album": "weight_album",
        "release_year": "weight_release_year",
    }
    updates: dict = {}
    for arg_name, setting_name in field_map.items():
        value = getattr(args, arg_name, None)
        if value is not None:
            updates[setting_name] = float(value)
    if not updates:
        raise CliError(
            "INVALID_ARGUMENT",
            "Provide at least one weight "
            "(--artist, --title, --duration, --official-channel, --album, --release-year). "
            "All six weights must sum to 100 after update.",
        )
    try:
        config = matching_settings_mod.update_matching_settings(**updates)
    except ValueError as exc:
        raise CliError("INVALID_ARGUMENT", str(exc)) from exc
    print_human(f"Scoring weights now sum to {config.scoring_weight_total():.0f}.")
    for key, value in updates.items():
        print_human(f"Set {key} to {value}.")
    log_operation_success("set-scoring-weights")
    return {"matching_settings": config.as_dict(), "updated": updates}


def cmd_show_settings(args: argparse.Namespace) -> dict:
    log_operation_start("show-settings")
    overview = status_overview.gather_settings_overview()
    status_overview.print_settings_overview(overview)
    log_operation_success("show-settings")
    return overview


def cmd_sync(args: argparse.Namespace, json_mode: bool) -> dict:
    log_operation_start("sync")
    if bool(args.all) == (args.playlist_id is not None):
        raise CliError("INVALID_ARGUMENT", "Provide exactly one of --playlist-id or --all.")

    def report_to_data(report: sync_mod.SyncReport) -> dict:
        results = [result.as_dict() for result in report.results]
        decision_requests = [
            _decision_request(
                result,
                playlists_mod.get_playlist(report.playlist_id)["library_id"],
                report.playlist_id,
            )
            for result in report.results
            if result.status == "needs_user_choice"
        ]
        data = {
            "playlist_id": report.playlist_id,
            "results": results,
            "summary": report.summary,
        }
        if decision_requests:
            data["decision_requests"] = decision_requests
        return data

    if args.playlist_id is not None:
        playlist = playlists_mod.get_playlist(args.playlist_id)
        if not playlist["enabled"]:
            name = playlist["name"] or playlist["external_id"]
            raise CliError(
                "PLAYLIST_DISABLED",
                f"Playlist {name!r} (id={args.playlist_id}) cannot be synced: "
                "this song was unset/disabled.",
            )
        playlist_name = playlist["name"] or playlist["external_id"]
        print_human(f"Syncing playlist {playlist_name!r} (id={args.playlist_id}).")
        report = sync_mod.sync_playlist(args.playlist_id, json_mode=json_mode)
        _print_summary_human(report.summary)
        log_operation_success("sync")
        return report_to_data(report)

    print_human("Syncing all enabled playlists.")
    all_reports = sync_mod.sync_all(json_mode=json_mode)
    playlists_data = []
    aggregate = {
        "total_items_seen": 0,
        "new_items_processed": 0,
        "downloaded": 0,
        "skipped_duplicate": 0,
        "skipped_blacklisted": 0,
        "needs_user_choice": 0,
        "failed": 0,
        "already_present": 0,
    }
    for playlist_id, report in all_reports.items():
        playlist = playlists_mod.get_playlist(playlist_id)
        playlist_name = playlist["name"] or playlist["external_id"]
        print_human(f"Finished playlist {playlist_name!r} (id={playlist_id}).")
        _print_summary_human(report.summary)
        playlists_data.append(report_to_data(report))
        for key in aggregate:
            aggregate[key] += report.summary[key]
    log_operation_success("sync")
    return {"playlists": playlists_data, "summary": aggregate}


def cmd_blacklist_song(args: argparse.Namespace) -> dict:
    log_operation_start("blacklist-song")
    if args.playlist_id is not None:
        playlists_mod.get_playlist(args.playlist_id)  # validate existence

    identity, _source_url = _resolve_track_identity(args)
    track_id = get_or_create_track(identity)
    blacklist_id, created = blacklist_track(
        track_id, playlist_id=args.playlist_id, reason=args.reason
    )

    conn = db.get_connection()
    row = conn.execute(
        "SELECT created_at FROM blacklist WHERE id = ?", (blacklist_id,)
    ).fetchone()
    scope = "global" if args.playlist_id is None else "playlist"
    print_human(
        f"{'Blacklisted' if created else 'Already blacklisted'} "
        f"{identity.title!r} ({scope})."
    )
    log_operation_success("blacklist-song")
    return {
        "blacklist_entry": {
            "blacklist_id": blacklist_id,
            "track": identity.as_dict(track_id),
            "scope": scope,
            "playlist_id": args.playlist_id,
            "reason": args.reason,
            "created_at": row["created_at"],
        },
        "created": created,
    }


def cmd_list_blacklisted(args: argparse.Namespace) -> dict:
    log_operation_start("list-blacklisted")
    entries = list_blacklisted(playlist_id=args.playlist_id)
    for entry in entries:
        print_human(
            f"[{entry['blacklist_id']}] {entry['track']['title']!r} "
            f"({entry['scope']})"
            + (f" reason: {entry['reason']}" if entry["reason"] else "")
        )
    if not entries:
        print_human("No blacklisted tracks.")
    log_operation_success("list-blacklisted")
    return {"count": len(entries), "entries": entries}


def cmd_resolve_duplicate(args: argparse.Namespace, json_mode: bool) -> dict:
    log_operation_start("resolve-duplicate")
    pending = sync_mod.get_pending_decision(args.request_id)
    if pending is None:
        raise CliError(
            "DUPLICATE_REQUEST_NOT_FOUND",
            f"No pending duplicate request with id {args.request_id!r}.",
        )

    identity = get_track_identity(pending["track_id"])
    duplicate = DuplicateResult(
        existing_track_id=pending["existing_track_id"],
        existing_local_path=pending["existing_local_path"],
        method=pending["method"],
        confidence=pending["confidence"] or "high",
        score=pending["score"],
    )

    if args.action == "skip":
        sync_mod.delete_pending_decision(args.request_id)
        result = ProcessResult(
            status="skipped_duplicate",
            track_id=pending["track_id"],
            track=identity,
            local_path=None,
            duplicate=duplicate,
            message="Duplicate skipped by user decision.",
        )
        log_process_result(result)
        log_operation_success("resolve-duplicate")
        return {"result": result.as_dict(), "action": "skip"}

    if args.action == "replace":
        sync_mod._remove_existing_track(pending["library_id"], duplicate)

    log_download_start(identity.title)
    try:
        local_path = sync_mod.download_track(
            identity,
            save_directory=Path(pending["save_directory"]),
            source_url=pending["source_url"],
        )
    except Exception as exc:
        raise CliError("DOWNLOAD_FAILED", f"Download failed: {exc}", EXIT_EXTERNAL) from exc

    result = sync_mod.finalize_downloaded_track(
        track_id=pending["track_id"],
        identity=identity,
        local_path=local_path,
        library_id=pending["library_id"],
        playlist_id=pending["playlist_id"],
        duplicate=duplicate,
    )
    sync_mod.delete_pending_decision(args.request_id)
    log_process_result(result)
    log_operation_success("resolve-duplicate")
    return {"result": result.as_dict(), "action": args.action}


# ---------------------------------------------------------------------------
# argument parsing


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spotify-sync",
        description="Sync Spotify / YouTube playlists to local audio libraries.",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit one JSON object on stdout."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "login", help="Authorize Spotify user access (needed for playlists)."
    )

    p = subparsers.add_parser("set-download-path", help="Register a library directory.")
    p.add_argument("--path", required=True)
    p.add_argument("--name")

    p = subparsers.add_parser("select-download-path", help="Select the active library.")
    p.add_argument("--path")
    p.add_argument("--name")

    p = subparsers.add_parser(
        "delete-download-path", help="Remove a registered library directory."
    )
    p.add_argument("--library-id", type=int)
    p.add_argument("--path")
    p.add_argument("--name")

    p = subparsers.add_parser("set-cookies", help="Set the global YouTube cookies file.")
    p.add_argument("--cookies-file", required=True)

    p = subparsers.add_parser("add-playlist", help="Track a playlist for syncing.")
    p.add_argument("--spotify-playlist-url")
    p.add_argument("--youtube-playlist-url")
    p.add_argument("--dest")
    p.add_argument("--library")

    subparsers.add_parser(
        "list-playlists", help="List all playlists currently being tracked."
    )

    subparsers.add_parser(
        "show-settings",
        help="Print settings, thresholds, toggles, and database stats.",
    )

    p = subparsers.add_parser(
        "set-audio-matching",
        help="Toggle chromaprint/embedding for duplicate and comparison phases.",
    )
    p.add_argument("--duplicate-chromaprint", choices=["on", "off"])
    p.add_argument("--duplicate-embedding", choices=["on", "off"])
    p.add_argument("--comparison-chromaprint", choices=["on", "off"])
    p.add_argument("--comparison-embedding", choices=["on", "off"])

    p = subparsers.add_parser(
        "set-thresholds",
        help="Set metadata/audio similarity thresholds.",
    )
    p.add_argument("--metadata-minimum-rating", type=float, dest="metadata_minimum_rating")
    p.add_argument("--audio-duplicate-threshold", type=float, dest="audio_duplicate_threshold")
    p.add_argument("--audio-review-threshold", type=float, dest="audio_review_threshold")
    p.add_argument(
        "--chromaprint-match-certainty", type=float, dest="chromaprint_match_certainty"
    )
    p.add_argument(
        "--embedding-match-threshold", type=float, dest="embedding_match_threshold"
    )

    p = subparsers.add_parser(
        "set-scoring-weights",
        help="Set metadata search scoring weights (must sum to 100).",
    )
    p.add_argument("--artist", type=float, dest="artist")
    p.add_argument("--title", type=float, dest="title")
    p.add_argument("--duration", type=float, dest="duration")
    p.add_argument("--official-channel", type=float, dest="official_channel")
    p.add_argument("--album", type=float, dest="album")
    p.add_argument("--release-year", type=float, dest="release_year")

    p = subparsers.add_parser("add-track", help="Download a single track.")
    p.add_argument("--spotify-track-url")
    p.add_argument("--youtube-url")
    p.add_argument("--spotify-playlist-url")
    p.add_argument("--youtube-playlist-url")
    p.add_argument("--index", type=int)
    p.add_argument("--from-playlist", type=int)
    p.add_argument("--dest")

    p = subparsers.add_parser("sync", help="Sync tracked playlists.")
    p.add_argument("--playlist-id", type=int)
    p.add_argument("--all", action="store_true")

    p = subparsers.add_parser(
        "remove-playlist", help="Stop tracking a playlist (files are kept)."
    )
    p.add_argument("--playlist-id", type=int, required=True)

    p = subparsers.add_parser(
        "unset-playlist",
        help="Unset a tracked playlist so sync --all skips it.",
    )
    p.add_argument("--playlist-id", type=int, required=True)

    p = subparsers.add_parser("blacklist-song", help="Blacklist a track.")
    p.add_argument("--spotify-track-url")
    p.add_argument("--youtube-url")
    p.add_argument("--spotify-playlist-url")
    p.add_argument("--youtube-playlist-url")
    p.add_argument("--index", type=int)
    p.add_argument("--playlist-id", type=int)
    p.add_argument("--reason")

    p = subparsers.add_parser("list-blacklisted", help="List blacklisted tracks.")
    p.add_argument("--playlist-id", type=int)

    p = subparsers.add_parser(
        "resolve-duplicate", help="Resolve a pending duplicate decision."
    )
    p.add_argument("--request-id", required=True)
    p.add_argument("--action", required=True, choices=ALLOWED_DUPLICATE_ACTIONS)

    return parser


def dispatch(args: argparse.Namespace, json_mode: bool) -> dict:
    if args.command == "login":
        return cmd_login(args)
    if args.command == "set-download-path":
        return cmd_set_download_path(args)
    if args.command == "select-download-path":
        return cmd_select_download_path(args)
    if args.command == "delete-download-path":
        return cmd_delete_download_path(args)
    if args.command == "set-cookies":
        return cmd_set_cookies(args)
    if args.command == "add-playlist":
        return cmd_add_playlist(args)
    if args.command == "list-playlists":
        return cmd_list_playlists(args)
    if args.command == "show-settings":
        return cmd_show_settings(args)
    if args.command == "set-audio-matching":
        return cmd_set_audio_matching(args)
    if args.command == "set-thresholds":
        return cmd_set_thresholds(args)
    if args.command == "set-scoring-weights":
        return cmd_set_scoring_weights(args)
    if args.command == "add-track":
        return cmd_add_track(args, json_mode)
    if args.command == "sync":
        return cmd_sync(args, json_mode)
    if args.command == "remove-playlist":
        return cmd_remove_playlist(args)
    if args.command == "unset-playlist":
        return cmd_unset_playlist(args)
    if args.command == "blacklist-song":
        return cmd_blacklist_song(args)
    if args.command == "list-blacklisted":
        return cmd_list_blacklisted(args)
    if args.command == "resolve-duplicate":
        return cmd_resolve_duplicate(args, json_mode)
    raise CliError("INVALID_ARGUMENT", f"Unknown command: {args.command}")


def _map_exception(exc: Exception) -> CliError:
    if isinstance(exc, CliError):
        return exc
    if isinstance(exc, LibraryNotFoundError):
        return CliError("LIBRARY_NOT_FOUND", str(exc))
    if isinstance(exc, PlaylistNotFoundError):
        return CliError("PLAYLIST_NOT_FOUND", str(exc))
    if isinstance(exc, IndexError):
        return CliError("PLAYLIST_INDEX_OUT_OF_RANGE", str(exc))
    if isinstance(exc, EnvironmentError) and "SPOTIPY" in str(exc):
        return CliError("SPOTIFY_AUTH_MISSING", str(exc))
    try:
        from spotify_auth import SpotifyUserAuthRequired

        if isinstance(exc, SpotifyUserAuthRequired):
            return CliError("SPOTIFY_AUTH_MISSING", str(exc))
    except ImportError:
        pass
    try:
        from spotipy.exceptions import SpotifyException

        if isinstance(exc, SpotifyException):
            if exc.http_status in (401, 403):
                return CliError(
                    "SPOTIFY_AUTH_MISSING",
                    f"{exc} -- if this is a playlist command, "
                    "run 'spotify-sync login' first.",
                )
            return CliError("TRACK_NOT_FOUND", str(exc), EXIT_EXTERNAL)
    except ImportError:
        pass
    if isinstance(exc, DownloadError):
        return CliError("DOWNLOAD_FAILED", str(exc), EXIT_EXTERNAL)
    try:
        import yt_dlp

        if isinstance(exc, yt_dlp.utils.YoutubeDLError):
            return CliError("YOUTUBE_EXTRACT_FAILED", str(exc), EXIT_EXTERNAL)
    except ImportError:
        pass
    if isinstance(exc, ValueError):
        return CliError("INVALID_ARGUMENT", str(exc))
    if isinstance(exc, LookupError):
        return CliError("TRACK_NOT_FOUND", str(exc))
    return CliError("DOWNLOAD_FAILED", str(exc), EXIT_EXTERNAL)


def main(argv: Optional[list[str]] = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    json_mode = settings.is_json_mode(argv)
    output.JSON_MODE = json_mode

    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        if json_mode and exc.code not in (0, None):
            command = next((a for a in argv if not a.startswith("-")), "unknown")
            emit_error(command, "INVALID_ARGUMENT", "Invalid command-line arguments.")
            return EXIT_VALIDATION
        return exc.code if isinstance(exc.code, int) else EXIT_VALIDATION

    db.init_db()

    try:
        try:
            data = dispatch(args, json_mode)
        except Exception as exc:  # noqa: BLE001 - map everything to envelopes
            error = _map_exception(exc)
            if json_mode:
                emit_error(args.command, error.code, str(error))
            else:
                log_operation_error(args.command, str(error))
            return error.exit_code

        if json_mode:
            emit_success(args.command, data)
        return EXIT_OK
    finally:
        db.reset_connection()


if __name__ == "__main__":
    raise SystemExit(main())
