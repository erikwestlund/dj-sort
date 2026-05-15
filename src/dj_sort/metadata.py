from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mutagen import File
from mutagen.aiff import AIFF
from mutagen.id3 import COMM, ID3, TCON, ID3NoHeaderError
from mutagen.mp4 import MP4FreeForm
from mutagen.wave import WAVE

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
    if path.suffix.casefold() == ".mp3":
        tags = {**_mp3_id3_tags(path), **tags}
    if path.suffix.casefold() in {".m4a", ".aac", ".alac"}:
        tags = {**_mp4_extra_tags(path), **tags}
    if path.suffix.casefold() == ".wav":
        tags = {**_wav_id3_tags(path), **tags}
    info = audio.info if audio is not None else None

    artist = _first(tags, "artist", "TPE1")
    title = _first(tags, "title", "TIT2")
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

    raw_key = _first(tags, "initialkey", "key", "TKEY")
    camelot_key = normalize_camelot_key(raw_key)
    if raw_key and not camelot_key:
        labels.append("Needs Key Review")

    return TrackMetadata(
        path=path,
        artist=artist,
        title=title,
        genre=_first(tags, "genre", "TCON"),
        bpm=_first(tags, "bpm", "TBPM"),
        raw_key=raw_key,
        camelot_key=camelot_key,
        album=_first(tags, "album", "TALB"),
        album_artist=_first(tags, "albumartist", "album artist", "TPE2"),
        track_number=_first(tags, "tracknumber", "TRCK"),
        release_date=_first(tags, "date", "year", "TDRC", "TYER"),
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
        _write_wav_genre(path, canonical_genre, original_genre, original_genre_comment_prefix)
        return
    if path.suffix.casefold() in {".aif", ".aiff"}:
        _write_aiff_genre(path, canonical_genre, original_genre, original_genre_comment_prefix)
        return
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


def read_original_genre_comment(path: Path, prefix: str = "dj-sort original genre:") -> str | None:
    cleaned_prefix = prefix.strip()
    if not cleaned_prefix:
        return None
    for comment in _comments(path):
        cleaned = comment.strip()
        if cleaned.casefold().startswith(cleaned_prefix.casefold()):
            return cleaned[len(cleaned_prefix) :].strip() or None
    return None


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


def _comments(path: Path) -> list[str]:
    comments: list[str] = []
    try:
        audio = File(path, easy=True)
    except Exception:  # noqa: BLE001 - malformed optional tags should not block UI hints
        audio = None
    if audio is not None and audio.tags is not None:
        comments.extend(str(value) for value in audio.tags.get("comment", []) if value)

    try:
        raw = File(path)
    except Exception:  # noqa: BLE001 - malformed optional tags should not block UI hints
        raw = None
    tags = raw.tags if raw is not None and raw.tags is not None else {}
    if hasattr(tags, "getall"):
        comments.extend(str(text) for frame in tags.getall("COMM") for text in frame.text if text)
    if path.suffix.casefold() == ".mp3" and not comments:
        try:
            id3_tags = ID3(path)
        except ID3NoHeaderError:
            id3_tags = None
        if id3_tags is not None:
            comments.extend(str(text) for frame in id3_tags.getall("COMM") for text in frame.text if text)
    if path.suffix.casefold() in {".m4a", ".aac", ".alac"}:
        comments.extend(str(value) for value in tags.get("\xa9cmt", []) if value)
    return list(dict.fromkeys(comments))


def _mp3_id3_tags(path: Path) -> dict[str, list[str]]:
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        return {}

    values: dict[str, list[str]] = {}
    if key := [str(text) for frame in tags.getall("TKEY") for text in frame.text]:
        values["initialkey"] = key
    if bpm := [str(text) for frame in tags.getall("TBPM") for text in frame.text]:
        values["bpm"] = bpm
    return values


def _mp4_extra_tags(path: Path) -> dict[str, list[str]]:
    audio = File(path)
    tags = audio.tags if audio is not None and audio.tags is not None else {}
    values: dict[str, list[str]] = {}
    for key in ("----:com.apple.iTunes:initialkey", "----:com.mixedinkey.mixedinkey:initialkey"):
        if extracted := _mp4_freeform_text(tags.get(key)):
            values["initialkey"] = extracted
            break
    return values


def _mp4_freeform_text(values: Any) -> list[str]:
    if not isinstance(values, list | tuple):
        return []
    result: list[str] = []
    for value in values:
        if isinstance(value, MP4FreeForm | bytes):
            result.append(bytes(value).decode("utf-8", errors="ignore").strip())
        elif value:
            result.append(str(value).strip())
    return [value for value in result if value]


def _wav_id3_tags(path: Path) -> dict[str, list[str]]:
    try:
        tags = WAVE(path).tags
    except Exception:  # noqa: BLE001 - unreadable optional WAV tags should not block metadata inference
        return {}
    if tags is None:
        return {}

    values: dict[str, list[str]] = {}
    if genre := [str(text) for frame in tags.getall("TCON") for text in frame.text]:
        values["genre"] = genre
    if artist := [str(text) for frame in tags.getall("TPE1") for text in frame.text]:
        values["artist"] = artist
    if title := [str(text) for frame in tags.getall("TIT2") for text in frame.text]:
        values["title"] = title
    return values


def _write_wav_genre(
    path: Path,
    canonical_genre: str,
    original_genre: str | None,
    original_genre_comment_prefix: str,
) -> None:
    audio = WAVE(path)
    if audio.tags is None:
        audio.add_tags()
    audio.tags.delall("TCON")
    audio.tags.add(TCON(encoding=3, text=[canonical_genre]))
    if comment := original_genre_comment(original_genre, canonical_genre, original_genre_comment_prefix):
        existing = [frame.text[0] for frame in audio.tags.getall("COMM") if frame.text]
        if comment not in existing:
            audio.tags.add(COMM(encoding=3, lang="eng", desc="dj-sort", text=[comment]))
    audio.save()


def _write_aiff_genre(
    path: Path,
    canonical_genre: str,
    original_genre: str | None,
    original_genre_comment_prefix: str,
) -> None:
    audio = AIFF(path)
    if audio.tags is None:
        audio.add_tags()
    audio.tags.delall("TCON")
    audio.tags.add(TCON(encoding=3, text=[canonical_genre]))
    if comment := original_genre_comment(original_genre, canonical_genre, original_genre_comment_prefix):
        existing = [frame.text[0] for frame in audio.tags.getall("COMM") if frame.text]
        if comment not in existing:
            audio.tags.add(COMM(encoding=3, lang="eng", desc="dj-sort", text=[comment]))
    audio.save()


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
        if hasattr(value, "text") and value.text:
            return str(value.text[0]).strip() or None
        if value:
            return str(value).strip() or None
    return None


def _duration_ms(info: Any) -> int | None:
    length = getattr(info, "length", None)
    return int(length * 1000) if length is not None else None


def _int_attr(info: Any, attr: str) -> int | None:
    value = getattr(info, attr, None)
    return int(value) if value is not None else None
