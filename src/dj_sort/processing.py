from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from dj_sort import database
from dj_sort.database import connect, initialize, utc_now
from dj_sort.hashing import sha256_audio_payload, sha256_file
from dj_sort.metadata import write_genre
from dj_sort.paths import ensure_unique_path, relative_archive_path
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
    archive_occupied: set[Path] = set()

    for skipped in planning_result.skipped:
        if skipped.reason in TRACKED_EXCLUSION_REASONS:
            database.save_excluded_song(connection, skipped)
            continue
        database.clear_excluded_song(connection, skipped.path)

    for plan in planning_result.plans:
        database.clear_excluded_song(connection, plan.source_path)

        processed.append(_process_one(settings, connection, plan, archive_occupied))

    connection.close()
    return ProcessingResult(processed=processed, planned=planning_result)


def _process_one(settings: Settings, connection, plan: Plan, archive_occupied: set[Path]) -> ProcessedFile:
    original_hash: str | None = None
    final_hash: str | None = None
    notes: list[str] = []
    source_archive_path: Path | None = None
    source_cleanup_status = "kept"
    source_archived_at: str | None = None
    source_removed_at: str | None = None

    try:
        original_hash = sha256_file(plan.source_path)
        existing = database.find_existing_by_source_hash(connection, plan.source_path, original_hash)
        if existing is not None and existing.current_path.exists():
            return ProcessedFile(
                source_path=plan.source_path,
                target_path=existing.current_path,
                status="unchanged",
                notes=f"already processed as song {existing.id}",
                source_cleanup_status="not_applicable",
            )

        audio_hash = sha256_audio_payload(plan.source_path)
        plan.target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(plan.source_path, plan.target_path)

        if plan.target_path.stat().st_size != plan.source_path.stat().st_size:
            raise ValueError("copied file size does not match source")
        copied_hash = sha256_file(plan.target_path)
        if copied_hash != original_hash:
            raise ValueError("copied file hash does not match source")

        if settings.write_canonical_genre_to_metadata and plan.canonical_genre != settings.unknown_genre_dir:
            try:
                write_genre(plan.target_path, plan.canonical_genre)
            except Exception as exc:  # noqa: BLE001 - metadata write failures should be reviewable
                notes.append(f"metadata_write_error: {exc}")

        final_hash = sha256_file(plan.target_path)
        stored = database.save_processed_song(
            connection,
            plan,
            original_hash=original_hash,
            final_hash=final_hash,
            audio_hash=audio_hash,
            status="needs_review" if notes else "processed",
            notes="; ".join(notes) if notes else None,
        )

        if not notes:
            source_archive_path, source_cleanup_status, source_archived_at, source_removed_at = _complete_source(
                settings,
                plan,
                archive_occupied,
            )
            if source_cleanup_status != "kept":
                database.update_song_source_cleanup(
                    connection,
                    stored.id,
                    source_cleanup_status=source_cleanup_status,
                    source_archive_path=source_archive_path,
                    source_archived_at=source_archived_at,
                    source_removed_at=source_removed_at,
                )

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


def _complete_source(
    settings: Settings,
    plan: Plan,
    archive_occupied: set[Path],
) -> tuple[Path | None, str, str | None, str | None]:
    action = settings.source_completion_action
    if action == "keep":
        return None, "kept", None, None

    if action in {"archive_move", "archive_copy"}:
        relative = relative_archive_path(settings.source_root, plan.source_path)
        archive_path = ensure_unique_path(
            settings.processed_source_root / relative,
            archive_occupied,
            str(plan.source_path),
        )
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        if action == "archive_copy":
            shutil.copy2(plan.source_path, archive_path)
            return archive_path, "archive_copied", utc_now(), None
        shutil.move(plan.source_path, archive_path)
        _remove_empty_source_dirs(settings, plan.source_path.parent)
        return archive_path, "archive_moved", utc_now(), utc_now()

    if action == "delete":
        plan.source_path.unlink()
        _remove_empty_source_dirs(settings, plan.source_path.parent)
        return None, "deleted", None, utc_now()

    raise ValueError(f"Unsupported source completion action: {action}")


def _remove_empty_source_dirs(settings: Settings, start: Path) -> None:
    if not settings.remove_empty_source_dirs:
        return
    source_root = settings.source_root.resolve()
    current = start.resolve()
    while current != source_root and source_root in current.parents:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent
