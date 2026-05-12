from __future__ import annotations

import csv
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from time import perf_counter
from typing import Annotated

import typer
import yaml

from dj_sort import database
from dj_sort.consolidation import consolidate_genres
from dj_sort.curation import CurationSyncResult, sync_navidrome_curation, sync_navidrome_curation_from_api
from dj_sort.database import connect, duplicate_report, excluded_report, genre_id, initialize
from dj_sort.genres import GenreMap
from dj_sort.hashing import sha256_file
from dj_sort.lookup import discogs_token_available, suggest_genre_for_track
from dj_sort.metadata import read_metadata, write_genre
from dj_sort.mixedinkey import MixedInKeyUpdate, apply_mixed_in_key_update
from dj_sort.navidrome import (
    NavidromeClient,
    filter_playlists,
    playlist_cache_path,
    rated_song_ids_from_database,
    read_playlist_cache,
    write_playlist_cache,
)
from dj_sort.paths import normalize_key, safe_path_part
from dj_sort.planning import PlanningResult, build_plan_for_file, build_plans, scan_audio_files
from dj_sort.playlists import export_genre_playlists
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
    typer.echo(f"automated_backup_dir: {settings.automated_backup_dir or '<disabled>'}")
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


@app.command("import-mixed-in-key")
def import_mixed_in_key(
    path: Annotated[Path, typer.Argument(help="Library path analyzed by Mixed In Key, relative to --base-dir unless absolute")] = Path("."),
    settings_path: SettingsPath = Path("settings.yaml"),
    base_dir: Annotated[
        str | None,
        typer.Option("--base-dir", help="Base directory: dj_library/uncategorizable/duplicates/unprocessed or absolute"),
    ] = "dj_library",
    write: Annotated[bool, typer.Option("--write", help="Write metadata, rename files, and update the database")] = False,
    include_energy: Annotated[bool, typer.Option("--include-energy/--no-include-energy", help="Include E<score> in filenames")] = True,
    limit: Annotated[int | None, typer.Option("--limit", help="Limit candidate audio files")] = None,
) -> None:
    """Import Mixed In Key comment/key/BPM/energy data and rename analyzed files."""
    settings = _load(settings_path, limit=limit)
    source_dir = _resolve_scan_dir(settings, base_dir, path)
    files, skipped_scan = scan_audio_files(source_dir, settings.recursive, settings.limit)

    updates: list[MixedInKeyUpdate] = []
    for audio_path in files:
        updates.append(
            apply_mixed_in_key_update(
                audio_path,
                include_energy=include_energy,
                write=write,
                duplicates_dir=settings.duplicates_dir,
                library_dir=settings.dj_library_dir,
            )
        )

    if write:
        _update_mixed_in_key_database(settings, updates)

    planned = [
        update
        for update in updates
        if update.status in {"planned", "updated", "planned_deleted_duplicate", "planned_moved_duplicate", "deleted_duplicate", "moved_duplicate"}
    ]
    skipped = [update for update in updates if update.status == "skipped"]
    renamed = [update for update in planned if update.changed_path]
    deleted_duplicates = [update for update in planned if update.status in {"planned_deleted_duplicate", "deleted_duplicate"}]
    moved_duplicates = [update for update in planned if update.status in {"planned_moved_duplicate", "moved_duplicate"}]
    action = "Updated" if write else "Would update"
    typer.echo(f"{action} {len(planned)} files from Mixed In Key metadata")
    typer.echo(f"Renamed files: {len(renamed)}")
    typer.echo(f"Deleted same-bitrate duplicates: {len(deleted_duplicates)}")
    typer.echo(f"Moved different-bitrate duplicates: {len(moved_duplicates)}")
    typer.echo(f"Skipped files: {len(skipped) + len(skipped_scan)}")
    for update in planned:
        info = update.info
        suffix = f" ({info.key}, {info.bpm} BPM, energy {info.energy})" if info else ""
        if update.changed_path:
            typer.echo(f"- {update.source_path} -> {update.target_path}{suffix}")
        else:
            typer.echo(f"- {update.source_path}{suffix}")
    if skipped:
        typer.echo("Skipped:")
        for update in skipped:
            typer.echo(f"- {update.source_path}: {update.notes}")
    for skipped_file in skipped_scan:
            typer.echo(f"- {skipped_file.path}: {skipped_file.reason}")


