from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from dj_sort.planning import Plan

SCHEMA = """
CREATE TABLE IF NOT EXISTS artist (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE COLLATE NOCASE,
  display_name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS genre (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE COLLATE NOCASE,
  display_name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS processing_label (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE COLLATE NOCASE,
  display_name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS duplicate_group (
  id INTEGER PRIMARY KEY,
  file_hash_without_metadata TEXT NOT NULL,
  rank_version INTEGER NOT NULL DEFAULT 1,
  preferred_song_id INTEGER,
  review_status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS potential_duplicate_group (
  id INTEGER PRIMARY KEY,
  normalized_artist_name TEXT NOT NULL,
  normalized_title TEXT NOT NULL,
  review_status TEXT NOT NULL DEFAULT 'pending',
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(normalized_artist_name, normalized_title)
);

CREATE TABLE IF NOT EXISTS song (
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

CREATE TABLE IF NOT EXISTS song_processing_label (
  song_id INTEGER NOT NULL REFERENCES song(id),
  processing_label_id INTEGER NOT NULL REFERENCES processing_label(id),
  created_at TEXT NOT NULL,
  PRIMARY KEY (song_id, processing_label_id)
);

CREATE TABLE IF NOT EXISTS song_operation_log (
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

CREATE INDEX IF NOT EXISTS idx_song_audio_hash ON song(file_hash_without_metadata);
CREATE UNIQUE INDEX IF NOT EXISTS idx_duplicate_group_audio_hash ON duplicate_group(file_hash_without_metadata);
CREATE INDEX IF NOT EXISTS idx_song_full_hash ON song(file_hash_with_metadata);
CREATE INDEX IF NOT EXISTS idx_song_original_full_hash ON song(original_file_hash_with_metadata);
CREATE INDEX IF NOT EXISTS idx_song_artist_title ON song(display_artist, title);
CREATE INDEX IF NOT EXISTS idx_song_normalized_artist_title ON song(normalized_display_artist, normalized_title);
CREATE INDEX IF NOT EXISTS idx_song_genre ON song(genre_id);
CREATE INDEX IF NOT EXISTS idx_song_potential_duplicate_group ON song(potential_duplicate_group_id);
CREATE INDEX IF NOT EXISTS idx_song_processing_label_label_id ON song_processing_label(processing_label_id);
CREATE INDEX IF NOT EXISTS idx_song_operation_log_song_id ON song_operation_log(song_id);
"""


@dataclass(frozen=True)
class StoredSong:
    id: int
    potential_duplicate_group_id: int | None


@dataclass(frozen=True)
class ExistingSong:
    id: int
    current_path: Path
    status: str


def connect(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)
    connection.commit()


def duplicate_report(connection: sqlite3.Connection) -> dict[str, object]:
    exact = _exact_duplicate_groups(connection)
    potential = _potential_duplicate_groups(connection)
    return {
        "report_type": "duplicates",
        "exact_duplicate_groups": exact,
        "potential_duplicate_groups": potential,
        "summary": {
            "exact_duplicate_groups": len(exact),
            "potential_duplicate_groups": len(potential),
        },
    }


def find_existing_by_source_hash(
    connection: sqlite3.Connection,
    source_path: Path,
    original_hash: str,
) -> ExistingSong | None:
    row = connection.execute(
        """
        SELECT id, current_path, processing_status
        FROM song
        WHERE source_path = ? AND original_file_hash_with_metadata = ?
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        (str(source_path), original_hash),
    ).fetchone()
    if row is None:
        return None
    return ExistingSong(
        id=int(row["id"]),
        current_path=Path(row["current_path"]),
        status=str(row["processing_status"]),
    )


def update_song_source_cleanup(
    connection: sqlite3.Connection,
    song_id: int,
    source_cleanup_status: str,
    source_archive_path: Path | None,
    source_archived_at: str | None,
    source_removed_at: str | None,
) -> None:
    now = utc_now()
    with connection:
        connection.execute(
            """
            UPDATE song
            SET source_cleanup_status = ?, source_archive_path = ?, source_archived_at = ?,
                source_removed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                source_cleanup_status,
                str(source_archive_path) if source_archive_path else None,
                source_archived_at,
                source_removed_at,
                now,
                song_id,
            ),
        )
        _log_operation(
            connection,
            song_id,
            "source_completion",
            None,
            str(source_archive_path) if source_archive_path else None,
            None,
            None,
            None,
            None,
            source_cleanup_status,
            None,
            now,
        )


