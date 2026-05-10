from __future__ import annotations

import shutil
from dataclasses import dataclass, replace
from pathlib import Path

from dj_sort import database
from dj_sort.database import connect, initialize
from dj_sort.hashing import sha256_audio_payload, sha256_file
from dj_sort.metadata import read_metadata, write_genre
from dj_sort.paths import ensure_unique_path
from dj_sort.planning import Plan, PlanningResult
from dj_sort.settings import Settings

TRACKED_EXCLUSION_REASONS = {"genre_not_whitelisted", "duration_exceeds_max", "blacklist_substring"}


@dataclass(frozen=True)
class ProcessedFile:
    source_path: Path
    target_path: Path
    status: str
    notes: str | None = None
    source_archive_path: Path | None = None
    source_cleanup_status: str = "kept"

    def to_dict(self) -> dict[str, str | None]:
        return {
            "source_path": str(self.source_path),
            "target_path": str(self.target_path),
            "status": self.status,
            "notes": self.notes,
            "source_archive_path": str(self.source_archive_path) if self.source_archive_path else None,
            "source_cleanup_status": self.source_cleanup_status,
        }


@dataclass(frozen=True)
class ProcessingResult:
    processed: list[ProcessedFile]
    planned: PlanningResult

    def to_dict(self) -> dict[str, object]:
        return {
            "report_type": "processing",
            "processed": [item.to_dict() for item in self.processed],
            "planned": self.planned.to_dict(),
            "summary": {
                "processed": len(self.processed),
                "planned": len(self.planned.plans),
                "skipped": len(self.planned.skipped),
                "errors": sum(1 for item in self.processed if item.status == "error"),
            },
        }


def process_plans(settings: Settings, planning_result: PlanningResult) -> ProcessingResult:
    connection = connect(settings.database_path)
    initialize(connection)
    processed: list[ProcessedFile] = []

    for skipped in planning_result.skipped:
        if skipped.reason == "delete_genre":
            if skipped.path.exists():
                skipped.path.unlink()
                _remove_empty_parents(skipped.path.parent, settings.unprocessed_music_dir)
            database.clear_excluded_song(connection, skipped.path)
            continue
        if skipped.reason in TRACKED_EXCLUSION_REASONS:
            database.save_excluded_song(connection, skipped)
            continue
        database.clear_excluded_song(connection, skipped.path)

    for plan in planning_result.plans:
        database.clear_excluded_song(connection, plan.source_path)

        processed.append(_process_one(settings, connection, plan))

    connection.close()
    return ProcessingResult(processed=processed, planned=planning_result)


def _process_one(settings: Settings, connection, plan: Plan) -> ProcessedFile:
    original_hash: str | None = None
    final_hash: str | None = None
    notes: list[str] = []
    source_archive_path: Path | None = None
    source_cleanup_status = "kept"

    try:
        original_hash = sha256_file(plan.source_path)
        existing = database.find_existing_by_source_hash(connection, plan.source_path, original_hash)
        should_reprocess_existing = (
            existing is not None
            and existing.current_path.exists()
            and _should_reprocess_existing(settings, existing.current_path, plan.target_path)
        )
        if existing is not None and existing.current_path.exists() and not should_reprocess_existing:
            return ProcessedFile(
                source_path=plan.source_path,
                target_path=existing.current_path,
                status="unchanged",
                notes=f"already processed as song {existing.id}",
                source_cleanup_status="not_applicable",
            )

        audio_hash = sha256_audio_payload(plan.source_path)
        if plan.target_path.exists():
            duplicate_result = _handle_filename_collision(settings, connection, plan, original_hash, audio_hash)
            if duplicate_result is not None:
                return duplicate_result

        plan.target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(plan.source_path, plan.target_path)

        if plan.target_path.stat().st_size != plan.source_path.stat().st_size:
            raise ValueError("copied file size does not match source")
        copied_hash = sha256_file(plan.target_path)
        if copied_hash != original_hash:
            raise ValueError("copied file hash does not match source")

        if _should_write_canonical_genre(settings, plan):
            try:
                original_genre = plan.raw_genre if settings.preserve_original_genre_in_comment else None
                write_genre(
                    plan.target_path,
                    plan.canonical_genre,
                    original_genre=original_genre,
                    original_genre_comment_prefix=settings.original_genre_comment_prefix,
                )
            except Exception as exc:  # noqa: BLE001 - metadata write failures should be reviewable
                notes.append(f"metadata_write_error: {exc}")

        final_hash = sha256_file(plan.target_path)
        database.save_processed_song(
            connection,
            plan,
            original_hash=original_hash,
            final_hash=final_hash,
            audio_hash=audio_hash,
            status="needs_review" if notes else "processed",
            notes="; ".join(notes) if notes else None,
        )
        if should_reprocess_existing and existing is not None:
            _cleanup_reprocessed_existing(settings, connection, existing.id, existing.current_path, plan.target_path)

        return ProcessedFile(
            source_path=plan.source_path,
            target_path=plan.target_path,
            status="needs_review" if notes else "processed",
            notes="; ".join(notes) if notes else None,
            source_archive_path=source_archive_path,
            source_cleanup_status=source_cleanup_status,
        )
    except Exception as exc:  # noqa: BLE001 - per-file failure should not abort batch
        if original_hash is not None:
            database.save_processed_song(
                connection,
                plan,
                original_hash=original_hash,
                final_hash=final_hash,
                audio_hash=None,
                status="error",
                notes=str(exc),
            )
        return ProcessedFile(plan.source_path, plan.target_path, "error", str(exc))


