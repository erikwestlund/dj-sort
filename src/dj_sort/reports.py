from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from dj_sort.genres import GenreMap
from dj_sort.metadata import read_metadata
from dj_sort.paths import normalize_key
from dj_sort.planning import PlanningResult, scan_audio_files
from dj_sort.settings import Settings


@dataclass
class GenreBucket:
    raw_genre: str
    normalized_genre: str
    canonical_genre: str
    count: int = 0
    examples: list[str] = field(default_factory=list)
    mapped: bool = False
    missing: bool = False


def render_planning_result(result: PlanningResult, output_format: str) -> str:
    if output_format == "json":
        return json.dumps(result.to_dict(), indent=2, sort_keys=True)
    if output_format == "yaml":
        return yaml.safe_dump(result.to_dict(), sort_keys=False)
    return _render_planning_text(result)


def add_report_metadata(data: dict[str, Any], elapsed_seconds: float | None = None) -> dict[str, Any]:
    data = dict(data)
    data.setdefault("generated_at", datetime.now(UTC).isoformat(timespec="seconds"))
    if elapsed_seconds is not None:
        summary = dict(data.get("summary", {}))
        summary["elapsed_seconds"] = round(elapsed_seconds, 3)
        data["summary"] = summary
    return data


def discover_genres(
    settings: Settings,
    genre_map: GenreMap,
    limit: int | None = None,
    source_dir: Path | None = None,
) -> dict[str, Any]:
    paths, skipped = scan_audio_files(source_dir or settings.unprocessed_music_dir, settings.recursive, limit)
    max_examples = settings.genre_discovery.max_examples_per_genre
    buckets: dict[str, GenreBucket] = {}
    errors: list[dict[str, str]] = []

    for path in paths:
        try:
            metadata = read_metadata(path)
            resolution = genre_map.resolve(metadata.genre, settings.missing_genre_dir)
            raw = resolution.raw_genre if not resolution.missing else "<missing>"
            key = normalize_key(raw)
            bucket = buckets.setdefault(
                key,
                GenreBucket(
                    raw_genre=raw,
                    normalized_genre=key,
                    canonical_genre=resolution.canonical_genre or settings.missing_genre_dir,
                    mapped=resolution.mapped,
                    missing=resolution.missing,
                ),
            )
            bucket.count += 1
            if len(bucket.examples) < max_examples:
                bucket.examples.append(str(path))
        except Exception as exc:  # noqa: BLE001 - report and continue
            errors.append({"path": str(path), "reason": str(exc)})

    genres = sorted((bucket.__dict__ for bucket in buckets.values()), key=lambda row: (-row["count"], row["raw_genre"]))
    return {
        "report_type": "genre_discovery",
        "genres": genres,
        "skipped": [item.to_dict() for item in skipped],
        "errors": errors,
        "summary": {
            "audio_files_scanned": len(paths),
            "unique_genres": len(genres),
            "missing_genre_files": sum(row["count"] for row in genres if row["missing"]),
            "metadata_errors": len(errors),
            "unsupported_files": len(skipped),
        },
    }


def render_genre_discovery(data: dict[str, Any], output_format: str) -> str:
    if output_format == "json":
        return json.dumps(data, indent=2, sort_keys=True)
    if output_format == "yaml":
        return yaml.safe_dump(
            {"genres": {row["raw_genre"]: None if not row["mapped"] else row["canonical_genre"] for row in data["genres"] if not row["missing"]}},
            sort_keys=True,
        )
    return _render_genre_text(data)


def potential_duplicate_summary(plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for plan in plans:
        grouped[(str(plan["normalized_artist"]), str(plan["normalized_title"]))].append(plan)
    return [
        {"artist": artist, "title": title, "count": len(items), "paths": [item["target_path"] for item in items]}
        for (artist, title), items in grouped.items()
        if len(items) > 1
    ]


def _render_planning_text(result: PlanningResult) -> str:
    lines = ["DJ Sort plan", "", f"Planned files: {len(result.plans)}", f"Skipped files: {len(result.skipped)}"]
    if result.plans:
        lines.append("")
        lines.append("Planned actions:")
        for plan in result.plans:
            labels = f" [{', '.join(plan.labels)}]" if plan.labels else ""
            lines.append(f"- {plan.source_path} -> {plan.target_path}{labels}")
    if result.skipped:
        lines.append("")
        lines.append("Skipped:")
        counts = Counter(item.reason for item in result.skipped)
        for reason, count in sorted(counts.items()):
            lines.append(f"- {reason}: {count}")
    return "\n".join(lines)


def append_elapsed(rendered: str, elapsed_seconds: float | None) -> str:
    if elapsed_seconds is None:
        return rendered
    return f"{rendered}\n\nElapsed: {elapsed_seconds:.3f}s"


def _render_genre_text(data: dict[str, Any]) -> str:
    lines = ["Raw Genre | Count | Canonical | Mapped", "--- | ---: | --- | ---"]
    for row in data["genres"]:
        mapped = "yes" if row["mapped"] else "no"
        lines.append(f"{row['raw_genre']} | {row['count']} | {row['canonical_genre']} | {mapped}")
    lines.append("")
    summary = data["summary"]
    lines.append(
        f"Scanned {summary['audio_files_scanned']} audio files, found {summary['unique_genres']} unique genres."
    )
    return "\n".join(lines)


def write_or_print(rendered: str, output: Path | None) -> None:
    if output is None:
        print(rendered)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered + "\n", encoding="utf-8")
