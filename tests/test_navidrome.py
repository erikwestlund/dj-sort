import os
import sqlite3
import time
from pathlib import Path

from typer.testing import CliRunner

from dj_sort.cli import app
from dj_sort.curation import CurationSyncResult, CurationTrack, TrackSyncPlan
from dj_sort.metadata import TrackMetadata
from dj_sort.navidrome import NavidromePlaylist, NavidromeScanStatus, NavidromeSong, filter_playlists

runner = CliRunner()


class FakeNavidromeClient:
    def __init__(self) -> None:
        self.ratings: list[tuple[str, int]] = []
        self.starred: list[str] = []
        self.added: list[tuple[str, str]] = []
        self.created: list[str] = []
        self.scans_started = 0
        self.scan_statuses = [NavidromeScanStatus(scanning=False, count=0)]

    def now_playing(self) -> NavidromeSong:
        return NavidromeSong(id="song-1", artist="Artist", title="Track", path="House/Artist - Track.mp3")

    def set_rating(self, song_id: str, rating: int) -> None:
        self.ratings.append((song_id, rating))

    def star(self, song_id: str) -> None:
        self.starred.append(song_id)

    def playlists(self) -> list[NavidromePlaylist]:
        return [
            NavidromePlaylist(id="unc", name="Uncurated: Indie Dance", song_count=10),
            NavidromePlaylist(id="indie", name="Indie Picks", song_count=3),
            NavidromePlaylist(id="house", name="House Crate", song_count=8),
        ]

    def create_playlist(self, name: str) -> NavidromePlaylist:
        self.created.append(name)
        return NavidromePlaylist(id="created", name=name, song_count=0)

    def playlist_songs(self, playlist_id: str) -> list[NavidromeSong]:
        return [
            NavidromeSong(id="song-1", artist="Artist", title="Track", album="Album"),
            NavidromeSong(id="song-2", artist="Other", title="Second"),
        ]

    def add_to_playlist(self, playlist_id: str, song_id: str) -> bool:
        self.added.append((playlist_id, song_id))
        return True

    def start_scan(self) -> NavidromeScanStatus:
        self.scans_started += 1
        return NavidromeScanStatus(scanning=True, count=0)

    def scan_status(self) -> NavidromeScanStatus:
        return self.scan_statuses.pop(0)


def test_filter_playlists_excludes_uncurated_and_fuzzy_matches() -> None:
    playlists = [
        NavidromePlaylist(id="1", name="Uncurated: House"),
        NavidromePlaylist(id="2", name="Indie Dance"),
        NavidromePlaylist(id="3", name="Tech House"),
    ]

    result = filter_playlists(playlists, "indie")

    assert [playlist.name for playlist in result] == ["Indie Dance"]


def test_rate_current_stars_five_star_tracks(tmp_path: Path, monkeypatch) -> None:
    settings_path = _write_settings(tmp_path)
    fake = FakeNavidromeClient()
    monkeypatch.setattr("dj_sort.cli._navidrome_client", lambda settings: fake)

    result = runner.invoke(app, ["rate-current", "5", "--settings", str(settings_path)])

    assert result.exit_code == 0
    assert fake.ratings == [("song-1", 5)]
    assert fake.starred == ["song-1"]
    assert "Rated 5 and favorited" in result.stdout


def test_categorize_current_uses_cached_number_selection(tmp_path: Path, monkeypatch) -> None:
    settings_path = _write_settings(tmp_path)
    fake = FakeNavidromeClient()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("dj_sort.cli._navidrome_client", lambda settings: fake)

    list_result = runner.invoke(app, ["categorize-current", "Indie", "--settings", str(settings_path)])
    add_result = runner.invoke(app, ["categorize-current", "1", "--settings", str(settings_path)])

    assert list_result.exit_code == 0
    assert "1. Indie Picks" in list_result.stdout
    assert "Uncurated" not in list_result.stdout
    assert add_result.exit_code == 0
    assert fake.added == [("indie", "song-1")]


def test_new_playlist_creates_playlist(tmp_path: Path, monkeypatch) -> None:
    settings_path = _write_settings(tmp_path)
    fake = FakeNavidromeClient()
    monkeypatch.setattr("dj_sort.cli._navidrome_client", lambda settings: fake)

    result = runner.invoke(app, ["new-playlist", "My Crate", "--settings", str(settings_path)])

    assert result.exit_code == 0
    assert fake.created == ["My Crate"]
    assert "Created playlist created: My Crate" in result.stdout