@app.command("export-navidrome-playlists")
def export_navidrome_playlists(
    settings_path: SettingsPath = Path("settings.yaml"),
    local_library_root: Annotated[
        Path | None,
        typer.Option("--local-library-root", help="Local Library folder to scan; defaults to dj_library_dir"),
    ] = None,
    server_library_root: Annotated[
        Path | None,
        typer.Option("--server-library-root", help="Library path as Navidrome sees it"),
    ] = None,
    output_dir: Annotated[
        Path | None,
        typer.Option("--output-dir", help="Where to write .m3u files; defaults to navidrome.output_dir or playlist_root"),
    ] = None,
    playlist_root: Annotated[
        Path | None,
        typer.Option("--playlist-root", help="Server playlist folder, used when --output-dir is omitted"),
    ] = None,
    write: Annotated[bool, typer.Option("--write", help="Write .m3u files; otherwise dry-run")] = False,
    playlist_name_prefix: Annotated[
        str | None,
        typer.Option("--playlist-name-prefix", help="Prefix for generated playlist names, e.g. 'Uncurated: '"),
    ] = None,
) -> None:
    """Generate Navidrome-compatible genre .m3u playlists from the library."""
    settings = _load(settings_path)
    local_root = (local_library_root or settings.dj_library_dir).expanduser()
    server_root = (server_library_root or settings.navidrome.library_root).expanduser()
    playlist_root = (playlist_root or settings.navidrome.playlist_root).expanduser()
    output_root = (output_dir or settings.navidrome.output_dir or playlist_root).expanduser()
    playlist_root_name = playlist_root.name

    try:
        exports = export_genre_playlists(
            local_root,
            server_root,
            output_root,
            playlist_root_name=playlist_root_name,
            include_extm3u_header=settings.navidrome.include_extm3u_header,
            playlist_name_prefix=(
                settings.navidrome.playlist_name_prefix if playlist_name_prefix is None else playlist_name_prefix
            ),
            write=write,
        )
    except Exception as exc:  # noqa: BLE001 - CLI should present concise filesystem errors
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    action = "Wrote" if write else "Would write"
    typer.echo(f"{action} {len(exports)} Navidrome playlists for {settings.navidrome.host}")
    typer.echo(f"Local library root: {local_root}")
    typer.echo(f"Server library root: {server_root}")
    typer.echo(f"Playlist output dir: {output_root}")
    for export in exports:
        typer.echo(f"- {export.path}: {export.track_count} tracks")


