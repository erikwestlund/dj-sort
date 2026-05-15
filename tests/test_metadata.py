import wave
from pathlib import Path

from mutagen.id3 import COMM, ID3

from dj_sort.metadata import (
    infer_artist_title,
    normalize_camelot_key,
    original_genre_comment,
    read_metadata,
    read_original_genre_comment,
    write_genre,
)


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


def test_original_genre_comment_only_when_genre_changes() -> None:
    assert original_genre_comment("House Music", "House") == "dj-sort original genre: House Music"
    assert original_genre_comment("House", "House") is None
    assert original_genre_comment(None, "House") is None


def test_read_original_genre_comment_from_written_genre(tmp_path: Path) -> None:
    path = tmp_path / "track.mp3"
    tags = ID3()
    tags.add(COMM(encoding=3, lang="eng", desc="dj-sort", text=["dj-sort original genre: Afro House"]))
    tags.save(path)

    assert read_original_genre_comment(path) == "Afro House"


def test_wav_genre_write_uses_id3_tags(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(b"\x00\x00" * 100)

    write_genre(path, "House")

    assert read_metadata(path).genre == "House"
