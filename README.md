# DJ Sort

Local-first DJ library pre-processor for organizing a messy recursive source dump into a clean managed DJ library before importing into Serato, Rekordbox, Traktor, Engine DJ, or similar software.

Happy Path:

1. Point `dj-sort` at a messy recursive source dump.
2. Run genre discovery.
3. Update `genres.yaml` with canonical mappings.
4. Run a dry run.
5. Process files into the managed library.
6. Review duplicate reports.
7. Import the managed library into DJ software.

## Features

- Recursively scans a source music dump.
- Reads audio metadata with `mutagen`.
- Discovers all unique raw genre values before processing.
- Applies canonical genre mappings from `genres.yaml`.
- Copies curated files into `{dj_library_dir}/{canonical_genre}/`.
- Routes missing or unmapped genre files into `uncategorizable_dir`.
- Renames files as `Artist - Title - BPM - Key.{ext}`.
- Omits BPM/key cleanly when missing.
- Infers artist/title from filenames like `Artist - Title.mp3`.
- Writes canonical genre metadata back to the managed library copy.
- Preserves changed raw genre values in a machine-readable comment on managed copies.
- Keeps unprocessed source files in place.
- Stores operational state in SQLite.
- Tracks exact duplicate groups when audio-payload hashing is available.
- Tracks potential duplicates by normalized artist/title.
- Supports dry-run mode by default.
- Supports `--limit` for safe batch processing.
- Supports after-the-fact genre consolidation for managed files.
- Exports Navidrome-compatible `.m3u` playlists for genre folders.

## Install

This project uses Python `3.12+` and `uv`.

Install `uv` first if needed:

```bash
brew install uv
```

Bootstrap the project:

```bash
scripts/bootstrap
```

This creates `.venv`, installs dependencies, validates the CLI, and prints a ready message.

Rebuild from scratch:

```bash
rm -rf .venv
scripts/bootstrap
```

You do not need to manually activate the virtualenv. Use `uv run ...` or the scripts in `scripts/`.

## Configure

Create local config files:

```bash
cp settings.example.yaml settings.yaml
cp genres.example.yaml genres.yaml
```

Edit `settings.yaml`:

```yaml
unprocessed_music_dir: /path/to/music-dump
dj_library_dir: /path/to/organized-dj-library
uncategorizable_dir: /path/to/uncategorizable-output
duplicates_dir: /path/to/duplicates
database_path: ./db/library.sqlite3
genre_map_path: ./genres.yaml
dry_run: true
limit: null
```

Important settings:

- `unprocessed_music_dir`: messy recursive source folder.
- `dj_library_dir`: organized managed DJ library output folder.
- `uncategorizable_dir`: valid audio that is not ready for the clean library.
- `duplicates_dir`: duplicate quarantine/review folder.
- `database_path`: SQLite database location. By convention this is `./db/library.sqlite3` so the operational DB can travel with local artifacts without being committed.
- `genre_map_path`: canonical genre mapping file.
- `dry_run`: defaults to `true` for safety.
- `limit`: process only N candidate audio files.
- `duplicate_policy`: `exact_only`, `potential_too`, or `report_only`.
- `preserve_original_genre_in_comment`: when canonical genre write-back changes a curated file's genre, append the raw genre to comments.
- `navidrome`: server/path settings for generating Navidrome `.m3u` playlists.

Edit `genres.yaml`:

```yaml
genres:
  Drum and Bass: Drum & Bass
  DnB: Drum & Bass
  D&B: Drum & Bass
  House Music: House
```

## Usage

Show help:

```bash
uv run dj-sort --help
```

Run diagnostics:

```bash
uv run dj-sort diagnostics --settings settings.yaml
```

### Discover Genres

List all unique genre metadata values in the source dump:

```bash
uv run dj-sort genres discover --settings settings.yaml
```

Shortcut for scanning a specific path under `unprocessed_music_dir`:

```bash
uv run dj-sort get-genres 2026-05-04 --settings settings.yaml
```

Use a different base directory:

```bash
uv run dj-sort get-genres House --base-dir dj_library --settings settings.yaml
uv run dj-sort get-genres "Missing Genre" --base-dir uncategorizable --settings settings.yaml
uv run dj-sort get-genres . --base-dir /absolute/path/to/music --settings settings.yaml
```

Create a YAML bootstrap report for genre mappings:

```bash
uv run dj-sort genres discover --settings settings.yaml --format yaml --output discovered-genres.yaml
```

### Dry Run

Dry-run a specific incoming directory:

```bash
uv run dj-sort transfer 2026-05-04 --settings settings.yaml
```

Use the same base directory handling as `get-genres`:

```bash
uv run dj-sort transfer . --base-dir /absolute/path/to/music --settings settings.yaml
```

