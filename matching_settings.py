"""Persisted matching toggles, thresholds, and metadata scoring weights."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from typing import Any, Optional

import settings

MATCHING_SETTINGS_KEY = "matching_settings"
CHROMAPRINT_STRATEGIES = ("acoustid_api", "local_scan")
DEFAULT_CHROMAPRINT_STRATEGY = "acoustid_api"

_runtime_chromaprint_strategy: Optional[str] = None


@dataclass
class MatchingSettings:
    """Global audio matching configuration (any combination of the four toggles)."""

    chromaprint_strategy: str = DEFAULT_CHROMAPRINT_STRATEGY
    duplicate_chromaprint: bool = False
    duplicate_embedding: bool = False
    comparison_chromaprint: bool = False
    comparison_embedding: bool = False
    comparison_metadata_fallback: bool = False
    metadata_minimum_rating: float = 50.0
    audio_duplicate_threshold: float = 0.95
    audio_review_threshold: float = 0.85
    chromaprint_match_certainty: float = 0.90
    embedding_match_threshold: float = 0.90
    max_audio_match_attempts: int = 3
    weight_artist: float = 30.0
    weight_title: float = 30.0
    weight_duration: float = 20.0
    weight_official_channel: float = 10.0
    weight_album: float = 5.0
    weight_release_year: float = 5.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def scoring_weight_total(self) -> float:
        return (
            self.weight_artist
            + self.weight_title
            + self.weight_duration
            + self.weight_official_channel
            + self.weight_album
            + self.weight_release_year
        )

    def to_scoring_weights(self):
        from search_candidates import ScoringWeights

        return ScoringWeights(
            exact_artist_match=self.weight_artist,
            exact_title_match=self.weight_title,
            duration_similarity=self.weight_duration,
            official_channel=self.weight_official_channel,
            album_similarity=self.weight_album,
            release_year_proximity=self.weight_release_year,
        )

    def duplicate_audio_enabled(self) -> bool:
        return self.duplicate_chromaprint or self.duplicate_embedding

    def comparison_audio_enabled(self) -> bool:
        return self.comparison_chromaprint or self.comparison_embedding


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "on", "yes")
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_strategy(value: Any, default: str) -> str:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in CHROMAPRINT_STRATEGIES:
            return normalized
    return default


def get_chromaprint_strategy() -> str:
    if _runtime_chromaprint_strategy is not None:
        return _runtime_chromaprint_strategy
    return load_matching_settings().chromaprint_strategy


def set_runtime_chromaprint_strategy(strategy: Optional[str]) -> None:
    global _runtime_chromaprint_strategy
    if strategy is None:
        _runtime_chromaprint_strategy = None
        return
    _runtime_chromaprint_strategy = parse_chromaprint_strategy(strategy)


def load_matching_settings() -> MatchingSettings:
    raw = settings.get_setting(MATCHING_SETTINGS_KEY)
    if not raw:
        return MatchingSettings()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return MatchingSettings()
    if not isinstance(data, dict):
        return MatchingSettings()

    defaults = MatchingSettings()
    kwargs: dict[str, Any] = {}
    for field in fields(MatchingSettings):
        if field.name not in data:
            continue
        value = data[field.name]
        default_value = getattr(defaults, field.name)
        if field.name == "chromaprint_strategy":
            kwargs[field.name] = _coerce_strategy(value, default_value)
        elif isinstance(default_value, bool):
            kwargs[field.name] = _coerce_bool(value, default_value)
        elif isinstance(default_value, int):
            kwargs[field.name] = _coerce_int(value, default_value)
        else:
            kwargs[field.name] = _coerce_float(value, default_value)
    return MatchingSettings(**kwargs)


def save_matching_settings(config: MatchingSettings) -> None:
    _validate_matching_settings(config)
    settings.set_setting(MATCHING_SETTINGS_KEY, json.dumps(config.as_dict()))


def update_matching_settings(**changes: Any) -> MatchingSettings:
    current = load_matching_settings()
    data = current.as_dict()
    for key, value in changes.items():
        if key not in data:
            raise ValueError(f"Unknown matching setting: {key!r}")
        data[key] = value
    updated = MatchingSettings(**data)
    save_matching_settings(updated)
    return updated


def _validate_matching_settings(config: MatchingSettings) -> None:
    total = config.scoring_weight_total()
    if abs(total - 100.0) > 0.01:
        raise ValueError(f"Scoring weights must sum to 100, got {total}")

    if config.audio_review_threshold > config.audio_duplicate_threshold:
        raise ValueError(
            "audio_review_threshold must be <= audio_duplicate_threshold"
        )

    for name in (
        "metadata_minimum_rating",
        "audio_duplicate_threshold",
        "audio_review_threshold",
        "chromaprint_match_certainty",
        "embedding_match_threshold",
    ):
        value = getattr(config, name)
        if name == "metadata_minimum_rating":
            if not 0 <= value <= 100:
                raise ValueError(f"{name} must be between 0 and 100")
        elif not 0 <= value <= 1:
            raise ValueError(f"{name} must be between 0 and 1")

    if not 1 <= config.max_audio_match_attempts <= 9:
        raise ValueError("max_audio_match_attempts must be between 1 and 9")

    if config.chromaprint_strategy not in CHROMAPRINT_STRATEGIES:
        raise ValueError(
            "chromaprint_strategy must be one of: "
            + ", ".join(CHROMAPRINT_STRATEGIES)
        )


def parse_chromaprint_strategy(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in CHROMAPRINT_STRATEGIES:
        raise ValueError(
            f"chromaprint_strategy must be one of {CHROMAPRINT_STRATEGIES}, got {value!r}"
        )
    return normalized


def parse_toggle(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in ("on", "true", "1", "yes"):
        return True
    if normalized in ("off", "false", "0", "no"):
        return False
    raise ValueError(f"Expected on/off, got {value!r}")
