# Spotify-Sync — Fable Implementation Packet v1

> **Purpose:** This document is the single source of truth for implementing the playlist-tracking CLI, duplicate detection, blacklist, and Hermes JSON mode on top of the existing `spotify_sync` codebase. **Do not redesign** — implement as specified.

---

## Fable directions (read first)

1. **Implement in phases** (see §12). Complete each phase before moving on.
2. **Reuse existing modules** — do not rewrite matching/download logic unless required:
   - `get_content.py` — Spotify metadata
   - `search_candidates.py` — `run_pipeline()`, candidate heap, scoring
   - `download_audio.py` — yt-dlp audio download
   - `isrc_match.py` — ISRC extraction/matching
   - `audio_similarity.py` — chromaprint + embedding matchers
   - `audio_segments.py` — middle-clip extraction
3. **Wrap, don't fork** — new code should call into existing functions.
4. **Dual output mode:**
   - **Human CLI (default):** readable logs on stdout; interactive prompts when `duplicate_policy=ask`.
   - **Hermes/Agent JSON mode:** `--json` or `SPOTIFY_SYNC_JSON=1` → exactly **one JSON object on stdout**; no interactive prompts; human logs go to **stderr only**.
5. **Minimize scope** — match existing code style (dataclasses, pathlib, minimal abstractions).
6. **Add `platformdirs` to `requirements.txt`** for user config path resolution.
7. **Entry point:** `cli.py` with `if __name__ == "__main__"`; optionally add `pyproject.toml` console script `spotify-sync` later.
8. **Tests:** add only if they cover real behavior (sync skip paths, blacklist, JSON envelope). Not required for every helper.
9. **Do not commit secrets** — `.env`, cookies files stay out of git.

---

## Locked product decisions

| Decision | Value |
|----------|-------|
| DB location | User config dir: `%APPDATA%\spotify_sync\state.db` (use `platformdirs`) |
| Duplicate scope | **Per target library/directory only** — not global across all libraries |
| Early duplicate check | **ISRC + metadata first** before API-heavy matching/download |
| Default duplicate config | `check_isrc=true`, `check_metadata=true`, `check_audio=false`, `duplicate_policy=skip` |
| Hermes duplicate prompt | JSON `needs_user_choice` (no interactive prompt) |
| Human duplicate prompt | Interactive prompt when `duplicate_policy=ask` |
| Default download path | Selected library unless `--dest` provided (single-track commands) |
| YouTube cookies | **Global** single cookies file via `set-cookies` |
| YouTube playlist ID | URL `list=` parameter |
| Playlist folders | `<library>/Playlists/<safe-name> [<playlist-id>]/` |
| All Songs folder | `<library>/All Songs/` — symlink → hardlink → **copy fallback** |
| Blacklist scope | Global by default; optional `--playlist-id` for playlist-scoped |
| Candidate selection | If chromaprint **and** embedding disabled → download **heap top**; else pick **best audio similarity**; fallback to heap top if no audio match |
| Unlimited playlists | SQLite-backed; incremental diff per sync |

---

## Existing codebase (do not delete)

```
spotify_sync/
  get_content.py          # get_track_info(), get_spotify_preview_url()
  search_candidates.py    # run_pipeline(), score_candidate(), heap logic
  download_audio.py       # download_audio(), build_audio_filename()
  isrc_match.py           # ISRC normalize/extract/match
  audio_similarity.py     # chromaprint + embedding matchers
  audio_segments.py       # middle segment extraction (ffmpeg)
  requirements.txt
```

Key existing entry: `run_pipeline(spotify_link, *, threshold, save_directory, max_candidates, weights) -> PipelineResult`

---

## New files to create

```
spotify_sync/
  cli.py
  app_paths.py
  db.py
  models.py
  settings.py
  libraries.py
  playlists.py
  tracks.py
  blacklist.py
  duplicates.py
  downloader.py
  linker.py
  sync.py
  output.py
  sources/
    __init__.py
    spotify_source.py
    youtube_source.py
```

---

## Phase 1 — App paths & database

### `app_paths.py`

```python
def get_app_home() -> Path:
    """Return %APPDATA%/spotify_sync (via platformdirs). Create if missing."""

def get_db_path() -> Path:
    """Return get_app_home() / 'state.db'."""

def ensure_app_home() -> Path: ...
```

### `db.py`

