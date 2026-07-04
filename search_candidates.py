from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal, Optional
from urllib.parse import quote_plus

import yt_dlp

from get_content import TrackInfo, get_track_info

Source = Literal["youtube_music", "youtube"]

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
    """Weights tuned for discriminating official uploads from covers and reuploads."""

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


# Artist + title dominate (60 pts) because wrong identity is the costliest error.
# Duration is next (20) to drop edits, sped-up versions, and live cuts.
# Official channel / album / year break ties among plausible matches.
DEFAULT_WEIGHTS = ScoringWeights()
DEFAULT_MIN_PASS_SCORE = 50.0


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


@dataclass
class ScoredCandidate:
    candidate: Candidate
    score: ScoreBreakdown

    @property
    def total_score(self) -> float:
        return self.score.total


def _normalize_text(value: str) -> str:
    value = value.lower()
    value = value.replace("'", "").replace("’", "").replace("`", "")
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


def _fetch_candidate_metadata(video_id: str, source: Source) -> Candidate:
    url = _watch_url(video_id, source)
    with yt_dlp.YoutubeDL(_YDL_METADATA_OPTS) as ydl:
        info = ydl.extract_info(url, download=False)

    release_year = info.get("release_year")
    if release_year is None and info.get("upload_date"):
        release_year = int(str(info["upload_date"])[:4])

    return Candidate(
        video_id=video_id,
        url=url,
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
        (_token_overlap(expected, actual) for expected in expected_artists for actual in candidate_artists),
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
        [
            candidate.title,
            candidate.description,
            candidate.album,
        ]
    )
    if normalized_album and normalized_album in _normalize_text(haystack):
        return 0.7

    overlap = _token_overlap(expected_album, candidate.album or candidate.title)
    if overlap >= 0.6:
        return 0.5
    return 0.0


def _release_year_ratio(
    expected_year: int, candidate_year: Optional[int]
) -> float:
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
    title, primary_artist, _, album, duration, _, _, release_year = track
    return ScoreBreakdown(
        artist_match=_artist_match_ratio(track, candidate),
        title_match=_title_match_ratio(title, candidate.title),
        duration_similarity=_duration_similarity_ratio(duration, candidate.duration),
        official_channel=_official_channel_ratio(track, candidate),
        album_similarity=_album_similarity_ratio(album, candidate),
        release_year_proximity=_release_year_ratio(release_year, candidate.release_year),
        weights=weights,
    )


def _collect_raw_candidates(
    track: TrackInfo,
    search_limit: int,
) -> list[tuple[str, Source]]:
    query = _build_search_query(track)
    seen: set[str] = set()
    ordered: list[tuple[str, Source]] = []

    for entry in _search_youtube_music(query, search_limit):
        video_id = entry["id"]
        if video_id not in seen:
            seen.add(video_id)
            ordered.append((video_id, "youtube_music"))

    if len(ordered) < 5:
        for entry in _search_youtube(query, search_limit):
            video_id = entry["id"]
            if video_id not in seen:
                seen.add(video_id)
                ordered.append((video_id, "youtube"))

    return ordered[:search_limit]


def find_candidates(
    track: TrackInfo,
    *,
    min_candidates: int = 5,
    max_candidates: int = 10,
    min_pass_score: float = DEFAULT_MIN_PASS_SCORE,
    weights: ScoringWeights = DEFAULT_WEIGHTS,
) -> list[ScoredCandidate]:
    """
    Search YouTube Music first, then YouTube, score against Spotify metadata,
    and drop candidates below min_pass_score.
    """
    if min_candidates > max_candidates:
        raise ValueError("min_candidates cannot exceed max_candidates")

    raw_candidates = _collect_raw_candidates(track, search_limit=max_candidates + 2)
    scored: list[ScoredCandidate] = []

    for video_id, source in raw_candidates:
        candidate = _fetch_candidate_metadata(video_id, source)
        breakdown = score_candidate(track, candidate, weights=weights)
        if breakdown.total >= min_pass_score:
            scored.append(ScoredCandidate(candidate=candidate, score=breakdown))

    scored.sort(key=lambda item: item.total_score, reverse=True)
    finalists = scored[:max_candidates]

    if len(finalists) < min_candidates:
        raise ValueError(
            f"Only {len(finalists)} candidates scored >= {min_pass_score}. "
            "Try lowering min_pass_score or verify the Spotify link."
        )

    return finalists


def find_candidates_from_spotify_link(
    spotify_link: str,
    **kwargs,
) -> list[ScoredCandidate]:
    track = get_track_info(spotify_link)
    return find_candidates(track, **kwargs)


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) < 2:
        print(f"Usage: python {sys.argv[0]} <spotify_track_url> [min_pass_score]")
        raise SystemExit(1)

    min_score = float(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_MIN_PASS_SCORE
    results = find_candidates_from_spotify_link(sys.argv[1], min_pass_score=min_score)

    for index, result in enumerate(results, start=1):
        payload = {
            "rank": index,
            "url": result.candidate.url,
            "source": result.candidate.source,
            "title": result.candidate.title,
            "artist": result.candidate.artist or result.candidate.channel,
            "duration": result.candidate.duration,
            "score": result.score.as_dict(),
        }
        print(json.dumps(payload, indent=2))
