import os
import re
from typing import Optional

import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

from isrc_match import normalize_isrc

TrackInfo = tuple[
    str,  # track title
    str,  # primary artist
    tuple[str, ...],  # featured artists
    str,  # album
    int,  # duration (seconds)
    str,  # ISRC
    int,  # popularity
    int,  # release year
]


_SPOTIFY_TRACK_ID_RE = re.compile(
    r"(?:spotify:track:|open\.spotify\.com/track/)([A-Za-z0-9]+)"
)


def parse_spotify_track_id(spotify_link: str) -> str:
    """Extract the Spotify track ID from a URL or URI."""
    match = _SPOTIFY_TRACK_ID_RE.search(spotify_link)
    if not match:
        raise ValueError(f"Could not parse Spotify track ID from: {spotify_link!r}")
    return match.group(1)


def _create_spotify_client() -> spotipy.Spotify:
    client_id = os.environ.get("SPOTIPY_CLIENT_ID")
    client_secret = os.environ.get("SPOTIPY_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise EnvironmentError(
            "Set SPOTIPY_CLIENT_ID and SPOTIPY_CLIENT_SECRET environment variables."
        )

    auth_manager = SpotifyClientCredentials(
        client_id=client_id,
        client_secret=client_secret,
    )
    return spotipy.Spotify(auth_manager=auth_manager)


def _parse_release_year(release_date: str) -> int:
    match = re.match(r"(\d{4})", release_date)
    if not match:
        raise ValueError(f"Could not parse release year from date: {release_date!r}")
    return int(match.group(1))


def extract_isrc_from_spotify_track(track: dict) -> Optional[str]:
    """Extract the ISRC string from a Spotify API track ``external_ids`` payload."""
    external_ids = track.get("external_ids")
    if not isinstance(external_ids, dict):
        return None
    isrc = external_ids.get("isrc")
    if not isrc or not isinstance(isrc, str):
        return None
    normalized = normalize_isrc(isrc)
    return normalized or None


def get_track_info(
    spotify_link: str,
    *,
    spotify_client: Optional[spotipy.Spotify] = None,
) -> TrackInfo:
    """
    Fetch track metadata from a Spotify track URL, URI, or ID.

    Returns:
        (title, primary_artist, featured_artists, album, duration_seconds,
         isrc, popularity, release_year)
    """
    sp = spotify_client or _create_spotify_client()
    track = sp.track(spotify_link)

    if track is None:
        raise ValueError(f"Track not found for link: {spotify_link}")

    artists = [artist["name"] for artist in track.get("artists", [])]
    primary_artist = artists[0] if artists else ""
    featured_artists = tuple(artists[1:])

    isrc = extract_isrc_from_spotify_track(track)
    if not isrc:
        raise ValueError(
            f"ISRC not available for track {track.get('name', spotify_link)!r}"
        )

    release_date = track.get("album", {}).get("release_date", "")
    release_year = _parse_release_year(release_date)

    return (
        track["name"],
        primary_artist,
        featured_artists,
        track.get("album", {}).get("name", ""),
        track.get("duration_ms", 0) // 1000,
        isrc,
        track.get("popularity", 0),
        release_year,
    )


def get_isrc_from_spotify_link(
    spotify_link: str,
    *,
    spotify_client: Optional[spotipy.Spotify] = None,
) -> Optional[str]:
    """Return the ISRC for a Spotify track URL, URI, or ID."""
    sp = spotify_client or _create_spotify_client()
    track = sp.track(spotify_link)
    if track is None:
        return None
    return extract_isrc_from_spotify_track(track)


def get_spotify_preview_url(
    spotify_link: str,
    *,
    spotify_client: Optional[spotipy.Spotify] = None,
) -> Optional[str]:
    """Return the 30-second Spotify preview MP3 URL, if available."""
    sp = spotify_client or _create_spotify_client()
    track = sp.track(spotify_link)
    if track is None:
        return None
    preview_url = track.get("preview_url")
    return preview_url if preview_url else None


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print(f"Usage: python {sys.argv[0]} <spotify_track_url>")
        raise SystemExit(1)

    print(get_track_info(sys.argv[1]))
