from __future__ import annotations

import json
import re
from dataclasses import dataclass
from itertools import count
from pathlib import Path
from typing import Any

from mutagen import File
from mutagen.id3 import COMM, ID3, TBPM, TKEY, TXXX, ID3NoHeaderError
from mutagen.mp4 import MP4, MP4FreeForm

from dj_sort.metadata import read_metadata
from dj_sort.paths import safe_path_part, shorten_filename_stem

MIXED_IN_KEY_COMMENT_RE = re.compile(
    r"^\s*(?P<key>0?(?:[1-9]|1[0-2])[AB])\s*-\s*(?P<bpm>\d+(?:\.\d+)?)\s*-\s*(?P<energy>10|[1-9])(?:\s*-\s*(?P<rest>.*))?\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MixedInKeyInfo:
    key: str
    bpm: str
    energy: str
    comment: str | None = None
    comment_rest: str | None = None

    @property
    def normalized_comment(self) -> str:
        prefix = f"{self.key} - {self.bpm} - {self.energy}"
        return f"{prefix} - {self.comment_rest}" if self.comment_rest else prefix


@dataclass(frozen=True)
class MixedInKeyUpdate:
    source_path: Path
    target_path: Path
    info: MixedInKeyInfo | None
    changed_metadata: bool
    changed_path: bool
    status: str
    notes: str | None = None


def parse_mixed_in_key_comment(comment: str) -> MixedInKeyInfo | None:
    match = MIXED_IN_KEY_COMMENT_RE.match(comment)
    if not match:
        return None
    return MixedInKeyInfo(
        key=_normalize_camelot(match.group("key")),
        bpm=_normalize_bpm(match.group("bpm")),
        energy=match.group("energy"),
        comment=comment,
        comment_rest=(match.group("rest") or "").strip() or None,
    )


def read_mixed_in_key_info(path: Path) -> MixedInKeyInfo | None:
    for comment in _comments(path):
        if parsed := parse_mixed_in_key_comment(comment):
            return parsed
    return _read_tag_fallback(path)


def mixed_in_key_target_path(path: Path, info: MixedInKeyInfo, include_energy: bool = True) -> Path:
    metadata = read_metadata(path)
    parts = [
        safe_path_part(metadata.artist or "Unknown Artist"),
        safe_path_part(metadata.title or path.stem),
        safe_path_part(info.key),
        safe_path_part(info.bpm),
    ]
    if include_energy:
        parts.append(f"E{safe_path_part(info.energy)}")
    stem = shorten_filename_stem(" - ".join(parts))
    return path.with_name(f"{stem}{path.suffix.casefold()}")


def apply_mixed_in_key_update(
    path: Path,
    include_energy: bool = True,
    write: bool = False,
    duplicates_dir: Path | None = None,
    library_dir: Path | None = None,
) -> MixedInKeyUpdate:
    info = read_mixed_in_key_info(path)
    if info is None:
        return MixedInKeyUpdate(path, path, None, False, False, "skipped", "missing Mixed In Key comment or tags")

    target_path = mixed_in_key_target_path(path, info, include_energy=include_energy)
    changed_path = target_path != path
    if changed_path and target_path.exists():
        same_bitrate = _same_bitrate(path, target_path)
        if not write:
            status = "planned_deleted_duplicate" if same_bitrate else "planned_moved_duplicate"
            notes = "same bitrate duplicate" if same_bitrate else f"different bitrate duplicate of {target_path}"
            return MixedInKeyUpdate(path, target_path, info, False, same_bitrate is False, status, notes)
        if same_bitrate:
            path.unlink()
            return MixedInKeyUpdate(path, target_path, info, False, False, "deleted_duplicate", "same bitrate duplicate")
        if duplicates_dir is None:
            return MixedInKeyUpdate(path, target_path, info, False, False, "skipped", "different bitrate duplicate needs duplicates_dir")
        duplicate_path = _duplicate_target_path(path, target_path, duplicates_dir, library_dir)
        duplicate_path.parent.mkdir(parents=True, exist_ok=True)
        _write_mixed_in_key_tags(path, info)
        path.rename(duplicate_path)
        return MixedInKeyUpdate(path, duplicate_path, info, True, True, "moved_duplicate", f"different bitrate duplicate of {target_path}")

    if not write:
        return MixedInKeyUpdate(path, target_path, info, True, changed_path, "planned")

    _write_mixed_in_key_tags(path, info)
    if changed_path:
        path.rename(target_path)
    return MixedInKeyUpdate(path, target_path, info, True, changed_path, "updated")


def _same_bitrate(path: Path, other_path: Path) -> bool:
    return read_metadata(path).bitrate == read_metadata(other_path).bitrate


def _duplicate_target_path(path: Path, clean_target_path: Path, duplicates_dir: Path, library_dir: Path | None) -> Path:
    if library_dir is not None:
        try:
            relative_parent = path.parent.relative_to(library_dir)
        except ValueError:
            relative_parent = Path()
    else:
        relative_parent = Path()
    return _unique_non_hash_path(duplicates_dir / relative_parent / clean_target_path.name)


def _unique_non_hash_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in count(2):
        candidate = path.with_name(f"{path.stem} - duplicate {index}{path.suffix}")
        if not candidate.exists():
            return candidate


def _comments(path: Path) -> list[str]:
    audio = File(path, easy=True)
    comments: list[str] = []
    if audio is not None and audio.tags is not None:
        comments.extend(str(value) for value in audio.tags.get("comment", []) if value)

    raw = File(path)
    tags = raw.tags if raw is not None and raw.tags is not None else {}
    if path.suffix.casefold() == ".mp3" and hasattr(tags, "getall"):
        comments.extend(str(text) for frame in tags.getall("COMM") for text in frame.text if text)
    if path.suffix.casefold() in {".m4a", ".aac", ".alac"}:
        comments.extend(str(value) for value in tags.get("\xa9cmt", []) if value)
    return list(dict.fromkeys(comments))


def _read_tag_fallback(path: Path) -> MixedInKeyInfo | None:
    suffix = path.suffix.casefold()
    if suffix == ".mp3":
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            return None
        key = _id3_text(tags.get("TKEY"))
        bpm = _id3_text(tags.get("TBPM"))
        energy = _id3_text(tags.get("TXXX:EnergyLevel"))
    elif suffix in {".m4a", ".aac", ".alac"}:
        audio = MP4(path)
        tags = audio.tags or {}
        key = _mp4_text(tags.get("----:com.apple.iTunes:initialkey")) or _mp4_text(
            tags.get("----:com.mixedinkey.mixedinkey:initialkey")
        )
        bpm = str(tags.get("tmpo", [""])[0] or "")
        energy = _mp4_text(tags.get("----:com.apple.iTunes:energylevel"))
    else:
        return None

    if not key or not bpm or not energy:
        return None
    return MixedInKeyInfo(key=_normalize_camelot(key), bpm=_normalize_bpm(bpm), energy=energy.strip())


def _write_mixed_in_key_tags(path: Path, info: MixedInKeyInfo) -> None:
    suffix = path.suffix.casefold()
    if suffix == ".mp3":
        _write_mp3_tags(path, info)
        return
    if suffix in {".m4a", ".aac", ".alac"}:
        _write_mp4_tags(path, info)
        return
    raise ValueError(f"Mixed In Key import is not supported for {path.suffix or 'this file type'}")


def _write_mp3_tags(path: Path, info: MixedInKeyInfo) -> None:
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    tags.delall("TKEY")
    tags.add(TKEY(encoding=3, text=[info.key]))
    tags.delall("TBPM")
    tags.add(TBPM(encoding=3, text=[info.bpm]))
    tags.delall("TXXX:EnergyLevel")
    tags.add(TXXX(encoding=3, desc="EnergyLevel", text=[info.energy]))
    _replace_or_add_comment(tags, info.normalized_comment)
    tags.save(path)


def _write_mp4_tags(path: Path, info: MixedInKeyInfo) -> None:
    audio = MP4(path)
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    tags["tmpo"] = [int(round(float(info.bpm)))]
    tags["\xa9cmt"] = [info.normalized_comment]
    tags["----:com.apple.iTunes:initialkey"] = [_mp4_freeform(info.key)]
    tags["----:com.mixedinkey.mixedinkey:initialkey"] = [_mp4_freeform(info.key)]
    tags["----:com.apple.iTunes:energylevel"] = [_mp4_freeform(info.energy)]
    tags["----:com.mixedinkey.mixedinkey:energy"] = [_mp4_freeform(_energy_json(info.energy))]
    audio.save()


def _replace_or_add_comment(tags: ID3, comment: str) -> None:
    target = None
    for frame in tags.getall("COMM"):
        if frame.desc in {"", "dj-sort"}:
            target = frame
            break
    if target is not None:
        target.text = [comment]
        return
    tags.add(COMM(encoding=3, lang="eng", desc="", text=[comment]))


def _id3_text(value: Any) -> str | None:
    if value is not None and getattr(value, "text", None):
        return str(value.text[0]).strip() or None
    return None


def _mp4_text(values: Any) -> str | None:
    if not isinstance(values, list | tuple) or not values:
        return None
    value = values[0]
    if isinstance(value, MP4FreeForm | bytes):
        return bytes(value).decode("utf-8", errors="ignore").strip() or None
    return str(value).strip() or None


def _mp4_freeform(value: str) -> MP4FreeForm:
    return MP4FreeForm(value.encode("utf-8"))


def _energy_json(energy: str) -> str:
    return json.dumps({"algorithm": 13, "energyLevel": int(energy), "source": "dj-sort/mixedinkey"})


def _normalize_camelot(value: str) -> str:
    cleaned = value.strip().upper()
    match = re.match(r"^0?([1-9]|1[0-2])([AB])$", cleaned)
    return f"{match.group(1)}{match.group(2)}" if match else cleaned


def _normalize_bpm(value: str) -> str:
    number = float(value.strip())
    return str(int(round(number)))
