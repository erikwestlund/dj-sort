# DJ Library Pre-Processor Architecture

## Purpose

Build a local-first personal power-user tool for preparing a large dump of audio files before importing them into DJ software such as Serato DJ, Rekordbox, Traktor, Engine DJ, or VirtualDJ.

The tool organizes files into a managed DJ library, normalizes genre metadata, creates predictable filenames, records operational state in SQLite, and produces duplicate/review reports.

Implementation speed matters. The architecture should be clear enough to support rapid iterative development without forcing perfect abstractions too early.

## Golden Path

```text
source dump -> dry run -> process into managed library -> review reports -> import managed library into DJ software
```

Typical real-world flow:

1. Point `dj-sort` at a messy recursive source dump.
2. Run `genres discover` to list every unique raw genre value.
3. Update `genres.yaml` with canonical genre mappings.
4. Run `process --dry-run --limit 50` to inspect planned changes.
5. Run `process --write` to copy files into the managed library.
6. Optionally archive completed source files into a processed-source archive.
7. Review duplicate and potential duplicate reports.
8. Import only the managed library into DJ software.

## Design Principles

1. Dry run by default.
2. Copy-first workflow by default.
3. Preserve source files by default.
4. Never overwrite files automatically.
5. Never auto-delete duplicates.
6. SQLite is the operational source of truth.
7. Filesystem layout should remain understandable even if the database is lost.
8. Prefer practical recovery over theoretical transaction perfection.
9. Denormalize intentionally where it improves reporting, debugging, and operational confidence.
10. Keep the architecture ambitious, but sequence implementation so each phase is useful on its own.

## Non-Goals

1. Do not replace DJ library software.
2. Do not edit beat grids, hot cues, loops, crates, playlists, or DJ software databases.
3. Do not write DJ-facing tags except the canonical genre metadata field.
4. Do not automatically delete duplicates.
5. Do not attempt perceptual fingerprinting in the initial implementation.
6. Do not build a perfect rollback system for filesystem operations.

## Supported Formats

Initial supported audio formats:

1. MP3
2. FLAC
3. M4A / AAC / ALAC
4. WAV
5. AIFF

Unsupported files should be skipped with a clear reason and included in reports.

## Implementation Sequencing

Build in this order:

1. Phase 1: Metadata read, settings load, recursive scan, and dry-run planning.
2. Phase 2: Copy pipeline into managed library with DB writes and operation logging.
3. Phase 3: Canonical genre metadata write-back and final full-file hash update.
4. Phase 4: Duplicate and potential duplicate reporting.
5. Phase 5: Source archival workflows.
6. Phase 6: Genre consolidation for already-managed files.

Each phase should leave the CLI usable. Avoid blocking early phases on harder future work such as perceptual fingerprinting or advanced duplicate ranking.

## Phase 1: MVP/Core Pipeline

### Scope

The first implementation should produce accurate plans before touching files.

Phase 1 includes:

1. Load and validate `settings.yaml`.
2. Resolve external binary paths.
3. Recursively scan `source_root`.
4. Read metadata from supported audio files.
5. Infer missing artist/title from filename when safe.
6. Apply genre mappings.
7. Normalize key to Camelot where possible.
8. Build destination paths.
9. Detect target path collisions.
10. Produce text and JSON dry-run reports.

Phase 1 does not need to copy files, write metadata, or compute metadata-independent hashes.

### Metadata Fields

Read these fields when available:

1. Artist
2. Title
3. Genre
4. BPM
5. Musical key
6. Album
7. Album artist
8. Track number
9. Year or release date
10. Duration
11. Bitrate
12. Sample rate
13. File extension
14. File size

The filename requires artist and title. BPM and key are optional components.

### Missing Metadata Policy

Default behavior:

1. Missing genre goes to `_Needs Genre`.
2. Missing artist uses `Unknown Artist` and records an internal processing label.
3. Missing title uses `Unknown Title` and records an internal processing label.
4. Missing BPM is omitted from the filename.
5. Missing key is omitted from the filename.

Filename inference should run before using unknown fallbacks.

Supported inference patterns:

```text
Artist - Title.ext
Artist-Title.ext
```

Inference rules:

1. Infer only when exactly one separator is found after trimming whitespace.
2. Do not infer from empty artist or title sides.
3. Prefer real metadata over inferred filename values.
4. Record `Inferred Metadata` as an internal processing label.

### Genre Discovery

Genre discovery is a read-only command for building and refining `genres.yaml`.