```python
def connect() -> sqlite3.Connection: ...
def init_db() -> None: ...
def migrate(conn: sqlite3.Connection) -> None: ...
```

### Schema (SQLite)

```sql
CREATE TABLE libraries (
  id INTEGER PRIMARY KEY,
  name TEXT,
  path TEXT NOT NULL UNIQUE
);

CREATE TABLE playlists (
  id INTEGER PRIMARY KEY,
  library_id INTEGER NOT NULL REFERENCES libraries(id),
  source TEXT NOT NULL CHECK(source IN ('spotify', 'youtube')),
  external_id TEXT NOT NULL,
  name TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  duplicate_policy TEXT NOT NULL DEFAULT 'skip',
  check_isrc INTEGER NOT NULL DEFAULT 1,
  check_metadata INTEGER NOT NULL DEFAULT 1,
  check_audio INTEGER NOT NULL DEFAULT 0,
  metadata_threshold REAL NOT NULL DEFAULT 90.0,
  audio_duplicate_threshold REAL NOT NULL DEFAULT 0.95,
  audio_review_threshold REAL NOT NULL DEFAULT 0.85,
  UNIQUE(library_id, source, external_id)
);

CREATE TABLE tracks (
  id INTEGER PRIMARY KEY,
  spotify_track_id TEXT,
  youtube_video_id TEXT,
  isrc TEXT,
  title_norm TEXT,
  artist_norm TEXT,
  duration_seconds INTEGER,
  fingerprint BLOB,
  UNIQUE(spotify_track_id),
  UNIQUE(youtube_video_id)
);

CREATE TABLE library_tracks (
  id INTEGER PRIMARY KEY,
  library_id INTEGER NOT NULL REFERENCES libraries(id),
  track_id INTEGER NOT NULL REFERENCES tracks(id),
  local_path TEXT NOT NULL,
  UNIQUE(library_id, track_id),
  UNIQUE(library_id, local_path)
);

CREATE TABLE playlist_items (
  id INTEGER PRIMARY KEY,
  playlist_id INTEGER NOT NULL REFERENCES playlists(id),
  track_id INTEGER NOT NULL REFERENCES tracks(id),
  added_at TEXT NOT NULL,
  removed_at TEXT,
  UNIQUE(playlist_id, track_id)
);

CREATE TABLE blacklist (
  id INTEGER PRIMARY KEY,
  track_id INTEGER NOT NULL REFERENCES tracks(id),
  playlist_id INTEGER REFERENCES playlists(id),  -- NULL = global
  reason TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(track_id, playlist_id)
);

CREATE TABLE settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- Indexes
CREATE INDEX idx_tracks_isrc ON tracks(isrc);
CREATE INDEX idx_library_tracks_library ON library_tracks(library_id);
CREATE INDEX idx_blacklist_track ON blacklist(track_id);
CREATE INDEX idx_playlist_items_playlist ON playlist_items(playlist_id);
```

Settings keys:
- `selected_library_id`
- `cookies_file`

---

## Phase 2 — Models & settings

### `models.py`

```python
DuplicatePolicy = Literal["skip", "ask", "replace", "keep_both"]

@dataclass
class DuplicateConfig:
    check_isrc: bool = True
    check_metadata: bool = True
    check_audio: bool = False
    metadata_threshold: float = 90.0
    audio_duplicate_threshold: float = 0.95
    audio_review_threshold: float = 0.85
    duplicate_policy: DuplicatePolicy = "skip"

@dataclass
class TrackIdentity:
    spotify_track_id: str | None
    youtube_video_id: str | None
    isrc: str | None
    title: str
    artist: str
    duration_seconds: int

@dataclass
class DuplicateResult:
    existing_track_id: int
    existing_local_path: str
    method: Literal["isrc", "metadata", "audio"]
    confidence: Literal["exact", "high", "review"]
    score: float | None

ProcessStatus = Literal[
    "downloaded", "skipped_duplicate", "skipped_blacklisted",
    "needs_user_choice", "failed", "already_present"
]

@dataclass
class ProcessResult:
    status: ProcessStatus
    track_id: int | None
    track: TrackIdentity | None
    local_path: str | None
    all_songs_link_path: str | None = None
    all_songs_link_method: Literal["symlink", "hardlink", "copy"] | None = None
    duplicate: DuplicateResult | None = None
    message: str | None = None
```

### `settings.py`

