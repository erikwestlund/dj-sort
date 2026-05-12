from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen


@dataclass(frozen=True)
class NavidromeSong:
    id: str
    title: str
    artist: str | None = None
    album: str | None = None
    path: str | None = None

    @property
    def display_name(self) -> str:
        artist = self.artist or "Unknown Artist"
        return f"{artist} - {self.title}"


@dataclass(frozen=True)
class NavidromePlaylist:
    id: str
    name: str
    song_count: int | None = None


@dataclass(frozen=True)
class NavidromeScanStatus:
    scanning: bool
    count: int | None = None


class NavidromeClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        api_version: str = "1.16.1",
        client_name: str = "dj-sort",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.api_version = api_version
        self.client_name = client_name

    def now_playing(self) -> NavidromeSong:
        payload = self._request("getNowPlaying")
        entries = _as_list(payload.get("nowPlaying", {}).get("entry"))
        if not entries:
            raise ValueError("Navidrome has no current now-playing track")
        matching_user = [entry for entry in entries if entry.get("username") == self.username]
        return _song_from_payload((matching_user or entries)[0])

    def set_rating(self, song_id: str, rating: int) -> None:
        self._request("setRating", {"id": song_id, "rating": str(rating)})

    def star(self, song_id: str) -> None:
        self._request("star", {"id": song_id})

    def playlists(self) -> list[NavidromePlaylist]:
        payload = self._request("getPlaylists")
        playlists = _as_list(payload.get("playlists", {}).get("playlist"))
        return [
            NavidromePlaylist(
                id=str(item["id"]),
                name=str(item["name"]),
                song_count=int(item["songCount"]) if item.get("songCount") is not None else None,
            )
            for item in playlists
        ]

    def create_playlist(self, name: str) -> NavidromePlaylist:
        payload = self._request("createPlaylist", {"name": name})
        if playlist := payload.get("playlist"):
            return _playlist_from_payload(playlist)
        matches = [playlist for playlist in self.playlists() if playlist.name.casefold() == name.casefold()]
        if not matches:
            raise ValueError(f"Navidrome did not return created playlist: {name}")
        return sorted(matches, key=lambda playlist: playlist.name.casefold())[0]

    def playlist_songs(self, playlist_id: str) -> list[NavidromeSong]:
        return [_song_from_payload(entry) for entry in self.playlist_song_payloads(playlist_id)]

    def playlist_song_payloads(self, playlist_id: str) -> list[dict[str, Any]]:
        payload = self._request("getPlaylist", {"id": playlist_id})
        return _as_list(payload.get("playlist", {}).get("entry"))

    def starred_song_payloads(self) -> list[dict[str, Any]]:
        payload = self._request("getStarred2")
        return _as_list(payload.get("starred2", {}).get("song"))

    def playlist_song_ids(self, playlist_id: str) -> set[str]:
        return {song.id for song in self.playlist_songs(playlist_id)}

    def add_to_playlist(self, playlist_id: str, song_id: str) -> bool:
        if song_id in self.playlist_song_ids(playlist_id):
            return False
        self._request("updatePlaylist", {"playlistId": playlist_id, "songIdToAdd": song_id})
        return True

    def start_scan(self) -> NavidromeScanStatus:
        payload = self._request("startScan")
        return _scan_status_from_payload(payload)

    def scan_status(self) -> NavidromeScanStatus:
        payload = self._request("getScanStatus")
        return _scan_status_from_payload(payload)

    def _request(self, endpoint: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        salt = secrets.token_hex(6)
        token = hashlib.md5((self.password + salt).encode("utf-8")).hexdigest()
        query = {
            "u": self.username,
            "t": token,
            "s": salt,
            "v": self.api_version,
            "c": self.client_name,
            "f": "json",
            **(params or {}),
        }
        url = f"{self.base_url}/rest/{endpoint}.view?{urlencode(query)}"
        try:
            with urlopen(url, timeout=15) as response:  # noqa: S310 - configured local Navidrome endpoint
                data = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise ValueError(f"Navidrome API error {exc.code}: {exc.reason}") from exc
        except URLError as exc:
            raise ValueError(f"Navidrome API connection failed: {exc.reason}") from exc

        envelope = data.get("subsonic-response", {})
        if envelope.get("status") == "failed":
            error = envelope.get("error", {})
            message = error.get("message") or "unknown error"
            raise ValueError(f"Navidrome API failed: {message}")
        return envelope


def filter_playlists(
    playlists: list[NavidromePlaylist],
    query: str | None,
    excluded_prefixes: tuple[str, ...] = ("Uncurated:",),
) -> list[NavidromePlaylist]:
    visible = [playlist for playlist in playlists if not _is_excluded_playlist(playlist.name, excluded_prefixes)]
    cleaned_query = (query or "").strip()
    if not cleaned_query:
        return sorted(visible, key=lambda playlist: playlist.name.casefold())
    scored = [(_playlist_match_score(playlist.name, cleaned_query), playlist) for playlist in visible]
    return [playlist for score, playlist in sorted(scored, key=lambda item: (-item[0], item[1].name.casefold())) if score > 0]


def playlist_cache_path(settings_path: Path) -> Path:
    return Path("reports") / f"{settings_path.stem}-navidrome-playlist-cache.json"


def write_playlist_cache(path: Path, playlists: list[NavidromePlaylist]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([{"id": playlist.id, "name": playlist.name, "song_count": playlist.song_count} for playlist in playlists], indent=2),
        encoding="utf-8",
    )


def read_playlist_cache(path: Path) -> list[NavidromePlaylist]:
    if not path.exists():
        raise FileNotFoundError(f"Playlist selection cache does not exist yet: {path}")
    rows = json.loads(path.read_text(encoding="utf-8"))
    return [
        NavidromePlaylist(
            id=str(row["id"]),
            name=str(row["name"]),
            song_count=int(row["song_count"]) if row.get("song_count") is not None else None,
        )
        for row in rows
    ]


def rated_song_ids_from_database(
    database_path: Path,
    username: str,
    rating: int = 5,
    only_unstarred: bool = True,
) -> list[str]:
    if not database_path.exists():
        raise FileNotFoundError(f"Navidrome database does not exist: {database_path}")
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        query = """
            SELECT annotation.item_id
            FROM annotation
            JOIN user ON user.id = annotation.user_id
            WHERE user.user_name = ?
              AND annotation.item_type = 'media_file'
              AND annotation.rating = ?
        """
        params: list[object] = [username, rating]
        if only_unstarred:
            query += " AND annotation.starred = 0"
        query += " ORDER BY annotation.item_id"
        rows = connection.execute(query, params).fetchall()
        return [str(row["item_id"]) for row in rows]
    finally:
        connection.close()


def _song_from_payload(payload: dict[str, Any]) -> NavidromeSong:
    return NavidromeSong(
        id=str(payload["id"]),
        title=str(payload.get("title") or "Unknown Title"),
        artist=str(payload["artist"]) if payload.get("artist") is not None else None,
        album=str(payload["album"]) if payload.get("album") is not None else None,
        path=str(payload["path"]) if payload.get("path") is not None else None,
    )


def _playlist_from_payload(payload: dict[str, Any]) -> NavidromePlaylist:
    return NavidromePlaylist(
        id=str(payload["id"]),
        name=str(payload["name"]),
        song_count=int(payload["songCount"]) if payload.get("songCount") is not None else None,
    )


def _scan_status_from_payload(payload: dict[str, Any]) -> NavidromeScanStatus:
    status = payload.get("scanStatus") or {}
    raw_scanning = status.get("scanning", False)
    scanning = raw_scanning if isinstance(raw_scanning, bool) else str(raw_scanning).casefold() == "true"
    return NavidromeScanStatus(
        scanning=scanning,
        count=int(status["count"]) if status.get("count") is not None else None,
    )


def _as_list(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _is_excluded_playlist(name: str, excluded_prefixes: tuple[str, ...]) -> bool:
    normalized = name.casefold()
    return any(normalized.startswith(prefix.casefold()) for prefix in excluded_prefixes if prefix)


def _playlist_match_score(name: str, query: str) -> int:
    normalized_name = name.casefold()
    normalized_query = query.casefold().strip()
    if normalized_name == normalized_query:
        return 1000
    if normalized_name.startswith(normalized_query):
        return 800
    if normalized_query in normalized_name:
        return 600
    tokens = [token for token in normalized_query.split() if token]
    if tokens and all(token in normalized_name for token in tokens):
        return 400
    if _is_subsequence(normalized_query.replace(" ", ""), normalized_name.replace(" ", "")):
        return 100
    return 0


def _is_subsequence(needle: str, haystack: str) -> bool:
    if not needle:
        return False
    iterator = iter(haystack)
    return all(char in iterator for char in needle)