def test_rescan_navidrome_starts_scan_and_can_wait(tmp_path: Path, monkeypatch) -> None:
    settings_path = _write_settings(tmp_path)
    fake = FakeNavidromeClient()
    fake.scan_statuses = [NavidromeScanStatus(scanning=False, count=10)]
    monkeypatch.setattr("dj_sort.cli._navidrome_client", lambda settings: fake)

    result = runner.invoke(app, ["rescan-navidrome", "--settings", str(settings_path), "--wait", "--poll-seconds", "1"])

    assert result.exit_code == 0
    assert fake.scans_started == 1
    assert "Started Navidrome rescan" in result.stdout
    assert "Navidrome rescan finished" in result.stdout


def test_list_playlists_caches_and_number_shows_songs(tmp_path: Path, monkeypatch) -> None:
    settings_path = _write_settings(tmp_path)
    fake = FakeNavidromeClient()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("dj_sort.cli._navidrome_client", lambda settings: fake)

    list_result = runner.invoke(app, ["list-playlists", "House", "--settings", str(settings_path)])
    songs_result = runner.invoke(app, ["list-playlists", "1", "--settings", str(settings_path)])

    assert list_result.exit_code == 0
    assert "1. House Crate" in list_result.stdout
    assert songs_result.exit_code == 0
    assert "House Crate: 2 tracks" in songs_result.stdout
    assert "1. Artist - Track [Album]" in songs_result.stdout


def test_re_genre_current_writes_genre_and_moves_file(tmp_path: Path, monkeypatch) -> None:
    settings_path = _write_library_settings(tmp_path)
    source = tmp_path / "library" / "House" / "Artist - Track.mp3"
    target = tmp_path / "library" / "Indie Dance" / "Artist - Track.mp3"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"audio")
    fake = FakeNavidromeClient()
    written = []
    monkeypatch.setattr("dj_sort.cli._navidrome_client", lambda settings: fake)
    monkeypatch.setattr("dj_sort.cli.read_metadata", lambda path: _metadata(path, genre="House"))
    monkeypatch.setattr("dj_sort.cli.write_genre", lambda path, genre, **kwargs: written.append((path, genre, kwargs)))

    result = runner.invoke(app, ["re-genre-current", "Indie Dance", "--settings", str(settings_path)])

    assert result.exit_code == 0
    assert source.exists() is False
    assert target.read_bytes() == b"audio"
    assert written[0][0] == source
    assert written[0][1] == "Indie Dance"
    assert written[0][2]["original_genre"] == "House"
    assert "Re-genred: Artist - Track" in result.stdout


