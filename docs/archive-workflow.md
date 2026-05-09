# Archive Workflow

This is the workflow for unpacking old ZIP collections and turning them into numbered review batches.

The goal is not to perfectly classify immediately. The goal is to make every archive visible, numbered, described, and ready for the normal genre workflow.

## Folder Layout

Source ZIPs:

```text
/Volumes/External/DJing/Unprocessed/Bulla/Archives
```

Extracted archives:

```text
/Volumes/External/DJing/Unprocessed/Bulla/Extracted Archives
```

Checklist and summaries:

```text
reports/archive-checklists/bulla-archives.md
reports/archive-checklists/bulla-extraction-manifest.json
reports/archive-checklists/bulla-archive-directory-summary.json
reports/archive-checklists/bulla-bulk-extraction-status.md
```

## Naming

Use numbered folders with readable names:

```text
1 - Old Downloads Added Feb 2015
2 - New Vinyl Rips
13 - 04 Bun It Feat. Fixate - 8A
84 - Vaping - 1A
```

Numbering should be stable once assigned. Do not renumber later unless there is a strong reason.

## Extract One Archive

Create the target folder:

```bash
mkdir "/Volumes/External/DJing/Unprocessed/Bulla/Extracted Archives/86 - Pretty Name"
```

Try the normal macOS extractor first:

```bash
ditto -x -k "/Volumes/External/DJing/Unprocessed/Bulla/Archives/archive.zip" "/Volumes/External/DJing/Unprocessed/Bulla/Extracted Archives/86 - Pretty Name"
```

If `ditto` fails with `Unknown compression type`, use The Unarchiver, `7zip`, or `unar`. Some ZIPs use Deflate64, which the built-in tools here do not support.

## Flatten Single Top-Level Folders

Many archives extract as:

```text
86 - Pretty Name/RAW_ARCHIVE_FOLDER/*.mp3
```

Flatten them to:

```text
86 - Pretty Name/*.mp3
```

Only flatten when there is exactly one top-level content folder and no meaningful sibling files.

## Remove Junk Files

Remove extraction junk like:

```text
__MACOSX
.DS_Store
```

## Sample A Directory

Use metadata to understand what is inside:

```bash
uv run python - <<'PY'
from pathlib import Path
from collections import Counter
from dj_sort.metadata import read_metadata, is_supported_audio

root = Path('/Volumes/External/DJing/Unprocessed/Bulla/Extracted Archives/84 - Vaping - 1A')
raw = Counter()
artists = Counter()

for path in root.rglob('*'):
    if not path.is_file() or not is_supported_audio(path):
        continue
    metadata = read_metadata(path)
    raw[metadata.genre or '<missing>'] += 1
    artists[metadata.artist or '<missing>'] += 1

print('raw genres')
for genre, count in raw.most_common(30):
    print(f'{count}\t{genre}')

print('\nartists')
for artist, count in artists.most_common(20):
    print(f'{count}\t{artist}')
PY
```

## Refresh The Archive Checklist

The checklist table should include:
- archive number
- status
- source ZIP
- pretty folder
- song count
- size
- description

Descriptions should be short and useful, for example:

```text
Themes: House, Techno, Drum & Bass, Bass. Sample artists: Phil Weeks, Ital Tek, Luca Lozano. Review tags: Techno/303, Chillout, Bass/Footwork.
```

## Process An Extracted Archive

Once an archive folder is ready, use the normal workflow on that one folder:

```bash
uv run dj-sort transfer "/Volumes/External/DJing/Unprocessed/Bulla/Extracted Archives/84 - Vaping - 1A" --settings settings.yaml --format json --output reports/dry-run-vaping.json
```

Then review unmapped genres, update `genres.yaml`, rerun dry-run, and eventually write:

```bash
uv run dj-sort transfer "/Volumes/External/DJing/Unprocessed/Bulla/Extracted Archives/84 - Vaping - 1A" --settings settings.yaml --write --format json --output reports/write-vaping.json
```

## Status Meanings

- `pending`: not extracted or not inspected yet.
- `extracted`: files are unpacked into a numbered folder.
- `reviewed`: description/genre split has been reviewed.
- `transferred`: archive has been processed into the library workflow.
- `skipped`: intentionally not processed.
- `in_progress_external`: currently being extracted by an external app.
- `error`: extraction or inspection failed.

## Current Notes

- Built-in tools may fail on Deflate64 ZIPs.
- The Unarchiver successfully extracted `Vaping - 1A` and `04 Bun It feat. Fixate - 8A` when built-in tools could not.
- Very large archives should be treated as batches. Do not try to manually tag thousands of tracks row by row; normalize repeatable genre labels first.
