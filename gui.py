"""One-page desktop GUI for spotify_sync."""

from __future__ import annotations

import io
import queue
import signal
import sys
import threading
import traceback
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Optional

import db
import libraries
import matching_settings as matching_settings_mod
import output
import playlists as playlists_mod
import settings
from cli import (
    _map_exception,
    cmd_add_playlist,
    cmd_add_track,
    cmd_blacklist_song,
    cmd_delete_download_path,
    cmd_list_blacklisted,
    cmd_login,
    cmd_remove_playlist,
    cmd_resolve_duplicate,
    cmd_unset_playlist,
    cmd_select_download_path,
    cmd_set_cookies,
    cmd_set_download_path,
    cmd_show_settings,
    cmd_sync,
)

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, ttk
except ImportError:  # pragma: no cover
    raise SystemExit("tkinter is required for the GUI (included with Python on Windows).")


# ---------------------------------------------------------------------------
# DPI / fonts (reduces blur on high-DPI Windows displays)


def _enable_high_dpi() -> float:
    """Return approximate scaling factor (1.0 = 96 DPI)."""
    scale = 1.0
    if sys.platform == "win32":
        try:
            import ctypes

            # Per-monitor DPI aware v2
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                import ctypes

                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
        try:
            import ctypes

            hdc = ctypes.windll.user32.GetDC(0)
            dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)  # LOGPIXELSX
            ctypes.windll.user32.ReleaseDC(0, hdc)
            scale = max(1.0, dpi / 96.0)
        except Exception:
            scale = 1.0
    return scale


UI_FONT = ("Segoe UI", 10)
UI_FONT_BOLD = ("Segoe UI", 10, "bold")
LOG_FONT = ("Consolas", 11)
ENTRY_WIDTH = 56


def _ns(**kwargs) -> SimpleNamespace:
    return SimpleNamespace(**kwargs)


class ToggleButton(ttk.Frame):
    """Clickable ON/OFF toggle styled as a push button."""

    def __init__(
        self,
        parent: ttk.Frame,
        label: str,
        variable: tk.BooleanVar,
        width: int = 24,
    ) -> None:
        super().__init__(parent)
        self._label = label
        self.variable = variable
        self.btn = tk.Button(
            self,
            width=width,
            font=UI_FONT,
            command=self._toggle,
            cursor="hand2",
            bd=2,
        )
        self.btn.pack(fill=tk.X)
        self.variable.trace_add("write", self._sync)
        self._sync()

    def _toggle(self) -> None:
        self.variable.set(not self.variable.get())

    def _sync(self, *_args) -> None:
        on = self.variable.get()
        state_text = f"{self._label}: ON" if on else f"{self._label}: OFF"
        self.btn.configure(
            text=state_text,
            relief=tk.SUNKEN if on else tk.RAISED,
            bg="#2e7d32" if on else "#ececec",
            fg="white" if on else "#222222",
            activebackground="#1b5e20" if on else "#d5d5d5",
            activeforeground="white" if on else "#222222",
        )


