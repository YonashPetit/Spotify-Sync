"""Spotify metadata adapter built on get_content / spotipy."""

from __future__ import annotations

import re
from typing import Iterator, Optional

import spotipy

PLAYLIST_PAGE_SIZE = 50

from get_content import _create_spotify_client, get_track_info, parse_spotify_track_id
from models import TrackIdentity

_SPOTIFY_PLAYLIST_ID_RE = re.compile(
    r"(?:spotify:playlist:|open\.spotify\.com/playlist/)([A-Za-z0-9]+)"
)


def parse_track_id(url: str) -> str:
    return parse_spotify_track_id(url)


def parse_playlist_id(url: str) -> str:
    match = _SPOTIFY_PLAYLIST_ID_RE.search(url)
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9]+", url):
        return url
    raise ValueError(f"Could not parse Spotify playlist ID from: {url!r}")


def track_url(track_id: str) -> str:
    return f"https://open.spotify.com/track/{track_id}"


def _playlist_client() -> spotipy.Spotify:
    """
    Client for playlist endpoints. Prefers a cached user OAuth token
    (required by Spotify for playlist item enumeration on newer API apps);
    falls back to Client Credentials, which may still work for older apps.
    """
    from spotify_auth import create_user_client, has_cached_token

    if has_cached_token():
        return create_user_client()
    return _create_spotify_client()


def fetch_track_identity(track_id_or_url: str) -> TrackIdentity:
    track_id = parse_track_id(track_id_or_url) if "spotify" in track_id_or_url else track_id_or_url
    title, primary_artist, _featured, _album, duration, isrc, _pop, _year = (
        get_track_info(track_id)
    )
    return TrackIdentity(
        spotify_track_id=track_id,
        youtube_video_id=None,
        isrc=isrc,
        title=title,
        artist=primary_artist,
        duration_seconds=duration,
    )


def _identity_from_item(item: dict) -> Optional[TrackIdentity]:
    # Feb 2026 API renamed the per-entry key from "track" to "item".
    track = item.get("item") or item.get("track")
    if track is None and "id" in item:
        track = item
    if not track or not track.get("id"):
        return None
    if track.get("type") not in (None, "track"):
        return None  # skip podcast episodes etc.
    artists = [artist.get("name") or "" for artist in track.get("artists", [])]
    return TrackIdentity(
        spotify_track_id=track["id"],
        youtube_video_id=None,
        isrc=(track.get("external_ids") or {}).get("isrc"),
        title=track.get("name", ""),
        artist=artists[0] if artists else "",
        duration_seconds=track.get("duration_ms", 0) // 1000,
    )


def fetch_playlist_metadata(
    playlist_id_or_url: str,
    *,
    spotify_client: Optional[spotipy.Spotify] = None,
) -> dict:
    playlist_id = parse_playlist_id(playlist_id_or_url)
    sp = spotify_client or _playlist_client()
    playlist = sp.playlist(
        playlist_id, fields="id,name,images,items.total,tracks.total"
    )
    total = (playlist.get("items") or {}).get("total")
    if total is None:
        total = (playlist.get("tracks") or {}).get("total", 0)
    images = playlist.get("images") or []
    cover_url = images[0].get("url") if images else None
    return {
        "external_id": playlist["id"],
        "name": playlist.get("name") or playlist["id"],
        "total_tracks": total or 0,
        "cover_url": cover_url,
    }


def _identities_from_items(items: list[dict]) -> list[TrackIdentity]:
    identities: list[TrackIdentity] = []
    for item in items:
        identity = _identity_from_item(item)
        if identity is not None:
            identities.append(identity)
    return identities


def iter_playlist_track_batches(
    playlist_id_or_url: str,
    *,
    batch_size: int = PLAYLIST_PAGE_SIZE,
    spotify_client: Optional[spotipy.Spotify] = None,
) -> Iterator[list[TrackIdentity]]:
    """
    Yield playlist tracks in API-sized pages (default 50).

    Each batch is a fresh Spotify page fetch so long playlists paginate
    reliably within a single sync run.
    """
    playlist_id = parse_playlist_id(playlist_id_or_url)
    sp = spotify_client or _playlist_client()
    try:
        yield from _iter_playlist_batches_offset(
            sp, playlist_id, batch_size=batch_size
        )
        return
    except spotipy.exceptions.SpotifyException:
        pass

    yield from _iter_playlist_batches_next(sp, playlist_id, batch_size=batch_size)


def _iter_playlist_batches_offset(
    sp: spotipy.Spotify,
    playlist_id: str,
    *,
    batch_size: int,
) -> Iterator[list[TrackIdentity]]:
    offset = 0
    while True:
        results = sp._get(
            f"playlists/{playlist_id}/items",
            limit=batch_size,
            offset=offset,
        )
        items = results.get("items", [])
        if not items:
            break

        batch = _identities_from_items(items)
        if batch:
            yield batch

        if len(items) < batch_size or not results.get("next"):
            break
        offset += len(items)


def _iter_playlist_batches_next(
    sp: spotipy.Spotify,
    playlist_id: str,
    *,
    batch_size: int,
) -> Iterator[list[TrackIdentity]]:
    results = sp.playlist_items(playlist_id, additional_types=("track",))
    pending: list[TrackIdentity] = []
    while results:
        for identity in _identities_from_items(results.get("items", [])):
            pending.append(identity)
            if len(pending) >= batch_size:
                yield pending
                pending = []
        results = sp.next(results) if results.get("next") else None
    if pending:
        yield pending


def iter_playlist_track_identities(
    playlist_id_or_url: str,
    *,
    spotify_client: Optional[spotipy.Spotify] = None,
) -> Iterator[TrackIdentity]:
    for batch in iter_playlist_track_batches(
        playlist_id_or_url, spotify_client=spotify_client
    ):
        yield from batch


def get_playlist_track_by_index(playlist_id_or_url: str, index: int) -> TrackIdentity:
    """0-based index."""
    if index < 0:
        raise IndexError(f"Playlist index must be >= 0, got {index}")
    for position, identity in enumerate(
        iter_playlist_track_identities(playlist_id_or_url)
    ):
        if position == index:
            return identity
    raise IndexError(f"Playlist index {index} is out of range.")
