"""Collect and format application settings, toggles, thresholds, and stats."""

from __future__ import annotations

import os
from dataclasses import asdict
from typing import Any, Optional

import db
import libraries
import playlists as playlists_mod
import settings
from app_paths import get_app_home, get_db_path
from models import DuplicateConfig
from output import print_human
from search_candidates import DEFAULT_WEIGHTS, MAX_CANDIDATES, THRESHOLD


def _bool_label(value: bool) -> str:
    return "on" if value else "off"


def _env_set(name: str) -> bool:
    return bool(os.environ.get(name))


def _db_count(table: str, where: str = "") -> int:
    conn = db.get_connection()
    query = f"SELECT COUNT(*) AS n FROM {table}"
    if where:
        query += f" WHERE {where}"
    row = conn.execute(query).fetchone()
    return int(row["n"])


def gather_settings_overview() -> dict[str, Any]:
    from audio_similarity import (
        AUDIO_MATCH_CERTAINTY,
        CHROMAPRINT_MIDDLE_SECONDS,
        EMBEDDING_MATCH_THRESHOLD,
        EMBEDDING_MIDDLE_SECONDS,
        ENABLE_CHROMAPRINT_MATCH,
        ENABLE_EMBEDDING_MATCH,
        MAX_AUDIO_MATCH_ATTEMPTS,
    )
    from download_audio import find_ffmpeg_location
    from spotify_auth import has_cached_token, redirect_uri, token_cache_path

    default_dup = DuplicateConfig()
    weights = asdict(DEFAULT_WEIGHTS)

    selected_library_id = settings.get_selected_library_id()
    selected_library: Optional[dict] = None
    if selected_library_id is not None:
        try:
            selected_library = libraries.get_library_row(selected_library_id)
        except libraries.LibraryNotFoundError:
            selected_library = None

    playlist_rows = playlists_mod.list_playlists()
    enabled_playlists = sum(1 for row in playlist_rows if row["enabled"])

    pending_decisions = _db_count("pending_decisions")
    blacklist_global = _db_count("blacklist", "playlist_id IS NULL")
    blacklist_playlist = _db_count("blacklist", "playlist_id IS NOT NULL")

    try:
        spotify_logged_in = has_cached_token()
    except EnvironmentError:
        spotify_logged_in = False

    return {
        "application": {
            "app_home": str(get_app_home()),
            "database": str(get_db_path()),
            "ffmpeg_on_path": find_ffmpeg_location() is not None,
            "spotify_sync_home_override": os.environ.get("SPOTIFY_SYNC_HOME"),
            "spotify_sync_ffmpeg_override": os.environ.get("SPOTIFY_SYNC_FFMPEG"),
        },
        "authentication": {
            "spotify_client_id_set": _env_set("SPOTIPY_CLIENT_ID"),
            "spotify_client_secret_set": _env_set("SPOTIPY_CLIENT_SECRET"),
            "spotify_redirect_uri": redirect_uri(),
            "spotify_user_logged_in": spotify_logged_in,
            "spotify_token_cache": str(token_cache_path()),
            "youtube_cookies_file": settings.get_cookies_file(),
        },
        "libraries": {
            "selected_library": selected_library,
            "registered_libraries": libraries.list_libraries(),
        },
        "duplicate_detection": {
            "description": (
                "Folder scan before download (non-recursive). "
                "Uses ISRC, YouTube filename patterns, and optional chromaprint."
            ),
            "defaults": default_dup.as_dict(),
            "notes": {
                "check_metadata": (
                    "Stored per playlist but not used by directory duplicate scan."
                ),
                "metadata_threshold": (
                    "Stored per playlist but not used by directory duplicate scan."
                ),
                "check_audio": (
                    "When on, compares chromaprint similarity against files "
                    "already in the target folder."
                ),
                "audio_duplicate_threshold": (
                    "Similarity >= this value is treated as a definite duplicate."
                ),
                "audio_review_threshold": (
                    "Similarity >= this (and below duplicate threshold) "
                    "may prompt for user choice when policy is ask."
                ),
            },
            "per_playlist": [
                {
                    "playlist_id": row["playlist_id"],
                    "name": row["name"] or row["external_id"],
                    "source": row["source"],
                    "enabled": row["enabled"],
                    **row["config"],
                }
                for row in playlist_rows
            ],
        },
        "song_confirmation": {
            "description": (
                "How Spotify tracks are matched to a YouTube source before download."
            ),
            "metadata_scoring": {
                "minimum_rating_to_consider": THRESHOLD,
                "max_search_candidates": MAX_CANDIDATES,
                "weights_percent": weights,
                "weight_fields": {
                    "exact_artist_match": "Artist name match",
                    "exact_title_match": "Title match",
                    "duration_similarity": "Duration closeness",
                    "official_channel": "Official channel bonus",
                    "album_similarity": "Album match",
                    "release_year_proximity": "Release year closeness",
                },
            },
            "audio_matching_module_defaults": {
                "chromaprint_enabled": ENABLE_CHROMAPRINT_MATCH,
                "embedding_enabled": ENABLE_EMBEDDING_MATCH,
                "chromaprint_middle_seconds": CHROMAPRINT_MIDDLE_SECONDS,
                "embedding_middle_seconds": EMBEDDING_MIDDLE_SECONDS,
                "chromaprint_match_certainty": AUDIO_MATCH_CERTAINTY,
                "embedding_match_threshold": EMBEDDING_MATCH_THRESHOLD,
                "max_audio_match_attempts": MAX_AUDIO_MATCH_ATTEMPTS,
            },
        "effective_during_sync_download": {
            "chromaprint_enabled": False,
            "embedding_enabled": False,
            "note": (
                "Sync uses metadata ranking + ISRC search by default. "
                "Module-level chromaprint/embedding toggles apply when "
                "those parameters are enabled for a download."
            ),
        },
            "isrc_direct_match": {
                "enabled": True,
                "description": "Try YouTube Music / YouTube ISRC search before metadata heap.",
            },
        },
        "stats": {
            "libraries": _db_count("libraries"),
            "playlists_total": len(playlist_rows),
            "playlists_enabled": enabled_playlists,
            "tracks": _db_count("tracks"),
            "library_track_links": _db_count("library_tracks"),
            "playlist_items_active": _db_count(
                "playlist_items", "removed_at IS NULL"
            ),
            "playlist_items_removed": _db_count(
                "playlist_items", "removed_at IS NOT NULL"
            ),
            "blacklist_global": blacklist_global,
            "blacklist_playlist_scoped": blacklist_playlist,
            "pending_duplicate_decisions": pending_decisions,
        },
    }