def save_processed_song(
    connection: sqlite3.Connection,
    plan: Plan,
    original_hash: str,
    final_hash: str | None,
    audio_hash: str | None,
    status: str,
    notes: str | None = None,
    source_cleanup_status: str = "kept",
    source_archive_path: Path | None = None,
    source_archived_at: str | None = None,
    source_removed_at: str | None = None,
) -> StoredSong:
    now = utc_now()
    with connection:
        artist_id = _upsert_named(connection, "artist", plan.normalized_artist, plan.artist, now)
        genre_id = _upsert_named(connection, "genre", plan.canonical_genre.casefold(), plan.canonical_genre, now)
        potential_group_id = _upsert_potential_duplicate_group(connection, plan, now)
        duplicate_group_id = _upsert_duplicate_group(connection, audio_hash, now) if audio_hash else None
        connection.execute(
            """
            INSERT INTO song (
              artist_id, genre_id, title, normalized_title, display_artist, normalized_display_artist,
              display_genre, raw_genre, bpm, raw_musical_key, musical_key, album, album_artist,
              track_number, release_date, duration_ms, bitrate, sample_rate, file_size, extension,
              source_path, current_path, source_removed_at, source_cleanup_status, source_archive_path,
              source_archived_at, original_file_hash_with_metadata, file_hash_with_metadata,
              file_hash_without_metadata, duplicate_group_id, potential_duplicate_group_id, rank_version, processing_status,
              processing_notes, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
            ON CONFLICT(current_path) DO UPDATE SET
              artist_id=excluded.artist_id,
              genre_id=excluded.genre_id,
              title=excluded.title,
              normalized_title=excluded.normalized_title,
              display_artist=excluded.display_artist,
              normalized_display_artist=excluded.normalized_display_artist,
              display_genre=excluded.display_genre,
              raw_genre=excluded.raw_genre,
              bpm=excluded.bpm,
              raw_musical_key=excluded.raw_musical_key,
              musical_key=excluded.musical_key,
              album=excluded.album,
              album_artist=excluded.album_artist,
              track_number=excluded.track_number,
              release_date=excluded.release_date,
              duration_ms=excluded.duration_ms,
              bitrate=excluded.bitrate,
              sample_rate=excluded.sample_rate,
              file_size=excluded.file_size,
              source_path=excluded.source_path,
              source_removed_at=excluded.source_removed_at,
              source_cleanup_status=excluded.source_cleanup_status,
              source_archive_path=excluded.source_archive_path,
              source_archived_at=excluded.source_archived_at,
              original_file_hash_with_metadata=excluded.original_file_hash_with_metadata,
              file_hash_with_metadata=excluded.file_hash_with_metadata,
              file_hash_without_metadata=excluded.file_hash_without_metadata,
              duplicate_group_id=excluded.duplicate_group_id,
              potential_duplicate_group_id=excluded.potential_duplicate_group_id,
              processing_status=excluded.processing_status,
              processing_notes=excluded.processing_notes,
              updated_at=excluded.updated_at
            """,
            (
                artist_id,
                genre_id,
                plan.title,
                plan.normalized_title,
                plan.artist,
                plan.normalized_artist,
                plan.canonical_genre,
                plan.raw_genre,
                plan.bpm,
                plan.raw_key,
                plan.camelot_key,
                plan.album,
                plan.album_artist,
                plan.track_number,
                plan.release_date,
                plan.duration_ms,
                plan.bitrate,
                plan.sample_rate,
                plan.file_size,
                plan.extension,
                str(plan.source_path),
                str(plan.target_path),
                source_removed_at,
                source_cleanup_status,
                str(source_archive_path) if source_archive_path else None,
                source_archived_at,
                original_hash,
                final_hash,
                audio_hash,
                duplicate_group_id,
                potential_group_id,
                status,
                notes,
                now,
                now,
            ),
        )
        row = connection.execute(
            "SELECT id, potential_duplicate_group_id FROM song WHERE current_path = ?",
            (str(plan.target_path),),
        ).fetchone()
        song_id = int(row["id"])
        _replace_labels(connection, song_id, plan.labels, now)
        _refresh_potential_duplicate_labels(connection, int(row["potential_duplicate_group_id"]), now)
        if duplicate_group_id is not None:
            _refresh_exact_duplicate_labels(connection, duplicate_group_id, now)
        _log_operation(
            connection,
            song_id,
            "process",
            str(plan.source_path),
            str(plan.target_path),
            plan.raw_genre,
            plan.canonical_genre,
            original_hash,
            final_hash,
            status,
            notes,
            now,
        )
    return StoredSong(id=song_id, potential_duplicate_group_id=row["potential_duplicate_group_id"])


