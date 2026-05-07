# DJ Sort

Local-first DJ library pre-processor for organizing a source dump into a managed DJ library before importing into Serato, Rekordbox, or similar software.

## Setup

```text
scripts/bootstrap
```

Rebuild the environment from scratch:

```text
rm -rf .venv
scripts/bootstrap
```

The normal workflow uses `uv run ...`, so manual virtualenv activation is not required.

## macOS Notes

This project is developed local-first on macOS. Large mounted music folders may perform better with direct local execution than Docker Desktop volume mounts. Keep personal absolute paths in `settings.yaml`, which is gitignored, and use `settings.example.yaml` as the committed template.

## Useful Commands

```text
uv run dj-sort --help
uv run dj-sort genres discover --settings settings.yaml
uv run dj-sort process --settings settings.yaml --dry-run --limit 25
uv run dj-sort process --settings settings.yaml --write --limit 25
uv run dj-sort diagnostics --settings settings.yaml
```

See `docs/spec.md` and `docs/checklist.md` for architecture and implementation status.
