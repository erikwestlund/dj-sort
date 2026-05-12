from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from mutagen.id3 import ID3

from dj_sort.curation import POPM_EMAIL, RATING_TO_POPM, read_mp3_curation_tags, sync_navidrome_curation, sync_navidrome_curation_from_api
from dj_sort.navidrome import NavidromePlaylist
from dj_sort.settings import Settings


def test_popm_rating_mapping() -> None:
    assert RATING_TO_POPM == {1: 1, 2: 64, 3: 128, 4: 196, 5: 255}


def test_sync_writes_mp3_rating_favorite_and_crates(tmp_path: Path) -> None:
    library = tmp_path / "Library"
    track = library / "House" / "Artist - Track.mp3"
    track.parent.mkdir(parents=True)
    track.write_bytes(b"audio")
    navidrome_db = tmp_path / "navidrome.db"
    _write_navidrome_db(navidrome_db, [("song-1", "House/Artist - Track.mp3")])
    _add_annotation(navidrome_db, "song-1", rating=5, starred=1)
    _add_playlist(navidrome_db, "Crate A", ["song-1"])
    _add_playlist(navidrome_db, "Uncurated: House", ["song-1"])

    result = sync_navidrome_curation(_settings(tmp_path, library), navidrome_db, "erik", write=True)

    tags = ID3(track)
    popm = tags.getall("POPM")[0]
    assert result.changed_files == 1
    assert popm.email == POPM_EMAIL
    assert popm.rating == 255
    assert read_mp3_curation_tags(track) == {"rating": 5, "popm": 255, "starred": True, "crates": ("Crate A",)}


def test_sync_exports_playlists_and_favorites(tmp_path: Path) -> None:
    library = tmp_path / "Library"
    track = library / "House" / "Artist - Track.mp3"
    other = library / "Techno" / "Other - Track.m4a"
    track.parent.mkdir(parents=True)
    other.parent.mkdir(parents=True)
    track.write_bytes(b"audio")
    other.write_bytes(b"audio")
    navidrome_db = tmp_path / "navidrome.db"
    _write_navidrome_db(navidrome_db, [("song-1", "House/Artist - Track.mp3"), ("song-2", "Techno/Other - Track.m4a")])
    _add_annotation(navidrome_db, "song-1", rating=5, starred=1)
    _add_playlist(navidrome_db, "Set Ideas", ["song-2", "song-1"])

    result = sync_navidrome_curation(_settings(tmp_path, library), navidrome_db, "erik", write=True)

    playlist_dir = tmp_path / "exports"
    assert result.playlists_written == 2
    assert (playlist_dir / "Favorites.m3u8").read_text(encoding="utf-8") == f"#EXTM3U\n{track}\n"
    assert (playlist_dir / "Set Ideas.m3u8").read_text(encoding="utf-8") == f"#EXTM3U\n{track}\n{other}\n"


def test_sync_dry_run_does_not_write(tmp_path: Path) -> None:
    library = tmp_path / "Library"
    track = library / "House" / "Artist - Track.mp3"
    track.parent.mkdir(parents=True)
    track.write_bytes(b"audio")
    navidrome_db = tmp_path / "navidrome.db"
    _write_navidrome_db(navidrome_db, [("song-1", "House/Artist - Track.mp3")])
    _add_annotation(navidrome_db, "song-1", rating=4, starred=0)

    result = sync_navidrome_curation(_settings(tmp_path, library), navidrome_db, "erik", write=False)

    assert result.changed_files == 1
    assert (tmp_path / "exports").exists() is False
    assert (tmp_path / "backups").exists() is False
    assert read_mp3_curation_tags(track)["rating"] is None


