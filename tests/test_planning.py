from pathlib import Path

from dj_sort.genres import GenreMap
from dj_sort.metadata import TrackMetadata
from dj_sort.planning import build_plans
from dj_sort.settings import Settings


def test_build_plans_routes_non_whitelisted_genres_to_uncategorizable_dir(tmp_path: Path, monkeypatch) -> None:
    track = tmp_path / "Ambient Track.mp3"
    settings = _settings(tmp_path)
    genre_map_path = tmp_path / "genres.yaml"
    genre_map_path.write_text("genres:\n  House Music: House\n  Techno:\n", encoding="utf-8")
    genre_map = GenreMap.load(genre_map_path)

    monkeypatch.setattr("dj_sort.planning.scan_audio_files", lambda source_root, recursive, limit: ([track], []))
    monkeypatch.setattr(
        "dj_sort.planning.read_metadata",
        lambda path: _metadata(path, genre="Ambient", duration_ms=180_000),
    )

    result = build_plans(settings, genre_map)

    assert result.skipped == []
    assert len(result.plans) == 1
    assert result.plans[0].canonical_genre == "Ambient"
    assert result.plans[0].target_path.parent == settings.uncategorizable_dir / settings.unmapped_genre_dir
    assert "Unmapped Genre" in result.plans[0].labels


def test_build_plans_skips_tracks_over_max_duration(tmp_path: Path, monkeypatch) -> None:
    track = tmp_path / "Long Track.mp3"
    settings = _settings(tmp_path)
    genre_map_path = tmp_path / "genres.yaml"
    genre_map_path.write_text("genres:\n  House Music: House\n", encoding="utf-8")
    genre_map = GenreMap.load(genre_map_path)

    monkeypatch.setattr("dj_sort.planning.scan_audio_files", lambda source_root, recursive, limit: ([track], []))
    monkeypatch.setattr(
        "dj_sort.planning.read_metadata",
        lambda path: _metadata(path, genre="House Music", duration_ms=16 * 60 * 1000),
    )

    result = build_plans(settings, genre_map)

    assert result.plans == []
    assert len(result.skipped) == 1
    assert result.skipped[0].reason == "duration_exceeds_max"
    assert result.skipped[0].canonical_genre == "House"


def test_build_plans_skips_blacklisted_substrings(tmp_path: Path, monkeypatch) -> None:
    track = tmp_path / "Artist - Title (Live).mp3"
    settings = _settings(tmp_path).model_copy(update={"blacklist_substrings": ["(Live", "Bonkers"]})
    genre_map_path = tmp_path / "genres.yaml"
    genre_map_path.write_text("genres:\n  House Music: House\n", encoding="utf-8")
    genre_map = GenreMap.load(genre_map_path)

    monkeypatch.setattr("dj_sort.planning.scan_audio_files", lambda source_root, recursive, limit: ([track], []))
    monkeypatch.setattr(
        "dj_sort.planning.read_metadata",
        lambda path: _metadata(path, genre="House Music", duration_ms=180_000, title="Title (Live)"),
    )

    result = build_plans(settings, genre_map)

    assert result.plans == []
    assert len(result.skipped) == 1
    assert result.skipped[0].reason == "blacklist_substring"
    assert result.skipped[0].notes == "matched blacklist substring: (Live"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        unprocessed_music_dir=tmp_path / "source",
        dj_library_dir=tmp_path / "library",
        uncategorizable_dir=tmp_path / "uncategorizable",
        duplicates_dir=tmp_path / "duplicates",
        database_path=tmp_path / "db.sqlite3",
        genre_map_path=tmp_path / "genres.yaml",
        dry_run=True,
        write_canonical_genre_to_metadata=False,
    )


def _metadata(path: Path, genre: str | None, duration_ms: int | None, title: str = "Title") -> TrackMetadata:
    return TrackMetadata(
        path=path,
        artist="Artist",
        title=title,
        genre=genre,
        bpm=None,
        raw_key=None,
        camelot_key=None,
        album=None,
        album_artist=None,
        track_number=None,
        release_date=None,
        duration_ms=duration_ms,
        bitrate=None,
        sample_rate=None,
        file_size=123,
        extension="mp3",
        inferred=False,
        labels=(),
    )
