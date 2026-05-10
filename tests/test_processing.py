from pathlib import Path

from dj_sort.database import connect, initialize
from dj_sort.planning import Plan, PlanningResult, SkippedFile
from dj_sort.processing import process_plans
from dj_sort.settings import Settings


def test_processing_is_idempotent_by_source_hash(tmp_path: Path) -> None:
    source = tmp_path / "source"
    library = tmp_path / "library"
    uncategorizable = tmp_path / "uncategorizable"
    source.mkdir()
    track = source / "Artist - Title.mp3"
    track.write_bytes(b"\xff\xfb" + b"audio-data")
    settings = _settings(source, library, uncategorizable, tmp_path / "db.sqlite3")

    first_plan = PlanningResult(plans=[_plan(track, uncategorizable / "Missing Genre" / "Artist - Title.mp3")], skipped=[])
    first_result = process_plans(settings, first_plan)
    second_plan = PlanningResult(plans=[_plan(track, uncategorizable / "Missing Genre" / "Artist - Title - suffix.mp3")], skipped=[])
    second_result = process_plans(settings, second_plan)

    assert first_result.processed[0].status in {"processed", "needs_review"}
    assert second_result.processed[0].status == "unchanged"
    assert len(list(uncategorizable.rglob("*.mp3"))) == 1


def test_processing_reprocesses_uncategorizable_source_when_plan_becomes_curated(tmp_path: Path) -> None:
    source = tmp_path / "source"
    library = tmp_path / "library"
    uncategorizable = tmp_path / "uncategorizable"
    source.mkdir()
    track = source / "Artist - Title.mp3"
    track.write_bytes(b"\xff\xfb" + b"audio-data")
    settings = _settings(source, library, uncategorizable, tmp_path / "db.sqlite3")

    first_plan = PlanningResult(plans=[_plan(track, uncategorizable / "Unmapped Genre" / "Artist - Title.mp3")], skipped=[])
    first_result = process_plans(settings, first_plan)
    second_plan = PlanningResult(plans=[_plan(track, library / "House" / "Artist - Title.mp3", canonical_genre="House")], skipped=[])
    second_result = process_plans(settings, second_plan)

    assert first_result.processed[0].status in {"processed", "needs_review"}
    assert second_result.processed[0].status in {"processed", "needs_review"}
    assert (library / "House" / "Artist - Title.mp3").exists()
    assert (uncategorizable / "Unmapped Genre" / "Artist - Title.mp3").exists() is False

    connection = connect(settings.database_path)
    initialize(connection)
    rows = connection.execute("SELECT current_path FROM song WHERE source_path = ?", (str(track),)).fetchall()
    connection.close()
    assert [row["current_path"] for row in rows] == [str(library / "House" / "Artist - Title.mp3")]


def test_processing_reprocesses_library_source_when_target_genre_changes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    library = tmp_path / "library"
    uncategorizable = tmp_path / "uncategorizable"
    source.mkdir()
    track = source / "Artist - Title.mp3"
    track.write_bytes(b"\xff\xfb" + b"audio-data")
    settings = _settings(source, library, uncategorizable, tmp_path / "db.sqlite3")

    first_plan = PlanningResult(plans=[_plan(track, library / "Disco" / "Artist - Title.mp3", canonical_genre="Disco")], skipped=[])
    first_result = process_plans(settings, first_plan)
    second_plan = PlanningResult(
        plans=[_plan(track, library / "Electro House" / "Artist - Title.mp3", canonical_genre="Electro House")],
        skipped=[],
    )
    second_result = process_plans(settings, second_plan)

    assert first_result.processed[0].status in {"processed", "needs_review"}
    assert second_result.processed[0].status in {"processed", "needs_review"}
    assert (library / "Disco" / "Artist - Title.mp3").exists() is False
    assert (library / "Electro House" / "Artist - Title.mp3").exists()

    connection = connect(settings.database_path)
    initialize(connection)
    rows = connection.execute("SELECT current_path FROM song WHERE source_path = ?", (str(track),)).fetchall()
    connection.close()
    assert [row["current_path"] for row in rows] == [str(library / "Electro House" / "Artist - Title.mp3")]


def test_processing_keeps_source_file_after_copy(tmp_path: Path) -> None:
    source = tmp_path / "source"
    library = tmp_path / "library"
    uncategorizable = tmp_path / "uncategorizable"
    source.mkdir()
    track = source / "Artist - Title.mp3"
    track.write_bytes(b"\xff\xfb" + b"audio-data")
    settings = _settings(source, library, uncategorizable, tmp_path / "db.sqlite3")

    plan = PlanningResult(plans=[_plan(track, library / "House" / "Artist - Title.mp3", canonical_genre="House")], skipped=[])
    result = process_plans(settings, plan)

    assert result.processed[0].source_cleanup_status == "kept"
    assert track.exists()
    assert (library / "House" / "Artist - Title.mp3").exists()