Behavior:

1. Recursively scan a configured path.
2. Read only metadata needed to extract genre values.
3. Preserve raw genre strings exactly as read.
4. Also report normalized genre keys used for grouping.
5. Count files per raw genre value.
6. Count files with missing genre separately.
7. Include example file paths per genre.
8. Support `limit` for quick sampling.
9. Do not copy, move, rename, hash, or write metadata.

Output should support text, JSON, and YAML suitable for bootstrapping a genre map.

Example text output:

```text
Raw Genre        Count  Canonical
Drum and Bass    132    Drum & Bass
DnB               44    Drum & Bass
Liquid DNB        12    <unmapped>
<missing>          9    _Needs Genre
```

Example YAML bootstrap output:

```yaml
genres:
  Drum and Bass: Drum & Bass
  DnB: Drum & Bass
  Liquid DNB: null
```

### Canonical Genre Handling

Genre normalization is driven by a user-editable YAML or JSON map.

Example `genres.yaml`:

```yaml
genres:
  Drum and Bass: Drum & Bass
  DnB: Drum & Bass
  D&B: Drum & Bass
  House Music: House
```

Rules:

1. Map keys are aliases.
2. Map values are canonical genre names.
3. Matching is case-insensitive by default.
4. Matching trims leading and trailing whitespace.
5. Matching collapses repeated internal whitespace.
6. The canonical value controls the destination directory name.
7. If no map entry matches, preserve the original genre by default.
8. Unknown genres are reported so the user can expand the map.
9. The canonical genre is written back into managed file metadata in Phase 3.

Treat genre as a single field for the initial version. Do not split on delimiters such as `;`, `/`, or `,`. If a format exposes multiple native genre values, join them into one display string and flag the file for review.

### Filename And Destination Planning

Default destination path:

```text
{library_root}/{canonical_genre}/{safe_filename}
```

Default filename template:

```text
{artist} - {title} - {bpm} - {key}.{ext}
```

If BPM or key is missing, remove that component and its adjacent separator.

Examples:

```text
Artist - Title - 174 - 8A.mp3
Artist - Title - 174.mp3
Artist - Title - 8A.mp3
Artist - Title.mp3
Unknown Artist - Unknown Title.mp3
```

Filename rules:

1. Artist means track artist, not album artist.
2. BPM defaults to integer formatting.
3. Key defaults to Camelot notation such as `8A` or `2B`.
4. Extension should be derived from actual file type when practical.
5. Unsafe path characters must be sanitized.
6. Unicode should be normalized consistently, preferably NFC.
7. Filename length should be capped to avoid path length failures.

Collision rules:

1. Never overwrite an existing file.
2. If a target path exists for a different file, append a deterministic suffix.
3. Prefer a short hash suffix such as ` - a1b2c3d4`.

## Phase 2: Operational Safety + Recovery

### Scope

Phase 2 turns plans into managed library files with practical recovery mechanics.

Phase 2 includes:

1. Create destination directories.
2. Copy source files into the managed library.
3. Verify the copied file initially matches source file size and full-file hash.
4. Insert and update SQLite records.
5. Record operation logs.
6. Keep source files by default.
7. Support `limit` for batch runs.

Phase 2 should still avoid metadata write-back until the copy pipeline is stable.

### Operational Model

Filesystem operations cannot be fully contained inside a SQLite transaction. The implementation should use clear statuses and operation logs rather than pretending full atomicity is possible.

Practical model:

1. Compute source full-file hash before copy.
2. Insert or update a `song` row with `processing_status = 'processing'`.
3. Copy the file into the managed destination path.
4. Verify copied file size and full-file hash.
5. Update `current_path`, hashes, and `processing_status`.
6. Record each meaningful step in `song_operation_log`.
7. On failure, mark `error` or `needs_review` and continue with the next file when safe.

Recovery should be pragmatic. A later run should be able to inspect the DB row, current path, source path, and hashes to decide whether the file was already copied, failed mid-run, or needs review.

### Source Completion Planning

Phase 2 should keep source files by default and record enough state for later source completion workflows.

Source completion actions such as `archive_move`, `archive_copy`, and `delete` are implemented in Phase 5 after the copy pipeline and metadata write-back are reliable.

### Idempotency Protections

Idempotency is more important than perfect rollback.

Rules:

1. Use source path, current path, full-file hash, and audio hash when available to recognize already-seen files.
2. If a file is already at its target path with matching hashes, mark it unchanged.
3. If a destination path exists for a different file, apply collision handling.
4. If a DB record exists but the file is missing, mark it `needs_review` rather than guessing.
5. Re-running the same command should not duplicate records or repeatedly rename files.