def _line(key: str, value: Any, indent: int = 0) -> None:
    prefix = "  " * indent
    print_human(f"{prefix}{key}: {value}")


def print_settings_overview(data: dict[str, Any]) -> None:
    app = data["application"]
    auth = data["authentication"]
    libs = data["libraries"]
    dup = data["duplicate_detection"]
    song = data["song_confirmation"]
    stats = data["stats"]
    defaults = dup["defaults"]
    meta = song["metadata_scoring"]
    audio_defaults = song["audio_matching_module_defaults"]
    audio_effective = song["effective_during_sync_download"]

    print_human("=== Application ===")
    _line("App data directory", app["app_home"])
    _line("Database", app["database"])
    _line("FFmpeg found on PATH", _bool_label(app["ffmpeg_on_path"]))
    if app["spotify_sync_home_override"]:
        _line("SPOTIFY_SYNC_HOME", app["spotify_sync_home_override"])
    if app["spotify_sync_ffmpeg_override"]:
        _line("SPOTIFY_SYNC_FFMPEG", app["spotify_sync_ffmpeg_override"])

    print_human("")
    print_human("=== Authentication ===")
    _line("Spotify client ID configured", _bool_label(auth["spotify_client_id_set"]))
    _line(
        "Spotify client secret configured",
        _bool_label(auth["spotify_client_secret_set"]),
    )
    _line("Spotify redirect URI", auth["spotify_redirect_uri"])
    _line("Spotify user logged in", _bool_label(auth["spotify_user_logged_in"]))
    _line("Spotify token cache", auth["spotify_token_cache"])
    _line(
        "YouTube cookies file",
        auth["youtube_cookies_file"] or "(not set)",
    )

    print_human("")
    print_human("=== Libraries ===")
    selected = libs["selected_library"]
    if selected:
        _line(
            "Selected library",
            f"{selected.get('name') or selected['path']} (id={selected['library_id']})",
        )
    else:
        _line("Selected library", "(none)")
    if not libs["registered_libraries"]:
        _line("Registered libraries", "(none)")
    for library in libs["registered_libraries"]:
        _line(
            f"Library {library['library_id']}",
            f"{library.get('name') or library['path']}",
            indent=1,
        )

    print_human("")
    print_human("=== Duplicate detection (folder scan) ===")
    _line("ISRC check", _bool_label(defaults["check_isrc"]))
    _line("Audio (chromaprint) check", _bool_label(defaults["check_audio"]))
    _line("Metadata scoring check", _bool_label(defaults["check_metadata"]))
    _line("Default duplicate policy", defaults["duplicate_policy"])
    _line("Audio duplicate threshold", defaults["audio_duplicate_threshold"])
    _line("Audio review threshold", defaults["audio_review_threshold"])
    _line("Metadata threshold (stored)", defaults["metadata_threshold"])

    if dup["per_playlist"]:
        print_human("")
        print_human("Per-playlist duplicate settings:")
        for row in dup["per_playlist"]:
            status = "enabled" if row["enabled"] else "disabled"
            _line(
                f"[{row['playlist_id']}] {row['name']!r} ({row['source']}, {status})",
                (
                    f"policy={row['duplicate_policy']}, "
                    f"isrc={_bool_label(row['check_isrc'])}, "
                    f"audio={_bool_label(row['check_audio'])}, "
                    f"audio_dup>={row['audio_duplicate_threshold']}, "
                    f"audio_review>={row['audio_review_threshold']}"
                ),
                indent=1,
            )
    else:
        _line("Per-playlist overrides", "(no playlists tracked)", indent=1)

    print_human("")
    print_human("=== Song confirmation (Spotify → YouTube) ===")
    _line("Metadata minimum rating", meta["minimum_rating_to_consider"])
    _line("Max search candidates", meta["max_search_candidates"])
    _line("ISRC direct search", _bool_label(song["isrc_direct_match"]["enabled"]))
    print_human("  Metadata scoring weights (percent):")
    for field, label in meta["weight_fields"].items():
        _line(label, meta["weights_percent"][field], indent=2)

    print_human("  Module defaults (when audio matching is enabled):")
    _line(
        "Chromaprint",
        _bool_label(audio_defaults["chromaprint_enabled"]),
        indent=2,
    )
    _line(
        "Vector embedding",
        _bool_label(audio_defaults["embedding_enabled"]),
        indent=2,
    )
    _line(
        "Chromaprint match certainty",
        audio_defaults["chromaprint_match_certainty"],
        indent=2,
    )
    _line(
        "Embedding match threshold",
        audio_defaults["embedding_match_threshold"],
        indent=2,
    )
    _line(
        "Max audio match attempts",
        audio_defaults["max_audio_match_attempts"],
        indent=2,
    )
    _line(
        "Chromaprint clip seconds",
        audio_defaults["chromaprint_middle_seconds"],
        indent=2,
    )
    _line(
        "Embedding clip seconds",
        audio_defaults["embedding_middle_seconds"],
        indent=2,
    )

    print_human("  Effective during sync download:")
    _line(
        "Chromaprint",
        _bool_label(audio_effective["chromaprint_enabled"]),
        indent=2,
    )
    _line(
        "Vector embedding",
        _bool_label(audio_effective["embedding_enabled"]),
        indent=2,
    )

    print_human("")
    print_human("=== Stats ===")
    _line("Libraries", stats["libraries"])
    _line(
        "Playlists",
        f"{stats['playlists_enabled']} enabled / {stats['playlists_total']} total",
    )
    _line("Unique tracks in database", stats["tracks"])
    _line("Downloaded library links", stats["library_track_links"])
    _line("Active playlist items", stats["playlist_items_active"])
    _line("Removed playlist items", stats["playlist_items_removed"])
    _line("Global blacklist entries", stats["blacklist_global"])
    _line("Playlist blacklist entries", stats["blacklist_playlist_scoped"])
    _line("Pending duplicate decisions", stats["pending_duplicate_decisions"])
