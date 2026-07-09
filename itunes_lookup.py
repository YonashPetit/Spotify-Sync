"""Resolve iTunes preview URLs from ISRC codes via the public Search API."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Optional

from isrc_match import normalize_isrc

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"


def lookup_preview_url_by_isrc(
    isrc: str,
    *,
    timeout: float = 15.0,
) -> Optional[str]:
    """
    Look up a 30-second iTunes preview URL for *isrc*.

    Uses the public iTunes Search API (no API key required):
    ``https://itunes.apple.com/search?term={ISRC}&entity=song&limit=1``
    """
    normalized = normalize_isrc(isrc)
    if not normalized:
        return None

    query = urllib.parse.urlencode(
        {
            "term": normalized,
            "entity": "song",
            "limit": 1,
        }
    )
    request = urllib.request.Request(
        f"{ITUNES_SEARCH_URL}?{query}",
        headers={"User-Agent": "spotify_sync/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read().decode("utf-8"))

    results = body.get("results")
    if not isinstance(results, list) or not results:
        return None

    preview_url = results[0].get("previewUrl")
    if not isinstance(preview_url, str) or not preview_url.strip():
        return None
    return preview_url.strip()
