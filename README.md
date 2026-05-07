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
- Copies files into `{library_root}/{canonical_genre}/`.
- Renames files as `Artist - Title - BPM - Key.{ext}`.
- Omits BPM/key cleanly when missing.
- Infers artist/title from filenames like `Artist - Title.mp3`.
- Writes canonical genre metadata back to the managed library copy.
- Keeps source files by default.
- Can move/copy processed source files into a separate archive location.
- Stores operational state in SQLite.
- Tracks exact duplicate groups when audio-payload hashing is available.
- Tracks potential duplicates by normalized artist/title.
- Supports dry-run mode by default.
- Supports `--limit` for safe batch processing.
- Supports after-the-fact genre consolidation for managed files.

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
source_root: /path/to/music-dump
library_root: /path/to/organized-dj-library
processed_source_root: /path/to/processed-source-archive
database_path: ~/.dj-sort/library.sqlite3
genre_map_path: ./genres.yaml
dry_run: true
limit: null
source_completion_action: keep
```

Important settings:

- `source_root`: messy recursive source folder.
- `library_root`: organized managed DJ library output folder.
- `processed_source_root`: optional archive for original source files after successful processing.
- `database_path`: SQLite database location.
- `genre_map_path`: canonical genre mapping file.
- `dry_run`: defaults to `true` for safety.
- `limit`: process only N candidate audio files.
- `source_completion_action`: `keep`, `archive_move`, `archive_copy`, or `delete`.

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

Create a YAML bootstrap report for genre mappings:

```bash
uv run dj-sort genres discover --settings settings.yaml --format yaml --output discovered-genres.yaml
```

### Dry Run

Always start here:

```bash
uv run dj-sort process --settings settings.yaml --dry-run --limit 25
```

Write a JSON dry-run report:

```bash
uv run dj-sort process --settings settings.yaml --dry-run --limit 25 --format json --output reports/dry-run.json
```

### Process Files

Copy files into the managed library and keep source files:

```bash
uv run dj-sort process --settings settings.yaml --write --limit 25
```

Copy files into the managed library and move originals into the processed-source archive:

```bash
uv run dj-sort process --settings settings.yaml --write --archive-source --limit 25
```

Copy source files into the archive while also keeping them in the source dump:

```bash
uv run dj-sort process --settings settings.yaml --write --source-completion-action archive_copy --limit 25
```

Delete source files after successful processing only if explicitly requested:

```bash
uv run dj-sort process --settings settings.yaml --write --delete-source --limit 25
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

## Safety Model

- Dry run is the default.
- Source files are kept by default.
- Files are copied into the managed library before source cleanup.
- The tool never overwrites existing files automatically.
- Duplicate reports are for review only.
- WAV metadata write-back is disabled pending format-specific validation.
- Personal paths live in `settings.yaml`, which is gitignored.

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

## macOS Notes

This project is developed local-first on macOS. Large mounted music folders may perform better with direct local execution than Docker Desktop volume mounts. Keep personal absolute paths in `settings.yaml`, which is gitignored, and use `settings.example.yaml` as the committed template.
