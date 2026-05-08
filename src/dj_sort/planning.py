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
    raw_genre: str | None = None
    canonical_genre: str | None = None
    duration_ms: int | None = None
    notes: str | None = None

    def to_dict(self) -> dict[str, str | int | None]:
        return {
            "path": str(self.path),
            "reason": self.reason,
            "raw_genre": self.raw_genre,
            "canonical_genre": self.canonical_genre,
            "duration_ms": self.duration_ms,
            "notes": self.notes,
        }


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
            genre = genre_map.resolve(metadata.genre, settings.unknown_genre_dir)
            skipped_file = _skip_for_filters(settings, genre_map, metadata, genre)
            if skipped_file is not None:
                skipped.append(skipped_file)
                continue
            plans.append(_build_plan(settings, metadata, occupied, genre, genre_map.is_whitelisted(genre.canonical_genre)))
        except Exception as exc:  # noqa: BLE001 - per-file failures should not abort planning
            skipped.append(SkippedFile(path=path, reason=f"metadata_error: {exc}"))

    return PlanningResult(plans=plans, skipped=skipped)


def _build_plan(
    settings: Settings,
    metadata: TrackMetadata,
    occupied: set[Path],
    genre,
    is_curated_genre: bool,
) -> Plan:
    labels = list(metadata.labels)
    if genre.missing:
        labels.append("Needs Genre")
    if not genre.missing and not is_curated_genre:
        labels.append("Uncurated Genre")

    safe_genre = safe_path_part(_target_genre_dir(settings, genre, is_curated_genre))
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


def _skip_for_filters(
    settings: Settings,
    genre_map: GenreMap,
    metadata: TrackMetadata,
    genre,
) -> SkippedFile | None:
    matched_blacklist = _matched_blacklist_substring(settings, metadata, genre.canonical_genre)
    if matched_blacklist is not None:
        return SkippedFile(
            path=metadata.path,
            reason="blacklist_substring",
            raw_genre=genre.raw_genre,
            canonical_genre=genre.canonical_genre,
            duration_ms=metadata.duration_ms,
            notes=f"matched blacklist substring: {matched_blacklist}",
        )

    if settings.max_duration_minutes is None or metadata.duration_ms is None:
        return None

    max_duration_ms = int(settings.max_duration_minutes * 60 * 1000)
    if metadata.duration_ms > max_duration_ms:
        return SkippedFile(
            path=metadata.path,
            reason="duration_exceeds_max",
            raw_genre=genre.raw_genre,
            canonical_genre=genre.canonical_genre,
            duration_ms=metadata.duration_ms,
        )
    return None


def _matched_blacklist_substring(
    settings: Settings,
    metadata: TrackMetadata,
    canonical_genre: str | None,
) -> str | None:
    haystacks = [
        str(metadata.path),
        metadata.path.name,
        metadata.artist or "",
        metadata.title or "",
        metadata.album or "",
        metadata.album_artist or "",
        metadata.genre or "",
        canonical_genre or "",
    ]
    searchable_text = "\n".join(part.casefold() for part in haystacks if part)
    for phrase in settings.blacklist_substrings:
        cleaned = phrase.strip()
        if cleaned and cleaned.casefold() in searchable_text:
            return cleaned
    return None


def _target_genre_dir(settings: Settings, genre, is_curated_genre: bool) -> str:
    if genre.missing:
        return genre.canonical_genre or settings.unknown_genre_dir
    if not is_curated_genre:
        return settings.uncurated_genre_dir
    return genre.canonical_genre or settings.unknown_genre_dir


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
