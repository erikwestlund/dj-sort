from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path
from time import perf_counter
from typing import Annotated

import typer
import yaml

from dj_sort.consolidation import consolidate_genres
from dj_sort.database import connect, duplicate_report, excluded_report, initialize
from dj_sort.genres import GenreMap
from dj_sort.metadata import read_metadata, write_genre
from dj_sort.paths import normalize_key
from dj_sort.planning import PlanningResult, build_plan_for_file, build_plans, scan_audio_files
from dj_sort.processing import process_plans
from dj_sort.reports import add_report_metadata, append_elapsed, discover_genres, render_genre_discovery, render_planning_result, write_or_print
from dj_sort.settings import Settings, load_settings, resolve_binaries

app = typer.Typer(help="DJ library pre-processor")
genres_app = typer.Typer(help="Genre discovery and consolidation commands")
app.add_typer(genres_app, name="genres")


SettingsPath = Annotated[Path, typer.Option("--settings", help="Path to settings.yaml")]
OptionalPath = Annotated[Path | None, typer.Option(help="Optional path override")]
OutputFormat = Annotated[str, typer.Option("--format", help="text, json, or yaml")]


@app.callback()
def main() -> None:
    """Organize source audio dumps into a managed DJ library."""


@app.command()
def diagnostics(settings_path: SettingsPath = Path("settings.yaml")) -> None:
    """Show settings and external binary diagnostics."""
    settings = _load(settings_path)
    binaries = resolve_binaries(settings)
    typer.echo("DJ Sort diagnostics")
    typer.echo(f"unprocessed_music_dir: {settings.unprocessed_music_dir}")
    typer.echo(f"dj_library_dir: {settings.dj_library_dir}")
    typer.echo(f"uncategorizable_dir: {settings.uncategorizable_dir}")
    typer.echo(f"duplicates_dir: {settings.duplicates_dir}")
    typer.echo(f"database_path: {settings.database_path}")
    typer.echo(f"genre_map_path: {settings.genre_map_path}")
    typer.echo(f"ffmpeg: {binaries.ffmpeg or '<not found>'}")
    typer.echo(f"ffprobe: {binaries.ffprobe or '<not found>'}")
    typer.echo(f"fpcalc: {binaries.fpcalc or '<not found>'}")


@app.command()
def scan(
    settings_path: SettingsPath = Path("settings.yaml"),
    unprocessed_music_dir: Annotated[Path | None, typer.Option("--unprocessed-music-dir", help="Override unprocessed music directory")] = None,
    limit: Annotated[int | None, typer.Option("--limit", help="Limit candidate audio files")] = None,
    output_format: OutputFormat = "text",
    output: Annotated[Path | None, typer.Option("--output", help="Write report to file")] = None,
) -> None:
    """Plan source files without writing anything."""
    start = perf_counter()
    _validate_output_format(output_format)
    settings = _load(settings_path, unprocessed_music_dir=unprocessed_music_dir, limit=limit)
    genre_map = GenreMap.load(settings.genre_map_path)
    result = build_plans(settings, genre_map, limit=settings.limit)
    elapsed = perf_counter() - start
    rendered = _render_structured_or_text(
        result.to_dict(),
        render_planning_result(result, "text"),
        output_format,
        elapsed,
    )
    write_or_print(rendered, output)