def update_consolidated_song(
    connection: sqlite3.Connection,
    song_id: int,
    genre_id: int,
    display_genre: str,
    new_path: Path,
    new_hash: str,
    previous_path: Path,
    previous_genre: str | None,
) -> None:
    now = utc_now()
    with connection:
        connection.execute(
            """
            UPDATE song
            SET genre_id = ?, display_genre = ?, current_path = ?, file_hash_with_metadata = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (genre_id, display_genre, str(new_path), new_hash, now, song_id),
        )
        _add_label_to_song(connection, song_id, "Genre Consolidated", now)
        _log_operation(
            connection,
            song_id,
            "genre_consolidation",
            str(previous_path),
            str(new_path),
            previous_genre,
            display_genre,
            None,
            new_hash,
            "processed",
            None,
            now,
        )


def genre_id(connection: sqlite3.Connection, display_genre: str) -> int:
    return _upsert_named(connection, "genre", display_genre.casefold(), display_genre, utc_now())


def songs_for_genre(connection: sqlite3.Connection, source_genre: str) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT * FROM song
        WHERE display_genre = ? COLLATE NOCASE OR raw_genre = ? COLLATE NOCASE
        ORDER BY current_path
        """,
        (source_genre, source_genre),
    ).fetchall()


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _upsert_named(
    connection: sqlite3.Connection,
    table: str,
    name: str,
    display_name: str,
    now: str,
) -> int:
    connection.execute(
        f"""
        INSERT INTO {table} (name, display_name, created_at, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET display_name=excluded.display_name, updated_at=excluded.updated_at
        """,
        (name, display_name, now, now),
    )
    row = connection.execute(f"SELECT id FROM {table} WHERE name = ?", (name,)).fetchone()
    return int(row["id"])


def _upsert_potential_duplicate_group(connection: sqlite3.Connection, plan: Plan, now: str) -> int:
    connection.execute(
        """
        INSERT INTO potential_duplicate_group (
          normalized_artist_name, normalized_title, review_status, created_at, updated_at
        ) VALUES (?, ?, 'pending', ?, ?)
        ON CONFLICT(normalized_artist_name, normalized_title) DO UPDATE SET updated_at=excluded.updated_at
        """,
        (plan.normalized_artist, plan.normalized_title, now, now),
    )
    row = connection.execute(
        """
        SELECT id FROM potential_duplicate_group
        WHERE normalized_artist_name = ? AND normalized_title = ?
        """,
        (plan.normalized_artist, plan.normalized_title),
    ).fetchone()
    return int(row["id"])


def _upsert_duplicate_group(connection: sqlite3.Connection, audio_hash: str, now: str) -> int:
    connection.execute(
        """
        INSERT INTO duplicate_group (file_hash_without_metadata, rank_version, review_status, created_at, updated_at)
        VALUES (?, 1, 'pending', ?, ?)
        ON CONFLICT(file_hash_without_metadata) DO UPDATE SET updated_at=excluded.updated_at
        """,
        (audio_hash, now, now),
    )
    row = connection.execute(
        """
        SELECT id FROM duplicate_group
        WHERE file_hash_without_metadata = ?
        ORDER BY id
        LIMIT 1
        """,
        (audio_hash,),
    ).fetchone()
    return int(row["id"])


def _replace_labels(
    connection: sqlite3.Connection,
    song_id: int,
    labels: tuple[str, ...],
    now: str,
) -> None:
    connection.execute("DELETE FROM song_processing_label WHERE song_id = ?", (song_id,))
    for label in labels:
        label_id = _upsert_named(connection, "processing_label", label.casefold(), label, now)
        connection.execute(
            """
            INSERT OR IGNORE INTO song_processing_label (song_id, processing_label_id, created_at)
            VALUES (?, ?, ?)
            """,
            (song_id, label_id, now),
        )


