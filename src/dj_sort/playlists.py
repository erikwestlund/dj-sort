from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dj_sort.metadata import is_supported_audio


@dataclass(frozen=True)
class PlaylistExport:
    name: str
    path: Path
    track_count: int


def export_genre_playlists(
    local_library_root: Path,
    server_library_root: Path,
    output_dir: Path,
    playlist_root_name: str = "_Playlists",
    include_extm3u_header: bool = True,
    playlist_name_prefix: str = "",
    write: bool = False,
) -> list[PlaylistExport]:
    if not local_library_root.exists():
        raise FileNotFoundError(f"Library root does not exist: {local_library_root}")
    if not local_library_root.is_dir():
        raise ValueError(f"Library root is not a directory: {local_library_root}")
    genres = [path for path in local_library_root.iterdir() if path.is_dir() and path.name != playlist_root_name]
    exports: list[PlaylistExport] = []
    if write:
        output_dir.mkdir(parents=True, exist_ok=True)
        for existing in output_dir.glob("*.m3u"):
            existing.unlink()

    for genre_dir in sorted(genres, key=lambda path: path.name.casefold()):
        tracks = _audio_files(genre_dir)
        if not tracks:
            continue
        playlist_name = f"{playlist_name_prefix}{genre_dir.name}"
        playlist_path = output_dir / f"{_playlist_file_stem(playlist_name)}.m3u"
        exports.append(PlaylistExport(name=playlist_name, path=playlist_path, track_count=len(tracks)))
        if write:
            _write_playlist(
                playlist_path,
                [_server_path(track, local_library_root, server_library_root) for track in tracks],
                include_extm3u_header,
            )
    return exports


def _audio_files(root: Path) -> list[Path]:
    return sorted(
        (path for path in root.rglob("*") if path.is_file() and is_supported_audio(path)),
        key=lambda path: path.relative_to(root).as_posix().casefold(),
    )


def _server_path(local_path: Path, local_library_root: Path, server_library_root: Path) -> str:
    return (server_library_root / local_path.relative_to(local_library_root)).as_posix()


def _write_playlist(path: Path, tracks: list[str], include_extm3u_header: bool) -> None:
    lines = ["#EXTM3U"] if include_extm3u_header else []
    lines.extend(tracks)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _playlist_file_stem(name: str) -> str:
    cleaned = " ".join(name.split()).strip(" .")
    for char in ('/', '\\', '\x00'):
        cleaned = cleaned.replace(char, "_")
    return cleaned or "Playlist"
