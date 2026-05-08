from __future__ import annotations

import shutil
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

ReportFormat = Literal["text", "json", "yaml"]
BpmFormat = Literal["integer", "one_decimal", "preserve"]
KeyFormat = Literal["camelot"]
DuplicatePolicy = Literal["exact_only", "potential_too", "report_only"]


class BinaryPaths(BaseModel):
    ffmpeg: Path | None = None
    ffprobe: Path | None = None
    fpcalc: Path | None = None


class GenreDiscoverySettings(BaseModel):
    max_examples_per_genre: int = Field(default=5, ge=0)
    include_missing_genre: bool = True
    output_format: ReportFormat = "text"


class GenreConsolidationSettings(BaseModel):
    mappings: dict[str, str] = Field(default_factory=dict)


class Settings(BaseModel):
    unprocessed_music_dir: Path
    dj_library_dir: Path = Field(default=Path("~/Music/DJ Library"), validate_default=True)
    uncategorizable_dir: Path = Field(default=Path("~/Music/DJ Uncategorizable"), validate_default=True)
    duplicates_dir: Path = Field(default=Path("~/Music/DJ Duplicates"), validate_default=True)
    database_path: Path = Path("~/.dj-sort/library.sqlite3")
    genre_map_path: Path = Path("./genres.yaml")
    binary_paths: BinaryPaths = Field(default_factory=BinaryPaths)
    genre_discovery: GenreDiscoverySettings = Field(default_factory=GenreDiscoverySettings)
    recursive: bool = True
    dry_run: bool = True
    limit: int | None = Field(default=None, ge=1)
    detect_potential_duplicates: bool = True
    duplicate_policy: DuplicatePolicy = "exact_only"
    strict: bool = False
    bpm_format: BpmFormat = "integer"
    key_format: KeyFormat = "camelot"
    missing_genre_dir: str = "Missing Genre"
    unmapped_genre_dir: str = "Unmapped Genre"
    needs_review_dir: str = "Needs Review"
    too_long_dir: str = "Too Long"
    blacklisted_dir: str = "Blacklisted"
    write_canonical_genre_to_metadata: bool = True
    preserve_original_genre_in_comment: bool = True
    original_genre_comment_prefix: str = "dj-sort original genre:"
    omit_missing_filename_parts: bool = True
    filename_template: str = "{artist} - {title} - {bpm} - {key}.{ext}"
    max_duration_minutes: float | None = Field(default=15, gt=0)
    blacklist_substrings: list[str] = Field(default_factory=list)
    remove_empty_genre_dirs: bool = False
    genre_consolidation: GenreConsolidationSettings = Field(
        default_factory=GenreConsolidationSettings
    )

    @field_validator(
        "unprocessed_music_dir",
        "dj_library_dir",
        "uncategorizable_dir",
        "duplicates_dir",
        "database_path",
        "genre_map_path",
        mode="before",
    )
    @classmethod
    def expand_paths(cls, value: str | Path) -> Path:
        return Path(value).expanduser()


class ResolvedBinaries(BaseModel):
    ffmpeg: Path | None = None
    ffprobe: Path | None = None
    fpcalc: Path | None = None


def load_settings(path: Path) -> Settings:
    if not path.exists():
        raise FileNotFoundError(f"Settings file not found: {path}")

    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    try:
        return Settings.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"Invalid settings in {path}:\n{exc}") from exc


def resolve_binaries(settings: Settings) -> ResolvedBinaries:
    return ResolvedBinaries(
        ffmpeg=_resolve_binary("ffmpeg", settings.binary_paths.ffmpeg),
        ffprobe=_resolve_binary("ffprobe", settings.binary_paths.ffprobe),
        fpcalc=_resolve_binary("fpcalc", settings.binary_paths.fpcalc),
    )


def _resolve_binary(name: str, configured_path: Path | None) -> Path | None:
    if configured_path is not None:
        expanded = configured_path.expanduser()
        if not expanded.exists():
            raise FileNotFoundError(f"Configured binary for {name} does not exist: {expanded}")
        if not expanded.is_file():
            raise ValueError(f"Configured binary for {name} is not a file: {expanded}")
        return expanded

    discovered = shutil.which(name)
    return Path(discovered) if discovered else None