## Phase 3: Metadata Write-Back And Hashing

### Scope

Phase 3 writes canonical genre metadata to the managed copy and updates final hashes.

Write-back rules:

1. Write canonical genre only to the managed library file.
2. Do not write canonical genre to the source file when source is preserved.
3. Do not write canonical genre to the processed-source archive copy.
4. If genre is missing, do not write a placeholder genre unless explicitly configured.
5. Use the correct metadata mechanism for the file type, such as ID3 for MP3.

### Hashing Strategy

Store three hashes where practical:

1. `original_file_hash_with_metadata`: complete source bytes before managed metadata writes.
2. `file_hash_with_metadata`: complete managed file bytes after canonical genre metadata write-back.
3. `file_hash_without_metadata`: best-effort exact audio-payload hash for duplicate grouping.

Keep the first implementation practical:

1. Full-file SHA-256 is required and should be streamed.
2. Metadata-independent hash is best-effort and can be nullable.
3. Start with the simplest reliable implementation per format.
4. Do not block the copy pipeline on perfect metadata-independent hashing.
5. Do not attempt perceptual fingerprinting in the core pipeline.

The metadata-independent hash is for exact encoded-audio duplicate grouping only. It will not detect re-encoded duplicates, different masters, different bitrates, or different codecs.

## Phase 4: Duplicate Detection

### Scope

The duplicate system is primarily a review and reporting system. It should not imply automated deletion decisions.

Keep two separate concepts:

1. Exact duplicate groups based on `file_hash_without_metadata`.
2. Potential duplicate groups based on normalized artist and normalized title.

### Exact Duplicate Groups

Exact duplicate candidates:

1. Same `file_hash_without_metadata`.
2. Possibly different `file_hash_with_metadata` due to tags or artwork.

Exact groups are strong evidence that the encoded audio payload is the same. They are still not auto-delete candidates.

### Potential Duplicate Groups

Potential duplicate candidates:

1. Same normalized artist name.
2. Same normalized song title.
3. Different or missing `file_hash_without_metadata`.

Potential duplicate groups are review hints. They may represent remixes, edits, remasters, radio edits, clean/dirty versions, re-encodes, or false positives.

Name normalization should be conservative:

1. Case-fold text.
2. Trim whitespace.
3. Collapse repeated internal whitespace.
4. Normalize Unicode consistently.
5. Do not remove remix/version/edit markers by default.

### Duplicate Reporting

Reports should show:

1. Exact duplicate groups.
2. Potential duplicate groups.
3. File paths.
4. Artist/title/genre/BPM/key.
5. File size, duration, bitrate, and format where available.
6. Current processing status.
7. Suggested review notes.

Advanced duplicate ranking is intentionally deferred. If implemented later, it should remain advisory and versioned with `rank_version`.

## Phase 5: Source Archival Workflows

### Scope

Source archival is the point where the tool can start cleaning the original dump folder without losing traceability.

This phase builds on the already-working copy pipeline. Do not implement archive deletion/move behavior before managed library copy, metadata write-back, and DB updates are reliable.

Phase 5 includes:

1. `archive_move` source completion.
2. `archive_copy` source completion.
3. `delete` source completion only when explicitly configured.
4. Archive collision handling.
5. Source cleanup reporting.
6. Optional empty source directory cleanup.

Supported actions:

1. `keep`: leave source file in place. This is the default.
2. `archive_move`: move source file into `processed_source_root`.
3. `archive_copy`: copy source file into `processed_source_root` and keep source in place.
4. `delete`: delete source file after successful processing.

Operational rules:

1. Default remains `keep`.
2. Prefer `archive_move` over `delete` when cleaning the dump folder.
3. Archive files preserve the original source bytes.
4. Archive files are not rewritten with canonical genre metadata.
5. Archive path and cleanup status are stored in SQLite.
6. Run only after managed copy verification, metadata write-back, and database update succeed.
7. Never run for skipped, failed, duplicate-review, or needs-review files by default.
8. Preserve the source file's relative path under `source_root` when archiving.
9. Never overwrite archive files.

## Phase 6: Genre Consolidation

### Scope

Genre consolidation updates already-managed files after processing.

Example:

```text
Drum and Bass -> Drum & Bass
DnB -> Drum & Bass
```

For each matching managed song:

1. Find songs by current canonical genre, raw genre, or explicit source genre name.
2. Resolve the target canonical genre.
3. Compute the new target path using the existing filename under the target genre directory.
4. Detect target path collisions before writing changes.
5. In dry-run mode, report planned metadata updates, DB updates, and file moves.
6. In write mode, update managed file genre metadata.
7. Move the managed file into the target genre directory.
8. Update `song.genre_id`, `display_genre`, `current_path`, `file_hash_with_metadata`, and `updated_at`.
9. Preserve `file_hash_without_metadata` because audio should not change.
10. Log the operation in `song_operation_log`.

Safety rules:

1. Default to dry-run.
2. Never operate on files outside `library_root` unless explicitly allowed.
3. Never overwrite a target path.
4. Do not remove empty genre directories unless `remove_empty_genre_dirs` is enabled.
5. If metadata write-back or file move fails, mark the song for review.

Genre consolidation should be done before importing into DJ software whenever possible because it changes file paths.

## SQLite Architecture

SQLite is the source of operational truth. The schema intentionally mixes normalization and denormalization.

Normalized tables provide stable identities:

1. `artist`
2. `genre`
3. `duplicate_group`
4. `potential_duplicate_group`
5. `processing_label`

The `song` table is intentionally denormalized for reporting and recovery:

1. Store `display_artist` even though `artist_id` exists.
2. Store `display_genre` and `raw_genre` even though `genre_id` exists.
3. Store source and current paths directly.
4. Store hash fields directly.
5. Store processing and cleanup status directly.

Denormalization is useful here because this is an operational tool, not a multi-user normalized application database.

### Schema Draft

```sql
CREATE TABLE artist (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE COLLATE NOCASE,
  display_name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE genre (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE COLLATE NOCASE,
  display_name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE processing_label (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE COLLATE NOCASE,
  display_name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE duplicate_group (
  id INTEGER PRIMARY KEY,
  file_hash_without_metadata TEXT NOT NULL,
  rank_version INTEGER NOT NULL DEFAULT 1,
  preferred_song_id INTEGER,
  review_status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE potential_duplicate_group (
  id INTEGER PRIMARY KEY,
  normalized_artist_name TEXT NOT NULL,
  normalized_title TEXT NOT NULL,
  review_status TEXT NOT NULL DEFAULT 'pending',
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(normalized_artist_name, normalized_title)
);

CREATE TABLE song (
  id INTEGER PRIMARY KEY,
  artist_id INTEGER NOT NULL REFERENCES artist(id),
  genre_id INTEGER REFERENCES genre(id),
  title TEXT NOT NULL,
  normalized_title TEXT NOT NULL,
  display_artist TEXT NOT NULL,
  normalized_display_artist TEXT NOT NULL,
  display_genre TEXT,
  raw_genre TEXT,
  bpm TEXT,
  raw_musical_key TEXT,
  musical_key TEXT,
  album TEXT,
  album_artist TEXT,
  track_number TEXT,
  release_date TEXT,
  duration_ms INTEGER,
  bitrate INTEGER,
  sample_rate INTEGER,
  file_size INTEGER NOT NULL,
  extension TEXT NOT NULL,
  source_path TEXT NOT NULL,
  current_path TEXT NOT NULL,
  source_removed_at TEXT,
  source_cleanup_status TEXT,
  source_archive_path TEXT,
  source_archived_at TEXT,
  original_file_hash_with_metadata TEXT NOT NULL,
  file_hash_with_metadata TEXT,
  file_hash_without_metadata TEXT,
  duplicate_group_id INTEGER,
  potential_duplicate_group_id INTEGER,
  duplicate_rank INTEGER,
  rank_version INTEGER NOT NULL DEFAULT 1,
  processing_status TEXT NOT NULL,
  processing_notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(current_path)
);

CREATE TABLE song_processing_label (
  song_id INTEGER NOT NULL REFERENCES song(id),
  processing_label_id INTEGER NOT NULL REFERENCES processing_label(id),
  created_at TEXT NOT NULL,
  PRIMARY KEY (song_id, processing_label_id)
);

CREATE TABLE song_operation_log (
  id INTEGER PRIMARY KEY,
  song_id INTEGER NOT NULL REFERENCES song(id),
  operation_type TEXT NOT NULL,
  previous_path TEXT,
  new_path TEXT,
  previous_genre TEXT,
  new_genre TEXT,
  previous_file_hash_with_metadata TEXT,
  new_file_hash_with_metadata TEXT,
  status TEXT NOT NULL,
  notes TEXT,
  created_at TEXT NOT NULL
);

CREATE INDEX idx_song_audio_hash ON song(file_hash_without_metadata);
CREATE INDEX idx_song_full_hash ON song(file_hash_with_metadata);
CREATE INDEX idx_song_original_full_hash ON song(original_file_hash_with_metadata);
CREATE INDEX idx_song_artist_title ON song(display_artist, title);
CREATE INDEX idx_song_normalized_artist_title ON song(normalized_display_artist, normalized_title);
CREATE INDEX idx_song_genre ON song(genre_id);
CREATE INDEX idx_song_potential_duplicate_group ON song(potential_duplicate_group_id);
CREATE INDEX idx_song_processing_label_label_id ON song_processing_label(processing_label_id);
CREATE INDEX idx_song_operation_log_song_id ON song_operation_log(song_id);
```

