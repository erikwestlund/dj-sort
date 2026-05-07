from pathlib import Path

from dj_sort.consolidation import consolidate_genres
from dj_sort.database import connect, initialize, save_processed_song
from dj_sort.planning import Plan
from dj_sort.settings import Settings


def test_consolidation_dry_run_plans_move(tmp_path: Path) -> None:
    library = tmp_path / "library"
    current = library / "DnB" / "Artist - Title.mp3"
    current.parent.mkdir(parents=True)
    current.write_bytes(b"\xff\xfb" + b"audio-data")
    settings = Settings(
        source_root=tmp_path / "source",
        library_root=library,
        processed_source_root=tmp_path / "archive",
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
