from pathlib import Path

from dj_sort.playlists import export_genre_playlists


def test_export_genre_playlists_writes_server_paths(tmp_path: Path) -> None:
    library = tmp_path / "Library"
    house = library / "House"
    techno = library / "Techno"
    playlist_dir = library / "_Playlists"
    house.mkdir(parents=True)
    techno.mkdir(parents=True)
    playlist_dir.mkdir(parents=True)
    (house / "Track A.mp3").write_bytes(b"audio")
    (techno / "Track B.m4a").write_bytes(b"audio")
    (playlist_dir / "Old.m3u").write_text("old\n", encoding="utf-8")

    exports = export_genre_playlists(
        library,
        Path("/srv/dj-library/Library"),
        playlist_dir,
        playlist_name_prefix="Uncurated: ",
        write=True,
    )

    assert [(export.name, export.track_count) for export in exports] == [("Uncurated: House", 1), ("Uncurated: Techno", 1)]
    assert (playlist_dir / "Old.m3u").exists() is False
    assert (playlist_dir / "Uncurated: House.m3u").read_text(encoding="utf-8") == (
        "#EXTM3U\n/srv/dj-library/Library/House/Track A.mp3\n"
    )
    assert (playlist_dir / "Uncurated: Techno.m3u").read_text(encoding="utf-8") == (
        "#EXTM3U\n/srv/dj-library/Library/Techno/Track B.m4a\n"
    )


def test_export_genre_playlists_dry_run_does_not_write(tmp_path: Path) -> None:
    library = tmp_path / "Library"
    house = library / "House"
    output = tmp_path / "playlists"
    house.mkdir(parents=True)
    (house / "Track A.mp3").write_bytes(b"audio")

    exports = export_genre_playlists(library, Path("/srv/dj-library/Library"), output)

    assert len(exports) == 1
    assert output.exists() is False