def test_sync_write_creates_automated_backup(tmp_path: Path) -> None:
    library = tmp_path / "Library"
    track = library / "House" / "Artist - Track.mp3"
    track.parent.mkdir(parents=True)
    track.write_bytes(b"audio")
    navidrome_db = tmp_path / "navidrome.db"
    _write_navidrome_db(navidrome_db, [("song-1", "House/Artist - Track.mp3")])
    _add_annotation(navidrome_db, "song-1", rating=4, starred=1)
    playlist_dir = tmp_path / "exports"
    playlist_dir.mkdir()
    (playlist_dir / "Favorites.m3u8").write_text("#EXTM3U\nold\n", encoding="utf-8")

    result = sync_navidrome_curation(_settings(tmp_path, library), navidrome_db, "erik", write=True)

    assert result.backup_path is not None
    assert (result.backup_path / "navidrome.db").read_bytes() == navidrome_db.read_bytes()
    assert (result.backup_path / "playlists-before" / "Favorites.m3u8").read_text(encoding="utf-8") == "#EXTM3U\nold\n"
    tags_before = json.loads((result.backup_path / "mp3-tags-before.json").read_text(encoding="utf-8"))
    assert tags_before == [
        {
            "navidrome_path": "House/Artist - Track.mp3",
            "path": str(track),
            "tags": {"crates": [], "popm": None, "rating": None, "starred": False},
        }
    ]
    manifest = json.loads((result.backup_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["source"] == "database"
    assert manifest["tracks"][0]["changes"] == ["rating: None -> 4", "popm: None -> 196", "starred: False -> True"]


def test_sync_treats_database_rating_zero_as_unrated(tmp_path: Path) -> None:
    library = tmp_path / "Library"
    track = library / "House" / "Artist - Track.mp3"
    track.parent.mkdir(parents=True)
    track.write_bytes(b"audio")
    navidrome_db = tmp_path / "navidrome.db"
    _write_navidrome_db(navidrome_db, [("song-1", "House/Artist - Track.mp3")])
    _add_annotation(navidrome_db, "song-1", rating=0, starred=0)

    result = sync_navidrome_curation(_settings(tmp_path, library), navidrome_db, "erik", write=False)

    assert result.tracks == []


def test_sync_is_idempotent(tmp_path: Path) -> None:
    library = tmp_path / "Library"
    track = library / "House" / "Artist - Track.mp3"
    track.parent.mkdir(parents=True)
    track.write_bytes(b"audio")
    navidrome_db = tmp_path / "navidrome.db"
    _write_navidrome_db(navidrome_db, [("song-1", "House/Artist - Track.mp3")])
    _add_annotation(navidrome_db, "song-1", rating=3, starred=0)

    first = sync_navidrome_curation(_settings(tmp_path, library), navidrome_db, "erik", write=True)
    second = sync_navidrome_curation(_settings(tmp_path, library), navidrome_db, "erik", write=True)

    assert first.changed_files == 1
    assert second.changed_files == 0
    assert second.unchanged_files == 1


def test_sync_reports_unsupported_and_uses_stale_path_fallback(tmp_path: Path) -> None:
    library = tmp_path / "Library"
    old_path = library / "Old" / "Artist - Track.mp3"
    new_path = library / "New" / "Artist - Track.mp3"
    m4a = library / "House" / "Other - Track.m4a"
    new_path.parent.mkdir(parents=True)
    m4a.parent.mkdir(parents=True)
    new_path.write_bytes(b"audio")
    m4a.write_bytes(b"audio")
    _write_stale_dj_sort_db(tmp_path / "library.sqlite3", old_path, new_path)
    navidrome_db = tmp_path / "navidrome.db"
    _write_navidrome_db(navidrome_db, [("song-1", "Old/Artist - Track.mp3"), ("song-2", "House/Other - Track.m4a")])
    _add_annotation(navidrome_db, "song-1", rating=5, starred=0)
    _add_annotation(navidrome_db, "song-2", rating=5, starred=0)

    result = sync_navidrome_curation(_settings(tmp_path, library), navidrome_db, "erik", write=False)

    assert len(result.missing) == 0
    assert result.unsupported_files == 1
    assert any(plan.track.local_path == new_path for plan in result.tracks)


def test_sync_from_api_uses_playlist_and_starred_payloads(tmp_path: Path) -> None:
    library = tmp_path / "Library"
    playlist_track = library / "House" / "Artist - Track.mp3"
    starred_track = library / "Techno" / "Starred - Track.mp3"
    playlist_track.parent.mkdir(parents=True)
    starred_track.parent.mkdir(parents=True)
    playlist_track.write_bytes(b"audio")
    starred_track.write_bytes(b"audio")
    client = FakeNavidromeClient()

    result = sync_navidrome_curation_from_api(_settings(tmp_path, library), client, write=False)

    assert result.changed_files == 2
    assert {plan.track.id for plan in result.tracks} == {"song-1", "song-2"}
    crates = {plan.track.id: plan.track.crates for plan in result.tracks}
    assert crates["song-1"] == ("Crate A",)
    assert crates["song-2"] == ()
    assert any(export.name == "Favorites" and export.tracks == (starred_track,) for export in result.playlist_exports)


def _settings(tmp_path: Path, library: Path) -> Settings:
    return Settings(
        unprocessed_music_dir=tmp_path / "source",
        dj_library_dir=library,
        database_path=tmp_path / "library.sqlite3",
        automated_backup_dir=tmp_path / "backups",
        navidrome={"library_root": "/music", "output_dir": tmp_path / "exports", "playlist_name_prefix": "Uncurated: "},
    )


def _write_navidrome_db(path: Path, media: list[tuple[str, str]]) -> None:
    connection = sqlite3.connect(path)
    with connection:
        connection.executescript(
            """
            CREATE TABLE user (id TEXT PRIMARY KEY, user_name TEXT NOT NULL);
            CREATE TABLE media_file (id TEXT PRIMARY KEY, path TEXT NOT NULL);
            CREATE TABLE annotation (
              ann_id TEXT PRIMARY KEY,
              user_id TEXT NOT NULL,
              item_id TEXT NOT NULL,
              item_type TEXT NOT NULL,
              rating INTEGER,
              starred INTEGER DEFAULT 0 NOT NULL
            );
            CREATE TABLE playlist (id TEXT PRIMARY KEY, name TEXT NOT NULL, owner TEXT DEFAULT '', public INTEGER DEFAULT 0);
            CREATE TABLE playlist_tracks (id INTEGER NOT NULL, playlist_id TEXT NOT NULL, media_file_id TEXT NOT NULL);
            INSERT INTO user (id, user_name) VALUES ('user-1', 'erik');
            """
        )
        connection.executemany("INSERT INTO media_file (id, path) VALUES (?, ?)", media)
    connection.close()


def _add_annotation(path: Path, item_id: str, rating: int, starred: int) -> None:
    connection = sqlite3.connect(path)
    with connection:
        connection.execute(
            "INSERT INTO annotation (ann_id, user_id, item_id, item_type, rating, starred) VALUES (?, 'user-1', ?, 'media_file', ?, ?)",
            (f"ann-{item_id}", item_id, rating, starred),
        )
    connection.close()


def _add_playlist(path: Path, name: str, track_ids: list[str]) -> None:
    connection = sqlite3.connect(path)
    playlist_id = name.casefold().replace(" ", "-")
    with connection:
        connection.execute("INSERT INTO playlist (id, name, owner, public) VALUES (?, ?, 'erik', 0)", (playlist_id, name))
        connection.executemany(
            "INSERT INTO playlist_tracks (id, playlist_id, media_file_id) VALUES (?, ?, ?)",
            [(index, playlist_id, track_id) for index, track_id in enumerate(track_ids, start=1)],
        )
    connection.close()


def _write_stale_dj_sort_db(path: Path, old_path: Path, new_path: Path) -> None:
    connection = sqlite3.connect(path)
    with connection:
        connection.execute("CREATE TABLE song_operation_log (previous_path TEXT, new_path TEXT, created_at TEXT)")
        connection.execute(
            "INSERT INTO song_operation_log (previous_path, new_path, created_at) VALUES (?, ?, '2026-05-12T00:00:00Z')",
            (str(old_path), str(new_path)),
        )
    connection.close()


class FakeNavidromeClient:
    def playlists(self) -> list[NavidromePlaylist]:
        return [
            NavidromePlaylist(id="crate-a", name="Crate A", song_count=1),
            NavidromePlaylist(id="uncurated", name="Uncurated: House", song_count=1),
        ]

    def playlist_song_payloads(self, playlist_id: str) -> list[dict[str, object]]:
        if playlist_id == "uncurated":
            return [{"id": "song-1", "path": "House/Artist - Track.mp3", "userRating": 5}]
        return [{"id": "song-1", "path": "House/Artist - Track.mp3", "userRating": 5}]

    def starred_song_payloads(self) -> list[dict[str, object]]:
        return [{"id": "song-2", "path": "Techno/Starred - Track.mp3", "starred": "2026-05-12T00:00:00Z", "userRating": 4}]
