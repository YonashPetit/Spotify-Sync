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
