from pathlib import Path

from dj_sort.settings import load_settings


def test_load_settings_defaults_output_dirs(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(
        "\n".join(
            [
                f"unprocessed_music_dir: {tmp_path / 'source'}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    settings = load_settings(settings_path)

    assert settings.dj_library_dir == Path("~/Music/DJ Library").expanduser()
    assert settings.uncategorizable_dir == Path("~/Music/DJ Uncategorizable").expanduser()
    assert settings.duplicates_dir == Path("~/Music/DJ Duplicates").expanduser()
