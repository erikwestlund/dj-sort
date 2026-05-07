from __future__ import annotations

import hashlib
from pathlib import Path


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_audio_payload(path: Path) -> str | None:
    """Best-effort exact encoded-audio hash that skips common metadata containers.

    This intentionally covers cheap, high-confidence cases first. Unsupported formats return
    None so the copy pipeline and potential duplicate reports can still work.
    """
    suffix = path.suffix.casefold()
    data = path.read_bytes()
    if suffix == ".mp3":
        payload = _mp3_payload(data)
    elif suffix == ".flac":
        payload = _flac_payload(data)
    else:
        return None
    return hashlib.sha256(payload).hexdigest() if payload else None


def _mp3_payload(data: bytes) -> bytes:
    start = 0
    end = len(data)
    if data.startswith(b"ID3") and len(data) >= 10:
        tag_size = _synchsafe_to_int(data[6:10])
        footer_size = 10 if data[5] & 0x10 else 0
        start = 10 + tag_size + footer_size
    if end >= 128 and data[end - 128 : end - 125] == b"TAG":
        end -= 128
    return data[start:end]


def _flac_payload(data: bytes) -> bytes | None:
    if not data.startswith(b"fLaC"):
        return None
    offset = 4
    while offset + 4 <= len(data):
        header = data[offset]
        is_last = bool(header & 0x80)
        block_length = int.from_bytes(data[offset + 1 : offset + 4], "big")
        offset += 4 + block_length
        if is_last:
            return data[offset:]
    return None


def _synchsafe_to_int(value: bytes) -> int:
    total = 0
    for byte in value:
        total = (total << 7) | (byte & 0x7F)
    return total