Hash fields should not be unique by default. Duplicate physical files can exist at different paths and should be recorded before any human review.

### Status Values

Suggested `processing_status` values:

1. `new`
2. `processing`
3. `processed`
4. `needs_review`
5. `duplicate_candidate`
6. `potential_duplicate_candidate`
7. `quarantined`
8. `skipped`
9. `error`

Suggested `source_cleanup_status` values:

1. `kept`
2. `archive_moved`
3. `archive_copied`
4. `deleted`
5. `failed`
6. `not_applicable`

Initial internal processing labels:

1. `Unknown Artist`
2. `Unknown Title`
3. `Inferred Metadata`
4. `Needs Genre`
5. `Needs Key Review`
6. `Genre Consolidated`
7. `Potential Duplicate`

## Settings

Defaults live in `settings.yaml`. CLI flags override settings for one run.

Suggested `settings.yaml`:

```yaml
source_root: /Music/Dump
library_root: /Music/DJ Library
processed_source_root: /Music/DJ Processed Source Archive
database_path: ~/.dj-sort/library.sqlite3
genre_map_path: ./genres.yaml
binary_paths:
  ffmpeg: null
  ffprobe: null
  fpcalc: null
genre_discovery:
  max_examples_per_genre: 5
  include_missing_genre: true
  output_format: text
recursive: true
dry_run: true
limit: null
source_completion_action: keep
source_archive_preserve_relative_path: true
remove_empty_source_dirs: false
detect_potential_duplicates: true
strict: false
bpm_format: integer
key_format: camelot
unknown_genre_dir: _Needs Genre
needs_review_dir: _Needs Review
quarantine_dir: _Duplicates Review
write_canonical_genre_to_metadata: true
omit_missing_filename_parts: true
filename_template: "{artist} - {title} - {bpm} - {key}.{ext}"
remove_empty_genre_dirs: false
genre_consolidation:
  mappings: {}
```

Binary resolution rules:

1. Explicit `binary_paths` values override system `PATH` discovery.
2. `null` means discover from system `PATH`.
3. Required missing binaries fail early with a clear error.
4. Optional missing binaries disable only the dependent feature.
5. Resolved binary paths should appear in diagnostic output.

## CLI Draft

Core commands:

```text
dj-sort scan --settings settings.yaml
dj-sort process --settings settings.yaml --dry-run
dj-sort process --settings settings.yaml --write --limit 100
dj-sort process --settings settings.yaml --write --archive-source
dj-sort duplicates --settings settings.yaml
dj-sort genres discover --settings settings.yaml
dj-sort genres discover --settings settings.yaml --format yaml --output discovered-genres.yaml
dj-sort genres consolidate --settings settings.yaml --from "DnB" --to "Drum & Bass" --dry-run
dj-sort genres consolidate --settings settings.yaml --from "DnB" --to "Drum & Bass" --write
```

Important CLI overrides:

1. `--source-root <path>`
2. `--library-root <path>`
3. `--processed-source-root <path>`
4. `--settings <path>`
5. `--dry-run`
6. `--write`
7. `--limit <n>`
8. `--source-completion-action <keep|archive_move|archive_copy|delete>`
9. `--keep-source`
10. `--archive-source`
11. `--delete-source`
12. `--format <text|json|yaml>`
13. `--output <path>`
14. `--ffmpeg <path>`
15. `--ffprobe <path>`
16. `--fpcalc <path>`

## Reports

Reports should be available as human-readable text and JSON. YAML is useful for genre discovery output.

Reports should cover:

