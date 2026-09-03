"""Locate a Java (or other) source file in the repo for a given fully
qualified classname, e.g. "com.e2net.billing.InvoiceServiceTest".
"""
from __future__ import annotations

import os


def find_source_file(repo_root: str, classname: str) -> str | None:
    if not classname:
        return None
    rel_path = classname.replace(".", os.sep)
    candidates = [
        rel_path + ".java",
        rel_path + ".kt",
        rel_path + ".scala",
    ]
    for root, _dirs, files in os.walk(repo_root):
        # Skip build output / vcs dirs to keep the walk fast.
        _dirs[:] = [d for d in _dirs if d not in (".git", "target", "build", "node_modules", ".idea")]
        for cand in candidates:
            cand_name = os.path.basename(cand)
            if cand_name in files and root.replace("\\", "/").endswith(
                os.path.dirname(cand).replace("\\", "/")
            ):
                return os.path.join(root, cand_name)
    # Fallback: match by filename only (handles nonstandard source roots).
    base_names = {os.path.basename(c) for c in candidates}
    for root, _dirs, files in os.walk(repo_root):
        _dirs[:] = [d for d in _dirs if d not in (".git", "target", "build", "node_modules", ".idea")]
        for f in files:
            if f in base_names:
                return os.path.join(root, f)
    return None


def find_source_file_multi(repo_roots: list, classname: str) -> str | None:
    """Tries find_source_file against each repo root in order, returning the
    first match (e.g. across e2net-platform, e2net-commons, e2net-tests).
    """
    for root in repo_roots:
        found = find_source_file(root, classname)
        if found:
            return found
    return None

