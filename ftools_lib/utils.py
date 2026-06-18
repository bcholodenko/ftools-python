"""
Small file helpers, equivalent to utils.h in the original project.
"""

from pathlib import Path


def read_file(path) -> bytes:
    p = Path(path)
    try:
        return p.read_bytes()
    except OSError as e:
        raise RuntimeError(f"Could not read file '{p}': {e}") from e


def write_file(path, data: bytes) -> None:
    p = Path(path)
    try:
        p.write_bytes(bytes(data))
    except OSError as e:
        raise RuntimeError(f"Could not write file '{p}': {e}") from e
