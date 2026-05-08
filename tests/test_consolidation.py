from pathlib import Path

from dj_sort.consolidation import consolidate_genres
from dj_sort.database import connect, initialize, save_processed_song, songs_for_genre
from dj_sort.planning import Plan
from dj_sort.settings import Settings


def test_consolidation_dry_run_plans_move(tmp_path: Path) -> None:
    library = tmp_path / "library"
    current = library / "DnB" / "Artist - Title.mp3"
    current.parent.mkdir(parents=True)
    current.write_bytes(b"\xff\xfb" + b"audio-data")
    settings = Settings(
        unprocessed_music_dir=tmp_path / "source",
        dj_library_dir=library,
        uncategorizable_dir=tmp_path / "uncategorizable",
        duplicates_dir=tmp_path / "duplicates",
        database_path=tmp_path / "db.sqlite3",
        genre_map_path=tmp_path / "genres.yaml",
    )
    connection = connect(settings.database_path)
    initialize(connection)
    save_processed_song(
        connection,
        _plan(current, current),
        original_hash="source-hash",
        final_hash="final-hash",
        audio_hash=None,
        status="processed",
    )
    connection.close()

    result = consolidate_genres(settings, {"DnB": "Drum & Bass"}, dry_run=True)

    assert result.dry_run is True
    assert len(result.actions) == 1
    assert result.actions[0].status == "planned"
    assert result.actions[0].target_path == library / "Drum & Bass" / "Artist - Title.mp3"


def test_consolidation_delete_target_removes_file_and_marks_song_deleted(tmp_path: Path) -> None:
    library = tmp_path / "library"
    current = library / "DnB" / "Artist - Title.mp3"
    current.parent.mkdir(parents=True)
    current.write_bytes(b"\xff\xfb" + b"audio-data")
    settings = Settings(
        unprocessed_music_dir=tmp_path / "source",
        dj_library_dir=library,
        uncategorizable_dir=tmp_path / "uncategorizable",
        duplicates_dir=tmp_path / "duplicates",
        database_path=tmp_path / "db.sqlite3",
        genre_map_path=tmp_path / "genres.yaml",
        remove_empty_genre_dirs=True,
    )
    connection = connect(settings.database_path)
    initialize(connection)
    save_processed_song(
        connection,
        _plan(current, current),
        original_hash="source-hash",
        final_hash="final-hash",
        audio_hash=None,
        status="processed",
    )
    connection.close()

    result = consolidate_genres(settings, {"DnB": "Delete"}, dry_run=False)

    assert len(result.actions) == 1
    assert result.actions[0].status == "deleted"
    assert current.exists() is False
    assert current.parent.exists() is False

    connection = connect(settings.database_path)
    deleted_song = connection.execute(
        "SELECT processing_status, processing_notes FROM song WHERE current_path = ?",
        (str(current),),
    ).fetchone()
    assert deleted_song is not None
    assert deleted_song["processing_status"] == "deleted"
    assert deleted_song["processing_notes"] == "deleted by genre consolidation"
    assert songs_for_genre(connection, "DnB") == []
    connection.close()


def _plan(source: Path, target: Path) -> Plan:
    return Plan(
        source_path=source,
        target_path=target,
        artist="Artist",
        normalized_artist="artist",
        title="Title",
        normalized_title="title",
        raw_genre="DnB",
        canonical_genre="DnB",
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