1. Files planned, processed, skipped, failed, and needing review.
2. Unknown or unmapped genres.
3. Unique raw genre names discovered recursively.
4. Exact duplicate groups.
5. Potential duplicate groups.
6. Filename collisions.
7. Source files archived, removed, or kept.
8. Limit value and whether the run stopped because the limit was reached.
9. Genre consolidation actions.
10. Resolved external binary paths in diagnostic mode.

## Likely Hard Parts

### WAV Metadata Inconsistencies

WAV metadata support varies across tools. Some files may read correctly in one application and not another. Treat WAV metadata write-back as higher risk and report unsupported or uncertain writes clearly.

### Metadata Rewrite Behavior

Writing metadata can change full-file bytes, tag ordering, embedded artwork layout, timestamps inside tags, or padding. This is why the system stores both original and final full-file hashes.

### Cross-Filesystem Operations

Source, archive, and managed library paths may live on different filesystems. Prefer copy-verify-source-completion over rename-based assumptions.

### Unicode And Path Normalization

macOS, Linux, and network filesystems can disagree on Unicode normalization and case sensitivity. Normalize filenames consistently and never rely on case-only differences.

### Key Normalization Ambiguity

Camelot conversion can be ambiguous when source keys are misspelled, enharmonic, or non-standard. If conversion is uncertain, omit key from filename and mark for review.

### Metadata-Independent Hashing

Exact audio-payload hashing can become a rabbit hole across formats. Keep it best-effort, nullable, and incrementally improved. Do not let it block the core copy pipeline.

## Tooling And Runtime

Use Python as the implementation language.

Recommended baseline:

1. Python `3.12+`
2. `uv` for package and environment management
3. `typer` for CLI
4. `pydantic` or `pydantic-settings` for settings validation
5. `PyYAML` or `ruamel.yaml` for YAML
6. `mutagen` for metadata read/write
7. `ffmpeg` and `ffprobe` for probing and future audio operations
8. Python built-in `sqlite3` initially
9. `pytest` for tests

Suggested layout:

```text
dj-sort/
  pyproject.toml
  settings.example.yaml
  genres.example.yaml
  src/dj_sort/
    cli.py
    settings.py
    metadata.py
    hashing.py
    database.py
    planning.py
    processing.py
    genres.py
    duplicates.py
    paths.py
    reports.py
  tests/
```

## Docker Strategy

The tool should be local-first but Docker-friendly.

Do not make Docker the primary workflow for the first implementation. Local execution is simpler for a personal file-management tool, especially on macOS where Docker volume performance and file permissions can be frustrating with large music folders.

Docker is useful later for:

1. Reproducible runtime environments.
2. Bundling native dependencies such as `ffmpeg`, `ffprobe`, and `fpcalc`.
3. Running on a NAS, server, or batch machine.
4. Avoiding local dependency setup for heavier audio or ML tooling.

Docker-friendly requirements:

1. All paths come from settings or CLI flags.
2. Source, library, and processed-source archive paths are mountable volumes.
3. Logs go to stdout/stderr.
4. Reports can be written to configurable paths.
5. External tools are discoverable and configurable.

Example future Docker command:

```text
docker run --rm \
  -v /Music/Dump:/source \
  -v "/Music/DJ Library:/library" \
  -v "/Music/DJ Processed Source Archive:/archive" \
  -v "$PWD/settings.yaml:/app/settings.yaml" \
  dj-sort process --settings /app/settings.yaml --dry-run --limit 100
```

## Future Enhancements

Future work should build on the operational model rather than disrupting it.

Possible enhancements:

1. Acoustic fingerprinting for re-encoded duplicate discovery.
2. ML-based genre classification.
3. Key and BPM analysis when metadata is missing.
4. ReplayGain or loudness analysis.
5. Web metadata lookup.
6. Interactive duplicate review UI.
7. Rekordbox XML export.
8. Serato crate generation.
9. Watch-folder mode.
10. Configurable artist/title cleanup rules.
11. DJ-facing tags or labels.

## MVP Definition

The first useful version is complete when it can:

1. Load settings and genre mappings.
2. Recursively scan a source dump.
3. Discover unique genre names.
4. Read artist, title, genre, BPM, and key metadata.
5. Plan destination paths using canonical genre and safe filenames.
6. Run dry-run with useful text and JSON reports.
7. Copy files into the managed library without overwriting.
8. Store song records in SQLite.
9. Write canonical genre metadata to managed copies.
10. Report exact and potential duplicate groups.
11. Keep source files by default.
12. Optionally archive source files after successful processing.
