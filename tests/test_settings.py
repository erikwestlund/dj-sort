from pathlib import Path

from dj_sort.settings import load_settings


def test_load_settings_defaults_library_root(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(
        "\n".join(
            [
                f"source_root: {tmp_path / 'source'}",
                f"processed_source_root: {tmp_path / 'archive'}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    settings = load_settings(settings_path)

    assert settings.library_root == Path("~/Music/DJ Library").expanduser()