@app.command("sync-navidrome-curation")
def sync_navidrome_curation_command(
    settings_path: SettingsPath = Path("settings.yaml"),
    database_path: Annotated[
        Path | None,
        typer.Option("--database-path", help="Path to navidrome.db; defaults to navidrome.database_path or NAVIDROME_DB"),
    ] = None,
    database_ssh: Annotated[
        str | None,
        typer.Option("--database-ssh", help="SSH SQLite source, for example erik@192.168.1.31:/srv/navidrome/navidrome.db"),
    ] = None,
    ssh_identity_file: Annotated[
        Path | None,
        typer.Option("--ssh-identity-file", help="Identity file to use for --database-ssh"),
    ] = None,
    source: Annotated[str, typer.Option("--source", help="Curation source: auto, db, ssh, or api")] = "auto",
    library_dir: Annotated[Path | None, typer.Option("--library-dir", help="Local DJ library root; defaults to dj_library_dir")] = None,
    output_dir: Annotated[Path | None, typer.Option("--output-dir", help="Directory for exported .m3u8 playlists")] = None,
    write: Annotated[bool, typer.Option("--write", help="Write MP3 curation tags and .m3u8 playlists")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show per-file curation details")] = False,
    rescan: Annotated[bool, typer.Option("--rescan/--no-rescan", help="Rescan Navidrome before --write and wait for completion")] = True,
    rescan_timeout_seconds: Annotated[int, typer.Option("--rescan-timeout-seconds", help="Maximum seconds to wait for pre-write rescan")] = 900,
    rescan_poll_seconds: Annotated[int, typer.Option("--rescan-poll-seconds", help="Seconds between pre-write scan status checks")] = 5,
) -> None:
    """Sync Navidrome ratings, favorites, and real playlists into recoverable tags and .m3u8 exports."""
    settings = _load(settings_path)
    try:
        if write and rescan:
            _trigger_navidrome_rescan(
                settings,
                wait=True,
                timeout_seconds=rescan_timeout_seconds,
                poll_seconds=rescan_poll_seconds,
            )
        result = _sync_navidrome_curation_from_source(
            settings=settings,
            source=source,
            database_path=database_path,
            database_ssh=database_ssh,
            ssh_identity_file=ssh_identity_file,
            library_dir=library_dir,
            output_dir=output_dir,
            write=write,
        )
    except Exception as exc:  # noqa: BLE001 - CLI should present concise DB/filesystem errors
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    _render_curation_sync_result(result, verbose=verbose)


@app.command("rescan-navidrome")
def rescan_navidrome(
    settings_path: SettingsPath = Path("settings.yaml"),
    wait: Annotated[bool, typer.Option("--wait", help="Wait for Navidrome to finish scanning")] = False,
    timeout_seconds: Annotated[int, typer.Option("--timeout-seconds", help="Maximum seconds to wait for --wait")] = 900,
    poll_seconds: Annotated[int, typer.Option("--poll-seconds", help="Seconds between scan status checks")] = 5,
) -> None:
    """Trigger a Navidrome library rescan through the Subsonic API."""
    settings = _load(settings_path)
    try:
        _trigger_navidrome_rescan(settings, wait=wait, timeout_seconds=timeout_seconds, poll_seconds=poll_seconds)
    except Exception as exc:  # noqa: BLE001 - CLI should present concise API errors
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@app.command("prune-automated-backups")
def prune_automated_backups(
    settings_path: SettingsPath = Path("settings.yaml"),
    days: Annotated[int, typer.Option("--days", help="Delete backup entries older than this many days")] = 30,
    write: Annotated[bool, typer.Option("--write", help="Delete matching backup entries; otherwise show a dry-run")] = False,
) -> None:
    """Prune old entries from automated_backup_dir."""
    if days < 0:
        typer.echo("Error: --days must be 0 or greater", err=True)
        raise typer.Exit(code=1)
    settings = _load(settings_path)
    try:
        entries = _old_automated_backup_entries(settings, days)
        if write:
            for entry in entries:
                if entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
    except Exception as exc:  # noqa: BLE001 - CLI should present concise filesystem errors
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    action = "Pruned" if write else "Would prune"
    backup_dir = settings.automated_backup_dir or "<disabled>"
    typer.echo(f"{action} {len(entries)} automated backup entries older than {days} days from {backup_dir}")
    for entry in entries:
        typer.echo(f"- {entry}")


@app.command("rate-current")
def rate_current(
    rating: Annotated[int, typer.Argument(help="Navidrome rating from 1 to 5")],
    settings_path: SettingsPath = Path("settings.yaml"),
    favorite_five: Annotated[bool, typer.Option("--favorite-five/--no-favorite-five", help="Favorite/star the track when rating is 5")] = True,
) -> None:
    """Rate the current Navidrome now-playing track."""
    if rating < 1 or rating > 5:
        typer.echo("Error: rating must be between 1 and 5", err=True)
        raise typer.Exit(code=1)
    settings = _load(settings_path)
    try:
        client = _navidrome_client(settings)
        song = client.now_playing()
        client.set_rating(song.id, rating)
        favorited = rating == 5 and favorite_five
        if favorited:
            client.star(song.id)
    except Exception as exc:  # noqa: BLE001 - CLI should present concise API errors
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    suffix = " and favorited" if favorited else ""
    typer.echo(f"Rated {rating}{suffix}: {song.display_name}")


@app.command("favorite-rated-five")
def favorite_rated_five(
    settings_path: SettingsPath = Path("settings.yaml"),
    database_path: Annotated[
        Path | None,
        typer.Option("--database-path", help="Path to navidrome.db; defaults to navidrome.database_path or NAVIDROME_DB"),
    ] = None,
    include_already_starred: Annotated[
        bool,
        typer.Option("--include-already-starred/--only-unstarred", help="Also call star for tracks already marked starred in the DB"),
    ] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show how many tracks would be favorited without writing")] = False,
) -> None:
    """Favorite/star every current user's Navidrome track rated 5."""
    settings = _load(settings_path)
    try:
        username = _navidrome_username(settings)
        db_path = _navidrome_database_path(settings, database_path)
        song_ids = rated_song_ids_from_database(db_path, username, rating=5, only_unstarred=not include_already_starred)
        if dry_run:
            typer.echo(f"Would favorite {len(song_ids)} rated-5 tracks for {username}")
            return
        client = _navidrome_client(settings)
        for song_id in song_ids:
            client.star(song_id)
    except Exception as exc:  # noqa: BLE001 - CLI should present concise API/DB errors
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Favorited {len(song_ids)} rated-5 tracks for {username}")


@app.command("new-playlist")
def new_playlist(
    name: Annotated[str, typer.Argument(help="Playlist name to create in Navidrome")],
    settings_path: SettingsPath = Path("settings.yaml"),
) -> None:
    """Create a Navidrome playlist."""
    cleaned = name.strip()
    if not cleaned:
        typer.echo("Error: playlist name cannot be blank", err=True)
        raise typer.Exit(code=1)
    settings = _load(settings_path)
    try:
        playlist = _navidrome_client(settings).create_playlist(cleaned)
    except Exception as exc:  # noqa: BLE001 - CLI should present concise API errors
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Created playlist {playlist.id}: {playlist.name}")


@app.command("list-playlist")
@app.command("list-playlists")
def list_playlists(
    target: Annotated[str | None, typer.Argument(help="Playlist fuzzy query, or a number from the previous list")] = None,
    settings_path: SettingsPath = Path("settings.yaml"),
    max_results: Annotated[int, typer.Option("--max-results", help="Maximum playlists or songs to show")] = 50,
) -> None:
    """List non-generated Navidrome playlists, or show one playlist's songs by number."""
    settings = _load(settings_path)
    cache_path = playlist_cache_path(settings_path)
    try:
        client = _navidrome_client(settings)
        if target is not None and target.strip().isdigit():
            playlist = _playlist_from_cached_number(cache_path, int(target.strip()))
            songs = client.playlist_songs(playlist.id)
            typer.echo(f"{playlist.name}: {len(songs)} tracks")
            for index, song in enumerate(songs[:max_results], start=1):
                album = f" [{song.album}]" if song.album else ""
                typer.echo(f"{index}. {song.display_name}{album}")
            if len(songs) > max_results:
                typer.echo(f"... {len(songs) - max_results} more")
            return

        matches = filter_playlists(client.playlists(), target, _navidrome_excluded_playlist_prefixes(settings))
    except Exception as exc:  # noqa: BLE001 - CLI should present concise API errors
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    shown = matches[:max_results]
    write_playlist_cache(cache_path, shown)
    typer.echo(f"Playlists: {len(matches)}")
    for index, playlist in enumerate(shown, start=1):
        count = f" ({playlist.song_count} tracks)" if playlist.song_count is not None else ""
        typer.echo(f"{index}. {playlist.name}{count}")
    if shown:
        typer.echo(f"Run again with a number, for example: dj-sort list-playlists 1 --settings {settings_path}")


@app.command("categorize-current")
def categorize_current(
    target: Annotated[str | None, typer.Argument(help="Playlist fuzzy query, or a number from the previous result list")] = None,
    settings_path: SettingsPath = Path("settings.yaml"),
    max_results: Annotated[int, typer.Option("--max-results", help="Maximum playlist choices to show")] = 25,
) -> None:
    """Add the current Navidrome now-playing track to a non-generated playlist."""
    settings = _load(settings_path)
    cache_path = playlist_cache_path(settings_path)
    try:
        client = _navidrome_client(settings)
        song = client.now_playing()
        if target is not None and target.strip().isdigit():
            playlist = _playlist_from_cached_number(cache_path, int(target.strip()))
            added = client.add_to_playlist(playlist.id, song.id)
            action = "Added" if added else "Already in"
            typer.echo(f"{action} playlist '{playlist.name}': {song.display_name}")
            return

        matches = filter_playlists(client.playlists(), target, _navidrome_excluded_playlist_prefixes(settings))
        if target and len(matches) == 1 and matches[0].name.casefold() == target.strip().casefold():
            added = client.add_to_playlist(matches[0].id, song.id)
            action = "Added" if added else "Already in"
            typer.echo(f"{action} playlist '{matches[0].name}': {song.display_name}")
            return
    except Exception as exc:  # noqa: BLE001 - CLI should present concise API errors
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    shown = matches[:max_results]
    write_playlist_cache(cache_path, shown)
    typer.echo(f"Current track: {song.display_name}")
    typer.echo(f"Playlist matches: {len(matches)}")
    for index, playlist in enumerate(shown, start=1):
        count = f" ({playlist.song_count} tracks)" if playlist.song_count is not None else ""
        typer.echo(f"{index}. {playlist.name}{count}")
    if shown:
        typer.echo(f"Run again with a number, for example: dj-sort categorize-current 1 --settings {settings_path}")


@app.command("re-genre-current")
def re_genre_current(
    target_genre: Annotated[str, typer.Argument(help="New genre for the current Navidrome now-playing track")],
    settings_path: SettingsPath = Path("settings.yaml"),
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show the tag/path change without writing")] = False,
) -> None:
    """Retag and move the current Navidrome now-playing track into a genre folder."""
    settings = _load(settings_path)
    try:
        genre = _resolve_target_genre(settings, target_genre)
        client = _navidrome_client(settings)
        song = client.now_playing()
        current_path = _local_path_for_navidrome_song(settings, song.path)
        metadata = read_metadata(current_path)
        target_path = settings.dj_library_dir / safe_path_part(genre) / current_path.name
        if target_path.exists() and target_path != current_path:
            raise ValueError(f"target file already exists, not overwriting: {target_path}")
        if dry_run:
            typer.echo(f"Would re-genre: {song.display_name}")
            typer.echo(f"Genre: {metadata.genre or '<missing>'} -> {genre}")
            typer.echo(f"Path: {current_path} -> {target_path}")
            return

        target_path.parent.mkdir(parents=True, exist_ok=True)
        original_genre = metadata.genre if settings.preserve_original_genre_in_comment else None
        write_genre(
            current_path,
            genre,
            original_genre=original_genre,
            original_genre_comment_prefix=settings.original_genre_comment_prefix,
        )
        if current_path != target_path:
            old_parent = current_path.parent
            current_path.rename(target_path)
            if settings.remove_empty_genre_dirs:
                _remove_empty_parents(old_parent, settings.dj_library_dir)
        _update_re_genred_song_database(settings, current_path, target_path, metadata.genre, genre)
    except Exception as exc:  # noqa: BLE001 - CLI should present concise API/filesystem errors
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"Re-genred: {song.display_name}")
    typer.echo(f"Genre: {metadata.genre or '<missing>'} -> {genre}")
    typer.echo(f"Path: {current_path} -> {target_path}")
    typer.echo("Regenerate Navidrome playlists and rescan Navidrome to refresh generated Uncurated playlists.")


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
    deleted = 0
    skipped = 0
    for row in updates:
        source_path = Path(row["source_path"])
        try:
            result = _apply_genre_update_row(settings, genre_map, row)
            updated += 1
            if result == "moved":
                moved += 1
            elif result == "deleted":
                deleted += 1
            elif result == "skipped":
                skipped += 1
        except Exception as exc:  # noqa: BLE001 - continue applying independent CSV rows
            errors.append((source_path, exc))
    typer.echo(f"Updated {updated} source files from {csv_path}")
    typer.echo(f"Moved to new destinations: {moved}")
    typer.echo(f"Deleted source files: {deleted}")
    typer.echo(f"Skipped re-transfer: {skipped}")
    if errors:
        typer.echo(f"Errors: {len(errors)}", err=True)
        for source_path, exc in errors:
            typer.echo(f"- {source_path}: {exc}", err=True)
        raise typer.Exit(code=1)


@app.command("suggest-genres")
def suggest_genres(
    csv_path: Annotated[Path, typer.Argument(help="CSV created by export-uncategorizable or a compatible review CSV")],
    settings_path: SettingsPath = Path("settings.yaml"),
    output: Annotated[Path | None, typer.Option("--output", help="Write enriched CSV to this path")] = None,
    review_output: Annotated[Path | None, typer.Option("--review-output", help="Write rows without clear suggestions to this CSV")] = None,
    limit: Annotated[int | None, typer.Option("--limit", help="Limit lookup rows for testing/rate limits")] = None,
    fill_empty: Annotated[bool, typer.Option("--fill-empty", help="Copy lookup suggestions into empty update_genre cells")] = False,
    fill_clear: Annotated[
        bool,
        typer.Option("--fill-clear", help="Copy clear lookup suggestions into update_genre and leave unclear rows blank"),
    ] = False,
    musicbrainz: Annotated[bool, typer.Option("--musicbrainz/--no-musicbrainz", help="Fall back to MusicBrainz for Discogs misses")] = True,
) -> None:
    """Suggest genres for review CSV rows using Discogs, falling back to MusicBrainz."""
    settings = _load(settings_path)
    csv_path = _resolve_genre_update_csv_path(csv_path)
    rows = _read_genre_update_rows(csv_path)
    genre_map = GenreMap.load(settings.genre_map_path)
    output = output or csv_path.with_name(f"{csv_path.stem}-suggestions.csv")

    fieldnames = list(rows[0].keys()) if rows else list(UNCATEGORIZABLE_CSV_FIELDS)
    for field in ["lookup_genre", "lookup_confidence", "lookup_source", "lookup_terms", "lookup_notes", "lookup_decision"]:
        if field not in fieldnames:
            fieldnames.append(field)

    lookup_count = 0
    suggestion_count = 0
    clear_count = 0
    review_rows = []
    for row in rows:
        row.setdefault("lookup_genre", "")
        row.setdefault("lookup_confidence", "")
        row.setdefault("lookup_source", "")
        row.setdefault("lookup_terms", "")
        row.setdefault("lookup_notes", "")
        row.setdefault("lookup_decision", "")
        if limit is not None and lookup_count >= limit:
            if fill_clear:
                row["update_genre"] = ""
                row["lookup_decision"] = "needs_review"
                review_rows.append(dict(row))
            continue
        suggestion = suggest_genre_for_track(
            row.get("artist", ""),
            row.get("title", ""),
            genre_map,
            musicbrainz_fallback=musicbrainz,
        )
        lookup_count += 1
        if suggestion is None:
            if fill_clear:
                row["update_genre"] = ""
                row["lookup_decision"] = "needs_review"
                review_rows.append(dict(row))
            continue
        suggestion_count += 1
        row["lookup_genre"] = suggestion.genre
        row["lookup_confidence"] = suggestion.confidence
        row["lookup_source"] = suggestion.source
        row["lookup_terms"] = "; ".join(suggestion.terms)
        row["lookup_notes"] = suggestion.notes
        clear_lookup = _is_clear_lookup(row)
        if fill_clear and clear_lookup:
            row["update_genre"] = suggestion.genre
            row["lookup_decision"] = "auto"
            clear_count += 1
        elif fill_clear:
            row["update_genre"] = ""
            row["lookup_decision"] = "needs_review"
            review_rows.append(dict(row))
        elif fill_empty and not row.get("update_genre", "").strip():
            row["update_genre"] = suggestion.genre

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    source_note = "Discogs + MusicBrainz" if discogs_token_available() else "MusicBrainz only; set DISCOGS_TOKEN for Discogs"
    typer.echo(f"Looked up {lookup_count} rows using {source_note}")
    typer.echo(f"Found {suggestion_count} suggestions")
    if fill_clear:
        typer.echo(f"Auto-filled clear suggestions: {clear_count}")
        typer.echo(f"Rows needing review: {len(review_rows)}")
    typer.echo(f"Wrote {output}")
    if review_output is not None:
        review_output.parent.mkdir(parents=True, exist_ok=True)
        with review_output.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(review_rows)
        typer.echo(f"Wrote {review_output}")


def _is_clear_lookup(row: dict[str, str]) -> bool:
    return bool(
        row.get("lookup_genre", "").strip()
        and row.get("lookup_source") == "discogs"
        and row.get("lookup_confidence") in {"medium", "high"}
        and row.get("lookup_terms", "").strip()
    )


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


def _old_automated_backup_entries(settings: Settings, days: int) -> list[Path]:
    backup_dir = settings.automated_backup_dir
    if backup_dir is None or not backup_dir.exists():
        return []
    if not backup_dir.is_dir():
        raise ValueError(f"automated_backup_dir is not a directory: {backup_dir}")
    cutoff = time.time() - (days * 24 * 60 * 60)
    return sorted((entry for entry in backup_dir.iterdir() if entry.stat().st_mtime < cutoff), key=lambda path: path.name.casefold())


def _trigger_navidrome_rescan(settings: Settings, wait: bool = False, timeout_seconds: int = 900, poll_seconds: int = 5) -> None:
    if timeout_seconds < 1:
        raise ValueError("--timeout-seconds must be at least 1")
    if poll_seconds < 1:
        raise ValueError("--poll-seconds must be at least 1")
    client = _navidrome_client(settings)
    status = client.start_scan()
    count = f", count={status.count}" if status.count is not None else ""
    typer.echo(f"Started Navidrome rescan: scanning={status.scanning}{count}")
    if not wait:
        typer.echo("Rescan is asynchronous; use rescan-navidrome --wait to wait for completion.")
        return

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        status = client.scan_status()
        count = f", count={status.count}" if status.count is not None else ""
        typer.echo(f"Navidrome scan status: scanning={status.scanning}{count}")
        if not status.scanning:
            typer.echo("Navidrome rescan finished")
            return
        time.sleep(poll_seconds)
    raise TimeoutError(f"Navidrome rescan did not finish within {timeout_seconds} seconds")


