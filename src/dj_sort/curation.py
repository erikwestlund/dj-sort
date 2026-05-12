from __future__ import annotations

import json
import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mutagen.id3 import ID3, POPM, TXXX, ID3NoHeaderError

from dj_sort.navidrome import NavidromeClient
from dj_sort.settings import Settings

POPM_EMAIL = "navidrome@dj-sort"
RATING_TO_POPM = {1: 1, 2: 64, 3: 128, 4: 196, 5: 255}
CURATION_TXXX_DESCS = {"RATING", "NAVIDROME_RATING", "FAVORITE", "NAVIDROME_STARRED", "CRATE"}


@dataclass(frozen=True)
class CurationTrack:
    id: str
    navidrome_path: str
    local_path: Path
    rating: int | None = None
    starred: bool = False
    crates: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlaylistExportPlan:
    name: str
    path: Path
    tracks: tuple[Path, ...]
    changed: bool


@dataclass(frozen=True)
class TrackSyncPlan:
    track: CurationTrack
    status: str
    changes: tuple[str, ...] = ()
    notes: str | None = None


@dataclass(frozen=True)
class CurationSyncResult:
    dry_run: bool
    tracks: list[TrackSyncPlan] = field(default_factory=list)
    playlist_exports: list[PlaylistExportPlan] = field(default_factory=list)
    missing: list[tuple[str, str]] = field(default_factory=list)
    source: str = "database"
    api_partial: bool = False
    backup_path: Path | None = None

    @property
    def changed_files(self) -> int:
        return sum(1 for plan in self.tracks if plan.status == "changed")

    @property
    def unchanged_files(self) -> int:
        return sum(1 for plan in self.tracks if plan.status == "unchanged")

    @property
    def unsupported_files(self) -> int:
        return sum(1 for plan in self.tracks if plan.status == "unsupported")

    @property
    def error_files(self) -> int:
        return sum(1 for plan in self.tracks if plan.status == "error")

    @property
    def playlists_written(self) -> int:
        return sum(1 for export in self.playlist_exports if export.changed)


def sync_navidrome_curation(
    settings: Settings,
    navidrome_database_path: Path,
    username: str,
    library_dir: Path | None = None,
    output_dir: Path | None = None,
    write: bool = False,
    source_name: str = "database",
) -> CurationSyncResult:
    local_library = (library_dir or settings.dj_library_dir).expanduser()
    playlist_output_dir = (output_dir or settings.navidrome.output_dir or (local_library / "playlists")).expanduser()
    excluded_prefixes = _excluded_playlist_prefixes(settings)
    db_snapshot = _read_navidrome_database(navidrome_database_path, settings, local_library, username, excluded_prefixes)
    tracks = db_snapshot["tracks"]
    missing = db_snapshot["missing"]
    playlist_exports = _plan_playlist_exports(db_snapshot["playlists"], playlist_output_dir)

    plans: list[TrackSyncPlan] = []
    for track in tracks:
        try:
            plans.append(_sync_track(track, write=False))
        except Exception as exc:  # noqa: BLE001 - one bad file should not abort the whole sync
            plans.append(TrackSyncPlan(track=track, status="error", notes=str(exc)))

    backup_path = _backup_curation_write(settings, navidrome_database_path, plans, playlist_exports, missing, source_name) if write else None
    if write:
        _write_planned_tracks(plans)
        playlist_output_dir.mkdir(parents=True, exist_ok=True)
        for export in playlist_exports:
            if export.changed:
                export.path.write_text(_m3u8_text(export.tracks), encoding="utf-8")

    return CurationSyncResult(
        dry_run=not write,
        tracks=plans,
        playlist_exports=playlist_exports,
        missing=missing,
        source=source_name,
        backup_path=backup_path,
    )


