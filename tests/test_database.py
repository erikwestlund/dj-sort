from pathlib import Path

from dj_sort.database import connect, duplicate_report, excluded_report, initialize, save_excluded_song, save_processed_song
from dj_sort.planning import Plan, SkippedFile


def test_save_processed_song_links_potential_duplicates(tmp_path: Path) -> None:
    db_path = tmp_path / "library.sqlite3"
    connection = connect(db_path)
    initialize(connection)

    plan_1 = _plan(tmp_path / "source-1.mp3", tmp_path / "library" / "House" / "A - B.mp3")
    plan_2 = _plan(tmp_path / "source-2.mp3", tmp_path / "library" / "House" / "A - B - 2.mp3")

    save_processed_song(connection, plan_1, "hash-1", "final-1", None, "processed")
    save_processed_song(connection, plan_2, "hash-2", "final-2", None, "processed")

    report = duplicate_report(connection)

    assert report["summary"]["potential_duplicate_groups"] == 1


def test_save_processed_song_links_exact_duplicates(tmp_path: Path) -> None:
    db_path = tmp_path / "library.sqlite3"
    connection = connect(db_path)
    initialize(connection)

    plan_1 = _plan(tmp_path / "source-1.mp3", tmp_path / "library" / "House" / "A - B.mp3")
    plan_2 = _plan(tmp_path / "source-2.mp3", tmp_path / "library" / "House" / "A - B - 2.mp3")

    save_processed_song(connection, plan_1, "hash-1", "final-1", "audio-hash", "processed")
    save_processed_song(connection, plan_2, "hash-2", "final-2", "audio-hash", "processed")

    report = duplicate_report(connection)

    assert report["summary"]["exact_duplicate_groups"] == 1


def test_excluded_report_filters_by_reason(tmp_path: Path) -> None:
    db_path = tmp_path / "library.sqlite3"
    connection = connect(db_path)
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

    report = excluded_report(connection, reason="blacklist_substring")

    assert report["summary"]["excluded"] == 1
    assert report["summary"]["reason"] == "blacklist_substring"
    assert report["excluded"][0]["source_path"] == str(tmp_path / "live.mp3")


def _plan(source: Path, target: Path) -> Plan:
    return Plan(
        source_path=source,
        target_path=target,
        artist="Artist",
        normalized_artist="artist",
        title="Title",
        normalized_title="title",
        raw_genre="House",
        canonical_genre="House",
        bpm="128",
        raw_key="8A",
        camelot_key="8A",
        album=None,
        album_artist=None,
        track_number=None,
        release_date=None,
        duration_ms=None,
        bitrate=None,
        sample_rate=None,
        file_size=123,
        extension="mp3",
        labels=(),
        needs_review=False,
        collision_adjusted=False,
    )
