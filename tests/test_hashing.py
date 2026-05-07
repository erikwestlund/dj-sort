from pathlib import Path

from dj_sort.hashing import sha256_audio_payload


def test_mp3_audio_payload_hash_ignores_id3v1(tmp_path: Path) -> None:
    audio = b"\xff\xfb" + b"audio" * 10
    tagged = audio + b"TAG" + (b"x" * 125)
    clean = audio
    tagged_path = tmp_path / "tagged.mp3"
    clean_path = tmp_path / "clean.mp3"
    tagged_path.write_bytes(tagged)
    clean_path.write_bytes(clean)

    assert sha256_audio_payload(tagged_path) == sha256_audio_payload(clean_path)


def test_unsupported_audio_payload_hash_is_nullable(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    path.write_bytes(b"RIFF....WAVE")

    assert sha256_audio_payload(path) is None