class GuiApp:
    def __init__(self, root: tk.Tk, scale: float) -> None:
        self.root = root
        self._scale = scale
        self.root.title("spotify_sync")
        self.root.minsize(int(980 * scale), int(760 * scale))
        self.root.geometry(f"{int(1100 * scale)}x{int(900 * scale)}")

        self._log_queue: queue.Queue[str] = queue.Queue()
        self._busy = False
        self._library_map: dict[str, dict] = {}

        output.JSON_MODE = False
        db.init_db()

        self._configure_styles()
        self._build_ui()
        self._refresh_libraries()
        self._refresh_playlists()
        self._refresh_pending()
        self._load_matching_settings_into_form()
        self._poll_log_queue()

    def _configure_styles(self) -> None:
        try:
            self.root.tk.call("tk", "scaling", self._scale)
        except tk.TclError:
            pass
        style = ttk.Style()
        try:
            if "vista" in style.theme_names():
                style.theme_use("vista")
        except tk.TclError:
            pass
        for name in ("TLabel", "TButton", "TCheckbutton", "TRadiobutton", "TEntry", "TCombobox"):
            style.configure(name, font=UI_FONT)
        style.configure("TLabelframe.Label", font=UI_FONT_BOLD)
        style.configure("Heading.TLabel", font=UI_FONT_BOLD)

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill=tk.BOTH, expand=True)

        canvas = tk.Canvas(outer, highlightthickness=0, bd=0)
        scrollbar = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=canvas.yview)
        self._scroll_body = ttk.Frame(canvas)
        self._scroll_body.bind(
            "<Configure>",
            lambda _e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        self._canvas_window = canvas.create_window(
            (0, 0), window=self._scroll_body, anchor="nw"
        )
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        def _fit_width(event: tk.Event) -> None:
            canvas.itemconfigure(self._canvas_window, width=event.width)

        canvas.bind("<Configure>", _fit_width)
        canvas.bind(
            "<MouseWheel>",
            lambda e: canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"),
        )

        body = self._scroll_body
        body.columnconfigure(0, weight=1)

        self._build_library_section(body, row=0)
        self._build_auth_section(body, row=1)
        self._build_single_song_section(body, row=2)
        self._build_playlist_section(body, row=3)
        self._build_matching_section(body, row=4)
        self._build_blacklist_section(body, row=5)
        self._build_duplicate_section(body, row=6)

        log_frame = ttk.LabelFrame(self.root, text="Log", padding=10)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        self.log_text = scrolledtext.ScrolledText(
            log_frame,
            height=14,
            wrap=tk.WORD,
            state=tk.DISABLED,
            font=LOG_FONT,
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)
        self.log_text.bind(
            "<MouseWheel>",
            lambda e: self.log_text.yview_scroll(int(-1 * (e.delta / 120)), "units"),
        )
        ttk.Button(log_frame, text="Clear log", command=self._clear_log).pack(
            anchor=tk.E, pady=(6, 0)
        )

    def _labeled_entry(
        self,
        parent: ttk.Frame,
        row: int,
        label: str,
        variable: tk.StringVar,
        *,
        colspan: int = 1,
        width: int | None = None,
    ) -> ttk.Entry:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky=tk.W, pady=(4, 0))
        entry = ttk.Entry(parent, textvariable=variable, width=width or ENTRY_WIDTH)
        entry.grid(
            row=row,
            column=1,
            columnspan=colspan,
            sticky="ew",
            padx=(8, 0),
            pady=(4, 0),
        )
        return entry

    def _build_library_section(self, parent: ttk.Frame, row: int) -> None:
        frame = ttk.LabelFrame(parent, text="Library (download folder)", padding=10)
        frame.grid(row=row, column=0, sticky="ew", pady=6)
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="Registered:").grid(row=0, column=0, sticky=tk.W)
        self.library_var = tk.StringVar()
        self.library_combo = ttk.Combobox(
            frame, textvariable=self.library_var, state="readonly", font=UI_FONT
        )
        self.library_combo.grid(row=0, column=1, columnspan=2, sticky="ew", padx=(8, 0))
        self.library_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_library_pick())

        self.library_path_var = tk.StringVar()
        self._labeled_entry(frame, 1, "Path:", self.library_path_var, colspan=1)
        ttk.Button(frame, text="Browse…", command=self._browse_library_path).grid(
            row=1, column=2, padx=(8, 0), pady=(4, 0)
        )

        self.library_name_var = tk.StringVar()
        self._labeled_entry(frame, 2, "Name:", self.library_name_var, width=28)

        btn_row = ttk.Frame(frame)
        btn_row.grid(row=3, column=0, columnspan=3, sticky=tk.W, pady=(10, 0))
        ttk.Button(btn_row, text="Register path", command=self._register_library).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(btn_row, text="Select", command=self._select_library).pack(
            side=tk.LEFT, padx=6
        )
        ttk.Button(btn_row, text="Delete", command=self._delete_library).pack(
            side=tk.LEFT, padx=6
        )
        self.selected_library_label = ttk.Label(frame, text="Selected: (none)")
        self.selected_library_label.grid(row=4, column=0, columnspan=3, sticky=tk.W, pady=(8, 0))

    def _build_auth_section(self, parent: ttk.Frame, row: int) -> None:
        frame = ttk.LabelFrame(parent, text="Authentication", padding=10)
        frame.grid(row=row, column=0, sticky="ew", pady=6)
        frame.columnconfigure(1, weight=1)

        ttk.Button(frame, text="Spotify login", command=self._spotify_login).grid(
            row=0, column=0, sticky=tk.W
        )
        self.spotify_status = ttk.Label(frame, text="Spotify: unknown")
        self.spotify_status.grid(row=0, column=1, sticky=tk.W, padx=12)

        self.cookies_var = tk.StringVar(value=settings.get_cookies_file() or "")
        self._labeled_entry(frame, 1, "YouTube cookies:", self.cookies_var)
        ttk.Button(frame, text="Browse…", command=self._browse_cookies).grid(
            row=1, column=2, padx=(8, 0), pady=(4, 0)
        )
        ttk.Button(frame, text="Set cookies", command=self._set_cookies).grid(
            row=2, column=1, sticky=tk.W, padx=(8, 0), pady=(6, 0)
        )

    def _build_single_song_section(self, parent: ttk.Frame, row: int) -> None:
        frame = ttk.LabelFrame(parent, text="Add single song", padding=10)
        frame.grid(row=row, column=0, sticky="ew", pady=6)
        frame.columnconfigure(1, weight=1)

        ttk.Label(
            frame,
            text="Download one track into the selected library (or override folder below).",
            style="Heading.TLabel",
        ).grid(row=0, column=0, columnspan=3, sticky=tk.W)

        self.song_mode_var = tk.StringVar(value="spotify")
        mode_row = ttk.Frame(frame)
        mode_row.grid(row=1, column=0, columnspan=3, sticky=tk.W, pady=(8, 4))
        for text, value in (
            ("Spotify track URL", "spotify"),
            ("YouTube URL", "youtube"),
            ("Pick from playlist", "playlist"),
        ):
            ttk.Radiobutton(
                mode_row, text=text, variable=self.song_mode_var, value=value
            ).pack(side=tk.LEFT, padx=(0, 16))

        self.spotify_track_var = tk.StringVar()
        self.youtube_track_var = tk.StringVar()
        self.track_playlist_var = tk.StringVar()
        self.track_index_var = tk.StringVar(value="0")
        self.song_dest_var = tk.StringVar()

        self._labeled_entry(frame, 2, "Spotify track URL:", self.spotify_track_var)
        self._labeled_entry(frame, 3, "YouTube URL:", self.youtube_track_var)

        ttk.Label(frame, text="Playlist URL:").grid(row=4, column=0, sticky=tk.W, pady=(4, 0))
        pl_row = ttk.Frame(frame)
        pl_row.grid(row=4, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=(4, 0))
        pl_row.columnconfigure(0, weight=1)
        ttk.Entry(pl_row, textvariable=self.track_playlist_var).grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Label(pl_row, text="Index:").grid(row=0, column=1, padx=(8, 4))
        ttk.Entry(pl_row, textvariable=self.track_index_var, width=6).grid(row=0, column=2)
        self.track_source_var = tk.StringVar(value="spotify")
        ttk.Label(pl_row, text="Source:").grid(row=0, column=3, padx=(8, 4))
        ttk.Combobox(
            pl_row,
            textvariable=self.track_source_var,
            values=("spotify", "youtube"),
            state="readonly",
            width=10,
        ).grid(row=0, column=4)

        self._labeled_entry(
            frame, 5, "Save to folder (optional):", self.song_dest_var, width=40
        )
        ttk.Button(frame, text="Browse…", command=self._browse_song_dest).grid(
            row=5, column=2, padx=(8, 0), pady=(4, 0)
        )

        ttk.Button(
            frame,
            text="Add song / Download",
            command=self._download_track,
        ).grid(row=6, column=0, columnspan=2, sticky=tk.W, pady=(12, 0))

    def _build_playlist_section(self, parent: ttk.Frame, row: int) -> None:
        frame = ttk.LabelFrame(parent, text="Playlists", padding=10)
        frame.grid(row=row, column=0, sticky="ew", pady=6)
        frame.columnconfigure(1, weight=1)

        self.spotify_playlist_var = tk.StringVar()
        self._labeled_entry(frame, 0, "Spotify playlist URL:", self.spotify_playlist_var)
        self.youtube_playlist_var = tk.StringVar()
        self._labeled_entry(frame, 1, "YouTube playlist URL:", self.youtube_playlist_var)

        btn_top = ttk.Frame(frame)
        btn_top.grid(row=2, column=0, columnspan=3, sticky=tk.W, pady=(8, 0))
        ttk.Button(btn_top, text="Add playlist", command=self._add_playlist).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(btn_top, text="Refresh list", command=self._refresh_playlists).pack(
            side=tk.LEFT, padx=6
        )

        list_frame = ttk.Frame(frame)
        list_frame.grid(row=3, column=0, columnspan=3, sticky="nsew", pady=(10, 0))
        list_frame.columnconfigure(0, weight=1)

        self.playlist_listbox = tk.Listbox(
            list_frame,
            height=6,
            selectmode=tk.EXTENDED,
            exportselection=False,
            font=UI_FONT,
        )
        self.playlist_listbox.grid(row=0, column=0, sticky="nsew")
        pl_scroll = ttk.Scrollbar(list_frame, command=self.playlist_listbox.yview)
        pl_scroll.grid(row=0, column=1, sticky="ns")
        self.playlist_listbox.configure(yscrollcommand=pl_scroll.set)

        btn_row = ttk.Frame(frame)
        btn_row.grid(row=4, column=0, columnspan=3, sticky=tk.W, pady=(8, 0))
        ttk.Button(btn_row, text="Sync selected", command=self._sync_selected).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(btn_row, text="Sync all enabled", command=self._sync_all).pack(
            side=tk.LEFT, padx=6
        )
        ttk.Button(btn_row, text="Unset tracked", command=self._unset_playlist).pack(
            side=tk.LEFT, padx=6
        )
        ttk.Button(btn_row, text="Remove selected", command=self._remove_playlist).pack(
            side=tk.LEFT, padx=6
        )

        self._playlist_rows: list[dict] = []

    def _build_matching_section(self, parent: ttk.Frame, row: int) -> None:
        frame = ttk.LabelFrame(
            parent, text="Matching, metadata criteria & thresholds", padding=10
        )
        frame.grid(row=row, column=0, sticky="ew", pady=6)
        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)

        # --- Duplicate check toggles ---
        dup_frame = ttk.LabelFrame(
            frame,
            text="Duplicate check (scan existing files in folder)",
            padding=8,
        )
        dup_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.dup_chroma_var = tk.BooleanVar()
        self.dup_embed_var = tk.BooleanVar()
        ToggleButton(
            dup_frame, "Chromaprint", self.dup_chroma_var
        ).pack(fill=tk.X, pady=4)
        ToggleButton(
            dup_frame, "Vector embedding", self.dup_embed_var
        ).pack(fill=tk.X, pady=4)

        self.threshold_vars: dict[str, tk.StringVar] = {}
        ttk.Label(dup_frame, text="Audio duplicate threshold (0–1):").pack(
            anchor=tk.W, pady=(8, 0)
        )
        self.threshold_vars["audio_duplicate_threshold"] = tk.StringVar()
        ttk.Entry(
            dup_frame, textvariable=self.threshold_vars["audio_duplicate_threshold"], width=12
        ).pack(anchor=tk.W, pady=2)
        ttk.Label(dup_frame, text="Audio review threshold (0–1):").pack(anchor=tk.W, pady=(6, 0))
        self.threshold_vars["audio_review_threshold"] = tk.StringVar()
        ttk.Entry(
            dup_frame, textvariable=self.threshold_vars["audio_review_threshold"], width=12
        ).pack(anchor=tk.W, pady=2)

        # --- Source comparison toggles ---
        cmp_frame = ttk.LabelFrame(
            frame,
            text="Source comparison (Spotify → YouTube matching)",
            padding=8,
        )
        cmp_frame.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        self.cmp_chroma_var = tk.BooleanVar()
        self.cmp_embed_var = tk.BooleanVar()
        ToggleButton(
            cmp_frame, "Chromaprint", self.cmp_chroma_var
        ).pack(fill=tk.X, pady=4)
        ToggleButton(
            cmp_frame, "Vector embedding", self.cmp_embed_var
        ).pack(fill=tk.X, pady=4)

        ttk.Label(cmp_frame, text="Metadata minimum rating (0–100):").pack(
            anchor=tk.W, pady=(8, 0)
        )
        self.threshold_vars["metadata_minimum_rating"] = tk.StringVar()
        ttk.Entry(
            cmp_frame, textvariable=self.threshold_vars["metadata_minimum_rating"], width=12
        ).pack(anchor=tk.W, pady=2)
        ttk.Label(cmp_frame, text="Chromaprint match certainty (0–1):").pack(
            anchor=tk.W, pady=(6, 0)
        )
        self.threshold_vars["chromaprint_match_certainty"] = tk.StringVar()
        ttk.Entry(
            cmp_frame, textvariable=self.threshold_vars["chromaprint_match_certainty"], width=12
        ).pack(anchor=tk.W, pady=2)
        ttk.Label(cmp_frame, text="Embedding match threshold (0–1):").pack(
            anchor=tk.W, pady=(6, 0)
        )
        self.threshold_vars["embedding_match_threshold"] = tk.StringVar()
        ttk.Entry(
            cmp_frame, textvariable=self.threshold_vars["embedding_match_threshold"], width=12
        ).pack(anchor=tk.W, pady=2)

        # --- Metadata scoring weights ---
        weights_frame = ttk.LabelFrame(
            frame, text="Metadata scoring weights (must sum to 100)", padding=8
        )
        weights_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self.weight_vars: dict[str, tk.StringVar] = {}
        weight_fields = [
            ("Artist match %", "weight_artist"),
            ("Title match %", "weight_title"),
            ("Duration similarity %", "weight_duration"),
            ("Official channel %", "weight_official_channel"),
            ("Album similarity %", "weight_album"),
            ("Release year %", "weight_release_year"),
        ]
        for i, (label, key) in enumerate(weight_fields):
            r, c = divmod(i, 3)
            cell = ttk.Frame(weights_frame)
            cell.grid(row=r, column=c, sticky="ew", padx=8, pady=4)
            ttk.Label(cell, text=label).pack(anchor=tk.W)
            var = tk.StringVar()
            self.weight_vars[key] = var
            ttk.Entry(cell, textvariable=var, width=8).pack(anchor=tk.W, pady=2)
        for c in range(3):
            weights_frame.columnconfigure(c, weight=1)

        self.weights_sum_label = ttk.Label(weights_frame, text="Weights sum: —")
        self.weights_sum_label.grid(row=2, column=0, columnspan=3, sticky=tk.W, pady=(6, 0))

        btn_row = ttk.Frame(frame)
        btn_row.grid(row=2, column=0, columnspan=2, sticky=tk.W, pady=(10, 0))
        ttk.Button(btn_row, text="Save settings", command=self._save_matching).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(btn_row, text="Reload settings", command=self._load_matching_settings_into_form).pack(
            side=tk.LEFT, padx=6
        )
        ttk.Button(btn_row, text="Show full settings", command=self._show_settings).pack(
            side=tk.LEFT, padx=6
        )

    def _build_blacklist_section(self, parent: ttk.Frame, row: int) -> None:
        frame = ttk.LabelFrame(parent, text="Blacklist", padding=10)
        frame.grid(row=row, column=0, sticky="ew", pady=6)
        frame.columnconfigure(1, weight=1)

        self.blacklist_url_var = tk.StringVar()
        self._labeled_entry(frame, 0, "Track URL:", self.blacklist_url_var)
        self.blacklist_reason_var = tk.StringVar()
        self._labeled_entry(frame, 1, "Reason:", self.blacklist_reason_var, width=40)

        ttk.Label(frame, text="Scope:").grid(row=2, column=0, sticky=tk.W, pady=(4, 0))
        self.blacklist_playlist_var = tk.StringVar(value="Global")
        self.blacklist_playlist_combo = ttk.Combobox(
            frame,
            textvariable=self.blacklist_playlist_var,
            state="readonly",
            font=UI_FONT,
        )
        self.blacklist_playlist_combo.grid(
            row=2, column=1, sticky="ew", padx=(8, 0), pady=(4, 0)
        )

        btn_row = ttk.Frame(frame)
        btn_row.grid(row=3, column=0, columnspan=3, sticky=tk.W, pady=(10, 0))
        ttk.Button(btn_row, text="Blacklist track", command=self._blacklist_track).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(btn_row, text="List blacklisted", command=self._list_blacklisted).pack(
            side=tk.LEFT, padx=6
        )

    def _build_duplicate_section(self, parent: ttk.Frame, row: int) -> None:
        frame = ttk.LabelFrame(parent, text="Resolve duplicate", padding=10)
        frame.grid(row=row, column=0, sticky="ew", pady=6)
        frame.columnconfigure(1, weight=1)

        self.pending_var = tk.StringVar()
        self._labeled_entry(frame, 0, "Pending request:", self.pending_var)
        ttk.Button(frame, text="Refresh", command=self._refresh_pending).grid(
            row=0, column=2, padx=(8, 0), pady=(4, 0)
        )

        ttk.Label(frame, text="Action:").grid(row=1, column=0, sticky=tk.W, pady=(4, 0))
        self.resolve_action_var = tk.StringVar(value="skip")
        ttk.Combobox(
            frame,
            textvariable=self.resolve_action_var,
            values=("skip", "replace", "keep_both"),
            state="readonly",
            width=16,
            font=UI_FONT,
        ).grid(row=1, column=1, sticky=tk.W, padx=(8, 0), pady=(4, 0))

        ttk.Button(frame, text="Resolve", command=self._resolve_duplicate).grid(
            row=2, column=0, sticky=tk.W, pady=(10, 0)
        )

    # ------------------------------------------------------------------ log

    def _append_log(self, text: str) -> None:
        # Only stick to bottom if the user is already at bottom.
        _first, last = self.log_text.yview()
        should_autoscroll = last >= 0.999
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, text)
        if not text.endswith("\n"):
            self.log_text.insert(tk.END, "\n")
        if should_autoscroll:
            self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _clear_log(self) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _poll_log_queue(self) -> None:
        while True:
            try:
                line = self._log_queue.get_nowait()
            except queue.Empty:
                break
            self._append_log(line)
        self.root.after(100, self._poll_log_queue)

    @contextmanager
    def _capture_output(self):
        class QueueWriter(io.TextIOBase):
            def __init__(self, q: queue.Queue) -> None:
                self._q = q

            def write(self, s: str) -> int:
                if s:
                    self._q.put(s.rstrip("\n") if s.endswith("\n") else s)
                return len(s)

            def flush(self) -> None:
                pass

        old_stdout = sys.stdout
        old_print_human = output.print_human

        def routed_print(message: str) -> None:
            self._log_queue.put(message)

        sys.stdout = QueueWriter(self._log_queue)
        output.print_human = routed_print
        try:
            yield
        finally:
            sys.stdout = old_stdout
            output.print_human = old_print_human

    def _run_task(self, label: str, fn: Callable[[], None], refresh: bool = True) -> None:
        if self._busy:
            messagebox.showwarning("Busy", "Wait for the current operation to finish.")
            return
        self._busy = True
        self._append_log(f"--- {label} ---")

        def worker() -> None:
            try:
                with self._capture_output():
                    fn()
            except Exception as exc:
                err = _map_exception(exc)
                self._log_queue.put(f"ERROR [{err.code}]: {err}")
                self._log_queue.put(traceback.format_exc())
            finally:
                self._log_queue.put(f"--- done: {label} ---")
                self.root.after(0, lambda: self._finish_task(refresh))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_task(self, refresh: bool) -> None:
        self._busy = False
        if refresh:
            self._after_refresh()

    def _after_refresh(self) -> None:
        self._refresh_libraries()
        self._refresh_playlists()
        self._refresh_pending()
        self._update_spotify_status()

    # ------------------------------------------------------------------ data

    def _library_label(self, lib: dict) -> str:
        name = lib.get("name") or Path(lib["path"]).name
        return f"[{lib['library_id']}] {name} — {lib['path']}"

    def _refresh_libraries(self) -> None:
        libs = libraries.list_libraries()
        labels = [self._library_label(lib) for lib in libs]
        self._library_map = {self._library_label(lib): lib for lib in libs}
        self.library_combo["values"] = labels

        selected_id = settings.get_selected_library_id()
        selected_label = None
        for lib in libs:
            if lib["library_id"] == selected_id:
                selected_label = self._library_label(lib)
                break
        if selected_label:
            self.library_var.set(selected_label)
            self.selected_library_label.configure(text=f"Selected: {selected_label}")
        else:
            self.selected_library_label.configure(text="Selected: (none)")

        self._update_spotify_status()

    def _refresh_playlists(self) -> None:
        self.playlist_listbox.delete(0, tk.END)
        self._playlist_rows = playlists_mod.list_playlists()
        scope_values = ["Global"]
        for pl in self._playlist_rows:
            name = pl["name"] or pl["external_id"]
            status = "on" if pl["enabled"] else "off"
            line = f"[{pl['playlist_id']}] {pl['source']}: {name} ({status})"
            self.playlist_listbox.insert(tk.END, line)
            scope_values.append(f"[{pl['playlist_id']}] {name}")
        self.blacklist_playlist_combo["values"] = scope_values
        if self.blacklist_playlist_var.get() not in scope_values:
            self.blacklist_playlist_var.set("Global")

    def _refresh_pending(self) -> None:
        conn = db.get_connection()
        rows = conn.execute(
            "SELECT request_id FROM pending_decisions ORDER BY created_at DESC"
        ).fetchall()
        ids = [row["request_id"] for row in rows]
        self.pending_var.set(ids[0] if ids else "")

    def _update_spotify_status(self) -> None:
        try:
            from spotify_auth import has_cached_token

            logged_in = has_cached_token()
        except Exception:
            logged_in = False
        self.spotify_status.configure(
            text=f"Spotify: {'logged in' if logged_in else 'not logged in'}"
        )

    def _load_matching_settings_into_form(self) -> None:
        cfg = matching_settings_mod.load_matching_settings()
        self.dup_chroma_var.set(cfg.duplicate_chromaprint)
        self.dup_embed_var.set(cfg.duplicate_embedding)
        self.cmp_chroma_var.set(cfg.comparison_chromaprint)
        self.cmp_embed_var.set(cfg.comparison_embedding)
        for key, var in self.threshold_vars.items():
            var.set(str(getattr(cfg, key)))
        for key, var in self.weight_vars.items():
            var.set(str(getattr(cfg, key)))
        total = cfg.scoring_weight_total()
        self.weights_sum_label.configure(text=f"Weights sum: {total:.0f} (target 100)")

    def _selected_library(self) -> Optional[dict]:
        return self._library_map.get(self.library_var.get())

    def _on_library_pick(self) -> None:
        lib = self._selected_library()
        if lib:
            self.library_path_var.set(lib["path"])
            self.library_name_var.set(lib.get("name") or "")

    def _selected_playlist_ids(self) -> list[int]:
        return [
            self._playlist_rows[i]["playlist_id"]
            for i in self.playlist_listbox.curselection()
        ]

    def _blacklist_playlist_id(self) -> Optional[int]:
        value = self.blacklist_playlist_var.get()
        if value == "Global" or not value:
            return None
        if value.startswith("[") and "]" in value:
            try:
                return int(value[1 : value.index("]")])
            except ValueError:
                return None
        return None

    # ------------------------------------------------------------------ actions

    def _browse_library_path(self) -> None:
        path = filedialog.askdirectory(title="Choose download folder")
        if path:
            self.library_path_var.set(path)

    def _browse_song_dest(self) -> None:
        path = filedialog.askdirectory(title="Override save folder for this song")
        if path:
            self.song_dest_var.set(path)

    def _browse_cookies(self) -> None:
        path = filedialog.askopenfilename(
            title="Choose cookies file",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if path:
            self.cookies_var.set(path)

    def _register_library(self) -> None:
        path = self.library_path_var.get().strip()
        if not path:
            messagebox.showerror("Missing path", "Enter a folder path first.")
            return
        name = self.library_name_var.get().strip() or None
        self._run_task(
            "register library",
            lambda: cmd_set_download_path(_ns(path=path, name=name)),
        )

    def _select_library(self) -> None:
        lib = self._selected_library()
        if not lib:
            messagebox.showerror("No library", "Pick a registered library from the dropdown.")
            return
        self._run_task(
            "select library",
            lambda: cmd_select_download_path(_ns(path=lib["path"], name=None)),
        )

    def _delete_library(self) -> None:
        lib = self._selected_library()
        if not lib:
            messagebox.showerror("No library", "Pick a library to delete.")
            return
        if not messagebox.askyesno(
            "Delete library",
            f"Remove library registration for:\n{lib['path']}\n\n"
            "Downloaded files on disk are not deleted.",
        ):
            return
        self._run_task(
            "delete library",
            lambda: cmd_delete_download_path(
                _ns(library_id=lib["library_id"], path=None, name=None)
            ),
        )

    def _spotify_login(self) -> None:
        self._run_task("Spotify login", lambda: cmd_login(_ns()))

    def _set_cookies(self) -> None:
        path = self.cookies_var.get().strip()
        if not path:
            messagebox.showerror("Missing file", "Choose a cookies file.")
            return
        self._run_task(
            "set cookies",
            lambda: cmd_set_cookies(_ns(cookies_file=path)),
            refresh=False,
        )

    def _add_playlist(self) -> None:
        spotify = self.spotify_playlist_var.get().strip()
        youtube = self.youtube_playlist_var.get().strip()
        if bool(spotify) == bool(youtube):
            messagebox.showerror(
                "URL required",
                "Provide exactly one of Spotify or YouTube playlist URL.",
            )
            return
        self._run_task(
            "add playlist",
            lambda: cmd_add_playlist(
                _ns(
                    spotify_playlist_url=spotify or None,
                    youtube_playlist_url=youtube or None,
                    dest=None,
                    library=None,
                )
            ),
        )

    def _sync_selected(self) -> None:
        ids = self._selected_playlist_ids()
        if not ids:
            messagebox.showerror("No selection", "Select one or more playlists to sync.")
            return
        disabled = [
            row for row in self._playlist_rows if row["playlist_id"] in ids and not row["enabled"]
        ]
        if disabled:
            names = ", ".join(
                repr(row["name"] or row["external_id"]) for row in disabled
            )
            messagebox.showerror(
                "Playlist unset",
                "Cannot sync unset/disabled playlist(s): "
                f"{names}. Re-enable or choose tracked playlists only.",
            )
            return

        def task() -> None:
            for playlist_id in ids:
                cmd_sync(_ns(playlist_id=playlist_id, all=False), json_mode=False)

        self._run_task(f"sync {len(ids)} playlist(s)", task)

    def _sync_all(self) -> None:
        self._run_task(
            "sync all playlists",
            lambda: cmd_sync(_ns(playlist_id=None, all=True), json_mode=False),
        )

    def _unset_playlist(self) -> None:
        ids = self._selected_playlist_ids()
        if len(ids) != 1:
            messagebox.showerror("Select one", "Select exactly one playlist to unset.")
            return
        self._run_task(
            "unset playlist",
            lambda: cmd_unset_playlist(_ns(playlist_id=ids[0])),
        )

    def _remove_playlist(self) -> None:
        ids = self._selected_playlist_ids()
        if len(ids) != 1:
            messagebox.showerror("Select one", "Select exactly one playlist to remove.")
            return
        if not messagebox.askyesno(
            "Remove playlist",
            "Stop tracking this playlist? Downloaded files are kept.",
        ):
            return
        self._run_task(
            "remove playlist",
            lambda: cmd_remove_playlist(_ns(playlist_id=ids[0])),
        )

    def _download_track(self) -> None:
        mode = self.song_mode_var.get()
        dest = self.song_dest_var.get().strip() or None
        args: dict = {
            "spotify_track_url": None,
            "youtube_url": None,
            "spotify_playlist_url": None,
            "youtube_playlist_url": None,
            "index": None,
            "from_playlist": None,
            "dest": dest,
        }

        if mode == "spotify":
            url = self.spotify_track_var.get().strip()
            if not url:
                messagebox.showerror("URL required", "Enter a Spotify track URL.")
                return
            args["spotify_track_url"] = url
        elif mode == "youtube":
            url = self.youtube_track_var.get().strip()
            if not url:
                messagebox.showerror("URL required", "Enter a YouTube URL.")
                return
            args["youtube_url"] = url
        else:
            pl_url = self.track_playlist_var.get().strip()
            if not pl_url:
                messagebox.showerror("URL required", "Enter a playlist URL.")
                return
            try:
                args["index"] = int(self.track_index_var.get().strip())
            except ValueError:
                messagebox.showerror("Invalid index", "Index must be an integer.")
                return
            if self.track_source_var.get() == "spotify":
                args["spotify_playlist_url"] = pl_url
            else:
                args["youtube_playlist_url"] = pl_url

        self._run_task(
            "add single song",
            lambda: cmd_add_track(_ns(**args), json_mode=False),
        )

    def _blacklist_track(self) -> None:
        url = self.blacklist_url_var.get().strip()
        if not url:
            messagebox.showerror("URL required", "Enter a Spotify or YouTube track URL.")
            return
        reason = self.blacklist_reason_var.get().strip() or None
        playlist_id = self._blacklist_playlist_id()
        if "spotify.com" in url or url.startswith("spotify:"):
            args = _ns(
                spotify_track_url=url,
                youtube_url=None,
                spotify_playlist_url=None,
                youtube_playlist_url=None,
                index=None,
                playlist_id=playlist_id,
                reason=reason,
            )
        else:
            args = _ns(
                spotify_track_url=None,
                youtube_url=url,
                spotify_playlist_url=None,
                youtube_playlist_url=None,
                index=None,
                playlist_id=playlist_id,
                reason=reason,
            )
        self._run_task("blacklist track", lambda: cmd_blacklist_song(args))

    def _list_blacklisted(self) -> None:
        playlist_id = self._blacklist_playlist_id()
        self._run_task(
            "list blacklisted",
            lambda: cmd_list_blacklisted(_ns(playlist_id=playlist_id)),
            refresh=False,
        )

    def _save_matching(self) -> None:
        def task() -> None:
            updates: dict = {
                "duplicate_chromaprint": self.dup_chroma_var.get(),
                "duplicate_embedding": self.dup_embed_var.get(),
                "comparison_chromaprint": self.cmp_chroma_var.get(),
                "comparison_embedding": self.cmp_embed_var.get(),
            }
            for key, var in self.threshold_vars.items():
                raw = var.get().strip()
                if raw:
                    updates[key] = float(raw)
            for key, var in self.weight_vars.items():
                raw = var.get().strip()
                if raw:
                    updates[key] = float(raw)
            config = matching_settings_mod.update_matching_settings(**updates)
            self.root.after(
                0,
                lambda: self.weights_sum_label.configure(
                    text=f"Weights sum: {config.scoring_weight_total():.0f} (target 100)"
                ),
            )
            output.print_human(
                "Saved matching settings "
                f"(weights sum={config.scoring_weight_total():.0f})."
            )

        self._run_task("save matching settings", task, refresh=False)

    def _show_settings(self) -> None:
        self._run_task(
            "show settings",
            lambda: cmd_show_settings(_ns()),
            refresh=False,
        )

    def _resolve_duplicate(self) -> None:
        request_id = self.pending_var.get().strip()
        action = self.resolve_action_var.get()
        if not request_id:
            messagebox.showerror("Missing request", "Enter or refresh a request id.")
            return
        self._run_task(
            "resolve duplicate",
            lambda: cmd_resolve_duplicate(
                _ns(request_id=request_id, action=action), json_mode=False
            ),
        )


def main() -> None:
    scale = _enable_high_dpi()
    root = tk.Tk()
    GuiApp(root, scale)

    shutting_down = {"value": False}

    def _graceful_shutdown() -> None:
        if shutting_down["value"]:
            return
        shutting_down["value"] = True
        try:
            db.reset_connection()
        finally:
            root.destroy()

    def _handle_sigint(_signum, _frame) -> None:
        root.after(0, _graceful_shutdown)

    root.protocol("WM_DELETE_WINDOW", _graceful_shutdown)
    signal.signal(signal.SIGINT, _handle_sigint)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        _graceful_shutdown()


if __name__ == "__main__":
    main()
