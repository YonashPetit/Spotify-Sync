"""Dual-mode output: human logs vs single-JSON-object Hermes mode."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from models import DuplicatePolicy

JSON_VERSION = "1"

# Set once by the CLI entry point.
JSON_MODE = False


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def emit_success(command: str, data: dict) -> None:
    """Print the single success JSON envelope on stdout."""
    envelope = {
        "ok": True,
        "command": command,
        "version": JSON_VERSION,
        "timestamp": _timestamp(),
        "data": data,
    }
    print(json.dumps(envelope, ensure_ascii=False))


def emit_error(command: str, code: str, message: str) -> None:
    """Print the single error JSON envelope on stdout."""
    envelope = {
        "ok": False,
        "command": command,
        "version": JSON_VERSION,
        "timestamp": _timestamp(),
        "error": {"code": code, "message": message},
    }
    print(json.dumps(envelope, ensure_ascii=False))


def print_human(message: str) -> None:
    """Human-readable log line: stdout normally, stderr in JSON mode."""
    stream = sys.stderr if JSON_MODE else sys.stdout
    print(message, file=stream, flush=True)


def log_operation_start(operation: str) -> None:
    print_human(f"Beginning {operation}.")


def log_operation_success(operation: str) -> None:
    print_human(f"Completed {operation}.")


def log_operation_error(operation: str, reason: str) -> None:
    print_human(f"Failed {operation} for {reason!r} reason.")


def song_title(title: str) -> str:
    cleaned = (title or "").strip()
    return cleaned or "Unknown"


def log_download_start(title: str) -> None:
    print_human(f"Beginning download of '{song_title(title)}'.")


def log_download_retry(title: str) -> None:
    print_human(f"Retrying to download '{song_title(title)}' track.")


def log_download_success(title: str) -> None:
    print_human(f"Completed download of '{song_title(title)}'.")


def log_download_success_with_reason(title: str, reason: str) -> None:
    print_human(f"Completed download of '{song_title(title)}' {reason}.")


def log_download_failed(title: str, reason: str) -> None:
    print_human(f"Failed to download '{song_title(title)}' for {reason!r} reason.")


def log_track_skipped(title: str, reason: str) -> None:
    print_human(f"Skipped '{song_title(title)}' ({reason}).")


def log_process_result(result) -> None:
    """Emit human logs for a :class:`ProcessResult` using the song title."""
    from models import ProcessResult

    if not isinstance(result, ProcessResult) or result.track is None:
        return
    title = result.track.title
    if result.status == "downloaded":
        if result.message:
            log_download_success_with_reason(title, result.message)
        else:
            log_download_success(title)
        return
    if result.status == "failed":
        reason = result.message or "unknown error"
        prefix = "Download failed: "
        if reason.startswith(prefix):
            reason = reason[len(prefix) :]
        log_download_failed(title, reason)
        return
    if result.status == "already_present":
        log_track_skipped(title, "already present")
        return
    if result.status == "adopted":
        print_human(
            f"Adopted '{song_title(title)}' from playlist folder"
            + (f" ({result.message})" if result.message else "")
            + "."
        )
        return
    if result.status == "skipped_duplicate":
        log_track_skipped(title, "duplicate")
        return
    if result.status == "skipped_blacklisted":
        log_track_skipped(title, "blacklisted")
        return
    if result.status == "needs_user_choice":
        print_human(
            f"Duplicate review needed for '{song_title(title)}' "
            f"(request_id={result.request_id})."
        )
        return
    if result.status == "cancelled":
        print_human(
            f"Cancelled download of '{song_title(title)}'"
            + (f" ({result.message})" if result.message else "")
            + "."
        )


def prompt_duplicate_choice() -> DuplicatePolicy:
    """Interactive duplicate prompt (human mode only)."""
    print("A potential duplicate of this track already exists in the library.")
    while True:
        answer = (
            input("Choose action - [s]kip / [r]eplace / [k]eep both: ")
            .strip()
            .lower()
        )
        if answer in ("s", "skip"):
            return "skip"
        if answer in ("r", "replace"):
            return "replace"
        if answer in ("k", "keep_both", "keep both", "keep"):
            return "keep_both"
        print("Please answer s, r, or k.")