def test_processing_records_tracked_exclusions(tmp_path: Path) -> None:
    source = tmp_path / "source"
    library = tmp_path / "library"
    uncategorizable = tmp_path / "uncategorizable"
    source.mkdir()
    settings = _settings(source, library, uncategorizable, tmp_path / "db.sqlite3")

    result = process_plans(
        settings,
        PlanningResult(
            plans=[],
            skipped=[
                SkippedFile(
                    path=source / "Ambient - Long.mp3",
                    reason="duration_exceeds_max",
                    raw_genre="Ambient",
                    canonical_genre="Ambient",
                    duration_ms=180_000,
                )
            ],
        ),
    )

    assert result.processed == []
    connection = connect(settings.database_path)
    initialize(connection)
    row = connection.execute(
        "SELECT source_path, raw_genre, canonical_genre, duration_ms, reason FROM excluded_song"
    ).fetchone()
    connection.close()

    assert row is not None
    assert row["source_path"] == str(source / "Ambient - Long.mp3")
    assert row["raw_genre"] == "Ambient"
    assert row["canonical_genre"] == "Ambient"
    assert row["duration_ms"] == 180_000
    assert row["reason"] == "duration_exceeds_max"


def test_processing_deletes_delete_genre_skips(tmp_path: Path) -> None:
    source = tmp_path / "source"
    library = tmp_path / "library"
    uncategorizable = tmp_path / "uncategorizable"
    source.mkdir()
    track = source / "Delete Me.mp3"
    track.write_bytes(b"delete")
    settings = _settings(source, library, uncategorizable, tmp_path / "db.sqlite3")

    result = process_plans(
        settings,
        PlanningResult(
            plans=[],
            skipped=[
                SkippedFile(
                    path=track,
                    reason="delete_genre",
                    raw_genre="Delete",
                    canonical_genre="Delete",
                    duration_ms=180_000,
                )
            ],
        ),
    )

    assert result.processed == []
    assert track.exists() is False


def test_processing_records_blacklist_exclusion_notes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    library = tmp_path / "library"
    uncategorizable = tmp_path / "uncategorizable"
    source.mkdir()
    settings = _settings(source, library, uncategorizable, tmp_path / "db.sqlite3")

    process_plans(
        settings,
        PlanningResult(
            plans=[],
            skipped=[
                SkippedFile(
                    path=source / "Artist - Title (Live).mp3",
                    reason="blacklist_substring",
                    raw_genre="House Music",
                    canonical_genre="House",
                    duration_ms=180_000,
                    notes="matched blacklist substring: (Live",
                )
            ],
        ),
    )

    connection = connect(settings.database_path)
    initialize(connection)
    row = connection.execute(
        "SELECT reason, notes FROM excluded_song WHERE source_path = ?",
        (str(source / "Artist - Title (Live).mp3"),),
    ).fetchone()
    connection.close()

    assert row is not None
    assert row["reason"] == "blacklist_substring"
    assert row["notes"] == "matched blacklist substring: (Live"


def test_processing_preserves_original_genre_comment_for_curated_output(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    library = tmp_path / "library"
    uncategorizable = tmp_path / "uncategorizable"
    source.mkdir()
    track = source / "Artist - Title.mp3"
    track.write_bytes(b"\xff\xfb" + b"audio-data")
    settings = _settings(source, library, uncategorizable, tmp_path / "db.sqlite3").model_copy(
        update={"write_canonical_genre_to_metadata": True}
    )
    calls = []
    monkeypatch.setattr("dj_sort.processing.write_genre", lambda *args, **kwargs: calls.append((args, kwargs)))

    plan = PlanningResult(
        plans=[_plan(track, library / "House" / "Artist - Title.mp3", raw_genre="House Music", canonical_genre="House")],
        skipped=[],
    )
    process_plans(settings, plan)

    assert calls == [
        (
            (library / "House" / "Artist - Title.mp3", "House"),
            {
                "original_genre": "House Music",
                "original_genre_comment_prefix": "dj-sort original genre:",
            },
        )
    ]


def test_processing_does_not_write_metadata_for_uncategorizable_output(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    library = tmp_path / "library"
    uncategorizable = tmp_path / "uncategorizable"
    source.mkdir()
    track = source / "Artist - Title.mp3"
    track.write_bytes(b"\xff\xfb" + b"audio-data")
    settings = _settings(source, library, uncategorizable, tmp_path / "db.sqlite3").model_copy(
        update={"write_canonical_genre_to_metadata": True}
    )
    calls = []
    monkeypatch.setattr("dj_sort.processing.write_genre", lambda *args, **kwargs: calls.append((args, kwargs)))

    plan = PlanningResult(
        plans=[
            _plan(
                track,
                uncategorizable / "Unmapped Genre" / "Artist - Title.mp3",
                raw_genre="Electronic",
                canonical_genre="Electronic",
            )
        ],
        skipped=[],
    )
    process_plans(settings, plan)

    assert calls == []


def _settings(source: Path, library: Path, uncategorizable: Path, database: Path) -> Settings:
    return Settings(
        unprocessed_music_dir=source,
        dj_library_dir=library,
        uncategorizable_dir=uncategorizable,
        duplicates_dir=source.parent / "duplicates",
        database_path=database,
        genre_map_path=source / "genres.yaml",
        dry_run=False,
        write_canonical_genre_to_metadata=False,
    )


def _plan(source: Path, target: Path, raw_genre: str | None = None, canonical_genre: str = "Missing Genre") -> Plan:
    return Plan(
        source_path=source,
        target_path=target,
        artist="Artist",
        normalized_artist="artist",
        title="Title",
        normalized_title="title",
        raw_genre=raw_genre,
        canonical_genre=canonical_genre,
        bpm=None,
        raw_key=None,
        camelot_key=None,
        album=None,
        album_artist=None,
        track_number=None,
        release_date=None,
        duration_ms=None,
        bitrate=None,
        sample_rate=None,
        file_size=source.stat().st_size,
        extension="mp3",
        labels=(),
        needs_review=False,
        collision_adjusted=False,
    )