def _should_write_canonical_genre(settings: Settings, plan: Plan) -> bool:
    if not settings.write_canonical_genre_to_metadata:
        return False
    if plan.canonical_genre == settings.missing_genre_dir:
        return False
    try:
        plan.target_path.resolve().relative_to(settings.dj_library_dir.resolve())
    except ValueError:
        return False
    return True


def _handle_filename_collision(
    settings: Settings,
    connection,
    plan: Plan,
    original_hash: str,
    audio_hash: str | None,
) -> ProcessedFile | None:
    if not _is_under(plan.target_path, settings.dj_library_dir):
        return None

    existing_metadata = read_metadata(plan.target_path)
    source_quality = _quality_score(plan.bitrate, plan.sample_rate, plan.file_size)
    existing_quality = _quality_score(existing_metadata.bitrate, existing_metadata.sample_rate, existing_metadata.file_size)
    if source_quality > existing_quality:
        duplicate_path = _duplicate_path_for(settings, plan.target_path)
        duplicate_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(plan.target_path, duplicate_path)
        _mark_existing_collision_duplicate(connection, plan.target_path, duplicate_path, "replaced by higher quality filename collision")
        return None

    duplicate_path = _duplicate_path_for(settings, plan.target_path)
    duplicate_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(plan.source_path, duplicate_path)
    if duplicate_path.stat().st_size != plan.source_path.stat().st_size:
        raise ValueError("copied duplicate file size does not match source")
    copied_hash = sha256_file(duplicate_path)
    if copied_hash != original_hash:
        raise ValueError("copied duplicate file hash does not match source")

    duplicate_plan = replace(plan, target_path=duplicate_path)
    database.save_processed_song(
        connection,
        duplicate_plan,
        original_hash=original_hash,
        final_hash=copied_hash,
        audio_hash=audio_hash,
        status="duplicate",
        notes=f"filename collision duplicate; kept existing library file: {plan.target_path}",
    )
    return ProcessedFile(
        source_path=plan.source_path,
        target_path=duplicate_path,
        status="duplicate",
        notes=f"filename collision duplicate; kept existing library file: {plan.target_path}",
        source_cleanup_status="kept",
    )


def _duplicate_path_for(settings: Settings, library_path: Path) -> Path:
    try:
        relative = library_path.relative_to(settings.dj_library_dir)
    except ValueError:
        relative = library_path.name
    target = settings.duplicates_dir / relative
    return ensure_unique_path(target, set(), str(library_path))


def _quality_score(bitrate: int | None, sample_rate: int | None, file_size: int | None) -> tuple[int, int, int]:
    return (bitrate or 0, sample_rate or 0, file_size or 0)


def _mark_existing_collision_duplicate(connection, previous_path: Path, duplicate_path: Path, notes: str) -> None:
    now = database.utc_now()
    with connection:
        connection.execute(
            """
            UPDATE song
            SET current_path = ?, processing_status = ?, processing_notes = ?, updated_at = ?
            WHERE current_path = ?
            """,
            (str(duplicate_path), "duplicate", notes, now, str(previous_path)),
        )


def _should_reprocess_existing(settings: Settings, existing_path: Path, target_path: Path) -> bool:
    if existing_path == target_path:
        return False
    if _is_under(existing_path, settings.uncategorizable_dir) and _is_under(target_path, settings.dj_library_dir):
        return True
    return _is_under(existing_path, settings.dj_library_dir) and _is_under(target_path, settings.dj_library_dir)


def _is_under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _cleanup_reprocessed_existing(settings: Settings, connection, song_id: int, old_path: Path, new_path: Path) -> None:
    is_managed_output = _is_under(old_path, settings.uncategorizable_dir) or _is_under(old_path, settings.dj_library_dir)
    if old_path != new_path and is_managed_output and old_path.exists():
        old_path.unlink()
        stop = settings.uncategorizable_dir if _is_under(old_path, settings.uncategorizable_dir) else settings.dj_library_dir
        _remove_empty_parents(old_path.parent, stop)
    database.delete_song(connection, song_id)


def _remove_empty_parents(start: Path, stop: Path) -> None:
    stop = stop.resolve()
    current = start.resolve()
    while current != stop and stop in current.parents:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent
