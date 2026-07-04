from __future__ import annotations

import heapq
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Literal, Optional
from urllib.parse import quote_plus

import yt_dlp

from download_audio import build_audio_filename, download_audio
from get_content import TrackInfo, get_track_info, parse_spotify_track_id
from isrc_match import extract_isrc_from_ytdlp_info, is_direct_isrc_match

Source = Literal["youtube_music", "youtube"]

# --- pipeline configuration ---
THRESHOLD = 50.0
SAVE_DIRECTORY = Path("downloads")
MAX_CANDIDATES = 9

_YDL_SEARCH_OPTS: dict = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "extract_flat": "in_playlist",
}

_YDL_METADATA_OPTS: dict = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
}


@dataclass(frozen=True)
class ScoringWeights:
    exact_artist_match: float = 30
    exact_title_match: float = 30
    duration_similarity: float = 20
    official_channel: float = 10
    album_similarity: float = 5
    release_year_proximity: float = 5

    def __post_init__(self) -> None:
        total = (
            self.exact_artist_match
            + self.exact_title_match
            + self.duration_similarity
            + self.official_channel
            + self.album_similarity
            + self.release_year_proximity
        )
        if abs(total - 100) > 0.01:
            raise ValueError(f"Scoring weights must sum to 100, got {total}")


DEFAULT_WEIGHTS = ScoringWeights()


@dataclass
class ScoreBreakdown:
    artist_match: float
    title_match: float
    duration_similarity: float
    official_channel: float
    album_similarity: float
    release_year_proximity: float
    weights: ScoringWeights = field(default_factory=lambda: DEFAULT_WEIGHTS)

    @property
    def total(self) -> float:
        w = self.weights
        return (
            self.artist_match * w.exact_artist_match
            + self.title_match * w.exact_title_match
            + self.duration_similarity * w.duration_similarity
            + self.official_channel * w.official_channel
            + self.album_similarity * w.album_similarity
            + self.release_year_proximity * w.release_year_proximity
        )

    def as_dict(self) -> dict[str, float]:
        w = self.weights
        return {
            "artist_match": round(self.artist_match * w.exact_artist_match, 2),
            "title_match": round(self.title_match * w.exact_title_match, 2),
            "duration_similarity": round(
                self.duration_similarity * w.duration_similarity, 2
            ),
            "official_channel": round(self.official_channel * w.official_channel, 2),
            "album_similarity": round(self.album_similarity * w.album_similarity, 2),
            "release_year_proximity": round(
                self.release_year_proximity * w.release_year_proximity, 2
            ),
            "total": round(self.total, 2),
        }


@dataclass
class Candidate:
    video_id: str
    url: str
    source: Source
    title: str = ""
    artist: str = ""
    channel: str = ""
    uploader: str = ""
    duration: Optional[int] = None
    album: str = ""
    release_year: Optional[int] = None
    upload_date: str = ""
    description: str = ""
    channel_is_verified: bool = False
    isrc: Optional[str] = None


@dataclass
class RankedCandidate:
    """Heap entry: Spotify-schema metadata + match rating + YouTube video ID."""

    video_id: str
    rating: float
    title: str
    primary_artist: str
    featured_artists: tuple[str, ...]
    album: str
    duration: int
    isrc: Optional[str]
    popularity: int
    release_year: int
    source: Source
    url: str

    def as_tuple(self) -> tuple:
        return (
            self.title,
            self.primary_artist,
            self.featured_artists,
            self.album,
            self.duration,
            self.isrc,
            self.popularity,
            self.release_year,
            self.rating,
            self.video_id,
        )


@dataclass
class PipelineResult:
    spotify_track_id: str
    track: TrackInfo
    direct_isrc_match: bool
    downloaded_path: Optional[Path]
    candidate_heap: list[RankedCandidate]

    @property
    def best_candidate(self) -> Optional[RankedCandidate]:
        return self.candidate_heap[0] if self.candidate_heap else None


# heapq is a min-heap; negate rating for max-heap behaviour.
HeapEntry = tuple[float, str, RankedCandidate]


