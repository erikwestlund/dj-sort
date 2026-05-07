# DJ Sort Implementation Checklist

Use this as the working build checklist. Keep items small enough to implement and verify independently.

## Phase 0: Project Setup

- [x] Create `pyproject.toml`.
- [x] Configure Python `3.12+`.
- [x] Configure `uv` project workflow.
- [x] Make environment setup scriptable from a clean checkout.
- [x] Add `scripts/bootstrap` to create/sync the local environment.
- [x] Ensure `scripts/bootstrap` installs or verifies `uv` prerequisites clearly.
- [x] Ensure `scripts/bootstrap` creates `.venv` through `uv sync`.
- [x] Commit `uv.lock` once dependencies are chosen.
- [x] Add `scripts/test` for running the test suite.
- [x] Add `scripts/lint` for formatting/lint checks.
- [x] Add `scripts/check` to run lint and tests together.
- [x] Add `scripts/run` wrapper for local CLI execution if useful.
- [x] Document direct command equivalents for every script.
- [x] Add CLI entrypoint for `dj-sort`.
- [x] Add initial package layout under `src/dj_sort/`.
- [x] Add `pytest` test setup.
- [x] Add `settings.example.yaml`.
- [x] Add `genres.example.yaml`.
- [x] Add basic README usage notes.

## Phase 0.1: Scriptable Environment Details

- [x] Support `uv sync` as the canonical setup command.
- [x] Support `uv run dj-sort --help` after bootstrap.
- [x] Avoid requiring manual shell activation for normal commands.
- [x] Keep `.venv/` local and gitignored.
- [x] Add `.python-version` if using a pinned local Python version.
- [x] Add `.gitignore` entries for `.venv/`, caches, reports, temp files, and local settings.
- [x] Add `settings.yaml` to `.gitignore` if it may contain personal paths.
- [x] Keep `settings.example.yaml` committed.
- [x] Keep `genres.example.yaml` committed.
- [x] Add bootstrap validation for Python version.
- [x] Add bootstrap validation for importability of `dj_sort`.
- [x] Add bootstrap validation for CLI startup.
- [x] Add optional diagnostics for `ffmpeg`, `ffprobe`, and `fpcalc` availability.
- [x] Document macOS setup assumptions.
- [x] Document how to rebuild the environment from scratch.

Suggested script commands:

```text
scripts/bootstrap
scripts/check
scripts/test
scripts/lint
uv run dj-sort --help
uv run dj-sort process --settings settings.yaml --dry-run --limit 10
```

## Phase 1: MVP/Core Pipeline

- [x] Load `settings.yaml`.
- [x] Validate settings with useful error messages.
- [x] Support `--settings` CLI override.
- [x] Resolve configured binary paths.
- [x] Fall back to system `PATH` for null binary paths.
- [x] Add diagnostic output for resolved binaries.
- [x] Recursively scan `source_root`.
- [x] Filter supported audio formats.
- [x] Report unsupported files with reasons.
- [x] Process files in deterministic path order.
- [x] Implement `limit` for candidate audio files.
- [x] Read metadata with `mutagen`.
- [x] Extract artist, title, genre, BPM, key, album, album artist, track number, release date, duration, bitrate, sample rate, extension, and file size where available.
- [x] Infer artist/title from `Artist - Title.ext`.
- [x] Infer artist/title from `Artist-Title.ext`.
- [x] Apply `Unknown Artist` fallback.
- [x] Apply `Unknown Title` fallback.
- [x] Track internal processing labels in planning output.
- [x] Load `genres.yaml`.
- [x] Normalize genre aliases for matching.
- [x] Apply canonical genre mappings.
- [x] Preserve unmapped genre values by default.
- [x] Route missing genre to `_Needs Genre`.
- [x] Normalize key to Camelot when confident.
- [x] Omit uncertain or missing key from filename.
- [x] Format BPM according to `bpm_format`.
- [x] Omit missing BPM from filename.
- [x] Generate safe destination directory.
- [x] Generate safe filename.
- [x] Remove extra separators when BPM/key are missing.
- [x] Sanitize path separators and control characters.
- [x] Normalize Unicode consistently.
- [x] Enforce filename/path length limits.
- [x] Detect destination path collisions.
- [x] Add deterministic collision suffix.
- [x] Implement `process --dry-run`.
- [x] Emit human-readable dry-run report.
- [x] Emit JSON dry-run report.

## Genre Discovery

- [x] Implement `genres discover` command.
- [x] Recursively scan configured input path.
- [x] Read only genre metadata.
- [x] Preserve raw genre values exactly as read.
- [x] Compute normalized genre grouping key.
- [x] Count files per raw genre value.
- [x] Count missing genre files.
- [x] Capture example paths per genre.
- [x] Respect `genre_discovery.max_examples_per_genre`.
- [x] Respect `limit`.
- [x] Mark genres already covered by `genres.yaml`.
- [x] Mark unmapped genres.
- [x] Output text report.
- [x] Output JSON report.
- [x] Output YAML bootstrap report.
- [x] Support `--format <text|json|yaml>`.
- [x] Support `--output <path>`.

## Phase 2: Copy Pipeline + SQLite

- [x] Create SQLite database on demand.
- [x] Create schema migrations or schema initialization.
- [x] Create `artist` table.
- [x] Create `genre` table.
- [x] Create `processing_label` table.
- [x] Create `duplicate_group` table.
- [x] Create `potential_duplicate_group` table.
- [x] Create `song` table.
- [x] Create `song_processing_label` table.
- [x] Create `song_operation_log` table.
- [x] Create required indexes.
- [x] Upsert artists case-insensitively.
- [x] Upsert genres case-insensitively.
- [x] Insert planned song records.
- [x] Store denormalized display fields on `song`.
- [x] Compute source full-file SHA-256 by streaming.
- [x] Copy source file into managed library.
- [x] Verify copied file size.
- [x] Verify copied file full-file hash.
- [x] Update `current_path`.
- [x] Update `processing_status`.
- [x] Record operation log entries.
- [x] Continue safely after per-file failures.
- [x] Mark failed files as `error` or `needs_review`.
- [x] Prevent automatic overwrites.
- [x] Make repeated runs idempotent.

