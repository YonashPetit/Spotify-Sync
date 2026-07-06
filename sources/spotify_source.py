"""Spotify metadata adapter built on get_content / spotipy."""

from __future__ import annotations

import re
from typing import Iterator, Optional

import spotipy

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
    track = item.get("track") if "track" in item else item
    if not track or not track.get("id"):
        return None
    artists = [artist["name"] for artist in track.get("artists", [])]
    return TrackIdentity(
        spotify_track_id=track["id"],
        youtube_video_id=None,
        isrc=track.get("external_ids", {}).get("isrc"),
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
    sp = spotify_client or _create_spotify_client()
    playlist = sp.playlist(playlist_id, fields="id,name,tracks.total")
    return {
        "external_id": playlist["id"],
        "name": playlist.get("name") or playlist["id"],
        "total_tracks": playlist.get("tracks", {}).get("total", 0),
    }


def iter_playlist_track_identities(
    playlist_id_or_url: str,
    *,
    spotify_client: Optional[spotipy.Spotify] = None,
) -> Iterator[TrackIdentity]:
    playlist_id = parse_playlist_id(playlist_id_or_url)
    sp = spotify_client or _create_spotify_client()
    results = sp.playlist_items(playlist_id, additional_types=("track",))
    while results:
        for item in results.get("items", []):
            identity = _identity_from_item(item)
            if identity is not None:
                yield identity
        results = sp.next(results) if results.get("next") else None


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
