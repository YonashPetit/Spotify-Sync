"""Spotify user OAuth (Authorization Code flow) for playlist item access.

Spotify requires a user token for playlist item enumeration on newer API
apps; Client Credentials only covers track/playlist metadata. The token is
cached in the app home directory and refreshed automatically.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import spotipy
from spotipy.cache_handler import CacheFileHandler
from spotipy.oauth2 import SpotifyOAuth

from app_paths import get_app_home

OAUTH_SCOPES = "playlist-read-private playlist-read-collaborative"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8888/callback"


class SpotifyUserAuthRequired(RuntimeError):
    pass


def token_cache_path() -> Path:
    return get_app_home() / "spotify_token_cache.json"


def legacy_token_cache_paths() -> list[Path]:
    """Spotipy's default ``.cache`` file (cwd) and other legacy locations."""
    return [Path.cwd() / ".cache"]


def clear_token_cache() -> list[str]:
    """Remove cached Spotify OAuth tokens. Returns paths that were deleted."""
    removed: list[str] = []
    for path in (token_cache_path(), *legacy_token_cache_paths()):
        if path.is_file():
            path.unlink()
            removed.append(str(path))
    return removed


def redirect_uri() -> str:
    return os.environ.get("SPOTIPY_REDIRECT_URI", DEFAULT_REDIRECT_URI)


def _oauth_manager(*, open_browser: bool) -> SpotifyOAuth:
    client_id = os.environ.get("SPOTIPY_CLIENT_ID")
    client_secret = os.environ.get("SPOTIPY_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise EnvironmentError(
            "Set SPOTIPY_CLIENT_ID and SPOTIPY_CLIENT_SECRET environment variables."
        )
    return SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri(),
        scope=OAUTH_SCOPES,
        cache_handler=CacheFileHandler(cache_path=str(token_cache_path())),
        open_browser=open_browser,
    )


def _validated_cached_token(manager: SpotifyOAuth) -> Optional[dict]:
    """Return a usable token from cache (refreshing if expired), else None."""
    token = manager.cache_handler.get_cached_token()
    if token is None:
        return None
    try:
        return manager.validate_token(token)
    except Exception:
        return None


def has_cached_token() -> bool:
    try:
        manager = _oauth_manager(open_browser=False)
    except EnvironmentError:
        return False
    return _validated_cached_token(manager) is not None


def _acquire_auth_code(manager: SpotifyOAuth, timeout_seconds: float) -> str:
    """
    Open the browser and catch the OAuth redirect with a local HTTP server.

    Unlike spotipy's built-in server, this uses short per-request timeouts so
    browsers' speculative keep-alive connections cannot hang the process.
    """
    import time
    import webbrowser
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, urlparse

    uri = urlparse(redirect_uri())
    outcome: dict[str, str] = {}

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            query = parse_qs(urlparse(self.path).query)
            if "code" in query:
                outcome["code"] = query["code"][0]
                body = b"Spotify authorization complete. You can close this tab."
            elif "error" in query:
                outcome["error"] = query["error"][0]
                body = b"Spotify authorization failed. You can close this tab."
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    server = HTTPServer((uri.hostname or "127.0.0.1", uri.port or 80), RedirectHandler)
    server.timeout = 1
    try:
        webbrowser.open(manager.get_authorize_url())
        deadline = time.monotonic() + timeout_seconds
        while not outcome and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()

    if "error" in outcome:
        raise SpotifyUserAuthRequired(
            f"Spotify authorization failed: {outcome['error']}"
        )
    if "code" not in outcome:
        raise SpotifyUserAuthRequired(
            "Timed out waiting for Spotify authorization in the browser."
        )
    return outcome["code"]


def login_interactive(
    *, timeout_seconds: float = 300.0, force: bool = False
) -> tuple[spotipy.Spotify, list[str]]:
    """
    Run the browser OAuth flow and return an authenticated client.

    When ``force`` is True (e.g. re-running ``login``), any existing token
    cache is cleared and the user must sign in again. Returns
    ``(client, cleared_cache_paths)``.
    """
    removed = clear_token_cache() if force else []
    manager = _oauth_manager(open_browser=False)
    if force or _validated_cached_token(manager) is None:
        code = _acquire_auth_code(manager, timeout_seconds)
        manager.get_access_token(code, as_dict=False)
    return spotipy.Spotify(auth_manager=manager), removed


def create_user_client() -> spotipy.Spotify:
    """Non-interactive user client. Raises if no valid cached token exists."""
    manager = _oauth_manager(open_browser=False)
    if _validated_cached_token(manager) is None:
        raise SpotifyUserAuthRequired(
            "Spotify user login required for playlist access. "
            "Run: spotify-sync login"
        )
    return spotipy.Spotify(auth_manager=manager)