def _normalize_text(value: str) -> str:
    value = value.lower()
    value = value.replace("'", "").replace("\u2019", "").replace("`", "")
    value = re.sub(r"\(feat\.[^)]*\)", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\(ft\.[^)]*\)", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\(with [^)]*\)", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\[[^\]]*\]", "", value)
    value = re.sub(r"\([^)]*\)", "", value)
    value = re.sub(r"[^\w\s]", " ", value)
    return " ".join(value.split())


def _token_overlap(left: str, right: str) -> float:
    left_tokens = set(_normalize_text(left).split())
    right_tokens = set(_normalize_text(right).split())
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _build_search_query(track: TrackInfo) -> str:
    title, primary_artist, featured_artists, *_ = track
    query_parts = [primary_artist, title]
    if featured_artists:
        query_parts.extend(featured_artists)
    return " ".join(part for part in query_parts if part)


def _search_youtube_music(query: str, limit: int) -> list[dict]:
    url = f"https://music.youtube.com/search?q={quote_plus(query)}"
    with yt_dlp.YoutubeDL(_YDL_SEARCH_OPTS) as ydl:
        info = ydl.extract_info(url, download=False)
    entries = info.get("entries") or []
    return [entry for entry in entries if entry.get("id")][:limit]


def _search_youtube(query: str, limit: int) -> list[dict]:
    with yt_dlp.YoutubeDL(_YDL_SEARCH_OPTS) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
    entries = info.get("entries") or []
    return [entry for entry in entries if entry.get("id")]


def _watch_url(video_id: str, source: Source) -> str:
    if source == "youtube_music":
        return f"https://music.youtube.com/watch?v={video_id}"
    return f"https://www.youtube.com/watch?v={video_id}"


def _fetch_ytdlp_info(video_id: str, source: Source) -> dict[str, Any]:
    url = _watch_url(video_id, source)
    with yt_dlp.YoutubeDL(_YDL_METADATA_OPTS) as ydl:
        info = ydl.extract_info(url, download=False)
    if info is None:
        raise ValueError(f"No metadata returned for {url}")
    return info


def _candidate_from_info(
    video_id: str, source: Source, info: dict[str, Any]
) -> Candidate:
    release_year = info.get("release_year")
    if release_year is None and info.get("upload_date"):
        release_year = int(str(info["upload_date"])[:4])

    return Candidate(
        video_id=video_id,
        url=_watch_url(video_id, source),
        source=source,
        title=info.get("title") or "",
        artist=info.get("artist") or info.get("creator") or "",
        channel=info.get("channel") or "",
        uploader=info.get("uploader") or "",
        duration=info.get("duration"),
        album=info.get("album") or "",
        release_year=release_year,
        upload_date=info.get("upload_date") or "",
        description=info.get("description") or "",
        channel_is_verified=bool(info.get("channel_is_verified")),
        isrc=extract_isrc_from_ytdlp_info(info),
    )


def _artist_match_ratio(track: TrackInfo, candidate: Candidate) -> float:
    _, primary_artist, featured_artists, *_ = track
    expected_artists = [primary_artist, *featured_artists]
    candidate_artists = [
        candidate.artist,
        candidate.channel,
        candidate.uploader,
        candidate.title,
    ]

    for expected in expected_artists:
        normalized_expected = _normalize_text(expected)
        if not normalized_expected:
            continue
        for actual in candidate_artists:
            normalized_actual = _normalize_text(actual)
            if normalized_expected == normalized_actual:
                return 1.0
            if normalized_expected in normalized_actual:
                return 0.9

    best = max(
        (
            _token_overlap(expected, actual)
            for expected in expected_artists
            for actual in candidate_artists
        ),
        default=0.0,
    )
    if best >= 0.8:
        return 0.75
    if best >= 0.5:
        return 0.4
    return 0.0


