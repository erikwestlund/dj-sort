from pathlib import Path

import pytest

from dj_sort.metadata import infer_artist_title, normalize_camelot_key, write_genre


def test_infer_artist_title_with_spaced_dash() -> None:
    assert infer_artist_title(Path("John Doe - Song Name.mp3")) == ("John Doe", "Song Name")


def test_infer_artist_title_with_plain_dash() -> None:
    assert infer_artist_title(Path("John Doe-Song Name.mp3")) == ("John Doe", "Song Name")


def test_infer_artist_title_rejects_multiple_separators() -> None:
    assert infer_artist_title(Path("John - Doe - Song Name.mp3")) == (None, None)


def test_normalize_camelot_key() -> None:
    assert normalize_camelot_key("8a") == "8A"
    assert normalize_camelot_key("A minor") == "8A"
    assert normalize_camelot_key("not a key") is None


def test_wav_genre_write_is_conservative(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    path.write_bytes(b"RIFF....WAVE")

    with pytest.raises(ValueError, match="WAV genre write-back is disabled"):
        write_genre(path, "House")
