from __future__ import annotations

import json
from pathlib import Path
from time import perf_counter
from typing import Annotated

import typer
import yaml

from dj_sort.consolidation import consolidate_genres
from dj_sort.database import connect, duplicate_report, initialize
from dj_sort.genres import GenreMap
from dj_sort.planning import build_plans
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
    typer.echo(f"source_root: {settings.source_root}")
    typer.echo(f"library_root: {settings.library_root}")
    typer.echo(f"database_path: {settings.database_path}")
    typer.echo(f"genre_map_path: {settings.genre_map_path}")
    typer.echo(f"ffmpeg: {binaries.ffmpeg or '<not found>'}")
    typer.echo(f"ffprobe: {binaries.ffprobe or '<not found>'}")
    typer.echo(f"fpcalc: {binaries.fpcalc or '<not found>'}")


@app.command()
def scan(
    settings_path: SettingsPath = Path("settings.yaml"),
    source_root: OptionalPath = None,
    limit: Annotated[int | None, typer.Option("--limit", help="Limit candidate audio files")] = None,
    output_format: OutputFormat = "text",
    output: Annotated[Path | None, typer.Option("--output", help="Write report to file")] = None,
) -> None:
    """Plan source files without writing anything."""
    start = perf_counter()
    _validate_output_format(output_format)
    settings = _load(settings_path, source_root=source_root, limit=limit)
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
    source_root: OptionalPath = None,
    library_root: OptionalPath = None,
    processed_source_root: OptionalPath = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Force dry-run mode")] = False,
    write: Annotated[bool, typer.Option("--write", help="Write filesystem and DB changes")] = False,
    limit: Annotated[int | None, typer.Option("--limit", help="Limit candidate audio files")] = None,
    source_completion_action: Annotated[
        str | None,
        typer.Option("--source-completion-action", help="keep, archive_move, archive_copy, or delete"),
    ] = None,
    archive_source: Annotated[bool, typer.Option("--archive-source", help="Shortcut for archive_move")] = False,
    keep_source: Annotated[bool, typer.Option("--keep-source", help="Shortcut for keep")] = False,
    delete_source: Annotated[bool, typer.Option("--delete-source", help="Shortcut for delete")] = False,
    output_format: OutputFormat = "text",
    output: Annotated[Path | None, typer.Option("--output", help="Write report to file")] = None,
) -> None:
    """Dry-run or process files into the managed library."""
    start = perf_counter()
    _validate_output_format(output_format)
    settings = _load(
        settings_path,
        source_root=source_root,
        library_root=library_root,
        processed_source_root=processed_source_root,
        limit=limit,
    )
    settings = _apply_source_completion_shortcuts(
        settings,
        source_completion_action=source_completion_action,
        archive_source=archive_source,
        keep_source=keep_source,
        delete_source=delete_source,
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


@genres_app.command("discover")
def genres_discover(
    settings_path: SettingsPath = Path("settings.yaml"),
    source_root: OptionalPath = None,
    limit: Annotated[int | None, typer.Option("--limit", help="Limit candidate audio files")] = None,
    output_format: OutputFormat = "text",
    output: Annotated[Path | None, typer.Option("--output", help="Write report to file")] = None,
) -> None:
    """Recursively discover unique raw genre metadata values."""
    start = perf_counter()
    _validate_output_format(output_format)
    settings = _load(settings_path, source_root=source_root, limit=limit)
    genre_map = GenreMap.load(settings.genre_map_path)
    data = discover_genres(settings, genre_map, limit=settings.limit)
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
    source_root: Path | None = None,
    library_root: Path | None = None,
    processed_source_root: Path | None = None,
    limit: int | None = None,
) -> Settings:
    try:
        settings = load_settings(settings_path)
        updates = {}
        if source_root is not None:
            updates["source_root"] = source_root.expanduser()
        if library_root is not None:
            updates["library_root"] = library_root.expanduser()
        if processed_source_root is not None:
            updates["processed_source_root"] = processed_source_root.expanduser()
        if limit is not None:
            updates["limit"] = limit
        if updates:
            settings = settings.model_copy(update=updates)
        resolve_binaries(settings)
        return settings
    except Exception as exc:  # noqa: BLE001 - CLI should present concise setup errors
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


def _apply_source_completion_shortcuts(
    settings: Settings,
    source_completion_action: str | None,
    archive_source: bool,
    keep_source: bool,
    delete_source: bool,
) -> Settings:
    action = source_completion_action
    if archive_source:
        action = "archive_move"
    if keep_source:
        action = "keep"
    if delete_source:
        action = "delete"
    if action is None:
        return settings
    return settings.model_copy(update={"source_completion_action": action})


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
