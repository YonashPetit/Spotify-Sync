"""YouTube metadata adapter built on yt-dlp."""

from __future__ import annotations

import re
from typing import Any, Iterator, Optional
from urllib.parse import parse_qs, urlparse

import yt_dlp

from isrc_match import extract_isrc_for_video
from models import TrackIdentity
from settings import get_cookies_file

_VIDEO_ID_RE = re.compile(
    r"(?:youtube\.com/watch\?.*?v=|youtu\.be/|music\.youtube\.com/watch\?.*?v=)"
    r"([A-Za-z0-9_-]{11})"
)


def parse_video_id(url: str) -> str:
    match = _VIDEO_ID_RE.search(url)
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url):
        return url
    raise ValueError(f"Could not parse YouTube video ID from: {url!r}")


def parse_playlist_list_id(url: str) -> str:
    """Extract the playlist ID from the URL ``list=`` parameter."""
    query = parse_qs(urlparse(url).query)
    values = query.get("list")
    if values and values[0]:
        return values[0]
    if re.fullmatch(r"[A-Za-z0-9_-]+", url):
        return url
    raise ValueError(f"Could not parse YouTube playlist ID from: {url!r}")


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def playlist_url(playlist_id: str) -> str:
    return f"https://www.youtube.com/playlist?list={playlist_id}"


def ydl_base_opts() -> dict:
    """Base yt-dlp options; injects the global cookies file when configured."""
    opts: dict = {"quiet": True, "no_warnings": True}
    cookies = get_cookies_file()
    if cookies:
        opts["cookiefile"] = cookies
    return opts


def _extract_info(url: str, extra_opts: Optional[dict] = None) -> dict[str, Any]:
    opts = {**ydl_base_opts(), "skip_download": True, **(extra_opts or {})}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if info is None:
        raise ValueError(f"No metadata returned for {url}")
    return info


def _identity_from_info(video_id: str, info: dict[str, Any]) -> TrackIdentity:
    return TrackIdentity(
        spotify_track_id=None,
        youtube_video_id=video_id,
        isrc=extract_isrc_for_video(video_id, "youtube", info),
        title=info.get("track") or info.get("title") or "",
        artist=(
            info.get("artist")
            or info.get("creator")
            or info.get("channel")
            or info.get("uploader")
            or ""
        ),
        duration_seconds=int(info.get("duration") or 0),
    )


def fetch_video_identity(url: str) -> TrackIdentity:
    video_id = parse_video_id(url)
    info = _extract_info(watch_url(video_id))
    return _identity_from_info(video_id, info)


def fetch_playlist_metadata(url: str) -> dict:
    playlist_id = parse_playlist_list_id(url)
    info = _extract_info(playlist_url(playlist_id), {"extract_flat": "in_playlist"})
    entries = info.get("entries") or []
    thumbnails = info.get("thumbnails") or []
    cover_url = info.get("thumbnail")
    if not cover_url and thumbnails:
        cover_url = thumbnails[-1].get("url")
    return {
        "external_id": playlist_id,
        "name": info.get("title") or playlist_id,
        "total_tracks": len(entries),
        "cover_url": cover_url,
    }


def iter_playlist_video_identities(url: str) -> Iterator[TrackIdentity]:
    playlist_id = parse_playlist_list_id(url)
    info = _extract_info(playlist_url(playlist_id), {"extract_flat": "in_playlist"})
    for entry in info.get("entries") or []:
        video_id = entry.get("id")
        if not video_id:
            continue
        yield TrackIdentity(
            spotify_track_id=None,
            youtube_video_id=video_id,
            isrc=None,
            title=entry.get("title") or "",
            artist=entry.get("channel") or entry.get("uploader") or "",
            duration_seconds=int(entry.get("duration") or 0),
        )


def get_playlist_video_by_index(url: str, index: int) -> TrackIdentity:
    """0-based index. Returns full identity (with ISRC attempt) for the item."""
    if index < 0:
        raise IndexError(f"Playlist index must be >= 0, got {index}")
    for position, identity in enumerate(iter_playlist_video_identities(url)):
        if position == index:
            return fetch_video_identity(identity.youtube_video_id)
    raise IndexError(f"Playlist index {index} is out of range.")
