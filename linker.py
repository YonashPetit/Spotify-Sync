"""Link downloaded files into the library-wide All Songs folder."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from models import LinkMethod


def link_into_all_songs(
    real_file: Path,
    all_songs_dir: Path,
) -> tuple[Path, LinkMethod]:
    """
    Create an entry in All Songs pointing to *real_file*.
    Order: symlink -> hardlink -> copy (copy is the final fallback).
    Returns (link_path, method_used).
    """
    all_songs_dir.mkdir(parents=True, exist_ok=True)
    link_path = all_songs_dir / real_file.name

    if link_path.exists() or link_path.is_symlink():
        try:
            if link_path.is_symlink() or os.path.samefile(link_path, real_file):
                return link_path, "symlink" if link_path.is_symlink() else "hardlink"
        except OSError:
            pass
        link_path.unlink()

    try:
        link_path.symlink_to(real_file)
        return link_path, "symlink"
    except OSError:
        pass

    try:
        os.link(real_file, link_path)
        return link_path, "hardlink"
    except OSError:
        pass

    shutil.copy2(real_file, link_path)
    return link_path, "copy"