```python
def get_setting(key: str) -> str | None: ...
def set_setting(key: str, value: str) -> None: ...
def get_selected_library_id() -> int | None: ...
def set_selected_library_id(library_id: int) -> None: ...
def get_cookies_file() -> str | None: ...
def set_cookies_file(path: str) -> None: ...
def is_json_mode(argv: list[str] | None = None) -> bool:
    """True if --json in argv or SPOTIFY_SYNC_JSON=1."""
```

---

## Phase 3 — Libraries, tracks, linker

### `libraries.py`

```python
def get_or_create_library(path: str, name: str | None = None) -> int: ...
def resolve_library(*, dest: str | None, library_name: str | None) -> int:
    """dest > library_name > selected_library_id. Raise if none."""

def get_library_path(library_id: int) -> Path: ...
def playlist_dir(library_id: int, playlist_name: str, external_id: str) -> Path:
    """<library>/Playlists/<safe-name> [<external_id>]/"""

def all_songs_dir(library_id: int) -> Path:
    """<library>/All Songs/"""

def sanitize_dir_name(name: str) -> str: ...
```

### `tracks.py`

```python
def normalize_title(value: str) -> str: ...
def normalize_artist(value: str) -> str: ...
def get_or_create_track(identity: TrackIdentity) -> int: ...
def get_track_identity(track_id: int) -> TrackIdentity: ...
def link_track_to_library(track_id: int, library_id: int, local_path: str) -> None: ...
def link_track_to_playlist(track_id: int, playlist_id: int) -> None: ...
def get_library_track_path(library_id: int, track_id: int) -> str | None: ...
```

### `linker.py`

```python
def link_into_all_songs(
    real_file: Path,
    all_songs_dir: Path,
) -> tuple[Path, Literal["symlink", "hardlink", "copy"]]:
    """
    Create entry in All Songs folder pointing to real_file.
    Order: symlink -> hardlink -> copy (copy is fallback).
    Return (link_path, method_used).
    """
```

---

## Phase 4 — Blacklist

### `blacklist.py`

```python
def blacklist_track(
    track_id: int,
    *,
    playlist_id: int | None = None,
    reason: str | None = None,
) -> tuple[int, bool]:
    """Return (blacklist_id, created). created=False if already exists."""

def is_blacklisted(track_id: int, playlist_id: int | None) -> bool:
    """
    True if global blacklist (playlist_id IS NULL) OR
    playlist-specific blacklist for this playlist_id.
    """

def list_blacklisted(*, playlist_id: int | None = None) -> list[dict]: ...
```

**Sync gate:** blacklist check runs **before** duplicate check and **before** any expensive pipeline work.

---

## Phase 5 — Duplicate detection (directory-scoped)

### `duplicates.py`

```python
def find_duplicate_in_library(
    library_id: int,
    identity: TrackIdentity,
    config: DuplicateConfig,
) -> DuplicateResult | None:
    """
    Check only tracks in library_id (via library_tracks join).
    Order (cheap first):
      1. ISRC (if config.check_isrc and identity.isrc)
      2. Metadata similarity (if config.check_metadata)
         - prefilter by duration ±5 seconds
         - reuse score_candidate-style logic or simplified title/artist/duration score
      3. Audio (if config.check_audio) — chromaprint fingerprint compare only
    Return None if no duplicate.
    """

def apply_duplicate_policy(
    result: DuplicateResult,
    policy: DuplicatePolicy,
    *,
    json_mode: bool,
) -> Literal["skip", "proceed", "needs_user_choice"]:
    """
    skip: default
    ask + json_mode: needs_user_choice
    ask + human: prompt via output.prompt_duplicate_choice()
    replace / keep_both: proceed with flag
    """
```

**Important:** duplicate means "already exists in **this target library**", not globally.

---

## Phase 6 — Source adapters

### `sources/spotify_source.py`

Reuse `get_content.get_track_info`, `parse_spotify_track_id`.

```python
def parse_track_id(url: str) -> str: ...
def parse_playlist_id(url: str) -> str: ...
def fetch_track_identity(track_id_or_url: str) -> TrackIdentity: ...
def fetch_playlist_metadata(playlist_id_or_url: str) -> dict: ...
def iter_playlist_track_identities(playlist_id_or_url: str) -> Iterator[TrackIdentity]: ...
def get_playlist_track_by_index(playlist_id_or_url: str, index: int) -> TrackIdentity:
    """0-based index."""
```

### `sources/youtube_source.py`

