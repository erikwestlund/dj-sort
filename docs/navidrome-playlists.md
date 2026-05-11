# Navidrome Playlists

This workflow exports DJ library genre folders as Navidrome-compatible `.m3u` playlists.

The main use case is browsing generated genre playlists in Navidrome or Subsonic clients like Amperfy.

## Current Server Layout

Server host:

```text
dj.lan
```

Host music folder:

```text
/srv/dj-library/Library
```

Navidrome container music folder:

```text
/music
```

Generated playlist folder on the host:

```text
/srv/dj-library/Library/_Playlists
```

Generated playlist folder inside Navidrome:

```text
/music/_Playlists
```

Playlist names are prefixed so they appear grouped in Navidrome's flat playlist list:

```text
Uncurated: Tech House
Uncurated: Bass
Uncurated: Future Bass
```

Navidrome does not have a true playlist-folder UI concept. Prefixes are the practical grouping mechanism.

## Navidrome Config Requirement

Navidrome only imports playlists from paths relative to its `MusicFolder`.

The Docker Compose config on `dj.lan` should contain:

```yaml
environment:
  ND_MUSICFOLDER: /music
  ND_PLAYLISTSPATH: _Playlists
volumes:
  - /srv/dj-library/Library:/music:ro
```

Important: do not set `ND_PLAYLISTSPATH` to an absolute path like `/playlists`. Navidrome documents `PlaylistsPath` as relative to `MusicFolder`; an absolute `/playlists` mount did not import generated playlists.

The active Compose file is:

```text
/opt/dj-library/docker-compose.yml
```

A backup was written during setup:

```text
/opt/dj-library/docker-compose.yml.bak
```

After changing Compose config, restart Navidrome:

```bash
ssh erik@dj.lan 'cd /opt/dj-library && sudo docker compose up -d navidrome'
```

## App Settings

The relevant `settings.yaml` section is:

```yaml
navidrome:
  host: dj.lan
  library_root: /music
  playlist_root: /srv/dj-library/Library/_Playlists
  output_dir: ./reports/navidrome-playlists
  include_extm3u_header: true
  playlist_name_prefix: "Uncurated: "
```

Meanings:

- `host`: SSH host label for the server.
- `library_root`: path written inside `.m3u` entries, as Navidrome sees the music folder.
- `playlist_root`: host path where `.m3u` files should live on the server.
- `output_dir`: local workstation output for dry-run/review. Use `--output-dir` to override when writing directly to the server.
- `playlist_name_prefix`: prefix added to playlist filenames and Navidrome playlist names.

## Regenerate Playlists On The Server

Run this after syncing new music to `dj.lan`:

```bash
ssh erik@dj.lan "python3 - <<'PY'
from pathlib import Path

host_music = Path('/srv/dj-library/Library')
playlist_dir = host_music / '_Playlists'
container_music = Path('/music')
prefix = 'Uncurated: '
exts = {'.mp3', '.m4a', '.aac', '.alac', '.flac', '.wav', '.aif', '.aiff'}

playlist_dir.mkdir(parents=True, exist_ok=True)
for old in playlist_dir.glob('*.m3u'):
    old.unlink()

count = 0
tracks_total = 0
for genre_dir in sorted(
    [p for p in host_music.iterdir() if p.is_dir() and p.name != '_Playlists'],
    key=lambda p: p.name.casefold(),
):
    tracks = sorted(
        [p for p in genre_dir.rglob('*') if p.is_file() and p.suffix.casefold() in exts],
        key=lambda p: p.relative_to(genre_dir).as_posix().casefold(),
    )
    if not tracks:
        continue

    playlist = playlist_dir / f'{prefix}{genre_dir.name}.m3u'
    lines = ['#EXTM3U']
    lines.extend((container_music / track.relative_to(host_music)).as_posix() for track in tracks)
    playlist.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    count += 1
    tracks_total += len(tracks)

print(f'Wrote {count} playlists, {tracks_total} tracks total to {playlist_dir}')
PY"
```

Then trigger a Navidrome rescan:

```bash
ssh erik@dj.lan 'docker exec navidrome /app/navidrome scan --full'
```

## App Command

The application also has a playlist export command:

```bash
uv run dj-sort export-navidrome-playlists
```

By default this is a dry-run. To write files from a machine that can see the library path:

```bash
uv run dj-sort export-navidrome-playlists --write
```

For server-local generation, run the command on `dj.lan` after installing or copying the app there, or pass server paths explicitly from an environment that has them mounted:

```bash
uv run dj-sort export-navidrome-playlists \
  --local-library-root /srv/dj-library/Library \
  --server-library-root /music \
  --output-dir /srv/dj-library/Library/_Playlists \
  --write
```

If running from the Mac and `/Volumes/External/DJing/Library` is not mounted, the command will fail cleanly with `Library root does not exist`.

## Verify Import

Check generated files:

```bash
ssh erik@dj.lan 'find /srv/dj-library/Library/_Playlists -maxdepth 1 -name "*.m3u" | wc -l'
```

Check the first lines of a playlist:

```bash
ssh erik@dj.lan 'sed -n "1,5p" "/srv/dj-library/Library/_Playlists/Uncurated: Tech House.m3u"'
```

Expected entries should look like:

```text
#EXTM3U
/music/Tech House/Some Artist - Some Track.mp3
```

Check Navidrome's imported playlist DB state:

```bash
ssh erik@dj.lan "python3 - <<'PY'
import sqlite3
conn = sqlite3.connect('/srv/navidrome/navidrome.db')
conn.row_factory = sqlite3.Row
print('playlist_count', conn.execute('SELECT COUNT(*) AS n FROM playlist').fetchone()['n'])
print('uncurated_count', conn.execute(\"SELECT COUNT(*) AS n FROM playlist WHERE name LIKE 'Uncurated:%'\").fetchone()['n'])
for row in conn.execute(\"SELECT name, song_count FROM playlist WHERE name LIKE 'Uncurated:%' ORDER BY name LIMIT 10\"):
    print(dict(row))
PY"
```

Known good result after setup:

```text
playlist_count: 106
uncurated_count: 106
```

## Clean Up Old Playlist Imports

When changing playlist names, Navidrome may retain previous synced playlist records after the files are removed. To delete only old unprefixed synced playlists from the `_Playlists` folder:

```bash
ssh erik@dj.lan "python3 - <<'PY'
import sqlite3
conn = sqlite3.connect('/srv/navidrome/navidrome.db')
conn.row_factory = sqlite3.Row
rows = conn.execute(\"SELECT id FROM playlist WHERE path LIKE '/music/_Playlists/%' AND name NOT LIKE 'Uncurated:%'\").fetchall()
ids = [row['id'] for row in rows]
print('old_synced_unprefixed', len(ids))
with conn:
    if ids:
        placeholders = ','.join('?' for _ in ids)
        conn.execute(f'DELETE FROM playlist_tracks WHERE playlist_id IN ({placeholders})', ids)
        conn.execute(f'DELETE FROM playlist_fields WHERE playlist_id IN ({placeholders})', ids)
        conn.execute(f'DELETE FROM playlist WHERE id IN ({placeholders})', ids)
PY"
```

Do not run broad playlist deletes unless you are intentionally removing user-created playlists too.

## Current Status

As of the last verified run:

- Navidrome imports the generated `.m3u` files.
- Playlist names use the `Uncurated: ` prefix.
- Navidrome DB has `106` imported `Uncurated:` playlists.
- Playlist track entries use `/music/...` container-visible paths.