def _navidrome_client(settings: Settings) -> NavidromeClient:
    username = _navidrome_username(settings)
    password = settings.navidrome.password or os.environ.get("NAVIDROME_PASSWORD")
    if not username or not password:
        raise ValueError("Navidrome credentials are required: set navidrome.username/password or NAVIDROME_USER/NAVIDROME_PASSWORD")
    return NavidromeClient(
        _navidrome_base_url(settings),
        username,
        password,
    )


def _navidrome_username(settings: Settings) -> str:
    username = settings.navidrome.username or os.environ.get("NAVIDROME_USER") or os.environ.get("NAVIDROME_USERNAME")
    if not username:
        raise ValueError("Navidrome username is required: set navidrome.username or NAVIDROME_USER")
    return username


def _navidrome_database_path(settings: Settings, database_path: Path | None = None) -> Path:
    configured = database_path or settings.navidrome.database_path
    env_path = os.environ.get("NAVIDROME_DB")
    if configured is None and env_path:
        configured = Path(env_path)
    if configured is None:
        raise ValueError("Navidrome database path is required: set navidrome.database_path, NAVIDROME_DB, or --database-path")
    return configured.expanduser()


def _navidrome_database_ssh(settings: Settings, database_ssh: str | None = None) -> str | None:
    return database_ssh or settings.navidrome.database_ssh or os.environ.get("NAVIDROME_DB_SSH")


def _navidrome_database_ssh_identity_file(settings: Settings, identity_file: Path | None = None) -> Path | None:
    configured = identity_file or settings.navidrome.database_ssh_identity_file
    env_path = os.environ.get("NAVIDROME_DB_SSH_IDENTITY_FILE")
    if configured is None and env_path:
        configured = Path(env_path)
    return configured.expanduser() if configured is not None else None


def _sync_navidrome_curation_from_source(
    settings: Settings,
    source: str,
    database_path: Path | None,
    database_ssh: str | None,
    ssh_identity_file: Path | None,
    library_dir: Path | None,
    output_dir: Path | None,
    write: bool,
) -> CurationSyncResult:
    normalized_source = source.strip().casefold()
    if normalized_source not in {"auto", "db", "ssh", "api"}:
        raise ValueError("--source must be one of: auto, db, ssh, api")
    username = _navidrome_username(settings)

    if normalized_source in {"auto", "db"}:
        try:
            resolved_database_path = _navidrome_database_path(settings, database_path)
        except ValueError:
            if normalized_source == "db":
                raise
        else:
            if resolved_database_path.exists():
                return sync_navidrome_curation(
                    settings,
                    resolved_database_path,
                    username,
                    library_dir=library_dir,
                    output_dir=output_dir,
                    write=write,
                )
            if normalized_source == "db":
                raise FileNotFoundError(f"Navidrome database does not exist: {resolved_database_path}")

    ssh_source = _navidrome_database_ssh(settings, database_ssh)
    if normalized_source in {"auto", "ssh"} and ssh_source:
        with tempfile.TemporaryDirectory(prefix="dj-sort-navidrome-") as temp_dir:
            snapshot_path = Path(temp_dir) / "navidrome.db"
            _copy_navidrome_database_over_ssh(ssh_source, snapshot_path, _navidrome_database_ssh_identity_file(settings, ssh_identity_file))
            result = sync_navidrome_curation(
                settings,
                snapshot_path,
                username,
                library_dir=library_dir,
                output_dir=output_dir,
                write=write,
                source_name="ssh",
            )
            return result
    if normalized_source == "ssh":
        raise ValueError("Navidrome SSH database source is required: set navidrome.database_ssh, NAVIDROME_DB_SSH, or --database-ssh")

    if normalized_source == "api" or _navidrome_api_credentials_available(settings):
        return sync_navidrome_curation_from_api(
            settings,
            _navidrome_client(settings),
            library_dir=library_dir,
            output_dir=output_dir,
            write=write,
        )
    raise ValueError(
        "No Navidrome curation source available: configure navidrome.database_path, navidrome.database_ssh, "
        "or Navidrome API credentials"
    )


def _navidrome_api_credentials_available(settings: Settings) -> bool:
    username = settings.navidrome.username or os.environ.get("NAVIDROME_USER") or os.environ.get("NAVIDROME_USERNAME")
    password = settings.navidrome.password or os.environ.get("NAVIDROME_PASSWORD")
    return bool(username and password)


REMOTE_SQLITE_BACKUP_SCRIPT = r"""
import os
import sqlite3
import sys
import tempfile

source = sys.argv[1]
fd, target = tempfile.mkstemp(prefix="navidrome-backup-", suffix=".db")
os.close(fd)
try:
    source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    target_connection = sqlite3.connect(target)
    source_connection.backup(target_connection)
    target_connection.close()
    source_connection.close()
    with open(target, "rb") as handle:
        sys.stdout.buffer.write(handle.read())
finally:
    try:
        os.unlink(target)
    except FileNotFoundError:
        pass
""".strip()


