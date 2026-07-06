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
from get_content import TrackInfo, get_track_info
from isrc_match import (
    extract_isrc_for_video,
    find_video_by_isrc_search,
    is_direct_isrc_match,
)
from audio_similarity import (
    ENABLE_CHROMAPRINT_MATCH,
    ENABLE_EMBEDDING_MATCH,
    resolve_by_audio_similarity,
)

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

_YDL_MUSIC_METADATA_OPTS: dict = {
    **_YDL_METADATA_OPTS,
    "extractor_args": {"youtube": {"player_client": ["web_music", "android_vr"]}},
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
    """
    Heap entry keyed by YouTube ``video_id`` for re-lookup on YouTube Music
    or regular YouTube via ``watch_url()``.
    """

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

    def watch_url(self) -> str:
        return _watch_url(self.video_id, self.source)

    def as_tuple(self) -> tuple:
        """(rating, video_id, title, primary_artist, featured_artists, album,
        duration, isrc, popularity, release_year, source, url)"""
        return (
            self.rating,
            self.video_id,
            self.title,
            self.primary_artist,
            self.featured_artists,
            self.album,
            self.duration,
            self.isrc,
            self.popularity,
            self.release_year,
            self.source,
            self.url,
        )


@dataclass
class PipelineResult:
    track: TrackInfo
    direct_isrc_match: bool
    downloaded_path: Optional[Path]
    matched_video_id: Optional[str]
    match_method: Optional[str]
    audio_match_certainty: Optional[float]
    candidate_heap: list[RankedCandidate]

    @property
    def best_candidate(self) -> Optional[RankedCandidate]:
        if not self.candidate_heap:
            return None
        return self.candidate_heap[0]


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
    opts = _YDL_MUSIC_METADATA_OPTS if source == "youtube_music" else _YDL_METADATA_OPTS
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if info is None:
        raise ValueError(f"No metadata returned for {url}")
    return info


def _candidate_from_info(
    video_id: str, source: Source, info: dict[str, Any]
) -> Candidate:
    canonical_id = info.get("id") or video_id
    release_year = info.get("release_year")
    if release_year is None and info.get("upload_date"):
        release_year = int(str(info["upload_date"])[:4])

    return Candidate(
        video_id=canonical_id,
        url=_watch_url(canonical_id, source),
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
        isrc=extract_isrc_for_video(canonical_id, source, info),
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
    """Return heap entries sorted by rating descending (highest first)."""
    return sorted(
        (entry[2] for entry in heap),
        key=lambda candidate: (-candidate.rating, candidate.video_id),
    )


def _download_matched_audio(
    track: TrackInfo,
    candidate: Candidate,
    *,
    save_directory: Path,
) -> Path:
    _, _, _, _, _, spotify_isrc, _, _ = track
    filename_base = build_audio_filename(spotify_isrc, candidate.video_id)
    return download_audio(
        candidate.url,
        save_directory,
        filename_base=filename_base,
    )


def try_direct_isrc_download(
    track: TrackInfo,
    candidate: Candidate,
    *,
    save_directory: Path = SAVE_DIRECTORY,
) -> Optional[Path]:
    """Download when the candidate metadata ISRC matches Spotify."""
    _, _, _, _, _, spotify_isrc, _, _ = track
    if not is_direct_isrc_match(spotify_isrc, candidate.isrc):
        return None
    return _download_matched_audio(track, candidate, save_directory=save_directory)


def _handle_direct_isrc_hit(
    track: TrackInfo,
    video_id: str,
    source: Source,
    *,
    save_directory: Path,
) -> PipelineResult:
    info = _fetch_ytdlp_info(video_id, source)
    candidate = _candidate_from_info(video_id, source, info)
    downloaded_path = _download_matched_audio(
        track, candidate, save_directory=save_directory
    )
    return PipelineResult(
        track=track,
        direct_isrc_match=True,
        downloaded_path=downloaded_path,
        matched_video_id=candidate.video_id,
        match_method="isrc",
        audio_match_certainty=None,
        candidate_heap=[],
    )


def run_pipeline(
    spotify_link: str,
    *,
    threshold: float = THRESHOLD,
    save_directory: Path | str = SAVE_DIRECTORY,
    max_candidates: int = MAX_CANDIDATES,
    weights: ScoringWeights = DEFAULT_WEIGHTS,
    enable_chromaprint: Optional[bool] = None,
    enable_embedding: Optional[bool] = None,
) -> PipelineResult:
    """
    Full search pipeline:
    1. Load Spotify metadata.
    2. Try ISRC search on YouTube Music (then YouTube) for a direct match.
    3. Otherwise search by artist/title one-by-one (up to max_candidates).
    4. On per-candidate ISRC metadata match: download and stop.
    5. Score remaining candidates; keep rating >= threshold in a max-heap.
    6. If enabled, run chromaprint / embedding matchers on top candidates.
    7. If audio matching is disabled (or finds nothing), download the heap top.

    ``enable_chromaprint`` / ``enable_embedding`` default to the module
    constants in ``audio_similarity`` when left as None.
    """
    use_chromaprint = (
        ENABLE_CHROMAPRINT_MATCH if enable_chromaprint is None else enable_chromaprint
    )
    use_embedding = (
        ENABLE_EMBEDDING_MATCH if enable_embedding is None else enable_embedding
    )
    save_directory = Path(save_directory)
    track = get_track_info(spotify_link)
    _, _, _, _, _, spotify_isrc, _, _ = track
    heap: list[HeapEntry] = []

    isrc_hit = find_video_by_isrc_search(spotify_isrc)
    if isrc_hit is not None:
        video_id, source = isrc_hit
        try:
            return _handle_direct_isrc_hit(
                track, video_id, source, save_directory=save_directory
            )
        except Exception:
            # ISRC search can surface unavailable/region-locked videos;
            # fall through to the regular candidate search.
            pass

    for video_id, source in iter_search_video_ids(track, max_candidates):
        try:
            info = _fetch_ytdlp_info(video_id, source)
        except Exception:
            # Skip unavailable / non-video entries (e.g. channels in results).
            continue
        candidate = _candidate_from_info(video_id, source, info)

        downloaded = try_direct_isrc_download(
            track, candidate, save_directory=save_directory
        )
        if downloaded is not None:
            return PipelineResult(
                track=track,
                direct_isrc_match=True,
                downloaded_path=downloaded,
                matched_video_id=candidate.video_id,
                match_method="isrc",
                audio_match_certainty=None,
                candidate_heap=[],
            )

        breakdown = score_candidate(track, candidate, weights=weights)
        if breakdown.total >= threshold:
            ranked = _build_ranked_candidate(track, candidate, breakdown.total)
            _heap_push(heap, ranked)

    sorted_candidates = heap_to_sorted_candidates(heap)

    if sorted_candidates and (use_chromaprint or use_embedding):
        audio_result = resolve_by_audio_similarity(
            track,
            sorted_candidates,
            save_directory,
            spotify_link=spotify_link,
            enable_chromaprint=use_chromaprint,
            enable_embedding=use_embedding,
        )
        if audio_result is not None and audio_result.downloaded_path is not None:
            return PipelineResult(
                track=track,
                direct_isrc_match=False,
                downloaded_path=audio_result.downloaded_path,
                matched_video_id=audio_result.video_id,
                match_method=audio_result.method,
                audio_match_certainty=audio_result.certainty,
                candidate_heap=sorted_candidates,
            )

    if sorted_candidates and not use_chromaprint and not use_embedding:
        # Audio matching fully disabled: trust the metadata ranking and
        # download the top-rated heap candidate.
        top = sorted_candidates[0]
        filename_base = build_audio_filename(spotify_isrc, top.video_id)
        downloaded_path = download_audio(
            top.watch_url(),
            save_directory,
            filename_base=filename_base,
        )
        return PipelineResult(
            track=track,
            direct_isrc_match=False,
            downloaded_path=downloaded_path,
            matched_video_id=top.video_id,
            match_method="heap_top",
            audio_match_certainty=None,
            candidate_heap=sorted_candidates,
        )

    return PipelineResult(
        track=track,
        direct_isrc_match=False,
        downloaded_path=None,
        matched_video_id=None,
        match_method=None,
        audio_match_certainty=None,
        candidate_heap=sorted_candidates,
    )


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print(f"Usage: python {sys.argv[0]} <spotify_track_url>")
        raise SystemExit(1)

    result = run_pipeline(sys.argv[1])
    output = {
        "direct_isrc_match": result.direct_isrc_match,
        "match_method": result.match_method,
        "audio_match_certainty": result.audio_match_certainty,
        "matched_video_id": result.matched_video_id,
        "downloaded_path": str(result.downloaded_path) if result.downloaded_path else None,
        "chromaprint_enabled": ENABLE_CHROMAPRINT_MATCH,
        "embedding_enabled": ENABLE_EMBEDDING_MATCH,
        "candidates": [
            {
                "rating": candidate.rating,
                "video_id": candidate.video_id,
                "url": candidate.watch_url(),
                "source": candidate.source,
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