Dry-run the whole unprocessed directory:

```bash
uv run dj-sort process --settings settings.yaml --dry-run --limit 25
```

Write a JSON dry-run report:

```bash
uv run dj-sort process --settings settings.yaml --dry-run --limit 25 --format json --output reports/dry-run.json
```

### Process Files

Copy curated files from a specific incoming directory and keep source files:

```bash
uv run dj-sort transfer 2026-05-04 --settings settings.yaml --write --limit 25
```

Copy curated files from the whole unprocessed directory and keep source files:

```bash
uv run dj-sort process --settings settings.yaml --write --limit 25
```

### Review Uncategorizable

Export files that would land in `uncategorizable_dir` to a CSV:

```bash
uv run dj-sort export-uncategorizable 2026-05-04 --settings settings.yaml
```

By default this writes `reports/uncategorizable/2026-05-04.csv`.

Fill in the `update_genre` column, then dry-run the metadata updates:

```bash
uv run dj-sort apply-genre-updates 2026-05-04.csv --settings settings.yaml
```

The apply command looks in the current path, `reports/`, and `reports/uncategorizable/`.

Apply the updates to the source files:

```bash
uv run dj-sort apply-genre-updates 2026-05-04.csv --settings settings.yaml --write
```

When applied with `--write`, each edited row is repaired in place: the source tag is updated, that file is re-transferred to its new destination, and the stale `uncategorizable_dir` copy for that source file is removed after the new copy succeeds.

Copy curated genres from exact artist/title duplicates, dry-run first:

```bash
uv run dj-sort copy-duplicate-genres 2026-05-04 --from-base "iTunes Library" --settings settings.yaml
```

Apply the duplicate genre updates:

```bash
uv run dj-sort copy-duplicate-genres 2026-05-04 --from-base "iTunes Library" --settings settings.yaml --write
```

### Duplicate Reports

Show exact and potential duplicate groups:

```bash
uv run dj-sort duplicates --settings settings.yaml
```

Write duplicate report to JSON:

```bash
uv run dj-sort duplicates --settings settings.yaml --format json --output reports/duplicates.json
```

### Genre Consolidation

Dry-run a managed-library genre merge:

```bash
uv run dj-sort genres consolidate --settings settings.yaml --from "DnB" --to "Drum & Bass" --dry-run
```

Apply the merge:

```bash
uv run dj-sort genres consolidate --settings settings.yaml --from "DnB" --to "Drum & Bass" --write
```

Genre consolidation updates managed file genre metadata, moves files to the target genre directory, and updates SQLite.

### Navidrome Playlists

Dry-run generated genre playlists:

```bash
uv run dj-sort export-navidrome-playlists --settings settings.yaml
```

Write playlists from a machine that can see the library path:

```bash
uv run dj-sort export-navidrome-playlists --settings settings.yaml --write
```

For the current `dj.lan` setup, playlists are generated under Navidrome's music folder as `_Playlists` and named with the `Uncurated: ` prefix. See `docs/navidrome-playlists.md` for the full server workflow, Docker Compose requirements, SSH regeneration script, and verification commands.

## Safety Model

- Dry run is the default.
- Source files are kept in place.
- Files are copied into the managed library before source cleanup.
- The tool never overwrites existing files automatically.
- Duplicate reports are for review only.
- WAV metadata write-back is disabled pending format-specific validation.
- Personal paths live in `settings.yaml`, which is gitignored.
- The operational SQLite DB lives in `db/`, which is gitignored and listed in `.artifacts` for manual portability between machines.

## Development

Run tests and lint:

```bash
scripts/check
```

Run tests only:

```bash
scripts/test
```

Run lint only:

```bash
scripts/lint
```

Project docs:

- `docs/spec.md`: architecture and implementation direction.
- `docs/checklist.md`: build checklist and remaining work.
- `docs/workflow.md`: fast batch processing and genre review workflow.
- `docs/archive-workflow.md`: archive extraction and numbered-batch workflow.
- `docs/navidrome-playlists.md`: Navidrome playlist generation, import, and verification workflow.

Local artifact convention:

- `settings.yaml`, `reports/`, and `db/` are local working artifacts and are ignored by git.
- `.artifacts` is a small manifest of local artifact paths to carry when moving the project state between machines.
- `db/library.sqlite3` is the active SQLite database path used by `settings.yaml`.

## macOS Notes

This project is developed local-first on macOS. Large mounted music folders may perform better with direct local execution than Docker Desktop volume mounts. Keep personal absolute paths in `settings.yaml`, which is gitignored, and use `settings.example.yaml` as the committed template. Keep local runtime state in `db/` and generated reports in `reports/`; both are gitignored and tracked in `.artifacts` as portable local state.