def _copy_navidrome_database_over_ssh(source: str, target: Path, identity_file: Path | None = None) -> None:
    host, remote_path = _split_ssh_database_source(source)
    command = ["ssh"]
    if identity_file is not None:
        command.extend(["-i", str(identity_file), "-o", "IdentitiesOnly=yes"])
    remote_command = f"python3 -c {shlex.quote(REMOTE_SQLITE_BACKUP_SCRIPT)} {shlex.quote(remote_path)}"
    command.extend([host, remote_command])
    with target.open("wb") as output:
        completed = subprocess.run(  # noqa: S603 - host/path are user-provided command options
            command,
            stdout=output,
            stderr=subprocess.PIPE,
            check=False,
        )
    if completed.returncode != 0:
        target.unlink(missing_ok=True)
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"Navidrome SSH database backup failed: {stderr or f'ssh exited {completed.returncode}'}")


def _split_ssh_database_source(source: str) -> tuple[str, str]:
    host, separator, path = source.partition(":")
    if not separator or not host or not path:
        raise ValueError("SSH database source must look like user@host:/path/to/navidrome.db")
    return host, path


def _navidrome_base_url(settings: Settings) -> str:
    configured = settings.navidrome.url or os.environ.get("NAVIDROME_URL")
    if configured:
        return configured.rstrip("/")
    host = settings.navidrome.host.rstrip("/")
    if host.startswith("http://") or host.startswith("https://"):
        return host
    return f"http://{host}:4533"


def _navidrome_excluded_playlist_prefixes(settings: Settings) -> tuple[str, ...]:
    prefixes = ["Uncurated:"]
    configured = settings.navidrome.playlist_name_prefix.strip()
    if configured:
        prefixes.append(configured)
    return tuple(dict.fromkeys(prefixes))


def _playlist_from_cached_number(cache_path: Path, number: int):
    playlists = read_playlist_cache(cache_path)
    if number < 1 or number > len(playlists):
        raise ValueError(f"Playlist number {number} is out of range for cached results at {cache_path}")
    return playlists[number - 1]


def _resolve_target_genre(settings: Settings, target_genre: str) -> str:
    cleaned = target_genre.strip()
    if not cleaned:
        raise ValueError("target genre cannot be blank")
    genre_map = GenreMap.load(settings.genre_map_path)
    resolved = genre_map.resolve(cleaned, settings.missing_genre_dir)
    return resolved.canonical_genre if resolved.canonical_genre and not resolved.missing else cleaned


def _local_path_for_navidrome_song(settings: Settings, navidrome_path: str | None) -> Path:
    if not navidrome_path:
        raise ValueError("current Navidrome track did not include a path")
    raw_path = Path(navidrome_path)
    server_root = settings.navidrome.library_root
    if raw_path.is_absolute():
        try:
            relative_path = raw_path.relative_to(server_root)
        except ValueError as exc:
            raise ValueError(f"current Navidrome track is outside configured library_root: {navidrome_path}") from exc
    else:
        relative_path = raw_path
    local_path = settings.dj_library_dir / relative_path
    if not local_path.exists():
        raise FileNotFoundError(f"current track file does not exist locally: {local_path}")
    if not local_path.is_file():
        raise ValueError(f"current track path is not a file: {local_path}")
    return local_path


def _update_re_genred_song_database(
    settings: Settings,
    previous_path: Path,
    new_path: Path,
    previous_genre: str | None,
    new_genre: str,
) -> None:
    connection = connect(settings.database_path)
    initialize(connection)
    try:
        row = connection.execute("SELECT id FROM song WHERE current_path = ?", (str(previous_path),)).fetchone()
        if row is None and previous_path != new_path:
            row = connection.execute("SELECT id FROM song WHERE current_path = ?", (str(new_path),)).fetchone()
        if row is None:
            return
        database_genre_id = genre_id(connection, new_genre)
        database.update_consolidated_song(
            connection,
            song_id=int(row["id"]),
            genre_id=database_genre_id,
            display_genre=new_genre,
            new_path=new_path,
            new_hash=sha256_file(new_path),
            previous_path=previous_path,
            previous_genre=previous_genre,
        )
    finally:
        connection.close()


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


def _update_mixed_in_key_database(settings: Settings, updates: list[MixedInKeyUpdate]) -> None:
    connection = connect(settings.database_path)
    initialize(connection)
    try:
        with connection:
            for update in updates:
                if update.status != "updated" or update.info is None:
                    continue
                metadata = read_metadata(update.target_path)
                row = connection.execute(
                    "SELECT id FROM song WHERE current_path = ?",
                    (str(update.source_path),),
                ).fetchone()
                if row is None:
                    row = connection.execute(
                        "SELECT id FROM song WHERE current_path = ?",
                        (str(update.target_path),),
                    ).fetchone()
                if row is None:
                    continue
                connection.execute(
                    """
                    UPDATE song
                    SET current_path = ?, bpm = ?, raw_musical_key = ?, musical_key = ?,
                        file_hash_with_metadata = ?, file_size = ?, updated_at = datetime('now')
                    WHERE id = ?
                    """,
                    (
                        str(update.target_path),
                        update.info.bpm,
                        update.info.key,
                        metadata.camelot_key or update.info.key,
                        sha256_file(update.target_path),
                        update.target_path.stat().st_size,
                        int(row["id"]),
                    ),
                )
    finally:
        connection.close()


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

    if _is_delete_update(update_genre):
        source_path = Path(row["source_path"])
        old_target_path = Path(row["target_path"]) if row.get("target_path") else None
        return source_path.exists() or (old_target_path is not None and old_target_path.exists())

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
    if _is_delete_update(update_genre):
        _delete_update_row(settings, source_path, old_target_path)
        return "deleted"

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
    if processed.status not in {"processed", "needs_review", "unchanged", "duplicate"}:
        raise ValueError(f"Re-transfer failed: {processed.notes or processed.status}")

    _cleanup_stale_uncategorizable_outputs(settings, source_path, processed.target_path, old_target_path)
    return "moved" if _is_under(processed.target_path, settings.dj_library_dir) else "skipped"