## Phase 3: Metadata Write-Back + Hashing

- [x] Write canonical genre to managed MP3 files.
- [x] Write canonical genre to managed FLAC files.
- [x] Write canonical genre to managed M4A/AAC/ALAC files.
- [x] Write canonical genre to managed AIFF files.
- [x] Handle WAV metadata conservatively.
- [x] Never write canonical genre to preserved source files.
- [x] Never write canonical genre to processed-source archive copies.
- [x] Compute final managed full-file SHA-256 after metadata write-back.
- [x] Store `original_file_hash_with_metadata`.
- [x] Store `file_hash_with_metadata`.
- [x] Implement best-effort `file_hash_without_metadata`.
- [x] Allow `file_hash_without_metadata` to be nullable.
- [x] Do not block copy pipeline on unsupported metadata-independent hashing.
- [x] Add review/error notes for metadata write failures.

## Phase 4: Duplicate Reporting

- [x] Group exact duplicates by `file_hash_without_metadata`.
- [x] Do not create exact groups for null audio hashes.
- [x] Normalize artist names for potential duplicate matching.
- [x] Normalize titles for potential duplicate matching.
- [x] Create `potential_duplicate_group` records.
- [x] Link songs to potential duplicate groups.
- [x] Add `Potential Duplicate` processing label.
- [x] Mark potential duplicates as review hints only.
- [x] Generate exact duplicate report.
- [x] Generate potential duplicate report.
- [x] Include paths, artist, title, genre, BPM, key, format, size, bitrate, and duration where available.
- [x] Avoid automated delete decisions.
- [x] Keep any duplicate ranking advisory only.

## Phase 5: Source Archival Workflows

- [x] Implement `source_completion_action: keep`.
- [x] Implement `source_completion_action: archive_move`.
- [x] Implement `source_completion_action: archive_copy`.
- [x] Implement `source_completion_action: delete` only when explicitly configured.
- [x] Add `--archive-source` shortcut.
- [x] Add `--keep-source` shortcut.
- [x] Add `--delete-source` shortcut.
- [x] Preserve source-relative paths under `processed_source_root`.
- [x] Detect archive path collisions.
- [x] Add deterministic archive collision suffix.
- [x] Never overwrite archive files.
- [x] Store `source_archive_path`.
- [x] Store `source_archived_at`.
- [x] Store `source_cleanup_status`.
- [x] Skip source completion for failed/skipped/review files.
- [x] Optionally remove empty source directories.
- [x] Report source files kept, archived, removed, or failed.

## Phase 6: Genre Consolidation

- [x] Implement `genres consolidate` command.
- [x] Support `--from <genre>`.
- [x] Support `--to <genre>`.
- [x] Support consolidation mappings from settings.
- [x] Default consolidation to dry-run.
- [x] Find songs by current canonical genre.
- [x] Find songs by raw genre.
- [x] Compute target genre directory.
- [x] Detect consolidation path collisions.
- [x] Report planned metadata updates.
- [x] Report planned file moves.
- [x] Write target genre to managed file metadata.
- [x] Move managed file into target genre directory.
- [x] Update `song.genre_id`.
- [x] Update `song.display_genre`.
- [x] Update `song.current_path`.
- [x] Update `song.file_hash_with_metadata`.
- [x] Preserve `song.file_hash_without_metadata`.
- [x] Log consolidation in `song_operation_log`.
- [x] Mark failed consolidation files for review.
- [x] Optionally remove empty genre directories.

## Reports And CLI Polish

- [x] Standardize text report format.
- [x] Standardize JSON report schema.
- [x] Add `--output <path>` across report-producing commands.
- [x] Add verbose/diagnostic mode.
- [x] Include resolved binary paths in diagnostic output.
- [x] Include summary counts for every command.
- [x] Include elapsed time in summaries.
- [x] Ensure non-zero exit codes for fatal config/setup failures.
- [x] Ensure per-file failures do not abort the whole batch unless unsafe.

## Likely Hard Parts To Spike

- [ ] Verify MP3 genre read/write behavior with ID3 versions.
- [ ] Verify FLAC genre read/write behavior.
- [ ] Verify M4A/AAC/ALAC genre read/write behavior.
- [ ] Verify AIFF genre read/write behavior.
- [x] Decide how conservative to be with WAV metadata writes.
- [ ] Test metadata write-back changes to full-file hashes.
- [x] Test Unicode normalization on macOS.
- [x] Test case-insensitive filename collisions.
- [x] Test very long artist/title filenames.
- [ ] Test cross-filesystem copy/archive behavior.
- [x] Test key normalization edge cases.
- [x] Prototype metadata-independent hashing per format.

## Future Enhancements

- [ ] Acoustic fingerprinting for re-encoded duplicate discovery.
- [ ] ML-based genre classification.
- [ ] Key analysis when metadata is missing.
- [ ] BPM analysis when metadata is missing.
- [ ] ReplayGain or loudness analysis.
- [ ] Web metadata lookup.
- [ ] Interactive duplicate review UI.
- [ ] Rekordbox XML export.
- [ ] Serato crate generation.
- [ ] Watch-folder mode.
- [ ] Configurable artist/title cleanup rules.
- [ ] DJ-facing tags or labels.
