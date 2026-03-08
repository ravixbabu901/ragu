"""Shared helper for resolving file paths relative to the download directory."""

import os

from userge import config


def resolve_download_path(input_str: str) -> str:
    """Resolve a file path argument to an absolute path.

    - Empty input  → raises ValueError.
    - Quoted input → strips surrounding quotes before processing.
    - Absolute path → returned as-is (after realpath normalization).
    - Relative path → joined with ``config.Dynamic.DOWN_PATH``.

    Raises ValueError for directory traversal attempts that escape the
    download directory.
    """
    if not input_str:
        raise ValueError("No file path provided.")

    # Strip matching surrounding quotes (single or double)
    if len(input_str) >= 2 and input_str[0] in ('"', "'") and input_str[-1] == input_str[0]:
        input_str = input_str[1:-1]

    input_str = input_str.strip()
    if not input_str:
        raise ValueError("No file path provided.")

    if os.path.isabs(input_str):
        return os.path.realpath(input_str)

    resolved = os.path.realpath(os.path.join(config.Dynamic.DOWN_PATH, input_str))
    down_path = os.path.realpath(config.Dynamic.DOWN_PATH)
    # Guard against directory traversal escaping the download directory
    if not resolved.startswith(down_path + os.sep) and resolved != down_path:
        raise ValueError(f"Path '{input_str}' is outside the download directory.")
    return resolved

