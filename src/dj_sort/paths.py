from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path

MAX_FILENAME_STEM_LENGTH = 180
UNSAFE_CHARS = re.compile(r"[\\/:*?\"<>|\x00-\x1f]")
WHITESPACE = re.compile(r"\s+")


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFC", value)
    value = WHITESPACE.sub(" ", value.strip())
    return value


def normalize_key(value: str) -> str:
    return normalize_text(value).casefold()


def safe_path_part(value: str, fallback: str = "Unknown") -> str:
    value = normalize_text(value)
    value = UNSAFE_CHARS.sub("_", value)
    value = value.strip(" .")
    return value or fallback


def shorten_filename_stem(stem: str, max_length: int = MAX_FILENAME_STEM_LENGTH) -> str:
    if len(stem) <= max_length:
        return stem
    digest = hashlib.sha1(stem.encode("utf-8")).hexdigest()[:8]
    return f"{stem[: max_length - 11].rstrip()} - {digest}"


def short_hash(value: str, length: int = 8) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:length]


def ensure_unique_path(path: Path, occupied: set[Path], suffix_seed: str) -> Path:
    if path not in occupied and not path.exists():
        occupied.add(path)
        return path

    suffix = short_hash(suffix_seed)
    candidate = path.with_name(f"{path.stem} - {suffix}{path.suffix}")
    counter = 2
    while candidate in occupied or candidate.exists():
        candidate = path.with_name(f"{path.stem} - {suffix}-{counter}{path.suffix}")
        counter += 1
    occupied.add(candidate)
    return candidate


def relative_archive_path(source_root: Path, source_path: Path) -> Path:
    try:
        return source_path.relative_to(source_root)
    except ValueError:
        return Path(source_path.name)
