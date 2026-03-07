#!/usr/bin/env python3

from __future__ import annotations

import shutil
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]

ROOT_TARGETS = [
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
]

FILE_SUFFIXES = {
    ".pyc",
    ".pyo",
}


def remove_path(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    print(f"removed {path.relative_to(REPO_ROOT)}")


def main() -> None:
    for name in ROOT_TARGETS:
        remove_path(REPO_ROOT / name)

    for directory in REPO_ROOT.rglob("__pycache__"):
        if directory.is_dir():
            remove_path(directory)

    for file_path in REPO_ROOT.rglob("*"):
        if file_path.is_file() and file_path.suffix in FILE_SUFFIXES:
            remove_path(file_path)


if __name__ == "__main__":
    main()
