from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from dj_sort.cli import _resolve_scan_dir, app
from dj_sort.database import connect, initialize, save_excluded_song
from dj_sort.metadata import TrackMetadata
from dj_sort.planning import Plan, PlanningResult, SkippedFile
from dj_sort.settings import Settings

runner = CliRunner()


def test_excluded_command_filters_by_reason(tmp_path: Path) -> None:
    settings_path = _write_settings(tmp_path)
    database_path = tmp_path / "library.sqlite3"
    connection = connect(database_path)
    initialize(connection)
    save_excluded_song(
        connection,
        SkippedFile(
            path=tmp_path / "ambient.mp3",
            reason="genre_not_whitelisted",
            raw_genre="Ambient",
            canonical_genre="Ambient",
            duration_ms=180_000,
        ),
    )
    save_excluded_song(
        connection,
        SkippedFile(
            path=tmp_path / "live.mp3",
            reason="blacklist_substring",
            raw_genre="House Music",
            canonical_genre="House",
            duration_ms=240_000,
            notes="matched blacklist substring: (Live",
        ),
    )
    connection.close()

    result = runner.invoke(app, ["excluded", "--settings", str(settings_path), "--reason", "blacklist_substring"])

    assert result.exit_code == 0
    assert "Excluded files: 1" in result.stdout
    assert "live.mp3" in result.stdout
    assert "ambient.mp3" not in result.stdout


