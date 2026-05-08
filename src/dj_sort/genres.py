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
    def __init__(self, mappings: dict[str, str] | None = None, whitelist: set[str] | None = None) -> None:
        self._mappings = mappings or {}
        self._normalized = {normalize_key(alias): canonical for alias, canonical in self._mappings.items()}
        canonical_whitelist = {
            normalize_key(cleaned)
            for canonical in self._mappings.values()
            if (cleaned := normalize_text(canonical))
        }
        explicit_whitelist = {
            normalize_key(cleaned)
            for genre in (whitelist or set())
            if (cleaned := normalize_text(genre))
        }
        self._whitelist = canonical_whitelist | explicit_whitelist

    @classmethod
    def load(cls, path: Path) -> GenreMap:
        if not path.exists():
            return cls({})
        with path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
        raw_mappings = raw.get("genres", {}) or {}
        mappings: dict[str, str] = {}
        whitelist: set[str] = set()
        for alias, canonical in raw_mappings.items():
            cleaned_alias = normalize_text(str(alias))
            if not cleaned_alias:
                continue
            cleaned_canonical = normalize_text(str(canonical)) if canonical is not None else None
            if cleaned_canonical:
                mappings[cleaned_alias] = cleaned_canonical
                continue
            whitelist.add(cleaned_alias)
        return cls(mappings, whitelist)

    @property
    def mappings(self) -> dict[str, str]:
        return dict(self._mappings)

    @property
    def whitelist(self) -> set[str]:
        return set(self._whitelist)

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

    def is_whitelisted(self, canonical_genre: str | None) -> bool:
        if not self._whitelist:
            return True
        if canonical_genre is None or not normalize_text(canonical_genre):
            return False
        return normalize_key(canonical_genre) in self._whitelist
