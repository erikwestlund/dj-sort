from pathlib import Path

from dj_sort.planning import Plan, PlanningResult
from dj_sort.processing import process_plans
from dj_sort.settings import Settings


def test_processing_is_idempotent_by_source_hash(tmp_path: Path) -> None:
    source = tmp_path / "source"
    library = tmp_path / "library"
    archive = tmp_path / "archive"
    source.mkdir()
    track = source / "Artist - Title.mp3"
    track.write_bytes(b"\xff\xfb" + b"audio-data")
    settings = _settings(source, library, archive, tmp_path / "db.sqlite3")

    first_plan = PlanningResult(plans=[_plan(track, library / "_Needs Genre" / "Artist - Title.mp3")], skipped=[])
    first_result = process_plans(settings, first_plan)
    second_plan = PlanningResult(plans=[_plan(track, library / "_Needs Genre" / "Artist - Title - suffix.mp3")], skipped=[])
    second_result = process_plans(settings, second_plan)

    assert first_result.processed[0].status in {"processed", "needs_review"}
    assert second_result.processed[0].status == "unchanged"
    assert len(list(library.rglob("*.mp3"))) == 1


def test_archive_move_preserves_relative_path_and_removes_empty_dirs(tmp_path: Path) -> None:
    source = tmp_path / "source"
    nested = source / "incoming" / "batch"
    library = tmp_path / "library"
    archive = tmp_path / "archive"
    nested.mkdir(parents=True)
    track = nested / "Artist - Title.mp3"
    track.write_bytes(b"\xff\xfb" + b"audio-data")
    settings = _settings(source, library, archive, tmp_path / "db.sqlite3").model_copy(
        update={"source_completion_action": "archive_move", "remove_empty_source_dirs": True}
    )

    plan = PlanningResult(plans=[_plan(track, library / "_Needs Genre" / "Artist - Title.mp3")], skipped=[])
    result = process_plans(settings, plan)

    archived = archive / "incoming" / "batch" / "Artist - Title.mp3"
    assert result.processed[0].source_cleanup_status == "archive_moved"
    assert archived.exists()
    assert not track.exists()
    assert not nested.exists()


def _settings(source: Path, library: Path, archive: Path, database: Path) -> Settings:
    return Settings(
        source_root=source,
        library_root=library,
        processed_source_root=archive,
        database_path=database,
        genre_map_path=source / "genres.yaml",
        dry_run=False,
        write_canonical_genre_to_metadata=False,
    )


def _plan(source: Path, target: Path) -> Plan:
    return Plan(
        source_path=source,
        target_path=target,
        artist="Artist",
        normalized_artist="artist",
        title="Title",
        normalized_title="title",
        raw_genre=None,
        canonical_genre="_Needs Genre",
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
