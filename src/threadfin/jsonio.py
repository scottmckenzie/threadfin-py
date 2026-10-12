"""JSON file helpers (port of the JSON helpers in ``toolchain.go``).

Differences from Go: writes are atomic (temp file + rename) so a crash cannot
leave a half-written ``settings.json``.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class JSONFileError(ValueError):
    """A JSON file could not be read or is not a JSON object."""


def to_json(data: Any) -> str:
    """Serialise like Go's ``json.MarshalIndent(v, "", "  ")``."""
    return json.dumps(data, indent=2, ensure_ascii=False)


def load_json_object(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Load a JSON file that must contain an object."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise JSONFileError(f"cannot read {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise JSONFileError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise JSONFileError(f"{path}: expected a JSON object, got {type(data).__name__}")
    return data


def write_bytes_atomic(path: str | os.PathLike[str], data: bytes, mode: int = 0o644) -> None:
    """Write ``data`` to ``path`` via a temp file in the same directory."""
    target = Path(path)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def write_text_atomic(path: str | os.PathLike[str], text: str, mode: int = 0o644) -> None:
    write_bytes_atomic(path, text.encode("utf-8"), mode)


def save_json(path: str | os.PathLike[str], data: Any, mode: int = 0o644) -> None:
    write_text_atomic(path, to_json(data), mode)