```python
def parse_video_id(url: str) -> str: ...
def parse_playlist_list_id(url: str) -> str: ...
def ydl_base_opts() -> dict:
    """Include cookiefile from settings if configured."""

def fetch_video_identity(url: str) -> TrackIdentity: ...
def fetch_playlist_metadata(url: str) -> dict: ...
def iter_playlist_video_identities(url: str) -> Iterator[TrackIdentity]: ...
def get_playlist_video_by_index(url: str, index: int) -> TrackIdentity: ...
```

Use `isrc_match.extract_isrc_for_video` when possible for YouTube tracks.

---

## Phase 7 — Downloader (wrap existing pipeline)

### `downloader.py`

```python
def download_spotify_track(
    identity: TrackIdentity,
    *,
    save_directory: Path,
    spotify_url: str,
    enable_chromaprint: bool = False,
    enable_embedding: bool = False,
) -> Path:
    """
    Call run_pipeline with save_directory.
    Candidate selection rule (modify run_pipeline or post-process):
      - If both chromaprint and embedding disabled:
          download top heap candidate if present
      - Else:
          use resolve_by_audio_similarity for best match
          fallback to heap top if no audio match qualifies
    Return local file path.
    """

def download_youtube_track(
    identity: TrackIdentity,
    *,
    save_directory: Path,
    youtube_url: str,
) -> Path:
    """Direct download via download_audio() using video URL."""
```

### Changes to `search_candidates.py` (minimal)

- Expose parameters for `enable_chromaprint` / `enable_embedding` instead of only module constants.
- When both disabled and heap has candidates but no ISRC hit: download `sorted_candidates[0]`.
- Do not change scoring weights unless necessary.

---

## Phase 8 — Sync orchestration

### `playlists.py`

```python
def add_playlist(
    *,
    source: Literal["spotify", "youtube"],
    external_id: str,
    library_id: int,
    name: str | None,
    config: DuplicateConfig | None = None,
) -> int: ...

def get_playlist(playlist_id: int) -> dict: ...
def list_playlists() -> list[dict]: ...
def playlist_duplicate_config(playlist_id: int) -> DuplicateConfig: ...
```

### `sync.py`

```python
def process_track_for_playlist(
    *,
    playlist_id: int,
    identity: TrackIdentity,
    save_directory: Path,
    library_id: int,
    config: DuplicateConfig,
    json_mode: bool,
    source_url: str | None = None,
) -> ProcessResult:
    """
    1. track_id = get_or_create_track(identity)
    2. if is_blacklisted(track_id, playlist_id) -> skipped_blacklisted
    3. dup = find_duplicate_in_library(library_id, identity, config)
    4. apply duplicate policy
    5. download (Spotify via run_pipeline, YouTube via direct download)
    6. link_track_to_library, link_track_to_playlist
    7. link_into_all_songs
    8. return ProcessResult
    """

def sync_playlist(playlist_id: int, *, json_mode: bool = False) -> list[ProcessResult]: ...
def sync_all(*, json_mode: bool = False) -> dict[int, list[ProcessResult]]: ...
```

**Sync diff logic:**
- Enumerate current playlist items from source API/yt-dlp.
- Upsert `playlist_items` (set `removed_at` for items no longer in playlist).
- Process items that are new to this playlist **and** not yet in `library_tracks` for this library.

---

## Phase 9 — Output & CLI

### `output.py`

```python
JSON_VERSION = "1"

def emit_success(command: str, data: dict) -> None: ...
def emit_error(command: str, code: str, message: str) -> None: ...
def print_human(message: str) -> None: ...
def prompt_duplicate_choice() -> DuplicatePolicy: ...
```

### `cli.py` — subcommands

| Command | Args |
|---------|------|
| `set-download-path` | `--path DIR [--name NAME]` |
| `select-download-path` | `--path DIR` \| `--name NAME` |
| `set-cookies` | `--cookies-file PATH` |
| `add-playlist` | `--spotify-playlist-url URL` \| `--youtube-playlist-url URL` `[--dest DIR\|--library NAME]` |
| `add-track` | `--spotify-track-url URL` \| `--youtube-url URL` `[--from-playlist ID] [--dest DIR]` |
| `add-track` | `--spotify-playlist-url URL --index N` \| `--youtube-playlist-url URL --index N` `[--dest DIR]` |
| `sync` | `--playlist-id ID` \| `--all` |
| `blacklist-song` | same URL modes as add-track `[--playlist-id ID] [--reason TEXT]` |
| `list-blacklisted` | `[--playlist-id ID]` |
| `resolve-duplicate` | `--request-id ID --action skip\|replace\|keep_both` |

