"""Application data paths (user config dir via platformdirs)."""

from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_data_dir

APP_NAME = "spotify_sync"


def get_app_home() -> Path:
    """Return %APPDATA%/spotify_sync (via platformdirs). Create if missing.

    Honors SPOTIFY_SYNC_HOME for tests / relocation.
    """
    override = os.environ.get("SPOTIFY_SYNC_HOME")
    if override:
        home = Path(override)
    else:
        home = Path(user_data_dir(APP_NAME, appauthor=False, roaming=True))
    home.mkdir(parents=True, exist_ok=True)
    return home


def ensure_app_home() -> Path:
    return get_app_home()


def get_db_path() -> Path:
    return get_app_home() / "state.db"