def sync_navidrome_curation_from_api(
    settings: Settings,
    client: NavidromeClient,
    library_dir: Path | None = None,
    output_dir: Path | None = None,
    write: bool = False,
) -> CurationSyncResult:
    local_library = (library_dir or settings.dj_library_dir).expanduser()
    playlist_output_dir = (output_dir or settings.navidrome.output_dir or (local_library / "playlists")).expanduser()
    excluded_prefixes = _excluded_playlist_prefixes(settings)
    api_snapshot = _read_navidrome_api(client, settings, local_library, excluded_prefixes)
    tracks = api_snapshot["tracks"]
    missing = api_snapshot["missing"]
    playlist_exports = _plan_playlist_exports(api_snapshot["playlists"], playlist_output_dir)

    plans: list[TrackSyncPlan] = []
    for track in tracks:
        try:
            plans.append(_sync_track(track, write=False))
        except Exception as exc:  # noqa: BLE001 - one bad file should not abort the whole sync
            plans.append(TrackSyncPlan(track=track, status="error", notes=str(exc)))

    backup_path = _backup_curation_write(settings, None, plans, playlist_exports, missing, "api") if write else None
    if write:
        _write_planned_tracks(plans)
        playlist_output_dir.mkdir(parents=True, exist_ok=True)
        for export in playlist_exports:
            if export.changed:
                export.path.write_text(_m3u8_text(export.tracks), encoding="utf-8")

    return CurationSyncResult(
        dry_run=not write,
        tracks=plans,
        playlist_exports=playlist_exports,
        missing=missing,
        source="api",
        api_partial=True,
        backup_path=backup_path,
    )


def _write_planned_tracks(plans: list[TrackSyncPlan]) -> None:
    for plan in plans:
        if plan.status == "changed":
            write_mp3_curation_tags(plan.track.local_path, rating=plan.track.rating, starred=plan.track.starred, crates=plan.track.crates)