Global flags: `--json`

**Default path rule:** single-track commands use `selected_library_id` unless `--dest` is provided.

---

## JSON output contract (Hermes mode)

### Global envelope

**Success:**
```json
{
  "ok": true,
  "command": "sync",
  "version": "1",
  "timestamp": "2026-07-06T15:30:00Z",
  "data": {}
}
```

**Error:**
```json
{
  "ok": false,
  "command": "sync",
  "version": "1",
  "timestamp": "2026-07-06T15:30:00Z",
  "error": {
    "code": "PLAYLIST_NOT_FOUND",
    "message": "Playlist id 42 does not exist."
  }
}
```

### Shared types

**track**
```json
{
  "track_id": 101,
  "spotify_track_id": "3n3Ppam7vgaVa1iaRUc9Lp",
  "youtube_video_id": null,
  "isrc": "USUM71703861",
  "title": "Mr. Brightside",
  "artist": "The Killers",
  "duration_seconds": 222
}
```

**duplicate**
```json
{
  "method": "isrc",
  "confidence": "exact",
  "score": null,
  "existing_track_id": 88,
  "existing_local_path": "D:\\Music\\...\\song.m4a"
}
```

**process_result**
```json
{
  "status": "downloaded",
  "track": { },
  "local_path": "D:\\...\\song.m4a",
  "all_songs_link_path": "D:\\...\\All Songs\\song.m4a",
  "all_songs_link_method": "symlink",
  "duplicate": null,
  "message": null
}
```

**status values:** `downloaded`, `skipped_duplicate`, `skipped_blacklisted`, `already_present`, `needs_user_choice`, `failed`

### Command-specific `data` shapes

#### `set-download-path`
```json
{ "library": { "library_id": 1, "name": "Main", "path": "D:\\Music\\SpotifySync" }, "created": true }
```

#### `select-download-path`
```json
{ "selected_library": { "library_id": 1, "name": "Main", "path": "D:\\Music\\SpotifySync" } }
```

#### `set-cookies`
```json
{ "cookies_file": "C:\\Users\\...\\cookies.txt", "scope": "global" }
```

#### `add-playlist`
```json
{
  "playlist": { "playlist_id": 7, "source": "spotify", "external_id": "...", "name": "...", "library_id": 1, "enabled": true, "config": { } },
  "playlist_directory": "D:\\...\\Playlists\\Name [id]",
  "created": true
}
```

#### `add-track` / `sync` (per result)
Include `result` (single) or `results` + `summary` (batch).

**summary**
```json
{
  "total_items_seen": 120,
  "new_items_processed": 4,
  "downloaded": 2,
  "skipped_duplicate": 1,
  "skipped_blacklisted": 1,
  "needs_user_choice": 0,
  "failed": 0,
  "already_present": 0
}
```

#### `blacklist-song`
```json
{
  "blacklist_entry": {
    "blacklist_id": 15,
    "track": { },
    "scope": "global",
    "playlist_id": null,
    "reason": "User dislikes this version",
    "created_at": "2026-07-06T15:30:00Z"
  },
  "created": true
}
```

`scope`: `"global"` when `playlist_id` is null, else `"playlist"`.

#### `list-blacklisted`
```json
{ "count": 2, "entries": [ { "blacklist_id": 15, "track": { }, "scope": "global", "playlist_id": null, "playlist_name": null, "reason": "...", "created_at": "..." } ] }
```

#### `needs_user_choice` (when `duplicate_policy=ask` in JSON mode)
```json
{
  "result": {
    "status": "needs_user_choice",
    "track": { },
    "duplicate": { },
    "message": "Potential duplicate in target library."
  },
  "decision_request": {
    "request_id": "dup-20260706-153000-201",
    "allowed_actions": ["skip", "replace", "keep_both"],
    "recommended_action": "skip",
    "context": { "library_id": 1, "playlist_id": 7 }
  }
}
```

Store pending `decision_request` in memory or a `pending_decisions` table for `resolve-duplicate`.

### Error codes