@app.command()
def process(
    settings_path: SettingsPath = Path("settings.yaml"),
    unprocessed_music_dir: Annotated[Path | None, typer.Option("--unprocessed-music-dir", help="Override unprocessed music directory")] = None,
    dj_library_dir: Annotated[Path | None, typer.Option("--dj-library-dir", help="Override DJ library directory")] = None,
    uncategorizable_dir: Annotated[Path | None, typer.Option("--uncategorizable-dir", help="Override uncategorizable directory")] = None,
    duplicates_dir: Annotated[Path | None, typer.Option("--duplicates-dir", help="Override duplicates directory")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Force dry-run mode")] = False,
    write: Annotated[bool, typer.Option("--write", help="Write filesystem and DB changes")] = False,
    limit: Annotated[int | None, typer.Option("--limit", help="Limit candidate audio files")] = None,
    output_format: OutputFormat = "text",
    output: Annotated[Path | None, typer.Option("--output", help="Write report to file")] = None,
) -> None:
    """Dry-run or process files into the managed library."""
    start = perf_counter()
    _validate_output_format(output_format)
    settings = _load(
        settings_path,
        unprocessed_music_dir=unprocessed_music_dir,
        dj_library_dir=dj_library_dir,
        uncategorizable_dir=uncategorizable_dir,
        duplicates_dir=duplicates_dir,
        limit=limit,
    )
    effective_dry_run = True if dry_run else settings.dry_run
    if write:
        effective_dry_run = False

    genre_map = GenreMap.load(settings.genre_map_path)
    planning_result = build_plans(settings, genre_map, limit=settings.limit)
    if effective_dry_run:
        elapsed = perf_counter() - start
        rendered = _render_structured_or_text(
            planning_result.to_dict(),
            render_planning_result(planning_result, "text"),
            output_format,
            elapsed,
        )
        write_or_print(rendered, output)
        return

    result = process_plans(settings, planning_result)
    elapsed = perf_counter() - start
    rendered = _render_structured_or_text(result.to_dict(), _render_process_text(result), output_format, elapsed)
    write_or_print(rendered, output)


@app.command("transfer")
def transfer(
    path: Annotated[Path, typer.Argument(help="Path to transfer, relative to the selected base directory unless absolute")] = Path("."),
    settings_path: SettingsPath = Path("settings.yaml"),
    base_dir: Annotated[
        str | None,
        typer.Option("--base-dir", help="Base directory: relative to unprocessed, absolute, or dj_library/uncategorizable/duplicates"),
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Force dry-run mode")] = False,
    write: Annotated[bool, typer.Option("--write", help="Write filesystem and DB changes")] = False,
    force_genre: Annotated[str | None, typer.Option("--force-genre", help="Route every file in this transfer as this genre")] = None,
    limit: Annotated[int | None, typer.Option("--limit", help="Limit candidate audio files")] = None,
    output_format: OutputFormat = "text",
    output: Annotated[Path | None, typer.Option("--output", help="Write report to file")] = None,
) -> None:
    """Dry-run or transfer files from a specific directory."""
    start = perf_counter()
    _validate_output_format(output_format)
    force_genre = _clean_force_genre(force_genre)
    settings = _load(settings_path, limit=limit)
    source_dir = _resolve_scan_dir(settings, base_dir, path)
    settings = settings.model_copy(update={"unprocessed_music_dir": source_dir})
    effective_dry_run = True if dry_run else settings.dry_run
    if write:
        effective_dry_run = False

    genre_map = GenreMap.load(settings.genre_map_path)
    planning_result = build_plans(settings, genre_map, limit=settings.limit, genre_override=force_genre)
    if effective_dry_run:
        elapsed = perf_counter() - start
        rendered = _render_structured_or_text(
            planning_result.to_dict(),
            render_planning_result(planning_result, "text"),
            output_format,
            elapsed,
        )
        write_or_print(rendered, output)
        return

    result = process_plans(settings, planning_result)
    elapsed = perf_counter() - start
    rendered = _render_structured_or_text(result.to_dict(), _render_process_text(result), output_format, elapsed)
    write_or_print(rendered, output)


@app.command("export-uncategorizable")
def export_uncategorizable(
    path: Annotated[Path, typer.Argument(help="Path to export, relative to the selected base directory unless absolute")] = Path("."),
    settings_path: SettingsPath = Path("settings.yaml"),
    output: Annotated[Path | None, typer.Option("--output", help="Write CSV to this path")] = None,
    base_dir: Annotated[
        str | None,
        typer.Option("--base-dir", help="Base directory: relative to unprocessed, absolute, or dj_library/uncategorizable/duplicates"),
    ] = None,
    limit: Annotated[int | None, typer.Option("--limit", help="Limit candidate audio files")] = None,
) -> None:
    """Export uncategorizable transfer rows to a CSV with an update_genre column."""
    settings = _load(settings_path, limit=limit)
    source_dir = _resolve_scan_dir(settings, base_dir, path)
    settings = settings.model_copy(update={"unprocessed_music_dir": source_dir})
    genre_map = GenreMap.load(settings.genre_map_path)
    planning_result = build_plans(settings, genre_map, limit=settings.limit)
    rows = _uncategorizable_csv_rows(settings, planning_result.plans)
    output = output or _default_uncategorizable_csv_path(path)
    _write_uncategorizable_csv(output, rows)
    typer.echo(f"Exported {len(rows)} uncategorizable rows to {output}")


@app.command("apply-genre-updates")
def apply_genre_updates(
    csv_path: Annotated[Path, typer.Argument(help="CSV created by export-uncategorizable")],
    settings_path: SettingsPath = Path("settings.yaml"),
    write: Annotated[bool, typer.Option("--write", help="Write genre updates to source files")] = False,
) -> None:
    """Apply edited update_genre values from an uncategorizable CSV."""
    settings = _load(settings_path)
    try:
        csv_path = _resolve_genre_update_csv_path(csv_path)
        rows = _read_genre_update_rows(csv_path)
    except Exception as exc:  # noqa: BLE001 - CLI should present concise CSV errors
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    genre_map = GenreMap.load(settings.genre_map_path)
    updates = [row for row in rows if _needs_genre_update(settings, genre_map, row)]
    if not write:
        typer.echo(f"Would update {len(updates)} files. Re-run with --write to apply.")
        for row in updates:
            typer.echo(f"- {row['source_path']}: {row['raw_genre'] or '<missing>'} -> {row['update_genre']}")
        return

    errors = []
    updated = 0
    moved = 0
    skipped = 0
    for row in updates:
        source_path = Path(row["source_path"])
        try:
            result = _apply_genre_update_row(settings, genre_map, row)
            updated += 1
            if result == "moved":
                moved += 1
            elif result == "skipped":
                skipped += 1
        except Exception as exc:  # noqa: BLE001 - continue applying independent CSV rows
            errors.append((source_path, exc))
    typer.echo(f"Updated {updated} source files from {csv_path}")
    typer.echo(f"Moved to new destinations: {moved}")
    typer.echo(f"Skipped re-transfer: {skipped}")
    if errors:
        typer.echo(f"Errors: {len(errors)}", err=True)
        for source_path, exc in errors:
            typer.echo(f"- {source_path}: {exc}", err=True)
        raise typer.Exit(code=1)


@app.command("copy-duplicate-genres")
def copy_duplicate_genres(
    path: Annotated[Path, typer.Argument(help="Target path to update, relative to the selected base directory unless absolute")] = Path("."),
    settings_path: SettingsPath = Path("settings.yaml"),
    from_base: Annotated[
        str,
        typer.Option("--from-base", help="Duplicate source directory, relative to unprocessed unless absolute/reserved"),
    ] = "iTunes Library",
    base_dir: Annotated[
        str | None,
        typer.Option("--base-dir", help="Target base directory: relative to unprocessed, absolute, or reserved"),
    ] = None,
    write: Annotated[bool, typer.Option("--write", help="Write genre updates to target source files")] = False,
    limit: Annotated[int | None, typer.Option("--limit", help="Limit target candidate audio files")] = None,
) -> None:
    """Copy curated genres from exact artist/title duplicates into target files."""
    settings = _load(settings_path, limit=limit)
    genre_map = GenreMap.load(settings.genre_map_path)
    target_dir = _resolve_scan_dir(settings, base_dir, path)
    duplicate_dir = _resolve_base_dir(settings, from_base)
    result = _duplicate_genre_updates(settings, genre_map, target_dir, duplicate_dir)

    if not write:
        _render_duplicate_genre_update_preview(result)
        return

    errors = []
    updated = 0
    for update in result["updates"]:
        try:
            write_genre(
                Path(update["target_path"]),
                str(update["new_genre"]),
                original_genre=str(update["old_genre"] or "") or None,
                original_genre_comment_prefix=settings.original_genre_comment_prefix,
            )
            updated += 1
        except Exception as exc:  # noqa: BLE001 - continue applying independent duplicate matches
            errors.append((update["target_path"], exc))

    typer.echo(f"Updated {updated} files from duplicate genres")
    typer.echo(f"Skipped conflicts: {len(result['conflicts'])}")
    typer.echo(f"Skipped no match: {result['skipped_no_match']}")
    typer.echo(f"Skipped non-curated: {result['skipped_non_curated']}")
    if errors:
        typer.echo(f"Errors: {len(errors)}", err=True)
        for target_path, exc in errors:
            typer.echo(f"- {target_path}: {exc}", err=True)
        raise typer.Exit(code=1)


@app.command()
def duplicates(
    settings_path: SettingsPath = Path("settings.yaml"),
    output_format: OutputFormat = "text",
    output: Annotated[Path | None, typer.Option("--output", help="Write report to file")] = None,
) -> None:
    """Report exact and potential duplicate groups from SQLite."""
    start = perf_counter()
    _validate_output_format(output_format)
    settings = _load(settings_path)
    connection = connect(settings.database_path)
    initialize(connection)
    report = duplicate_report(connection)
    connection.close()
    elapsed = perf_counter() - start
    rendered = _render_structured_or_text(report, _render_duplicate_text(report), output_format, elapsed)
    write_or_print(rendered, output)


@app.command()
def excluded(
    settings_path: SettingsPath = Path("settings.yaml"),
    reason: Annotated[str | None, typer.Option("--reason", help="Filter by exclusion reason")] = None,
    output_format: OutputFormat = "text",
    output: Annotated[Path | None, typer.Option("--output", help="Write report to file")] = None,
) -> None:
    """Report files excluded from import by first-pass filters."""
    start = perf_counter()
    _validate_output_format(output_format)
    settings = _load(settings_path)
    connection = connect(settings.database_path)
    initialize(connection)
    report = excluded_report(connection, reason=reason)
    connection.close()
    elapsed = perf_counter() - start
    rendered = _render_structured_or_text(report, _render_excluded_text(report), output_format, elapsed)
    write_or_print(rendered, output)


@genres_app.command("discover")
def genres_discover(
    settings_path: SettingsPath = Path("settings.yaml"),
    unprocessed_music_dir: Annotated[Path | None, typer.Option("--unprocessed-music-dir", help="Override unprocessed music directory")] = None,
    limit: Annotated[int | None, typer.Option("--limit", help="Limit candidate audio files")] = None,
    output_format: OutputFormat = "text",
    output: Annotated[Path | None, typer.Option("--output", help="Write report to file")] = None,
) -> None:
    """Recursively discover unique raw genre metadata values."""
    start = perf_counter()
    _validate_output_format(output_format)
    settings = _load(settings_path, unprocessed_music_dir=unprocessed_music_dir, limit=limit)
    genre_map = GenreMap.load(settings.genre_map_path)
    data = discover_genres(settings, genre_map, limit=settings.limit)
    elapsed = perf_counter() - start
    if output_format == "text":
        rendered = append_elapsed(render_genre_discovery(data, output_format), elapsed)
    else:
        rendered = render_genre_discovery(add_report_metadata(data, elapsed), output_format)
    write_or_print(rendered, output)


@app.command("get-genres")
def get_genres(
    path: Annotated[Path, typer.Argument(help="Path to scan, relative to the selected base directory unless absolute")] = Path("."),
    settings_path: SettingsPath = Path("settings.yaml"),
    base_dir: Annotated[
        str | None,
        typer.Option("--base-dir", help="Base directory: relative to unprocessed, absolute, or dj_library/uncategorizable/duplicates"),
    ] = None,
    limit: Annotated[int | None, typer.Option("--limit", help="Limit candidate audio files")] = None,
    output_format: OutputFormat = "text",
    output: Annotated[Path | None, typer.Option("--output", help="Write report to file")] = None,
) -> None:
    """Discover unique raw genre metadata values under a directory."""
    start = perf_counter()
    _validate_output_format(output_format)
    settings = _load(settings_path, limit=limit)
    source_dir = _resolve_scan_dir(settings, base_dir, path)
    genre_map = GenreMap.load(settings.genre_map_path)
    data = discover_genres(settings, genre_map, limit=settings.limit, source_dir=source_dir)
    elapsed = perf_counter() - start
    if output_format == "text":
        rendered = append_elapsed(render_genre_discovery(data, output_format), elapsed)
    else:
        rendered = render_genre_discovery(add_report_metadata(data, elapsed), output_format)
    write_or_print(rendered, output)


@genres_app.command("consolidate")
def genres_consolidate(
    settings_path: SettingsPath = Path("settings.yaml"),
    from_genre: Annotated[str | None, typer.Option("--from", help="Source genre name")] = None,
    to_genre: Annotated[str | None, typer.Option("--to", help="Target canonical genre name")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Force dry-run mode")] = False,
    write: Annotated[bool, typer.Option("--write", help="Apply consolidation")] = False,
    limit: Annotated[int | None, typer.Option("--limit", help="Limit songs to consolidate")] = None,
    output_format: OutputFormat = "text",
    output: Annotated[Path | None, typer.Option("--output", help="Write report to file")] = None,
) -> None:
    """Consolidate already-managed files from one genre to another."""
    start = perf_counter()
    _validate_output_format(output_format)
    settings = _load(settings_path, limit=limit)
    mappings = dict(settings.genre_consolidation.mappings)
    if from_genre or to_genre:
        if not from_genre or not to_genre:
            typer.echo("Error: --from and --to must be provided together", err=True)
            raise typer.Exit(code=1)
        mappings = {from_genre: to_genre}
    if not mappings:
        typer.echo("Error: no consolidation mappings provided", err=True)
        raise typer.Exit(code=1)
    effective_dry_run = True if dry_run else settings.dry_run
    if write:
        effective_dry_run = False
    result = consolidate_genres(settings, mappings, dry_run=effective_dry_run, limit=settings.limit)
    elapsed = perf_counter() - start
    rendered = _render_structured_or_text(
        result.to_dict(),
        _render_consolidation_text(result),
        output_format,
        elapsed,
    )
    write_or_print(rendered, output)


def _validate_output_format(output_format: str) -> None:
    if output_format not in {"text", "json", "yaml"}:
        typer.echo(f"Error: unsupported output format: {output_format}", err=True)
        raise typer.Exit(code=1)


def _clean_force_genre(force_genre: str | None) -> str | None:
    if force_genre is None:
        return None
    cleaned = force_genre.strip()
    if cleaned:
        return cleaned
    typer.echo("Error: --force-genre cannot be blank", err=True)
    raise typer.Exit(code=1)


def _render_structured_or_text(
    payload: dict[str, object],
    text: str,
    output_format: str,
    elapsed_seconds: float,
) -> str:
    if output_format == "json":
        return json.dumps(add_report_metadata(payload, elapsed_seconds), indent=2, sort_keys=True)
    if output_format == "yaml":
        return yaml.safe_dump(add_report_metadata(payload, elapsed_seconds), sort_keys=False)
    return append_elapsed(text, elapsed_seconds)


def _load(
    settings_path: Path,
    unprocessed_music_dir: Path | None = None,
    dj_library_dir: Path | None = None,
    uncategorizable_dir: Path | None = None,
    duplicates_dir: Path | None = None,
    limit: int | None = None,
) -> Settings:
    try:
        settings = load_settings(settings_path)
        updates = {}
        if unprocessed_music_dir is not None:
            updates["unprocessed_music_dir"] = unprocessed_music_dir.expanduser()
        if dj_library_dir is not None:
            updates["dj_library_dir"] = dj_library_dir.expanduser()
        if uncategorizable_dir is not None:
            updates["uncategorizable_dir"] = uncategorizable_dir.expanduser()
        if duplicates_dir is not None:
            updates["duplicates_dir"] = duplicates_dir.expanduser()
        if limit is not None:
            updates["limit"] = limit
        if updates:
            settings = settings.model_copy(update=updates)
        resolve_binaries(settings)
        return settings
    except Exception as exc:  # noqa: BLE001 - CLI should present concise setup errors
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


def _resolve_scan_dir(settings: Settings, base_dir: str | None, path: Path) -> Path:
    base = _resolve_base_dir(settings, base_dir)
    expanded_path = path.expanduser()
    if expanded_path.is_absolute():
        return expanded_path
    return base / expanded_path


def _resolve_base_dir(settings: Settings, base_dir: str | None) -> Path:
    if base_dir is None or not base_dir.strip():
        return settings.unprocessed_music_dir

    reserved = {
        "unprocessed": settings.unprocessed_music_dir,
        "unprocessed_music": settings.unprocessed_music_dir,
        "dj_library": settings.dj_library_dir,
        "uncategorizable": settings.uncategorizable_dir,
        "duplicates": settings.duplicates_dir,
    }
    normalized = base_dir.strip().casefold().replace("-", "_")
    if normalized in reserved:
        return reserved[normalized]

    candidate = Path(base_dir).expanduser()
    if candidate.is_absolute():
        return candidate
    return settings.unprocessed_music_dir / candidate


UNCATEGORIZABLE_CSV_FIELDS = [
    "update_genre",
    "raw_genre",
    "canonical_genre",
    "reason",
    "artist",
    "title",
    "bpm",
    "key",
    "duration_ms",
    "bitrate",
    "extension",
    "source_path",
    "target_path",
]


def _default_uncategorizable_csv_path(path: Path) -> Path:
    return Path("reports") / "uncategorizable" / f"{_csv_report_stem(path)}.csv"


def _csv_report_stem(path: Path) -> str:
    name = path.name
    if name in {"", "."}:
        return "uncategorizable"
    if name.endswith(".csv"):
        return name[:-4]
    return name


def _resolve_genre_update_csv_path(csv_path: Path) -> Path:
    expanded = csv_path.expanduser()
    variants = [expanded]
    if expanded.suffix != ".csv":
        variants.append(expanded.with_name(f"{expanded.name}.csv"))
    candidates = []
    for variant in variants:
        candidates.append(variant)
        if not variant.is_absolute():
            candidates.extend(
                [
                    Path("reports") / variant,
                    Path("reports") / "uncategorizable" / variant,
                ]
            )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    searched = ", ".join(str(candidate) for candidate in candidates)
    raise FileNotFoundError(f"CSV not found. Looked in: {searched}")


def _uncategorizable_csv_rows(settings: Settings, plans) -> list[dict[str, object]]:
    rows = []
    uncategorizable_dir = settings.uncategorizable_dir.resolve()
    for plan in plans:
        try:
            plan.target_path.resolve().relative_to(uncategorizable_dir)
        except ValueError:
            continue
        rows.append(
            {
                "update_genre": "",
                "raw_genre": plan.raw_genre or "",
                "canonical_genre": plan.canonical_genre,
                "reason": ", ".join(plan.labels),
                "artist": plan.artist,
                "title": plan.title,
                "bpm": plan.bpm or "",
                "key": plan.camelot_key or "",
                "duration_ms": plan.duration_ms or "",
                "bitrate": plan.bitrate or "",
                "extension": plan.extension,
                "source_path": str(plan.source_path),
                "target_path": str(plan.target_path),
            }
        )
    return rows


def _write_uncategorizable_csv(output: Path, rows: list[dict[str, object]]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=UNCATEGORIZABLE_CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _read_genre_update_rows(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"source_path", "update_genre", "raw_genre"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"CSV is missing required columns: {', '.join(sorted(missing))}")
        return [dict(row) for row in reader]


def _needs_genre_update(settings: Settings, genre_map: GenreMap, row: dict[str, str]) -> bool:
    update_genre = row["update_genre"].strip()
    if not update_genre:
        return False

    source_path = Path(row["source_path"])
    if not source_path.exists():
        return True

    desired = genre_map.resolve(update_genre, settings.missing_genre_dir)
    desired_genre = desired.canonical_genre or update_genre
    try:
        current = genre_map.resolve(read_metadata(source_path).genre, settings.missing_genre_dir)
    except Exception:  # noqa: BLE001 - apply should surface unreadable-file errors in write mode
        return True

    current_genre = current.canonical_genre
    if current_genre is None or current_genre.casefold() != desired_genre.casefold():
        return True
    return _has_stale_uncategorizable_output(settings, source_path, row)


def _apply_genre_update_row(settings: Settings, genre_map: GenreMap, row: dict[str, str]) -> str:
    source_path = Path(row["source_path"])
    old_target_path = Path(row["target_path"]) if row.get("target_path") else None
    update_genre = row["update_genre"].strip()
    if not update_genre:
        return "skipped"

    write_genre(
        source_path,
        update_genre,
        original_genre=row["raw_genre"].strip() or None,
        original_genre_comment_prefix=settings.original_genre_comment_prefix,
    )
    planning_result = build_plan_for_file(settings, genre_map, source_path)
    if planning_result.skipped:
        reason = planning_result.skipped[0].reason
        raise ValueError(f"Updated source no longer produces a transfer plan: {reason}")
    if not planning_result.plans:
        raise ValueError("Updated source did not produce a transfer plan")

    processing_result = process_plans(settings, PlanningResult(plans=[planning_result.plans[0]], skipped=[]))
    processed = processing_result.processed[0]
    if processed.status not in {"processed", "needs_review", "unchanged"}:
        raise ValueError(f"Re-transfer failed: {processed.notes or processed.status}")

    _cleanup_stale_uncategorizable_outputs(settings, source_path, processed.target_path, old_target_path)
    return "moved" if _is_under(processed.target_path, settings.dj_library_dir) else "skipped"


def _has_stale_uncategorizable_output(settings: Settings, source_path: Path, row: dict[str, str]) -> bool:
    old_target_path = Path(row["target_path"]) if row.get("target_path") else None
    if old_target_path is not None and _is_under(old_target_path, settings.uncategorizable_dir) and old_target_path.exists():
        return True

    connection = connect(settings.database_path)
    initialize(connection)
    try:
        rows = connection.execute(
            """
            SELECT current_path
            FROM song
            WHERE source_path = ?
            """,
            (str(source_path),),
        ).fetchall()
    finally:
        connection.close()
    return any(_is_under(Path(row["current_path"]), settings.uncategorizable_dir) for row in rows)


def _cleanup_stale_uncategorizable_outputs(
    settings: Settings,
    source_path: Path,
    keep_target_path: Path,
    old_target_path: Path | None,
) -> None:
    stale_paths = set()
    if old_target_path is not None and _is_under(old_target_path, settings.uncategorizable_dir):
        stale_paths.add(old_target_path)

    connection = connect(settings.database_path)
    initialize(connection)
    rows = connection.execute(
        """
        SELECT id, current_path
        FROM song
        WHERE source_path = ?
        """,
        (str(source_path),),
    ).fetchall()
    stale_ids = []
    for row in rows:
        current_path = Path(row["current_path"])
        if current_path == keep_target_path:
            continue
        if _is_under(current_path, settings.uncategorizable_dir):
            stale_ids.append(int(row["id"]))
            stale_paths.add(current_path)

    for stale_path in stale_paths:
        if stale_path != keep_target_path and stale_path.exists():
            stale_path.unlink()
            _remove_empty_parents(stale_path.parent, settings.uncategorizable_dir)

    if stale_ids:
        placeholders = ",".join("?" for _ in stale_ids)
        with connection:
            connection.execute(f"DELETE FROM song_processing_label WHERE song_id IN ({placeholders})", stale_ids)
            connection.execute(f"DELETE FROM song_operation_log WHERE song_id IN ({placeholders})", stale_ids)
            connection.execute(f"DELETE FROM song WHERE id IN ({placeholders})", stale_ids)
    connection.close()


def _is_under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _remove_empty_parents(start: Path, stop: Path) -> None:
    stop = stop.resolve()
    current = start.resolve()
    while current != stop and stop in current.parents:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _duplicate_genre_updates(settings: Settings, genre_map: GenreMap, target_dir: Path, duplicate_dir: Path) -> dict[str, object]:
    target_paths, _ = scan_audio_files(target_dir, settings.recursive, settings.limit)
    duplicate_genres = _duplicate_curated_genres(settings, genre_map, duplicate_dir)
    updates = []
    conflicts = []
    skipped_no_match = 0
    skipped_non_curated = 0
    skipped_same = 0

    for target_path in target_paths:
        metadata = read_metadata(target_path)
        key = (normalize_key(metadata.artist or "Unknown Artist"), normalize_key(metadata.title or "Unknown Title"))
        candidates = duplicate_genres.get(key)
        if not candidates:
            skipped_no_match += 1
            continue

        canonical_genres = {candidate["canonical_genre"] for candidate in candidates}
        if len(canonical_genres) != 1:
            conflicts.append(
                {
                    "target_path": str(target_path),
                    "artist": metadata.artist,
                    "title": metadata.title,
                    "candidate_genres": sorted(canonical_genres),
                    "candidate_paths": [candidate["path"] for candidate in candidates],
                }
            )
            continue

        new_genre = canonical_genres.pop()
        current = genre_map.resolve(metadata.genre, settings.missing_genre_dir)
        if current.canonical_genre and current.canonical_genre.casefold() == str(new_genre).casefold():
            skipped_same += 1
            continue

        if not genre_map.is_whitelisted(new_genre):
            skipped_non_curated += 1
            continue

        updates.append(
            {
                "target_path": str(target_path),
                "artist": metadata.artist or "Unknown Artist",
                "title": metadata.title or "Unknown Title",
                "old_genre": metadata.genre,
                "new_genre": new_genre,
                "candidate_paths": [candidate["path"] for candidate in candidates],
            }
        )

    return {
        "updates": updates,
        "conflicts": conflicts,
        "skipped_no_match": skipped_no_match,
        "skipped_non_curated": skipped_non_curated,
        "skipped_same": skipped_same,
        "target_files_scanned": len(target_paths),
        "duplicate_dir": str(duplicate_dir),
    }


def _duplicate_curated_genres(settings: Settings, genre_map: GenreMap, duplicate_dir: Path) -> dict[tuple[str, str], list[dict[str, str]]]:
    duplicate_paths, _ = scan_audio_files(duplicate_dir, settings.recursive, None)
    by_key: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for duplicate_path in duplicate_paths:
        metadata = read_metadata(duplicate_path)
        resolution = genre_map.resolve(metadata.genre, settings.missing_genre_dir)
        if resolution.missing or not resolution.canonical_genre or not genre_map.is_whitelisted(resolution.canonical_genre):
            continue
        key = (normalize_key(metadata.artist or "Unknown Artist"), normalize_key(metadata.title or "Unknown Title"))
        by_key[key].append(
            {
                "path": str(duplicate_path),
                "raw_genre": metadata.genre or "",
                "canonical_genre": resolution.canonical_genre,
            }
        )
    return by_key


def _render_duplicate_genre_update_preview(result: dict[str, object]) -> None:
    updates = result["updates"]
    conflicts = result["conflicts"]
    typer.echo(f"Would update {len(updates)} files from duplicate genres. Re-run with --write to apply.")
    typer.echo(f"Target files scanned: {result['target_files_scanned']}")
    typer.echo(f"Duplicate source: {result['duplicate_dir']}")
    typer.echo(f"Skipped same genre: {result['skipped_same']}")
    typer.echo(f"Skipped no match: {result['skipped_no_match']}")
    typer.echo(f"Skipped non-curated: {result['skipped_non_curated']}")
    typer.echo(f"Skipped conflicts: {len(conflicts)}")
    for update in updates:
        typer.echo(f"- {update['target_path']}: {update['old_genre'] or '<missing>'} -> {update['new_genre']}")
    if conflicts:
        typer.echo("Conflicts:")
        for conflict in conflicts:
            typer.echo(f"- {conflict['target_path']}: {', '.join(conflict['candidate_genres'])}")


def _render_process_text(result) -> str:
    lines = ["DJ Sort process", "", f"Processed files: {len(result.processed)}"]
    errors = [item for item in result.processed if item.status == "error"]
    if errors:
        lines.append(f"Errors: {len(errors)}")
    lines.append("")
    for item in result.processed:
        suffix = f" ({item.notes})" if item.notes else ""
        cleanup = f" source={item.source_cleanup_status}"
        if item.source_archive_path:
            cleanup = f" source={item.source_cleanup_status}:{item.source_archive_path}"
        lines.append(f"- {item.status}: {item.source_path} -> {item.target_path}{cleanup}{suffix}")
    return "\n".join(lines)


def _render_duplicate_text(report: dict[str, object]) -> str:
    lines = ["DJ Sort duplicates", ""]
    summary = report["summary"]
    lines.append(f"Exact duplicate groups: {summary['exact_duplicate_groups']}")
    lines.append(f"Potential duplicate groups: {summary['potential_duplicate_groups']}")
    potential = report["potential_duplicate_groups"]
    if potential:
        lines.append("")
        lines.append("Potential duplicates:")
        for group in potential:
            lines.append(
                f"- {group['normalized_artist_name']} - {group['normalized_title']} ({group['count']})"
            )
            for song in group["songs"]:
                details = ", ".join(
                    str(value)
                    for value in [
                        song.get("display_genre"),
                        song.get("bpm"),
                        song.get("musical_key"),
                        song.get("extension"),
                        song.get("bitrate"),
                        song.get("duration_ms"),
                    ]
                    if value is not None
                )
                suffix = f" [{details}]" if details else ""
                lines.append(f"  {song['current_path']}{suffix}")
    return "\n".join(lines)


def _render_consolidation_text(result) -> str:
    mode = "dry-run" if result.dry_run else "write"
    lines = [f"DJ Sort genre consolidation ({mode})", "", f"Actions: {len(result.actions)}"]
    for action in result.actions:
        suffix = f" ({action.notes})" if action.notes else ""
        lines.append(
            f"- {action.status}: {action.current_path} -> {action.target_path} "
            f"[{action.source_genre} -> {action.target_genre}]{suffix}"
        )
    return "\n".join(lines)


def _render_excluded_text(report: dict[str, object]) -> str:
    summary = report["summary"]
    reason = summary.get("reason")
    heading = "DJ Sort excluded songs"
    if reason:
        heading = f"{heading} ({reason})"
    lines = [heading, "", f"Excluded files: {summary['excluded']}"]
    excluded = report["excluded"]
    if excluded:
        lines.append("")
        for row in excluded:
            details = ", ".join(
                str(value)
                for value in [
                    row.get("reason"),
                    row.get("canonical_genre"),
                    row.get("duration_ms"),
                    row.get("notes"),
                ]
                if value is not None
            )
            suffix = f" [{details}]" if details else ""
            lines.append(f"- {row['source_path']}{suffix}")
    return "\n".join(lines)