def _is_delete_update(update_genre: str) -> bool:
    return update_genre.strip().casefold() == "delete"


def _delete_update_row(settings: Settings, source_path: Path, old_target_path: Path | None) -> None:
    stale_paths = set()
    if old_target_path is not None:
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
    stale_ids = [int(row["id"]) for row in rows]
    stale_paths.update(Path(row["current_path"]) for row in rows)

    for stale_path in stale_paths:
        if stale_path.exists():
            stale_path.unlink()
            _remove_known_empty_parents(settings, stale_path.parent)

    if source_path.exists():
        source_path.unlink()
        _remove_known_empty_parents(settings, source_path.parent)

    if stale_ids:
        placeholders = ",".join("?" for _ in stale_ids)
        with connection:
            connection.execute(f"DELETE FROM song_processing_label WHERE song_id IN ({placeholders})", stale_ids)
            connection.execute(f"DELETE FROM song_operation_log WHERE song_id IN ({placeholders})", stale_ids)
            connection.execute(f"DELETE FROM song WHERE id IN ({placeholders})", stale_ids)
    connection.close()


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


def _remove_known_empty_parents(settings: Settings, start: Path) -> None:
    for stop in (settings.uncategorizable_dir, settings.dj_library_dir, settings.unprocessed_music_dir):
        try:
            start.resolve().relative_to(stop.resolve())
        except ValueError:
            continue
        _remove_empty_parents(start, stop)
        return


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


def _render_curation_sync_result(result: CurationSyncResult, verbose: bool = False) -> None:
    action = "Would sync" if result.dry_run else "Synced"
    typer.echo(f"{action} Navidrome curation (source: {result.source})")
    if result.api_partial:
        typer.echo("API source is partial: it syncs starred tracks and playlist members, not every rated track.")
    if result.backup_path is not None:
        typer.echo(f"Backup: {result.backup_path}")
    if verbose:
        changed = [plan for plan in result.tracks if plan.status == "changed"]
        unsupported = [plan for plan in result.tracks if plan.status == "unsupported"]
        errors = [plan for plan in result.tracks if plan.status == "error"]
        if changed:
            typer.echo("Tag updates: Navidrome curation differs from local MP3 curation frames; these are the planned frame changes.")
            for plan in changed:
                typer.echo(f"- {plan.track.local_path}")
                for change in plan.changes:
                    typer.echo(f"  {change}")
        if unsupported:
            typer.echo("Unsupported metadata writes: these files stay in playlist exports, but tag writes are MP3-only.")
            for plan in unsupported:
                typer.echo(f"- unsupported: {plan.track.local_path}: {plan.notes}")
        if errors:
            typer.echo("Errors: these files could not be inspected or written and need manual review.")
            for plan in errors:
                typer.echo(f"- error: {plan.track.local_path}: {plan.notes}")
        if result.playlist_exports:
            typer.echo("Playlist exports: write means the .m3u8 content differs from the current exported file or does not exist yet.")
            for playlist in result.playlist_exports:
                status = "write" if playlist.changed else "unchanged"
                typer.echo(f"- playlist {status}: {playlist.path} ({len(playlist.tracks)} tracks)")
        if result.missing:
            typer.echo(
                "Missing/stale paths: Navidrome points at paths that do not currently resolve locally; "
                "no tag or playlist write is made for these paths."
            )
            for navidrome_path, local_path in result.missing:
                typer.echo(f"- missing/stale: {navidrome_path} -> {local_path}")
                typer.echo(f"  Meaning: {_curation_missing_path_explanation(navidrome_path, local_path)}")
    elif result.unsupported_files or result.error_files or result.missing:
        typer.echo("Use --verbose to show skipped files and errors.")
    typer.echo(
        "Summary: "
        f"files changed={result.changed_files}, "
        f"playlists written={result.playlists_written}, "
        f"skipped missing={len(result.missing)}, "
        f"skipped unsupported={result.unsupported_files}, "
        f"errors={result.error_files}, "
        f"unchanged={result.unchanged_files}"
    )


def _curation_missing_path_explanation(navidrome_path: str, local_path: str) -> str:
    if ".sync-conflict-" in Path(navidrome_path).name:
        return (
            "this is a Syncthing conflict-copy filename recorded in Navidrome, but that conflict file is not present locally. "
            "It is usually safe to ignore for curation sync; rescan Navidrome or remove the stale conflict file there if it keeps appearing."
        )
    if ".sync-conflict-" in Path(local_path).name:
        return (
            "Navidrome points at a Syncthing conflict-copy filename that is missing locally. "
            "Compare it with the normal track filename before deleting anything; curation sync skipped it."
        )
    return "the path from Navidrome did not match an existing local file, so curation sync skipped it."


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
