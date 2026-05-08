from pathlib import Path

from typer.testing import CliRunner

from dj_sort.cli import app
from dj_sort.database import connect, initialize, save_excluded_song
from dj_sort.planning import SkippedFile


runner = CliRunner()


def test_excluded_command_filters_by_reason(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.yaml"
    database_path = tmp_path / "library.sqlite3"
    settings_path.write_text(
        "\n".join(
            [
                f"source_root: {tmp_path / 'source'}",
                f"library_root: {tmp_path / 'library'}",
                f"processed_source_root: {tmp_path / 'archive'}",
                f"database_path: {database_path}",
                f"genre_map_path: {tmp_path / 'genres.yaml'}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
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