def _add_label_to_song(
    connection: sqlite3.Connection,
    song_id: int,
    label: str,
    now: str,
) -> None:
    label_id = _upsert_named(connection, "processing_label", label.casefold(), label, now)
    connection.execute(
        """
        INSERT OR IGNORE INTO song_processing_label (song_id, processing_label_id, created_at)
        VALUES (?, ?, ?)
        """,
        (song_id, label_id, now),
    )


def _refresh_potential_duplicate_labels(
    connection: sqlite3.Connection,
    potential_group_id: int,
    now: str,
) -> None:
    rows = connection.execute(
        "SELECT id FROM song WHERE potential_duplicate_group_id = ?",
        (potential_group_id,),
    ).fetchall()
    if len(rows) < 2:
        return
    for row in rows:
        _add_label_to_song(connection, int(row["id"]), "Potential Duplicate", now)


def _refresh_exact_duplicate_labels(
    connection: sqlite3.Connection,
    duplicate_group_id: int,
    now: str,
) -> None:
    rows = connection.execute(
        "SELECT id FROM song WHERE duplicate_group_id = ?",
        (duplicate_group_id,),
    ).fetchall()
    if len(rows) < 2:
        return
    for row in rows:
        _add_label_to_song(connection, int(row["id"]), "Duplicate Candidate", now)


def _exact_duplicate_groups(connection: sqlite3.Connection) -> list[dict[str, object]]:
    groups = []
    rows = connection.execute(
        """
        SELECT file_hash_without_metadata, COUNT(*) AS count
        FROM song
        WHERE file_hash_without_metadata IS NOT NULL
        GROUP BY file_hash_without_metadata
        HAVING COUNT(*) > 1
        ORDER BY count DESC
        """
    ).fetchall()
    for row in rows:
        songs = connection.execute(
            """
            SELECT current_path, display_artist, title, display_genre, bpm, musical_key,
                   extension, file_size, bitrate, duration_ms
            FROM song
            WHERE file_hash_without_metadata = ?
            ORDER BY current_path
            """,
            (row["file_hash_without_metadata"],),
        ).fetchall()
        groups.append(
            {
                "file_hash_without_metadata": row["file_hash_without_metadata"],
                "count": row["count"],
                "songs": [dict(song) for song in songs],
            }
        )
    return groups


def _potential_duplicate_groups(connection: sqlite3.Connection) -> list[dict[str, object]]:
    groups = []
    rows = connection.execute(
        """
        SELECT pdg.id, pdg.normalized_artist_name, pdg.normalized_title, COUNT(song.id) AS count
        FROM potential_duplicate_group pdg
        JOIN song ON song.potential_duplicate_group_id = pdg.id
        GROUP BY pdg.id
        HAVING COUNT(song.id) > 1
        ORDER BY count DESC
        """
    ).fetchall()
    for row in rows:
        songs = connection.execute(
            """
            SELECT current_path, display_artist, title, display_genre, bpm, musical_key,
                   extension, file_size, bitrate, duration_ms
            FROM song
            WHERE potential_duplicate_group_id = ?
            ORDER BY current_path
            """,
            (row["id"],),
        ).fetchall()
        groups.append(
            {
                "normalized_artist_name": row["normalized_artist_name"],
                "normalized_title": row["normalized_title"],
                "count": row["count"],
                "songs": [dict(song) for song in songs],
                "paths": [song["current_path"] for song in songs],
            }
        )
    return groups


def _log_operation(
    connection: sqlite3.Connection,
    song_id: int,
    operation_type: str,
    previous_path: str | None,
    new_path: str | None,
    previous_genre: str | None,
    new_genre: str | None,
    previous_hash: str | None,
    new_hash: str | None,
    status: str,
    notes: str | None,
    now: str,
) -> None:
    connection.execute(
        """
        INSERT INTO song_operation_log (
          song_id, operation_type, previous_path, new_path, previous_genre, new_genre,
          previous_file_hash_with_metadata, new_file_hash_with_metadata, status, notes, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            song_id,
            operation_type,
            previous_path,
            new_path,
            previous_genre,
            new_genre,
            previous_hash,
            new_hash,
            status,
            notes,
            now,
        ),
    )
