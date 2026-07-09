"""Track API / download rate-limit hits for logging and recovery."""

from __future__ import annotations

import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from output import print_human

_RATE_LIMIT_PATTERNS = (
    re.compile(r"rate\s*limit", re.IGNORECASE),
    re.compile(r"too many requests", re.IGNORECASE),
    re.compile(r"\b429\b"),
    re.compile(r"quota exceeded", re.IGNORECASE),
    re.compile(r"resource_exhausted", re.IGNORECASE),
    re.compile(r"slow down", re.IGNORECASE),
)


@dataclass
class RateLimitHit:
    source: str
    operation: str
    message: str
    at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RateLimitState:
    hits: list[RateLimitHit] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"count": len(self.hits), "hits": [hit.as_dict() for hit in self.hits]}


_lock = threading.Lock()
_state = RateLimitState()


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_rate_limited(exc: BaseException) -> bool:
    http_status = getattr(exc, "http_status", None)
    if http_status == 429:
        return True
    text = str(exc)
    return any(pattern.search(text) for pattern in _RATE_LIMIT_PATTERNS)


def infer_source(exc: BaseException) -> str:
    try:
        from spotipy.exceptions import SpotifyException

        if isinstance(exc, SpotifyException):
            return "spotify"
    except ImportError:
        pass
    try:
        import yt_dlp

        if isinstance(exc, yt_dlp.utils.YoutubeDLError):
            return "youtube"
    except ImportError:
        pass

    message = str(exc).lower()
    if "acoustid" in message:
        return "acoustid"
    if "spotify" in message or "spotipy" in message:
        return "spotify"
    if "youtube" in message or "yt-dlp" in message or "yt_dlp" in message:
        return "youtube"
    return "unknown"


def record_rate_limit(
    *,
    source: str,
    operation: str,
    message: str,
) -> RateLimitHit:
    hit = RateLimitHit(
        source=source,
        operation=operation,
        message=message.strip(),
        at=_utc_now(),
    )
    with _lock:
        _state.hits.append(hit)
        total = len(_state.hits)
    print_human(
        f"RATE LIMIT HIT ({source}): {hit.message} "
        f"[during {operation}; total hits this session: {total}]"
    )
    return hit


def note_exception(exc: BaseException, *, operation: str) -> bool:
    """Record and log a rate-limit hit when *exc* looks like throttling."""
    if not is_rate_limited(exc):
        return False
    record_rate_limit(
        source=infer_source(exc),
        operation=operation,
        message=str(exc),
    )
    return True


def hit_count() -> int:
    with _lock:
        return len(_state.hits)


def snapshot() -> RateLimitState:
    with _lock:
        return RateLimitState(hits=list(_state.hits))


def clear() -> int:
    with _lock:
        cleared = len(_state.hits)
        _state.hits.clear()
    return cleared


def print_summary_if_any() -> None:
    with _lock:
        hits = list(_state.hits)
    if not hits:
        return
    print_human(f"Rate limits hit this session: {len(hits)}.")
    for index, hit in enumerate(hits, start=1):
        print_human(
            f"  {index}. [{hit.at}] {hit.source} during {hit.operation}: {hit.message}"
        )


def reset_for_sync() -> dict[str, Any]:
    """Clear rate-limit counters and reset the per-thread DB connection."""
    import db

    cleared_hits = clear()
    db.reset_connection()
    if cleared_hits:
        print_human(
            f"Reset database connection and cleared {cleared_hits} "
            "recorded rate-limit hit(s). Safe to retry sync."
        )
    else:
        print_human(
            "Reset database connection. No rate-limit hits were recorded this session."
        )
    return {"cleared_rate_limit_hits": cleared_hits}
