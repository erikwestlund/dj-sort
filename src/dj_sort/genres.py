from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from dj_sort.paths import normalize_key, normalize_text


@dataclass(frozen=True)
class GenreResolution:
    raw_genre: str | None
    canonical_genre: str | None
    mapped: bool
    missing: bool


class GenreMap:
    def __init__(self, mappings: dict[str, str] | None = None) -> None:
        self._mappings = mappings or {}
        self._normalized = {normalize_key(alias): canonical for alias, canonical in self._mappings.items()}

    @classmethod
    def load(cls, path: Path) -> GenreMap:
        if not path.exists():
            return cls({})
        with path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
        mappings = raw.get("genres", {}) or {}
        return cls({str(alias): str(canonical) for alias, canonical in mappings.items() if canonical})

    @property
    def mappings(self) -> dict[str, str]:
        return dict(self._mappings)

    def resolve(self, raw_genre: str | None, missing_fallback: str) -> GenreResolution:
        if raw_genre is None or not normalize_text(raw_genre):
            return GenreResolution(
                raw_genre=None,
                canonical_genre=missing_fallback,
                mapped=False,
                missing=True,
            )

        cleaned = normalize_text(raw_genre)
        mapped = self._normalized.get(normalize_key(cleaned))
        return GenreResolution(
            raw_genre=cleaned,
            canonical_genre=mapped or cleaned,
            mapped=mapped is not None,
            missing=False,
        )

    def canonical_for_report(self, raw_genre: str | None, missing_fallback: str) -> str:
        return self.resolve(raw_genre, missing_fallback).canonical_genre or missing_fallback
