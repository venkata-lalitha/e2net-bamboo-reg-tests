"""Applies Patch objects to files on disk, with optional .bak backups."""
from __future__ import annotations

import shutil

from fix_rules import Patch


def apply_patch(patch: Patch, create_backup: bool = True) -> bool:
    """Replaces the single occurrence of old_text with new_text in file_path.
    Returns True if applied, False if old_text was not found exactly once.
    """
    with open(patch.file_path, "r", encoding="utf-8") as f:
        content = f.read()

    if content.count(patch.old_text) != 1:
        return False

    if create_backup:
        shutil.copyfile(patch.file_path, patch.file_path + ".bak")

    new_content = content.replace(patch.old_text, patch.new_text, 1)
    with open(patch.file_path, "w", encoding="utf-8") as f:
        f.write(new_content)
    return True