def _title_match_ratio(expected_title: str, candidate_title: str) -> float:
    left = _normalize_text(expected_title)
    right = _normalize_text(candidate_title)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    if left in right or right in left:
        return 0.85
    overlap = _token_overlap(left, right)
    if overlap >= 0.75:
        return 0.65
    if overlap >= 0.5:
        return 0.35
    return 0.0


def _duration_similarity_ratio(
    expected_seconds: int, candidate_seconds: Optional[int]
) -> float:
    if candidate_seconds is None:
        return 0.0
    diff = abs(expected_seconds - candidate_seconds)
    if diff <= 3:
        return 1.0
    if diff <= 10:
        return 0.85
    if diff <= 30:
        return 0.6
    if diff <= 60:
        return 0.3
    return 0.0


def _official_channel_ratio(track: TrackInfo, candidate: Candidate) -> float:
    _, primary_artist, *_ = track
    description = candidate.description.lower()
    channel = candidate.channel.lower()

    if "provided to youtube by" in description:
        return 1.0
    if channel.endswith(" - topic") or "vevo" in channel:
        return 1.0
    if _normalize_text(primary_artist) == _normalize_text(candidate.channel):
        return 0.9
    if candidate.channel_is_verified and _artist_match_ratio(track, candidate) >= 0.75:
        return 0.7
    if candidate.channel_is_verified:
        return 0.3
    return 0.0


def _album_similarity_ratio(expected_album: str, candidate: Candidate) -> float:
    if not expected_album:
        return 0.0

    normalized_album = _normalize_text(expected_album)
    candidate_album = _normalize_text(candidate.album)
    if normalized_album and normalized_album == candidate_album:
        return 1.0

    haystack = " ".join(
        [candidate.title, candidate.description, candidate.album]
    )
    if normalized_album and normalized_album in _normalize_text(haystack):
        return 0.7

    overlap = _token_overlap(expected_album, candidate.album or candidate.title)
    if overlap >= 0.6:
        return 0.5
    return 0.0


def _release_year_ratio(expected_year: int, candidate_year: Optional[int]) -> float:
    if candidate_year is None:
        return 0.0
    diff = abs(expected_year - candidate_year)
    if diff == 0:
        return 1.0
    if diff == 1:
        return 0.6
    if diff == 2:
        return 0.3
    return 0.0


def score_candidate(
    track: TrackInfo,
    candidate: Candidate,
    weights: ScoringWeights = DEFAULT_WEIGHTS,
) -> ScoreBreakdown:
    title, _, _, album, duration, _, _, release_year = track
    return ScoreBreakdown(
        artist_match=_artist_match_ratio(track, candidate),
        title_match=_title_match_ratio(title, candidate.title),
        duration_similarity=_duration_similarity_ratio(duration, candidate.duration),
        official_channel=_official_channel_ratio(track, candidate),
        album_similarity=_album_similarity_ratio(album, candidate),
        release_year_proximity=_release_year_ratio(
            release_year, candidate.release_year
        ),
        weights=weights,
    )


def iter_search_video_ids(
    track: TrackInfo,
    max_count: int = MAX_CANDIDATES,
) -> Iterator[tuple[str, Source]]:
    """
    Yield up to *max_count* unique video IDs, preferring YouTube Music results.
    Falls back to regular YouTube when Music returns no matches.
    """
    query = _build_search_query(track)
    music_entries = _search_youtube_music(query, max_count)

    if music_entries:
        source: Source = "youtube_music"
        entries = music_entries
    else:
        source = "youtube"
        entries = _search_youtube(query, max_count)

    seen: set[str] = set()
    yielded = 0
    for entry in entries:
        if yielded >= max_count:
            break
        video_id = entry.get("id")
        if not video_id or video_id in seen:
            continue
        seen.add(video_id)
        yield video_id, source
        yielded += 1


def _build_ranked_candidate(
    track: TrackInfo,
    candidate: Candidate,
    rating: float,
) -> RankedCandidate:
    _, _, _, _, _, _, popularity, spotify_release_year = track
    return RankedCandidate(
        video_id=candidate.video_id,
        rating=rating,
        title=candidate.title,
        primary_artist=candidate.artist or candidate.channel or candidate.uploader,
        featured_artists=(),
        album=candidate.album,
        duration=candidate.duration or 0,
        isrc=candidate.isrc,
        popularity=popularity,
        release_year=candidate.release_year or spotify_release_year,
        source=candidate.source,
        url=candidate.url,
    )


