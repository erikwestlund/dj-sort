from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mutagen import File
from mutagen.id3 import COMM, ID3, ID3NoHeaderError

SUPPORTED_EXTENSIONS = {".mp3", ".flac", ".m4a", ".aac", ".alac", ".wav", ".aiff", ".aif"}
CAMELot_RE = re.compile(r"^(?:[1-9]|1[0-2])[AB]$", re.IGNORECASE)

TRADITIONAL_TO_CAMELOT = {
    "a-flat minor": "1A",
    "g# minor": "1A",
    "b major": "1B",
    "e-flat minor": "2A",
    "d# minor": "2A",
    "f# major": "2B",
    "b-flat minor": "3A",
    "a# minor": "3A",
    "d-flat major": "3B",
    "c# major": "3B",
    "f minor": "4A",
    "a-flat major": "4B",
    "g# major": "4B",
    "c minor": "5A",
    "e-flat major": "5B",
    "d# major": "5B",
    "g minor": "6A",
    "b-flat major": "6B",
    "a# major": "6B",
    "d minor": "7A",
    "f major": "7B",
    "a minor": "8A",
    "c major": "8B",
    "e minor": "9A",
    "g major": "9B",
    "b minor": "10A",
    "d major": "10B",
    "f# minor": "11A",
    "g-flat minor": "11A",
    "a major": "11B",
    "d-flat minor": "12A",
    "c# minor": "12A",
    "e major": "12B",
}


@dataclass(frozen=True)
class TrackMetadata:
    path: Path
    artist: str | None
    title: str | None
    genre: str | None
    bpm: str | None
    raw_key: str | None
    camelot_key: str | None
    album: str | None
    album_artist: str | None
    track_number: str | None
    release_date: str | None
    duration_ms: int | None
    bitrate: int | None
    sample_rate: int | None
    file_size: int
    extension: str
    inferred: bool
    labels: tuple[str, ...]


def is_supported_audio(path: Path) -> bool:
    return path.suffix.casefold() in SUPPORTED_EXTENSIONS


def read_metadata(path: Path) -> TrackMetadata:
    audio = File(path, easy=True)
    tags = audio.tags if audio is not None and audio.tags is not None else {}
    info = audio.info if audio is not None else None

    artist = _first(tags, "artist")
    title = _first(tags, "title")
    inferred = False
    labels: list[str] = []

    if not artist or not title:
        inferred_artist, inferred_title = infer_artist_title(path)
        if not artist and inferred_artist:
            artist = inferred_artist
            inferred = True
        if not title and inferred_title:
            title = inferred_title
            inferred = True
        if inferred:
            labels.append("Inferred Metadata")

    if not artist:
        artist = "Unknown Artist"
        labels.append("Unknown Artist")
    if not title:
        title = "Unknown Title"
        labels.append("Unknown Title")

    raw_key = _first(tags, "initialkey", "key")
    camelot_key = normalize_camelot_key(raw_key)
    if raw_key and not camelot_key:
        labels.append("Needs Key Review")

    return TrackMetadata(
        path=path,
        artist=artist,
        title=title,
        genre=_first(tags, "genre"),
        bpm=_first(tags, "bpm"),
        raw_key=raw_key,
        camelot_key=camelot_key,
        album=_first(tags, "album"),
        album_artist=_first(tags, "albumartist", "album artist"),
        track_number=_first(tags, "tracknumber"),
        release_date=_first(tags, "date", "year"),
        duration_ms=_duration_ms(info),
        bitrate=_int_attr(info, "bitrate"),
        sample_rate=_int_attr(info, "sample_rate"),
        file_size=path.stat().st_size,
        extension=path.suffix.casefold().lstrip("."),
        inferred=inferred,
        labels=tuple(dict.fromkeys(labels)),
    )


def write_genre(
    path: Path,
    canonical_genre: str,
    original_genre: str | None = None,
    original_genre_comment_prefix: str = "dj-sort original genre:",
) -> None:
    if path.suffix.casefold() == ".wav":
        raise ValueError("WAV genre write-back is disabled pending format-specific validation")
    audio = File(path, easy=True)
    if audio is None:
        raise ValueError(f"Unsupported metadata write: {path}")
    audio["genre"] = [canonical_genre]
    comment = original_genre_comment(original_genre, canonical_genre, original_genre_comment_prefix)
    if comment is None:
        audio.save()
        return

    try:
        _append_easy_comment(audio, comment)
        audio.save()
        return
    except KeyError:
        audio.save()
        if path.suffix.casefold() == ".mp3":
            _append_id3_comment(path, comment)
            return
        raise ValueError(f"Comment metadata write is not supported for {path.suffix or 'this file type'}") from None


def original_genre_comment(
    original_genre: str | None,
    canonical_genre: str,
    prefix: str = "dj-sort original genre:",
) -> str | None:
    if not original_genre:
        return None
    cleaned_original = original_genre.strip()
    cleaned_canonical = canonical_genre.strip()
    if not cleaned_original or cleaned_original.casefold() == cleaned_canonical.casefold():
        return None
    return f"{prefix.rstrip()} {cleaned_original}"


def _append_easy_comment(audio: Any, comment: str) -> None:
    existing = [str(value) for value in audio.get("comment", [])]
    if comment not in existing:
        audio["comment"] = [*existing, comment]


def _append_id3_comment(path: Path, comment: str) -> None:
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    existing = [frame.text[0] for frame in tags.getall("COMM") if frame.text]
    if comment not in existing:
        tags.add(COMM(encoding=3, lang="eng", desc="dj-sort", text=[comment]))
    tags.save(path)


def infer_artist_title(path: Path) -> tuple[str | None, str | None]:
    stem = path.stem.strip()
    if stem.count(" - ") == 1:
        left, right = stem.split(" - ", 1)
    elif stem.count("-") == 1:
        left, right = stem.split("-", 1)
    else:
        return None, None

    artist = left.strip()
    title = right.strip()
    if not artist or not title:
        return None, None
    return artist, title


def normalize_camelot_key(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip()
    if CAMELot_RE.match(cleaned):
        return cleaned.upper()
    normalized = " ".join(cleaned.casefold().split())
    if normalized.endswith(" maj"):
        normalized = f"{normalized[:-4]} major"
    if normalized.endswith(" min"):
        normalized = f"{normalized[:-4]} minor"
    return TRADITIONAL_TO_CAMELOT.get(normalized)


def _first(tags: Any, *keys: str) -> str | None:
    for key in keys:
        value = tags.get(key) if hasattr(tags, "get") else None
        if isinstance(value, list | tuple) and value:
            return str(value[0]).strip() or None
        if value:
            return str(value).strip() or None
    return None


def _duration_ms(info: Any) -> int | None:
    length = getattr(info, "length", None)
    return int(length * 1000) if length is not None else None


def _int_attr(info: Any, attr: str) -> int | None:
    value = getattr(info, attr, None)
    return int(value) if value is not None else None
