"""Fetch rich track metadata and embed it into downloaded audio files."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from models import TrackIdentity

_USER_AGENT = "spotify-sync/1.0 (local music sync tool)"


@dataclass
class TrackMetadata:
    title: str = ""
    artist: str = ""
    album: str = ""
    genre: str = ""
    year: Optional[int] = None
    track_number: Optional[int] = None
    isrc: Optional[str] = None
    cover_url: Optional[str] = None


def _musicbrainz_get(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def _musicbrainz_genre(isrc: str) -> str:
    try:
        isrc_data = _musicbrainz_get(
            f"https://musicbrainz.org/ws/2/isrc/{urllib.parse.quote(isrc)}?fmt=json"
        )
        recordings = isrc_data.get("recordings") or []
        if not recordings:
            return ""
        recording_id = recordings[0].get("id")
        if not recording_id:
            return ""
        recording = _musicbrainz_get(
            f"https://musicbrainz.org/ws/2/recording/{recording_id}"
            "?fmt=json&inc=genres"
        )
    except Exception:
        return ""

    genres = recording.get("genres") or []
    if not genres:
        return ""
    best = max(genres, key=lambda genre: genre.get("count", 0))
    return (best.get("name") or "").title()


def fetch_spotify_track_metadata(track_id: str) -> TrackMetadata:
    from get_content import _create_spotify_client

    sp = _create_spotify_client()
    track = sp.track(track_id)
    if track is None:
        raise ValueError(f"Track not found: {track_id}")

    album = track.get("album") or {}
    artists = track.get("artists") or []
    artist_names = [a.get("name", "") for a in artists if a.get("name")]

    genre = ""
    if artists and artists[0].get("id"):
        try:
            artist = sp.artist(artists[0]["id"])
            genres = artist.get("genres") or []
            if genres:
                genre = genres[0].title()
        except Exception:
            pass

    isrc = (track.get("external_ids") or {}).get("isrc")
    if not genre and isrc:
        genre = _musicbrainz_genre(isrc)

    year: Optional[int] = None
    release_date = album.get("release_date") or ""
    if release_date[:4].isdigit():
        year = int(release_date[:4])

    images = album.get("images") or []
    cover_url = images[0].get("url") if images else None

    return TrackMetadata(
        title=track.get("name", ""),
        artist=", ".join(artist_names),
        album=album.get("name", ""),
        genre=genre,
        year=year,
        track_number=track.get("track_number"),
        isrc=isrc,
        cover_url=cover_url,
    )


def fetch_youtube_video_metadata(video_id: str) -> TrackMetadata:
    from sources.youtube_source import _extract_info, watch_url

    info = _extract_info(watch_url(video_id))

    year: Optional[int] = None
    release_year = info.get("release_year")
    if release_year:
        year = int(release_year)
    elif info.get("upload_date"):
        year = int(str(info["upload_date"])[:4])

    genre = info.get("genre") or ""
    if not genre:
        categories = info.get("categories") or []
        if categories and categories[0] != "Music":
            genre = categories[0]

    cover_url = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"

    return TrackMetadata(
        title=info.get("track") or info.get("title") or "",
        artist=(
            info.get("artist")
            or info.get("creator")
            or info.get("channel")
            or info.get("uploader")
            or ""
        ),
        album=info.get("album") or "",
        genre=genre,
        year=year,
        cover_url=cover_url,
    )


def fetch_metadata_for_identity(identity: TrackIdentity) -> Optional[TrackMetadata]:
    if identity.spotify_track_id:
        return fetch_spotify_track_metadata(identity.spotify_track_id)
    if identity.youtube_video_id:
        return fetch_youtube_video_metadata(identity.youtube_video_id)
    return None


def _download_cover(cover_url: str) -> Optional[tuple[bytes, str]]:
    try:
        request = urllib.request.Request(cover_url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(request, timeout=15) as response:
            data = response.read()
            content_type = response.headers.get_content_type()
    except Exception:
        return None

    if content_type == "image/png" or data[:8] == b"\x89PNG\r\n\x1a\n":
        return data, "png"
    if content_type == "image/jpeg" or data[:3] == b"\xff\xd8\xff":
        return data, "jpeg"
    return None


def _embed_mp4(path: Path, meta: TrackMetadata) -> None:
    from mutagen.mp4 import MP4, MP4Cover

    audio = MP4(str(path))
    if meta.title:
        audio["\xa9nam"] = [meta.title]
    if meta.artist:
        audio["\xa9ART"] = [meta.artist]
        audio["aART"] = [meta.artist]
    if meta.album:
        audio["\xa9alb"] = [meta.album]
    if meta.genre:
        audio["\xa9gen"] = [meta.genre]
    if meta.year:
        audio["\xa9day"] = [str(meta.year)]
    if meta.track_number:
        audio["trkn"] = [(meta.track_number, 0)]
    if meta.isrc:
        audio["----:com.apple.iTunes:ISRC"] = [meta.isrc.encode()]

    if meta.cover_url:
        cover = _download_cover(meta.cover_url)
        if cover is not None:
            data, image_format = cover
            mp4_format = (
                MP4Cover.FORMAT_PNG if image_format == "png" else MP4Cover.FORMAT_JPEG
            )
            audio["covr"] = [MP4Cover(data, imageformat=mp4_format)]

    audio.save()


def _embed_generic(path: Path, meta: TrackMetadata) -> None:
    import mutagen

    audio = mutagen.File(str(path), easy=True)
    if audio is None:
        return
    if meta.title:
        audio["title"] = meta.title
    if meta.artist:
        audio["artist"] = meta.artist
    if meta.album:
        audio["album"] = meta.album
    if meta.genre:
        audio["genre"] = meta.genre
    if meta.year:
        audio["date"] = str(meta.year)
    if meta.isrc:
        audio["isrc"] = meta.isrc
    audio.save()


def embed_metadata(path: Path, meta: TrackMetadata) -> None:
    if path.suffix.lower() in (".m4a", ".mp4"):
        _embed_mp4(path, meta)
    else:
        _embed_generic(path, meta)


def tag_downloaded_file(path: Path, identity: TrackIdentity) -> bool:
    """Best-effort tagging; never fails the download."""
    try:
        meta = fetch_metadata_for_identity(identity)
        if meta is None:
            meta = TrackMetadata(
                title=identity.title,
                artist=identity.artist,
                isrc=identity.isrc,
            )
        else:
            if not meta.title:
                meta.title = identity.title
            if not meta.artist:
                meta.artist = identity.artist
            if not meta.isrc and identity.isrc:
                meta.isrc = identity.isrc
        embed_metadata(path, meta)
        return True
    except Exception:
        return False