def _heap_push(heap: list[HeapEntry], ranked: RankedCandidate) -> None:
    heapq.heappush(heap, (-ranked.rating, ranked.video_id, ranked))


def heap_to_sorted_candidates(heap: list[HeapEntry]) -> list[RankedCandidate]:
    return [
        entry[2]
        for entry in sorted(heap, key=lambda item: (item[0], item[1]))
    ]


def try_direct_isrc_download(
    track: TrackInfo,
    candidate: Candidate,
    *,
    save_directory: Path = SAVE_DIRECTORY,
) -> Optional[Path]:
    """
    If the candidate ISRC matches Spotify, download audio and return the file path.
    """
    _, _, _, _, _, spotify_isrc, _, _ = track
    if not is_direct_isrc_match(spotify_isrc, candidate.isrc):
        return None

    filename_base = build_audio_filename(spotify_isrc, candidate.video_id)
    return download_audio(
        candidate.url,
        save_directory,
        filename_base=filename_base,
    )


def run_pipeline(
    spotify_link: str,
    *,
    threshold: float = THRESHOLD,
    save_directory: Path | str = SAVE_DIRECTORY,
    max_candidates: int = MAX_CANDIDATES,
    weights: ScoringWeights = DEFAULT_WEIGHTS,
) -> PipelineResult:
    """
    Full search pipeline:
    1. Load Spotify metadata (+ track ID for re-lookup).
    2. Search YouTube Music one-by-one (up to max_candidates), then YouTube if empty.
    3. On ISRC direct match: download audio immediately and stop.
    4. Otherwise score each result; keep those >= threshold in a max-heap.
    """
    save_directory = Path(save_directory)
    track = get_track_info(spotify_link)
    spotify_track_id = parse_spotify_track_id(spotify_link)
    heap: list[HeapEntry] = []

    for video_id, source in iter_search_video_ids(track, max_candidates):
        info = _fetch_ytdlp_info(video_id, source)
        candidate = _candidate_from_info(video_id, source, info)

        downloaded = try_direct_isrc_download(
            track, candidate, save_directory=save_directory
        )
        if downloaded is not None:
            return PipelineResult(
                spotify_track_id=spotify_track_id,
                track=track,
                direct_isrc_match=True,
                downloaded_path=downloaded,
                candidate_heap=[],
            )

        breakdown = score_candidate(track, candidate, weights=weights)
        if breakdown.total >= threshold:
            ranked = _build_ranked_candidate(track, candidate, breakdown.total)
            _heap_push(heap, ranked)

    return PipelineResult(
        spotify_track_id=spotify_track_id,
        track=track,
        direct_isrc_match=False,
        downloaded_path=None,
        candidate_heap=heap_to_sorted_candidates(heap),
    )


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print(f"Usage: python {sys.argv[0]} <spotify_track_url>")
        raise SystemExit(1)

    result = run_pipeline(sys.argv[1])
    output = {
        "spotify_track_id": result.spotify_track_id,
        "direct_isrc_match": result.direct_isrc_match,
        "downloaded_path": str(result.downloaded_path) if result.downloaded_path else None,
        "candidates": [
            {
                "video_id": candidate.video_id,
                "url": candidate.url,
                "source": candidate.source,
                "rating": candidate.rating,
                "metadata": {
                    "title": candidate.title,
                    "primary_artist": candidate.primary_artist,
                    "featured_artists": list(candidate.featured_artists),
                    "album": candidate.album,
                    "duration": candidate.duration,
                    "isrc": candidate.isrc,
                    "popularity": candidate.popularity,
                    "release_year": candidate.release_year,
                },
            }
            for candidate in result.candidate_heap
        ],
    }
    print(json.dumps(output, indent=2))