def test_resolve_scan_dir_defaults_paths_under_unprocessed(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    result = _resolve_scan_dir(settings, None, Path("2026-05-04"))

    assert result == tmp_path / "source" / "2026-05-04"


def test_resolve_scan_dir_accepts_reserved_base_dir(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    result = _resolve_scan_dir(settings, "uncategorizable", Path("Missing Genre"))

    assert result == tmp_path / "uncategorizable" / "Missing Genre"


def test_resolve_scan_dir_accepts_absolute_path(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    result = _resolve_scan_dir(settings, "dj_library", tmp_path / "external")

    assert result == tmp_path / "external"


def test_transfer_scopes_input_to_relative_unprocessed_path(tmp_path: Path, monkeypatch) -> None:
    settings_path = _write_settings(tmp_path)
    captured = {}

    def fake_build_plans(settings: Settings, genre_map, limit=None, genre_override=None):
        captured["unprocessed_music_dir"] = settings.unprocessed_music_dir
        captured["limit"] = limit
        captured["genre_override"] = genre_override
        return PlanningResult(plans=[], skipped=[])

    monkeypatch.setattr("dj_sort.cli.build_plans", fake_build_plans)

    result = runner.invoke(app, ["transfer", "2026-05-04", "--settings", str(settings_path), "--limit", "10"])

    assert result.exit_code == 0
    assert captured == {"unprocessed_music_dir": tmp_path / "source" / "2026-05-04", "limit": 10, "genre_override": None}
    assert "Planned files: 0" in result.stdout


def test_transfer_passes_force_genre_to_planning(tmp_path: Path, monkeypatch) -> None:
    settings_path = _write_settings(tmp_path)
    captured = {}

    def fake_build_plans(settings: Settings, genre_map, limit=None, genre_override=None):
        captured["genre_override"] = genre_override
        return PlanningResult(plans=[], skipped=[])

    monkeypatch.setattr("dj_sort.cli.build_plans", fake_build_plans)

    result = runner.invoke(app, ["transfer", "batch", "--settings", str(settings_path), "--force-genre", " Electro House "])

    assert result.exit_code == 0
    assert captured == {"genre_override": "Electro House"}
    assert "Planned files: 0" in result.stdout


def test_export_uncategorizable_writes_update_genre_csv(tmp_path: Path, monkeypatch) -> None:
    settings_path = _write_settings(tmp_path)
    output = tmp_path / "review.csv"
    source = tmp_path / "source" / "batch" / "track.mp3"
    target = tmp_path / "uncategorizable" / "Unmapped Genre" / "Artist - Title.mp3"

    monkeypatch.setattr(
        "dj_sort.cli.build_plans",
        lambda settings, genre_map, limit=None: PlanningResult(plans=[_plan(source, target)], skipped=[]),
    )

    result = runner.invoke(app, ["export-uncategorizable", "batch", "--settings", str(settings_path), "--output", str(output)])

    assert result.exit_code == 0
    contents = output.read_text(encoding="utf-8")
    assert contents.startswith("update_genre,raw_genre,canonical_genre")
    assert ",Electronic,Electronic,Unmapped Genre,Artist,Title," in contents
    assert str(source) in contents


def test_export_uncategorizable_defaults_to_report_path(tmp_path: Path, monkeypatch) -> None:
    settings_path = _write_settings(tmp_path)
    source = tmp_path / "source" / "batch" / "track.mp3"
    target = tmp_path / "uncategorizable" / "Unmapped Genre" / "Artist - Title.mp3"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "dj_sort.cli.build_plans",
        lambda settings, genre_map, limit=None: PlanningResult(plans=[_plan(source, target)], skipped=[]),
    )

    result = runner.invoke(app, ["export-uncategorizable", "batch", "--settings", str(settings_path)])

    output = tmp_path / "reports" / "uncategorizable" / "batch.csv"
    assert result.exit_code == 0
    assert output.exists()
    assert f"Exported 1 uncategorizable rows to {output.relative_to(tmp_path)}" in result.stdout


def test_apply_genre_updates_dry_run_lists_updates(tmp_path: Path) -> None:
    settings_path = _write_settings(tmp_path)
    csv_path = tmp_path / "review.csv"
    csv_path.write_text(
        "update_genre,raw_genre,source_path\n"
        f"Electro House,Other,{tmp_path / 'source' / 'track.mp3'}\n"
        f",Other,{tmp_path / 'source' / 'skip.mp3'}\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["apply-genre-updates", str(csv_path), "--settings", str(settings_path)])

    assert result.exit_code == 0
    assert "Would update 1 files" in result.stdout
    assert "Other -> Electro House" in result.stdout


def test_apply_genre_updates_resolves_from_uncategorizable_reports(tmp_path: Path, monkeypatch) -> None:
    settings_path = _write_settings(tmp_path)
    report_dir = tmp_path / "reports" / "uncategorizable"
    report_dir.mkdir(parents=True)
    csv_path = report_dir / "2026-05-04.csv"
    csv_path.write_text(
        "update_genre,raw_genre,source_path\n"
        f"Electro House,Other,{tmp_path / 'source' / 'track.mp3'}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["apply-genre-updates", "2026-05-04", "--settings", str(settings_path)])

    assert result.exit_code == 0
    assert "Would update 1 files" in result.stdout


def test_apply_genre_updates_skips_stale_rows_when_source_is_current(tmp_path: Path, monkeypatch) -> None:
    settings_path = _write_settings(tmp_path)
    genre_map = tmp_path / "genres.yaml"
    genre_map.write_text("genres:\n  Electro House: Electro House\n", encoding="utf-8")
    source = tmp_path / "source" / "track.mp3"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source")
    csv_path = tmp_path / "review.csv"
    csv_path.write_text(
        "update_genre,raw_genre,source_path,target_path\n"
        f"Electro House,Other,{source},{tmp_path / 'uncategorizable' / 'Unmapped Genre' / 'Artist - Title.mp3'}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("dj_sort.cli.read_metadata", lambda path: _metadata(path, genre="Electro House"))

    result = runner.invoke(app, ["apply-genre-updates", str(csv_path), "--settings", str(settings_path)])

    assert result.exit_code == 0
    assert "Would update 0 files" in result.stdout


def test_apply_genre_updates_write_removes_stale_uncategorizable_copy(tmp_path: Path, monkeypatch) -> None:
    settings_path = _write_settings(tmp_path)
    source = tmp_path / "source" / "track.mp3"
    stale = tmp_path / "uncategorizable" / "Unmapped Genre" / "Artist - Title.mp3"
    target = tmp_path / "library" / "Electro House" / "Artist - Title.mp3"
    source.parent.mkdir(parents=True)
    stale.parent.mkdir(parents=True)
    target.parent.mkdir(parents=True)
    source.write_bytes(b"source")
    stale.write_bytes(b"stale")
    target.write_bytes(b"target")
    csv_path = tmp_path / "review.csv"
    csv_path.write_text(
        "update_genre,raw_genre,source_path,target_path\n"
        f"Electro House,Other,{source},{stale}\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("dj_sort.cli.write_genre", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "dj_sort.cli.build_plan_for_file",
        lambda settings, genre_map, path: PlanningResult(plans=[_plan(source, target)], skipped=[]),
    )
    monkeypatch.setattr(
        "dj_sort.cli.process_plans",
        lambda settings, planning_result: SimpleNamespace(
            processed=[SimpleNamespace(status="processed", target_path=target, notes=None)]
        ),
    )

    result = runner.invoke(app, ["apply-genre-updates", str(csv_path), "--settings", str(settings_path), "--write"])

    assert result.exit_code == 0
    assert stale.exists() is False
    assert "Moved to new destinations: 1" in result.stdout


def test_copy_duplicate_genres_dry_run_lists_curated_updates(tmp_path: Path, monkeypatch) -> None:
    settings_path = _write_settings(tmp_path)
    target = tmp_path / "source" / "batch" / "track.mp3"
    duplicate = tmp_path / "source" / "iTunes Library" / "track.mp3"
    genre_map = tmp_path / "genres.yaml"
    genre_map.write_text("genres:\n  Electro House: Electro House\n", encoding="utf-8")

    monkeypatch.setattr(
        "dj_sort.cli.scan_audio_files",
        lambda music_dir, recursive, limit: ([target], []) if music_dir == tmp_path / "source" / "batch" else ([duplicate], []),
    )
    monkeypatch.setattr(
        "dj_sort.cli.read_metadata",
        lambda path: _metadata(path, genre="Other" if path == target else "Electro House"),
    )

    result = runner.invoke(
        app,
        ["copy-duplicate-genres", "batch", "--from-base", "iTunes Library", "--settings", str(settings_path)],
    )

    assert result.exit_code == 0
    assert "Would update 1 files" in result.stdout
    assert "Other -> Electro House" in result.stdout


def _write_settings(tmp_path: Path) -> Path:
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(
        "\n".join(
            [
                f"unprocessed_music_dir: {tmp_path / 'source'}",
                f"dj_library_dir: {tmp_path / 'library'}",
                f"uncategorizable_dir: {tmp_path / 'uncategorizable'}",
                f"duplicates_dir: {tmp_path / 'duplicates'}",
                f"database_path: {tmp_path / 'library.sqlite3'}",
                f"genre_map_path: {tmp_path / 'genres.yaml'}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return settings_path


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        unprocessed_music_dir=tmp_path / "source",
        dj_library_dir=tmp_path / "library",
        uncategorizable_dir=tmp_path / "uncategorizable",
        duplicates_dir=tmp_path / "duplicates",
        database_path=tmp_path / "library.sqlite3",
        genre_map_path=tmp_path / "genres.yaml",
    )


def _plan(source: Path, target: Path) -> Plan:
    return Plan(
        source_path=source,
        target_path=target,
        artist="Artist",
        normalized_artist="artist",
        title="Title",
        normalized_title="title",
        raw_genre="Electronic",
        canonical_genre="Electronic",
        bpm="128",
        raw_key=None,
        camelot_key=None,
        album=None,
        album_artist=None,
        track_number=None,
        release_date=None,
        duration_ms=180_000,
        bitrate=320000,
        sample_rate=None,
        file_size=123,
        extension="mp3",
        labels=("Unmapped Genre",),
        needs_review=True,
        collision_adjusted=False,
    )


def _metadata(path: Path, genre: str | None) -> TrackMetadata:
    return TrackMetadata(
        path=path,
        artist="Artist",
        title="Title",
        genre=genre,
        bpm=None,
        raw_key=None,
        camelot_key=None,
        album=None,
        album_artist=None,
        track_number=None,
        release_date=None,
        duration_ms=180_000,
        bitrate=320000,
        sample_rate=None,
        file_size=123,
        extension="mp3",
        inferred=False,
        labels=(),
    )