def test_favorite_rated_five_stars_unstarred_rating_five_tracks(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "navidrome.db"
    _write_navidrome_db(db_path)
    settings_path = _write_settings(tmp_path)
    fake = FakeNavidromeClient()
    monkeypatch.setenv("NAVIDROME_USER", "erik")
    monkeypatch.setenv("NAVIDROME_DB", str(db_path))
    monkeypatch.setattr("dj_sort.cli._navidrome_client", lambda settings: fake)

    result = runner.invoke(app, ["favorite-rated-five", "--settings", str(settings_path)])

    assert result.exit_code == 0
    assert fake.starred == ["song-1"]
    assert "Favorited 1 rated-5 tracks for erik" in result.stdout


def test_sync_curation_auto_falls_back_to_ssh_when_configured_db_is_missing(tmp_path: Path, monkeypatch) -> None:
    settings_path = _write_curation_settings(
        tmp_path,
        database_path=tmp_path / "missing.db",
        database_ssh="erik@example:/srv/navidrome/navidrome.db",
        identity_file=tmp_path / "id_ed25519",
    )
    copy_calls = []
    sync_calls = []

    def fake_copy(source: str, target: Path, identity_file: Path | None = None) -> None:
        copy_calls.append((source, target, identity_file))
        target.write_bytes(b"sqlite snapshot")

    def fake_sync(settings, navidrome_database_path: Path, username: str, **kwargs) -> CurationSyncResult:
        sync_calls.append((navidrome_database_path, username, kwargs))
        return CurationSyncResult(dry_run=True, source=kwargs.get("source_name", "database"))

    monkeypatch.setattr("dj_sort.cli._copy_navidrome_database_over_ssh", fake_copy)
    monkeypatch.setattr("dj_sort.cli.sync_navidrome_curation", fake_sync)

    result = runner.invoke(app, ["sync-navidrome-curation", "--settings", str(settings_path)])

    assert result.exit_code == 0
    assert copy_calls[0][0] == "erik@example:/srv/navidrome/navidrome.db"
    assert copy_calls[0][2] == tmp_path / "id_ed25519"
    assert sync_calls[0][1] == "erik"
    assert sync_calls[0][2]["source_name"] == "ssh"
    assert "source: ssh" in result.stdout


def test_sync_curation_db_source_fails_when_database_is_missing(tmp_path: Path) -> None:
    settings_path = _write_curation_settings(tmp_path, database_path=tmp_path / "missing.db")

    result = runner.invoke(app, ["sync-navidrome-curation", "--source", "db", "--settings", str(settings_path)])

    assert result.exit_code == 1
    assert "Navidrome database does not exist" in result.stderr


def test_sync_curation_auto_requires_a_configured_source(tmp_path: Path) -> None:
    settings_path = _write_curation_settings(tmp_path)

    result = runner.invoke(app, ["sync-navidrome-curation", "--settings", str(settings_path)])

    assert result.exit_code == 1
    assert "No Navidrome curation source available" in result.stderr


def test_sync_curation_output_is_concise_unless_verbose(tmp_path: Path, monkeypatch) -> None:
    settings_path = _write_curation_settings(tmp_path)
    track_path = tmp_path / "library" / "House" / "Artist - Track.mp3"
    unsupported_path = tmp_path / "library" / "House" / "Artist - Track.m4a"

    def fake_source_sync(**kwargs) -> CurationSyncResult:
        track = CurationTrack(id="song-1", navidrome_path="House/Artist - Track.mp3", local_path=track_path, rating=5)
        unsupported = CurationTrack(id="song-2", navidrome_path="House/Artist - Track.m4a", local_path=unsupported_path, rating=5)
        plan = TrackSyncPlan(track=track, status="changed", changes=("rating: None -> 5",))
        unsupported_plan = TrackSyncPlan(track=unsupported, status="unsupported", notes="metadata writes are currently MP3-only")
        return CurationSyncResult(
            dry_run=True,
            tracks=[plan, unsupported_plan],
            missing=[("Old/File.sync-conflict-20260510-214932-DFDCTZ4.mp3", str(tmp_path / "missing.mp3"))],
        )

    monkeypatch.setattr("dj_sort.cli._sync_navidrome_curation_from_source", fake_source_sync)

    concise = runner.invoke(app, ["sync-navidrome-curation", "--settings", str(settings_path)])
    verbose = runner.invoke(app, ["sync-navidrome-curation", "--settings", str(settings_path), "--verbose"])

    assert concise.exit_code == 0
    assert str(track_path) not in concise.stdout
    assert "Summary: files changed=1" in concise.stdout
    assert verbose.exit_code == 0
    assert "Tag updates: Navidrome curation differs" in verbose.stdout
    assert "Unsupported metadata writes" in verbose.stdout
    assert "Missing/stale paths" in verbose.stdout
    assert "Syncthing conflict-copy filename recorded in Navidrome" in verbose.stdout
    assert str(track_path) in verbose.stdout
    assert "rating: None -> 5" in verbose.stdout


def test_sync_curation_write_rescans_and_waits_before_sync(tmp_path: Path, monkeypatch) -> None:
    settings_path = _write_curation_settings(tmp_path)
    calls = []

    def fake_rescan(settings, wait: bool = False, timeout_seconds: int = 900, poll_seconds: int = 5) -> None:
        calls.append(("rescan", wait, timeout_seconds, poll_seconds))

    def fake_source_sync(**kwargs) -> CurationSyncResult:
        calls.append(("sync", kwargs["write"]))
        return CurationSyncResult(dry_run=False)

    monkeypatch.setattr("dj_sort.cli._trigger_navidrome_rescan", fake_rescan)
    monkeypatch.setattr("dj_sort.cli._sync_navidrome_curation_from_source", fake_source_sync)

    result = runner.invoke(
        app,
        [
            "sync-navidrome-curation",
            "--settings",
            str(settings_path),
            "--write",
            "--rescan-timeout-seconds",
            "30",
            "--rescan-poll-seconds",
            "1",
        ],
    )

    assert result.exit_code == 0
    assert calls == [("rescan", True, 30, 1), ("sync", True)]


def test_sync_curation_write_can_skip_rescan(tmp_path: Path, monkeypatch) -> None:
    settings_path = _write_curation_settings(tmp_path)
    rescans = []

    monkeypatch.setattr("dj_sort.cli._trigger_navidrome_rescan", lambda *args, **kwargs: rescans.append((args, kwargs)))
    monkeypatch.setattr("dj_sort.cli._sync_navidrome_curation_from_source", lambda **kwargs: CurationSyncResult(dry_run=False))

    result = runner.invoke(app, ["sync-navidrome-curation", "--settings", str(settings_path), "--write", "--no-rescan"])

    assert result.exit_code == 0
    assert rescans == []


def test_prune_automated_backups_respects_days_and_write(tmp_path: Path) -> None:
    settings_path = _write_curation_settings(tmp_path)
    backup_dir = tmp_path / "backups"
    old_backup = backup_dir / "old"
    new_backup = backup_dir / "new"
    old_backup.mkdir(parents=True)
    new_backup.mkdir()
    old_time = time.time() - (31 * 24 * 60 * 60)
    os.utime(old_backup, (old_time, old_time))

    dry_run = runner.invoke(app, ["prune-automated-backups", "--settings", str(settings_path), "--days", "30"])

    assert dry_run.exit_code == 0
    assert old_backup.exists()
    assert "Would prune 1" in dry_run.stdout
    write = runner.invoke(app, ["prune-automated-backups", "--settings", str(settings_path), "--days", "30", "--write"])

    assert write.exit_code == 0
    assert old_backup.exists() is False
    assert new_backup.exists()
    assert "Pruned 1" in write.stdout


def _write_settings(tmp_path: Path) -> Path:
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(f"unprocessed_music_dir: {tmp_path / 'source'}\n", encoding="utf-8")
    return settings_path


def _write_library_settings(tmp_path: Path) -> Path:
    genre_map = tmp_path / "genres.yaml"
    genre_map.write_text("genres:\n  Indie Dance: Indie Dance\n", encoding="utf-8")
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(
        "\n".join(
            [
                f"unprocessed_music_dir: {tmp_path / 'source'}",
                f"dj_library_dir: {tmp_path / 'library'}",
                f"database_path: {tmp_path / 'library.sqlite3'}",
                f"genre_map_path: {genre_map}",
                "navidrome:",
                "  library_root: /music",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return settings_path


def _write_curation_settings(
    tmp_path: Path,
    database_path: Path | None = None,
    database_ssh: str | None = None,
    identity_file: Path | None = None,
) -> Path:
    settings_path = tmp_path / "settings.yaml"
    lines = [
        f"unprocessed_music_dir: {tmp_path / 'source'}",
        f"dj_library_dir: {tmp_path / 'library'}",
        f"automated_backup_dir: {tmp_path / 'backups'}",
        "navidrome:",
        "  username: erik",
        "  library_root: /music",
    ]
    if database_path is not None:
        lines.append(f"  database_path: {database_path}")
    if database_ssh is not None:
        lines.append(f"  database_ssh: {database_ssh}")
    if identity_file is not None:
        lines.append(f"  database_ssh_identity_file: {identity_file}")
    settings_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return settings_path


def _metadata(path: Path, genre: str | None) -> TrackMetadata:
    return TrackMetadata(
        path=path,
        artist="Artist",
        title="Track",
        genre=genre,
        bpm=None,
        raw_key=None,
        camelot_key=None,
        album=None,
        album_artist=None,
        track_number=None,
        release_date=None,
        duration_ms=None,
        bitrate=320000,
        sample_rate=None,
        file_size=path.stat().st_size,
        extension=path.suffix.casefold().lstrip("."),
        inferred=False,
        labels=(),
    )


def _write_navidrome_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    with connection:
        connection.executescript(
            """
            CREATE TABLE user (id TEXT PRIMARY KEY, user_name TEXT NOT NULL);
            CREATE TABLE annotation (
              ann_id TEXT PRIMARY KEY,
              user_id TEXT NOT NULL,
              item_id TEXT NOT NULL,
              item_type TEXT NOT NULL,
              rating INTEGER,
              starred INTEGER DEFAULT 0 NOT NULL
            );
            INSERT INTO user (id, user_name) VALUES ('user-1', 'erik');
            INSERT INTO annotation (ann_id, user_id, item_id, item_type, rating, starred)
            VALUES ('ann-1', 'user-1', 'song-1', 'media_file', 5, 0),
                   ('ann-2', 'user-1', 'song-2', 'media_file', 4, 0),
                   ('ann-3', 'user-1', 'song-3', 'media_file', 5, 1);
            """
        )
    connection.close()
