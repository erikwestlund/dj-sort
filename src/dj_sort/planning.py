from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path

from dj_sort.genres import GenreMap
from dj_sort.metadata import TrackMetadata, is_supported_audio, read_metadata
from dj_sort.paths import ensure_unique_path, normalize_key, safe_path_part, shorten_filename_stem
from dj_sort.settings import Settings


@dataclass(frozen=True)
class Plan:
    source_path: Path
    target_path: Path
    artist: str
    normalized_artist: str
    title: str
    normalized_title: str
    raw_genre: str | None
    canonical_genre: str
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
    labels: tuple[str, ...]
    needs_review: bool
    collision_adjusted: bool

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["source_path"] = str(self.source_path)
        data["target_path"] = str(self.target_path)
        data["labels"] = list(self.labels)
        return data


@dataclass(frozen=True)
class SkippedFile:
    path: Path
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"path": str(self.path), "reason": self.reason}


@dataclass(frozen=True)
class PlanningResult:
    plans: list[Plan]
    skipped: list[SkippedFile]

    def to_dict(self) -> dict[str, object]:
        return {
            "report_type": "planning",
            "plans": [plan.to_dict() for plan in self.plans],
            "skipped": [skipped.to_dict() for skipped in self.skipped],
            "summary": {
                "planned": len(self.plans),
                "skipped": len(self.skipped),
            },
        }


def scan_audio_files(source_root: Path, recursive: bool, limit: int | None) -> tuple[list[Path], list[SkippedFile]]:
    if not source_root.exists():
        raise FileNotFoundError(f"Source root does not exist: {source_root}")
    if not source_root.is_dir():
        raise NotADirectoryError(f"Source root is not a directory: {source_root}")

    iterator = source_root.rglob("*") if recursive else source_root.glob("*")
    candidates: list[Path] = []
    skipped: list[SkippedFile] = []
    for path in sorted(iterator):
        if path.is_dir():
            continue
        if not is_supported_audio(path):
            skipped.append(SkippedFile(path=path, reason="unsupported_extension"))
            continue
        candidates.append(path)
        if limit is not None and len(candidates) >= limit:
            break
    return candidates, skipped


def build_plans(settings: Settings, genre_map: GenreMap, limit: int | None = None) -> PlanningResult:
    paths, skipped = scan_audio_files(settings.source_root, settings.recursive, limit)
    occupied: set[Path] = set()
    plans: list[Plan] = []

    for path in paths:
        try:
            metadata = read_metadata(path)
            plans.append(_build_plan(settings, genre_map, metadata, occupied))
        except Exception as exc:  # noqa: BLE001 - per-file failures should not abort planning
            skipped.append(SkippedFile(path=path, reason=f"metadata_error: {exc}"))

    return PlanningResult(plans=plans, skipped=skipped)


def _build_plan(
    settings: Settings,
    genre_map: GenreMap,
    metadata: TrackMetadata,
    occupied: set[Path],
) -> Plan:
    genre = genre_map.resolve(metadata.genre, settings.unknown_genre_dir)
    labels = list(metadata.labels)
    if genre.missing:
        labels.append("Needs Genre")

    safe_genre = safe_path_part(genre.canonical_genre or settings.unknown_genre_dir)
    filename = _filename(settings, metadata)
    target = settings.library_root / safe_genre / filename
    unique_target = ensure_unique_path(target, occupied, str(metadata.path))

    return Plan(
        source_path=metadata.path,
        target_path=unique_target,
        artist=metadata.artist or "Unknown Artist",
        normalized_artist=normalize_key(metadata.artist or "Unknown Artist"),
        title=metadata.title or "Unknown Title",
        normalized_title=normalize_key(metadata.title or "Unknown Title"),
        raw_genre=genre.raw_genre,
        canonical_genre=genre.canonical_genre or settings.unknown_genre_dir,
        bpm=_format_bpm(metadata.bpm, settings.bpm_format),
        raw_key=metadata.raw_key,
        camelot_key=metadata.camelot_key,
        album=metadata.album,
        album_artist=metadata.album_artist,
        track_number=metadata.track_number,
        release_date=metadata.release_date,
        duration_ms=metadata.duration_ms,
        bitrate=metadata.bitrate,
        sample_rate=metadata.sample_rate,
        file_size=metadata.file_size,
        extension=metadata.extension,
        labels=tuple(dict.fromkeys(labels)),
        needs_review=bool(labels),
        collision_adjusted=unique_target != target,
    )


def _filename(settings: Settings, metadata: TrackMetadata) -> str:
    parts = [safe_path_part(metadata.artist or "Unknown Artist"), safe_path_part(metadata.title or "Unknown Title")]
    bpm = _format_bpm(metadata.bpm, settings.bpm_format)
    if bpm:
        parts.append(safe_path_part(bpm))
    if metadata.camelot_key:
        parts.append(safe_path_part(metadata.camelot_key))
    stem = shorten_filename_stem(" - ".join(parts))
    return f"{stem}.{metadata.extension.lower()}"


def _format_bpm(value: str | None, bpm_format: str) -> str | None:
    if not value:
        return None
    value = value.strip()
    if bpm_format == "preserve":
        return value

    match = re.search(r"\d+(?:\.\d+)?", value)
    if not match:
        return None
    bpm = float(match.group(0))
    if bpm_format == "one_decimal":
        return f"{bpm:.1f}"
    return str(round(bpm))
