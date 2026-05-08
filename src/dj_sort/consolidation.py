from __future__ import annotations

import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

from dj_sort import database
from dj_sort.database import connect, genre_id, initialize, songs_for_genre
from dj_sort.hashing import sha256_file
from dj_sort.metadata import write_genre
from dj_sort.paths import ensure_unique_path, safe_path_part
from dj_sort.settings import Settings

DELETE_GENRE = "Delete"


@dataclass(frozen=True)
class ConsolidationAction:
    song_id: int
    source_genre: str
    target_genre: str
    current_path: Path
    target_path: Path
    status: str
    notes: str | None = None

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["current_path"] = str(self.current_path)
        data["target_path"] = str(self.target_path)
        return data


@dataclass(frozen=True)
class ConsolidationResult:
    actions: list[ConsolidationAction]
    dry_run: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "report_type": "genre_consolidation",
            "dry_run": self.dry_run,
            "actions": [action.to_dict() for action in self.actions],
            "summary": {
                "actions": len(self.actions),
                "errors": sum(1 for action in self.actions if action.status == "error"),
            },
        }


def consolidate_genres(
    settings: Settings,
    mappings: dict[str, str],
    dry_run: bool,
    limit: int | None = None,
) -> ConsolidationResult:
    connection = connect(settings.database_path)
    initialize(connection)
    occupied: set[Path] = set()
    actions: list[ConsolidationAction] = []

    try:
        for source_genre, target_genre in mappings.items():
            target_genre = target_genre.strip()
            if not target_genre:
                continue
            for row in songs_for_genre(connection, source_genre):
                if limit is not None and len(actions) >= limit:
                    return ConsolidationResult(actions=actions, dry_run=dry_run)
                action = _plan_action(settings, row, source_genre, target_genre, occupied)
                if dry_run:
                    actions.append(action)
                    continue
                actions.append(_apply_action(settings, connection, action))
    finally:
        connection.close()

    return ConsolidationResult(actions=actions, dry_run=dry_run)


def remove_empty_genre_dirs(settings: Settings, start: Path) -> None:
    if not settings.remove_empty_genre_dirs:
        return
    library_root = settings.dj_library_dir.resolve()
    current = start.resolve()
    while current != library_root and library_root in current.parents:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _plan_action(
    settings: Settings,
    row,
    source_genre: str,
    target_genre: str,
    occupied: set[Path],
) -> ConsolidationAction:
    current_path = Path(row["current_path"])
    delete_target = target_genre.casefold() == DELETE_GENRE.casefold()
    if delete_target:
        target_path = current_path
    else:
        target_dir = settings.dj_library_dir / safe_path_part(target_genre)
        target_path = ensure_unique_path(target_dir / current_path.name, occupied, f"{row['id']}:{target_genre}")
    if not delete_target and current_path == target_path and row["display_genre"] == target_genre:
        status = "unchanged"
    elif not current_path.exists():
        status = "error"
    else:
        status = "planned"
    notes = None if status != "error" else "current file is missing"
    return ConsolidationAction(
        song_id=int(row["id"]),
        source_genre=source_genre,
        target_genre=target_genre,
        current_path=current_path,
        target_path=target_path,
        status=status,
        notes=notes,
    )


def _apply_action(settings: Settings, connection, action: ConsolidationAction) -> ConsolidationAction:
    if action.status == "unchanged":
        return action
    if action.status == "error":
        return action

    try:
        if action.target_genre.casefold() == DELETE_GENRE.casefold():
            old_parent = action.current_path.parent
            action.current_path.unlink()
            remove_empty_genre_dirs(settings, old_parent)
            database.mark_song_deleted(
                connection,
                song_id=action.song_id,
                previous_path=action.current_path,
                previous_genre=action.source_genre,
                deleted_genre=action.target_genre,
            )
            return ConsolidationAction(
                song_id=action.song_id,
                source_genre=action.source_genre,
                target_genre=action.target_genre,
                current_path=action.current_path,
                target_path=action.target_path,
                status="deleted",
            )

        action.target_path.parent.mkdir(parents=True, exist_ok=True)
        original_genre = action.source_genre if settings.preserve_original_genre_in_comment else None
        write_genre(
            action.current_path,
            action.target_genre,
            original_genre=original_genre,
            original_genre_comment_prefix=settings.original_genre_comment_prefix,
        )
        if action.current_path != action.target_path:
            old_parent = action.current_path.parent
            shutil.move(action.current_path, action.target_path)
            remove_empty_genre_dirs(settings, old_parent)
        final_hash = sha256_file(action.target_path)
        database.update_consolidated_song(
            connection,
            song_id=action.song_id,
            genre_id=genre_id(connection, action.target_genre),
            display_genre=action.target_genre,
            new_path=action.target_path,
            new_hash=final_hash,
            previous_path=action.current_path,
            previous_genre=action.source_genre,
        )
        return ConsolidationAction(
            song_id=action.song_id,
            source_genre=action.source_genre,
            target_genre=action.target_genre,
            current_path=action.current_path,
            target_path=action.target_path,
            status="processed",
        )
    except Exception as exc:  # noqa: BLE001 - mark per-file failure in report
        return ConsolidationAction(
            song_id=action.song_id,
            source_genre=action.source_genre,
            target_genre=action.target_genre,
            current_path=action.current_path,
            target_path=action.target_path,
            status="error",
            notes=str(exc),
        )