| Code | When |
|------|------|
| `INVALID_ARGUMENT` | Bad CLI args |
| `LIBRARY_NOT_FOUND` | Unknown library |
| `PLAYLIST_NOT_FOUND` | Unknown playlist |
| `TRACK_NOT_FOUND` | Track resolution failed |
| `PLAYLIST_INDEX_OUT_OF_RANGE` | Bad --index |
| `COOKIES_FILE_NOT_FOUND` | set-cookies path invalid |
| `SPOTIFY_AUTH_MISSING` | Missing SPOTIPY_* env vars |
| `YOUTUBE_EXTRACT_FAILED` | yt-dlp error |
| `DOWNLOAD_FAILED` | Download pipeline failed |
| `DUPLICATE_REQUEST_NOT_FOUND` | Bad resolve-duplicate request_id |

### Exit codes

- `0` — success (including `needs_user_choice` in JSON mode)
- `2` — validation error
- `3` — external/download failure

---

## Processing flow (reference diagram)

```
Playlist sync / add-track
        │
        ▼
  Resolve track identity (minimal metadata)
        │
        ▼
  Blacklisted? ──yes──► skipped_blacklisted
        │ no
        ▼
  Duplicate in target library? (ISRC → metadata → audio)
        │
        ├─ yes + policy=skip ──► skipped_duplicate
        ├─ yes + policy=ask + json ──► needs_user_choice
        ├─ yes + policy=ask + human ──► prompt
        └─ no / proceed
                │
                ▼
        Download (run_pipeline or direct)
                │
                ▼
        Save library_tracks + playlist_items
                │
                ▼
        Link into All Songs (symlink→hardlink→copy)
                │
                ▼
           downloaded
```

---

## Implementation order (phases)

| Phase | Modules | Done when |
|-------|---------|-----------|
| 1 | `app_paths`, `db`, `settings` | DB creates, settings read/write |
| 2 | `models`, `libraries`, `tracks` | Library CRUD, track identity |
| 3 | `blacklist`, `duplicates` | Skip paths work |
| 4 | `sources/*`, `linker` | Spotify/YouTube metadata + linking |
| 5 | `downloader` + `search_candidates` tweak | Single track downloads |
| 6 | `playlists`, `sync` | Playlist add + sync diff |
| 7 | `output`, `cli` | All commands + JSON mode |
| 8 | `resolve-duplicate`, smoke tests | End-to-end Hermes flow |

---

## Smoke test checklist

- [ ] `set-download-path` + `select-download-path` persist correctly
- [ ] `set-cookies` injects into yt-dlp opts
- [ ] `add-playlist` (Spotify + YouTube) creates playlist row + directory
- [ ] `add-track` with `--dest` overrides selected library
- [ ] `add-track` without `--dest` uses selected library
- [ ] `add-track --spotify-playlist-url URL --index 0` works
- [ ] `sync --playlist-id` downloads only new items
- [ ] Duplicate in same library → `skipped_duplicate`
- [ ] Same song in different library → allowed
- [ ] Blacklisted track → `skipped_blacklisted` (no download)
- [ ] `list-blacklisted` returns correct entries
- [ ] All Songs link falls back to copy when symlink/hardlink fail
- [ ] `--json` emits single JSON object, no prompts
- [ ] Human mode prompts on `duplicate_policy=ask`
- [ ] Chromaprint+embedding disabled → downloads heap top candidate

---

## Environment variables

| Variable | Required for |
|----------|--------------|
| `SPOTIPY_CLIENT_ID` | Spotify API |
| `SPOTIPY_CLIENT_SECRET` | Spotify API |
| `ACOUSTID_API_KEY` | Chromaprint DB lookup (optional if direct fingerprint only) |
| `SPOTIFY_SYNC_JSON` | Force JSON mode |

External tools: `ffmpeg` on PATH; `fpcalc` optional for chromaprint.

---

## Out of scope for v1

- Web UI
- Global cross-library duplicate blocking
- Automatic Spotify OAuth user playlist access beyond Client Credentials (unless already supported)
- Embedding-based duplicate detection (audio duplicate check uses chromaprint only when enabled)
- `unblacklist-song` command (add later if needed)

---

## Copy-paste prompt for new Fable window

```
Implement spotify_sync CLI per FABLE_IMPLEMENTATION_PACKET.md in this repo.
Follow phases 1–8 in order. Reuse existing modules (get_content, search_candidates,
download_audio, isrc_match, audio_similarity, audio_segments). Do not redesign.
Implement --json Hermes mode and human CLI mode. Run smoke tests from checklist.
```