def _backup_curation_write(
    settings: Settings,
    navidrome_database_path: Path | None,
    plans: list[TrackSyncPlan],
    playlist_exports: list[PlaylistExportPlan],
    missing: list[tuple[str, str]],
    source: str,
) -> Path | None:
    changed_tracks = [plan for plan in plans if plan.status == "changed"]
    changed_playlists = [export for export in playlist_exports if export.changed]
    if not changed_tracks and not changed_playlists:
        return None
    backup_root = settings.automated_backup_dir
    if backup_root is None:
        return None
    backup_dir = _new_backup_dir(backup_root.expanduser(), "navidrome-curation")
    if navidrome_database_path is not None and navidrome_database_path.exists():
        shutil.copy2(navidrome_database_path, backup_dir / "navidrome.db")
    _backup_existing_playlists(changed_playlists, backup_dir / "playlists-before")
    (backup_dir / "mp3-tags-before.json").write_text(json.dumps(_mp3_tags_before(changed_tracks), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (backup_dir / "manifest.json").write_text(
        json.dumps(_curation_manifest(source, plans, playlist_exports, missing), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return backup_dir


def _new_backup_dir(root: Path, name: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    for suffix in [""] + [f"-{index}" for index in range(1, 100)]:
        backup_dir = root / f"{timestamp}-{name}{suffix}"
        try:
            backup_dir.mkdir()
        except FileExistsError:
            continue
        return backup_dir
    raise FileExistsError(f"could not create unique backup directory under {root}")


def _backup_existing_playlists(exports: list[PlaylistExportPlan], backup_dir: Path) -> None:
    for export in exports:
        if export.path.exists():
            destination = backup_dir / export.path.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(export.path, destination)


def _mp3_tags_before(plans: list[TrackSyncPlan]) -> list[dict[str, object]]:
    rows = []
    for plan in plans:
        rows.append(
            {
                "path": str(plan.track.local_path),
                "navidrome_path": plan.track.navidrome_path,
                "tags": read_mp3_curation_tags(plan.track.local_path),
            }
        )
    return rows


def _curation_manifest(
    source: str,
    plans: list[TrackSyncPlan],
    playlist_exports: list[PlaylistExportPlan],
    missing: list[tuple[str, str]],
) -> dict[str, object]:
    return {
        "created_at": datetime.now(UTC).isoformat(),
        "source": source,
        "tracks": [
            {
                "id": plan.track.id,
                "navidrome_path": plan.track.navidrome_path,
                "local_path": str(plan.track.local_path),
                "rating": plan.track.rating,
                "starred": plan.track.starred,
                "crates": list(plan.track.crates),
                "status": plan.status,
                "changes": list(plan.changes),
                "notes": plan.notes,
            }
            for plan in plans
        ],
        "playlist_exports": [
            {
                "name": export.name,
                "path": str(export.path),
                "tracks": [str(track) for track in export.tracks],
                "changed": export.changed,
            }
            for export in playlist_exports
        ],
        "missing": [{"navidrome_path": navidrome_path, "local_path": local_path} for navidrome_path, local_path in missing],
    }


def _read_navidrome_database(
    database_path: Path,
    settings: Settings,
    local_library: Path,
    username: str,
    excluded_prefixes: tuple[str, ...],
) -> dict[str, object]:
    if not database_path.exists():
        raise FileNotFoundError(f"Navidrome database does not exist: {database_path}")
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        media_rows = connection.execute(
            """
            SELECT media_file.id, media_file.path, annotation.rating, annotation.starred
            FROM media_file
            LEFT JOIN annotation ON annotation.item_id = media_file.id AND annotation.item_type = 'media_file'
            LEFT JOIN user ON user.id = annotation.user_id
            WHERE annotation.user_id IS NULL OR user.user_name = ?
            ORDER BY media_file.path
            """,
            (username,),
        ).fetchall()
        playlists = _playlist_memberships(connection, username, excluded_prefixes)
        crates_by_track: dict[str, set[str]] = {}
        for playlist_name, track_ids in playlists.items():
            for track_id in track_ids:
                crates_by_track.setdefault(track_id, set()).add(playlist_name)

        tracks: list[CurationTrack] = []
        missing: list[tuple[str, str]] = []
        for row in media_rows:
            rating = _database_rating(row["rating"])
            starred = bool(row["starred"])
            crates = tuple(sorted(crates_by_track.get(str(row["id"]), set()), key=str.casefold))
            if rating is None and not starred and not crates:
                continue
            local_path = _resolve_media_path(settings, local_library, str(row["path"]))
            if not local_path.exists():
                local_path = _resolve_stale_path(settings, local_path) or local_path
            if not local_path.exists():
                missing.append((str(row["path"]), str(local_path)))
                continue
            tracks.append(
                CurationTrack(
                    id=str(row["id"]),
                    navidrome_path=str(row["path"]),
                    local_path=local_path,
                    rating=rating,
                    starred=starred,
                    crates=crates,
                )
            )
        playlist_exports = {
            name: tuple(
                track.local_path for track in tracks if track.id in track_ids and track.local_path.exists()
            )
            for name, track_ids in playlists.items()
        }
        favorites = tuple(track.local_path for track in tracks if track.starred and track.local_path.exists())
        if favorites:
            playlist_exports["Favorites"] = favorites
        return {"tracks": tracks, "missing": missing, "playlists": playlist_exports}
    finally:
        connection.close()


def _playlist_memberships(connection: sqlite3.Connection, username: str, excluded_prefixes: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
    playlist_columns = _table_columns(connection, "playlist")
    if "owner" in playlist_columns:
        rows = connection.execute(
            """
            SELECT playlist.name, playlist_tracks.media_file_id, playlist_tracks.id AS position
            FROM playlist
            JOIN playlist_tracks ON playlist_tracks.playlist_id = playlist.id
            WHERE playlist.owner = ? OR playlist.owner = '' OR playlist.public = 1
            ORDER BY playlist.name COLLATE NOCASE, playlist_tracks.id
            """,
            (username,),
        ).fetchall()
    elif "owner_id" in playlist_columns:
        rows = connection.execute(
            """
            SELECT playlist.name, playlist_tracks.media_file_id, playlist_tracks.id AS position
            FROM playlist
            JOIN playlist_tracks ON playlist_tracks.playlist_id = playlist.id
            LEFT JOIN user ON user.id = playlist.owner_id
            WHERE user.user_name = ? OR playlist.public = 1
            ORDER BY playlist.name COLLATE NOCASE, playlist_tracks.id
            """,
            (username,),
        ).fetchall()
    else:
        rows = connection.execute(
            """
            SELECT playlist.name, playlist_tracks.media_file_id, playlist_tracks.id AS position
            FROM playlist
            JOIN playlist_tracks ON playlist_tracks.playlist_id = playlist.id
            ORDER BY playlist.name COLLATE NOCASE, playlist_tracks.id
            """
        ).fetchall()
    memberships: dict[str, list[str]] = {}
    for row in rows:
        name = str(row["name"])
        if _is_generated_playlist(name, excluded_prefixes):
            continue
        memberships.setdefault(name, []).append(str(row["media_file_id"]))
    return {name: tuple(track_ids) for name, track_ids in memberships.items()}


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _read_navidrome_api(
    client: NavidromeClient,
    settings: Settings,
    local_library: Path,
    excluded_prefixes: tuple[str, ...],
) -> dict[str, object]:
    api_tracks: dict[str, dict[str, Any]] = {}
    crates_by_track: dict[str, set[str]] = {}
    playlist_ids: dict[str, list[str]] = {}

    for playlist in client.playlists():
        if _is_generated_playlist(playlist.name, excluded_prefixes):
            continue
        track_ids: list[str] = []
        for entry in client.playlist_song_payloads(playlist.id):
            track_id = str(entry["id"])
            api_tracks[track_id] = _merge_api_track(api_tracks.get(track_id), entry)
            crates_by_track.setdefault(track_id, set()).add(playlist.name)
            track_ids.append(track_id)
        playlist_ids[playlist.name] = track_ids

    for entry in client.starred_song_payloads():
        track_id = str(entry["id"])
        api_tracks[track_id] = _merge_api_track(api_tracks.get(track_id), {**entry, "starred": entry.get("starred") or True})

    tracks_by_id: dict[str, CurationTrack] = {}
    missing: list[tuple[str, str]] = []
    for track_id, entry in sorted(api_tracks.items(), key=lambda item: str(item[1].get("path", "")).casefold()):
        navidrome_path = str(entry.get("path") or "")
        if not navidrome_path:
            missing.append((track_id, "<missing API path>"))
            continue
        rating = _api_rating(entry)
        starred = bool(entry.get("starred"))
        crates = tuple(sorted(crates_by_track.get(track_id, set()), key=str.casefold))
        if rating is None and not starred and not crates:
            continue
        local_path = _resolve_media_path(settings, local_library, navidrome_path)
        if not local_path.exists():
            local_path = _resolve_stale_path(settings, local_path) or local_path
        if not local_path.exists():
            missing.append((navidrome_path, str(local_path)))
            continue
        tracks_by_id[track_id] = CurationTrack(
            id=track_id,
            navidrome_path=navidrome_path,
            local_path=local_path,
            rating=rating,
            starred=starred,
            crates=crates,
        )

    playlist_exports = {
        name: tuple(tracks_by_id[track_id].local_path for track_id in track_ids if track_id in tracks_by_id)
        for name, track_ids in playlist_ids.items()
    }
    favorites = tuple(track.local_path for track in tracks_by_id.values() if track.starred and track.local_path.exists())
    if favorites:
        playlist_exports["Favorites"] = favorites
    return {"tracks": list(tracks_by_id.values()), "missing": missing, "playlists": playlist_exports}


def _database_rating(raw_rating: object) -> int | None:
    if raw_rating is None:
        return None
    rating = int(raw_rating)
    return rating if rating in RATING_TO_POPM else None


def _merge_api_track(existing: dict[str, Any] | None, incoming: dict[str, Any]) -> dict[str, Any]:
    if existing is None:
        return dict(incoming)
    merged = {**existing, **incoming}
    if existing.get("userRating") and not incoming.get("userRating"):
        merged["userRating"] = existing["userRating"]
    if existing.get("starred") and not incoming.get("starred"):
        merged["starred"] = existing["starred"]
    return merged


def _api_rating(entry: dict[str, Any]) -> int | None:
    raw_rating = entry.get("userRating")
    if raw_rating is None:
        return None
    rating = int(raw_rating)
    return rating if rating in RATING_TO_POPM else None


def _sync_track(track: CurationTrack, write: bool) -> TrackSyncPlan:
    if track.local_path.suffix.casefold() != ".mp3":
        return TrackSyncPlan(track=track, status="unsupported", notes="metadata writes are currently MP3-only")
    current = read_mp3_curation_tags(track.local_path)
    desired = _desired_tags(track)
    changes = _tag_changes(current, desired)
    if not changes:
        return TrackSyncPlan(track=track, status="unchanged")
    if write:
        write_mp3_curation_tags(track.local_path, rating=track.rating, starred=track.starred, crates=track.crates)
    return TrackSyncPlan(track=track, status="changed", changes=tuple(changes))


def read_mp3_curation_tags(path: Path) -> dict[str, object]:
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        return {"rating": None, "popm": None, "starred": False, "crates": ()}
    rating = _txxx_text(tags, "NAVIDROME_RATING") or _txxx_text(tags, "RATING")
    starred = _txxx_text(tags, "NAVIDROME_STARRED") == "1" or _txxx_text(tags, "FAVORITE") == "1"
    crates = tuple(sorted(_txxx_values(tags, "CRATE"), key=str.casefold))
    popm = next((frame.rating for frame in tags.getall("POPM") if frame.email == POPM_EMAIL), None)
    return {"rating": int(rating) if rating and rating.isdigit() else None, "popm": popm, "starred": starred, "crates": crates}


def write_mp3_curation_tags(path: Path, rating: int | None, starred: bool, crates: tuple[str, ...]) -> None:
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    _delete_curation_frames(tags)
    if rating is not None:
        tags.add(POPM(email=POPM_EMAIL, rating=RATING_TO_POPM[rating], count=0))
        tags.add(TXXX(encoding=3, desc="RATING", text=[str(rating)]))
        tags.add(TXXX(encoding=3, desc="NAVIDROME_RATING", text=[str(rating)]))
    if starred:
        tags.add(TXXX(encoding=3, desc="FAVORITE", text=["1"]))
        tags.add(TXXX(encoding=3, desc="NAVIDROME_STARRED", text=["1"]))
    if crates:
        tags.add(TXXX(encoding=3, desc="CRATE", text=list(crates)))
    tags.save(path)


def _delete_curation_frames(tags: ID3) -> None:
    for frame in list(tags.getall("POPM")):
        if frame.email == POPM_EMAIL:
            tags.delall(f"POPM:{frame.email}")
    for desc in CURATION_TXXX_DESCS:
        tags.delall(f"TXXX:{desc}")


def _desired_tags(track: CurationTrack) -> dict[str, object]:
    return {
        "rating": track.rating,
        "popm": RATING_TO_POPM.get(track.rating) if track.rating is not None else None,
        "starred": track.starred,
        "crates": track.crates,
    }


def _tag_changes(current: dict[str, object], desired: dict[str, object]) -> list[str]:
    changes = []
    for key in ("rating", "popm", "starred", "crates"):
        if current[key] != desired[key]:
            changes.append(f"{key}: {current[key]!r} -> {desired[key]!r}")
    return changes


def _plan_playlist_exports(playlists: dict[str, tuple[Path, ...]], output_dir: Path) -> list[PlaylistExportPlan]:
    exports = []
    for name, tracks in sorted(playlists.items(), key=lambda item: item[0].casefold()):
        path = output_dir / f"{_playlist_file_stem(name)}.m3u8"
        text = _m3u8_text(tracks)
        changed = not path.exists() or path.read_text(encoding="utf-8") != text
        exports.append(PlaylistExportPlan(name=name, path=path, tracks=tracks, changed=changed))
    return exports


def _m3u8_text(tracks: tuple[Path, ...]) -> str:
    return "#EXTM3U\n" + "".join(f"{track}\n" for track in sorted(tracks, key=lambda path: str(path).casefold()))


def _resolve_media_path(settings: Settings, local_library: Path, navidrome_path: str) -> Path:
    raw_path = Path(navidrome_path)
    if raw_path.is_absolute():
        try:
            return local_library / raw_path.relative_to(settings.navidrome.library_root)
        except ValueError:
            return raw_path
    return local_library / raw_path


def _resolve_stale_path(settings: Settings, stale_path: Path) -> Path | None:
    if not settings.database_path.exists():
        return None
    connection = sqlite3.connect(f"file:{settings.database_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            """
            SELECT new_path
            FROM song_operation_log
            WHERE previous_path = ? AND new_path IS NOT NULL
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (str(stale_path),),
        ).fetchone()
        return Path(row["new_path"]) if row else None
    except sqlite3.Error:
        return None
    finally:
        connection.close()


def _excluded_playlist_prefixes(settings: Settings) -> tuple[str, ...]:
    prefixes = ["Uncurated"]
    if settings.navidrome.playlist_name_prefix.strip():
        prefixes.append(settings.navidrome.playlist_name_prefix.strip())
    return tuple(dict.fromkeys(prefixes))


def _is_generated_playlist(name: str, excluded_prefixes: tuple[str, ...]) -> bool:
    normalized = name.casefold()
    return any(normalized.startswith(prefix.casefold()) for prefix in excluded_prefixes if prefix)


def _playlist_file_stem(name: str) -> str:
    cleaned = " ".join(name.split()).strip(" .")
    cleaned = re.sub(r"[/\\\x00]", "_", cleaned)
    return cleaned or "Playlist"


def _txxx_text(tags: ID3, desc: str) -> str | None:
    values = _txxx_values(tags, desc)
    return values[0] if values else None


def _txxx_values(tags: ID3, desc: str) -> list[str]:
    values: list[str] = []
    for frame in tags.getall(f"TXXX:{desc}"):
        values.extend(str(value) for value in frame.text if str(value).strip())
    return values
