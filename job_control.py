"""Cooperative cancellation for long-running sync / import jobs."""

from __future__ import annotations

import threading
from pathlib import Path

from app_paths import get_app_home

STOP_FLAG_NAME = "stop_sync.request"

_stop_event = threading.Event()


class SyncStopped(Exception):
    """Raised when a sync/import job observes a stop request."""


def stop_flag_path() -> Path:
    return get_app_home() / STOP_FLAG_NAME


def request_stop() -> Path:
    """
    Ask the running sync/import to stop after the current song finishes.

    Sets an in-process event and writes a flag file so a separate CLI process
    can interrupt another ``spotify-sync sync`` run.
    """
    _stop_event.set()
    path = stop_flag_path()
    path.write_text("stop\n", encoding="utf-8")
    return path


def clear_stop() -> None:
    """Clear any previous stop request (call at the start of a job)."""
    _stop_event.clear()
    path = stop_flag_path()
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def stop_requested() -> bool:
    if _stop_event.is_set():
        return True
    return stop_flag_path().is_file()


def check_stop(context: str = "operation") -> None:
    """Raise ``SyncStopped`` if a stop has been requested."""
    if stop_requested():
        message = f"Stopped during {context}." if context else "Stopped."
        raise SyncStopped(message)
