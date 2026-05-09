# Fast Genre Workflow

This is the quick loop for turning a messy incoming folder into a clean managed DJ library.

The core idea:
- Run a dry scan.
- See which raw genres are not categorizable.
- Add obvious aliases to `genres.yaml`.
- Re-run until most files route to `Library`.
- Export the remaining review rows to CSV.
- Fill `update_genre` only for track-level decisions.
- Apply with `--write` when the dry-run is clean.

## 1. Pick A Batch

Use paths relative to `unprocessed_music_dir` when possible:

```bash
uv run dj-sort transfer "Bulla In Process" --settings settings.yaml --format json --output reports/dry-run.json
```

For absolute paths:

```bash
uv run dj-sort transfer "/Volumes/External/DJing/Unprocessed/Bulla In Process" --settings settings.yaml
```

Default behavior is dry-run unless `--write` is passed.

## 2. Review Genre Split

Summarize a JSON dry-run:

```bash
uv run python - <<'PY'
import json
from pathlib import Path
from collections import Counter

data = json.loads(Path('reports/dry-run.json').read_text())
plans = data['plans']
roots = Counter(
    'uncategorizable' if '/Uncategorizable/' in p['target_path'] else 'library'
    for p in plans
)
unc = [p for p in plans if '/Uncategorizable/' in p['target_path']]

print(dict(roots))
for genre, count in Counter(p['raw_genre'] or '<missing>' for p in unc).most_common(50):
    print(f'{count}\t{genre}')
PY
```

## 3. Add Obvious Alias Mappings

Update `genres.yaml` for raw genre labels that are clearly aliases or category decisions.

Examples:

```yaml
genres:
  FidgetyElectroShit: Fidget House
  Fidget-Step: Dubstep
  Grime/Dubstep: Grime
  Electro-Disco: Electro House
  Broken Beat: Breakbeat
```

Use aliases for repeatable raw labels. Do not use aliases for one-off track-specific guesses unless that raw label always means the same thing.

## 4. Re-run Dry Transfer

```bash
uv run dj-sort transfer "Bulla In Process" --settings settings.yaml --format json --output reports/dry-run-after-maps.json
```

Repeat steps 2-4 until remaining `Uncategorizable` rows are mostly real review work.

## 5. Copy Genres From Known Duplicates

If a better-tagged source exists, use it conservatively:

```bash
uv run dj-sort copy-duplicate-genres "Bulla In Process" --from-base "iTunes Library" --settings settings.yaml
```

If the dry-run has no conflicts and the suggestions look right:

```bash
uv run dj-sort copy-duplicate-genres "Bulla In Process" --from-base "iTunes Library" --settings settings.yaml --write
```

This writes source metadata for exact normalized artist/title matches only when the duplicate source has a curated genre.

## 6. Write The Batch

When the dry-run split looks good:

```bash
uv run dj-sort transfer "Bulla In Process" --settings settings.yaml --write --format json --output reports/transfer-write.json
```

This copies curated tracks into `dj_library_dir` and review tracks into `uncategorizable_dir`. Source files stay in place.

## 7. Export Review CSV

```bash
uv run dj-sort export-uncategorizable "Bulla In Process" --settings settings.yaml
```

Default output:

```text
reports/uncategorizable/Bulla In Process.csv
```

Use this CSV for track-specific decisions. Fill only `update_genre`.

## 8. Apply Track-Level Decisions

Dry-run first:

```bash
uv run dj-sort apply-genre-updates "Bulla In Process" --settings settings.yaml
```

Then write:

```bash
uv run dj-sort apply-genre-updates "Bulla In Process" --settings settings.yaml --write
```

`apply-genre-updates --write` does three things per row:
- Writes the selected genre to the source file metadata.
- Reprocesses that one source file into the library.
- Removes the stale generated `Uncategorizable` copy after the new library copy succeeds.

## 9. Refresh The CSV

```bash
uv run dj-sort export-uncategorizable "Bulla In Process" --settings settings.yaml
```

Then verify no pending CSV updates remain:

```bash
uv run dj-sort apply-genre-updates "Bulla In Process" --settings settings.yaml
```

## 10. Verify Counts

```bash
rg --files "/Volumes/External/DJing/Library" -g "*.mp3" -g "*.m4a" -g "*.flac" -g "*.wav" -g "*.aif" -g "*.aiff" | wc -l
rg --files "/Volumes/External/DJing/Uncategorizable" -g "*.mp3" -g "*.m4a" -g "*.flac" -g "*.wav" -g "*.aif" -g "*.aiff" | wc -l
```

## Safety Rules

- Dry-run first unless the previous output is already understood.
- Source files under `unprocessed_music_dir` are kept in place.
- Do not map generic `Other` globally unless every row with that raw genre really means the same thing.
- Prefer `genres.yaml` for repeatable raw labels.
- Prefer CSV `update_genre` for track-specific decisions.
- WAV metadata write-back is intentionally disabled; WAV rows need a separate manual or conversion workflow.
- If a managed file was previously placed in the wrong library genre, rerunning `transfer --write` can now reprocess it into the new library genre and remove the stale managed copy.
