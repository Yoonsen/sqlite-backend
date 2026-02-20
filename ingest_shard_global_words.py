#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Ingest shard words into global lexicon and write global IDs back to shard."
    )
    p.add_argument("--global-db", required=True, help="Path to global words DB")
    p.add_argument("--shard-db", required=True, help="Path to shard main DB")
    p.add_argument(
        "--shard-id",
        default="",
        help="Optional shard identifier (defaults to shard filename stem)",
    )
    return p.parse_args()


def ensure_global_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS global_cf_lexicon (
            global_cf_id INTEGER PRIMARY KEY,
            cf_word TEXT NOT NULL UNIQUE,
            shard_count INTEGER NOT NULL DEFAULT 0,
            sum_docfreq INTEGER NOT NULL DEFAULT 0,
            sum_total_tf INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS global_raw_lexicon (
            global_raw_id INTEGER PRIMARY KEY,
            word TEXT NOT NULL UNIQUE,
            global_cf_id INTEGER NOT NULL,
            shard_count INTEGER NOT NULL DEFAULT 0,
            sum_docfreq INTEGER NOT NULL DEFAULT 0,
            sum_total_tf INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(global_cf_id) REFERENCES global_cf_lexicon(global_cf_id)
        );

        CREATE INDEX IF NOT EXISTS global_raw_lexicon_cf_id_idx
            ON global_raw_lexicon(global_cf_id);

        CREATE TABLE IF NOT EXISTS shard_ingest_log (
            shard_id TEXT NOT NULL PRIMARY KEY,
            shard_path TEXT NOT NULL,
            ingested_at TEXT NOT NULL,
            rows_total INTEGER NOT NULL,
            rows_with_global INTEGER NOT NULL
        ) WITHOUT ROWID;

        CREATE TABLE IF NOT EXISTS shard_word_stats (
            shard_id TEXT NOT NULL,
            global_raw_id INTEGER NOT NULL,
            docfreq INTEGER NOT NULL,
            total_tf INTEGER NOT NULL,
            PRIMARY KEY (shard_id, global_raw_id)
        ) WITHOUT ROWID;

        CREATE INDEX IF NOT EXISTS shard_word_stats_raw_idx
            ON shard_word_stats(global_raw_id);

        CREATE TABLE IF NOT EXISTS meta (
            key TEXT NOT NULL PRIMARY KEY,
            value TEXT NOT NULL
        ) WITHOUT ROWID;
        """
    )


def ensure_global_columns(con: sqlite3.Connection) -> None:
    def add_col_if_missing(table: str, col: str, decl: str) -> None:
        cols = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    add_col_if_missing("global_cf_lexicon", "shard_count", "INTEGER NOT NULL DEFAULT 0")
    add_col_if_missing("global_cf_lexicon", "sum_docfreq", "INTEGER NOT NULL DEFAULT 0")
    add_col_if_missing("global_cf_lexicon", "sum_total_tf", "INTEGER NOT NULL DEFAULT 0")
    add_col_if_missing("global_raw_lexicon", "shard_count", "INTEGER NOT NULL DEFAULT 0")
    add_col_if_missing("global_raw_lexicon", "sum_docfreq", "INTEGER NOT NULL DEFAULT 0")
    add_col_if_missing("global_raw_lexicon", "sum_total_tf", "INTEGER NOT NULL DEFAULT 0")
    con.commit()


def ensure_shard_columns(con: sqlite3.Connection) -> None:
    cols = {row[1] for row in con.execute("PRAGMA table_info(words)")}
    if "global_id" not in cols:
        con.execute("ALTER TABLE words ADD COLUMN global_id INTEGER")
    if "global_cf_id" not in cols:
        con.execute("ALTER TABLE words ADD COLUMN global_cf_id INTEGER")
    if "global_raw_id" not in cols:
        con.execute("ALTER TABLE words ADD COLUMN global_raw_id INTEGER")
    con.commit()


def ingest(global_db: Path, shard_db: Path, shard_id: str) -> None:
    t0 = time.time()
    con = sqlite3.connect(str(global_db))
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA temp_store=FILE")
        con.execute("PRAGMA cache_size=-80000")
        con.execute("PRAGMA mmap_size=0")
        ensure_global_schema(con)
        ensure_global_columns(con)

        con.execute("ATTACH DATABASE ? AS shard", (str(shard_db),))
        try:
            ensure_shard_columns(con)

            # Ensure we have one canonical case-folded entry per lowercase form.
            con.execute(
                """
                INSERT OR IGNORE INTO global_cf_lexicon(cf_word)
                SELECT DISTINCT lower(word)
                FROM shard.words
                """
            )
            con.commit()

            # Ensure every raw form exists and points to a global_cf_id.
            con.execute(
                """
                INSERT OR IGNORE INTO global_raw_lexicon(word, global_cf_id)
                SELECT w.word, c.global_cf_id
                FROM shard.words w
                JOIN global_cf_lexicon c
                  ON c.cf_word = lower(w.word)
                """
            )
            con.commit()

            # Write global IDs back into the shard words table.
            con.execute(
                """
                UPDATE shard.words
                SET global_cf_id = (
                        SELECT c.global_cf_id
                        FROM global_cf_lexicon c
                        WHERE c.cf_word = lower(shard.words.word)
                    ),
                    global_raw_id = (
                        SELECT r.global_raw_id
                        FROM global_raw_lexicon r
                        WHERE r.word = shard.words.word
                    ),
                    global_id = (
                        SELECT c.global_cf_id
                        FROM global_cf_lexicon c
                        WHERE c.cf_word = lower(shard.words.word)
                    )
                """
            )
            con.commit()

            # Refresh per-shard frequency contribution (idempotent on reruns).
            con.execute("DELETE FROM shard_word_stats WHERE shard_id = ?", (shard_id,))
            con.execute(
                """
                INSERT INTO shard_word_stats(shard_id, global_raw_id, docfreq, total_tf)
                SELECT ?, global_raw_id, COALESCE(docfreq, 0), COALESCE(total_tf, 0)
                FROM shard.words
                WHERE global_raw_id IS NOT NULL
                """,
                (shard_id,),
            )
            con.commit()

            # Recompute global raw aggregates from all shard contributions.
            con.execute(
                """
                UPDATE global_raw_lexicon
                SET shard_count = COALESCE((
                        SELECT COUNT(*)
                        FROM shard_word_stats s
                        WHERE s.global_raw_id = global_raw_lexicon.global_raw_id
                    ), 0),
                    sum_docfreq = COALESCE((
                        SELECT SUM(s.docfreq)
                        FROM shard_word_stats s
                        WHERE s.global_raw_id = global_raw_lexicon.global_raw_id
                    ), 0),
                    sum_total_tf = COALESCE((
                        SELECT SUM(s.total_tf)
                        FROM shard_word_stats s
                        WHERE s.global_raw_id = global_raw_lexicon.global_raw_id
                    ), 0)
                """
            )
            con.commit()

            # Recompute global casefold aggregates by folding raw rows.
            con.execute(
                """
                UPDATE global_cf_lexicon
                SET shard_count = COALESCE((
                        SELECT COUNT(DISTINCT s.shard_id)
                        FROM global_raw_lexicon r
                        JOIN shard_word_stats s ON s.global_raw_id = r.global_raw_id
                        WHERE r.global_cf_id = global_cf_lexicon.global_cf_id
                    ), 0),
                    sum_docfreq = COALESCE((
                        SELECT SUM(s.docfreq)
                        FROM global_raw_lexicon r
                        JOIN shard_word_stats s ON s.global_raw_id = r.global_raw_id
                        WHERE r.global_cf_id = global_cf_lexicon.global_cf_id
                    ), 0),
                    sum_total_tf = COALESCE((
                        SELECT SUM(s.total_tf)
                        FROM global_raw_lexicon r
                        JOIN shard_word_stats s ON s.global_raw_id = r.global_raw_id
                        WHERE r.global_cf_id = global_cf_lexicon.global_cf_id
                    ), 0)
                """
            )
            con.commit()

            rows_total = con.execute("SELECT COUNT(*) FROM shard.words").fetchone()[0]
            rows_with_global = con.execute(
                "SELECT COUNT(*) FROM shard.words WHERE global_id IS NOT NULL"
            ).fetchone()[0]

            con.execute(
                """
                INSERT OR REPLACE INTO shard_ingest_log(
                    shard_id, shard_path, ingested_at, rows_total, rows_with_global
                ) VALUES (?, ?, datetime('now'), ?, ?)
                """,
                (shard_id, str(shard_db), int(rows_total), int(rows_with_global)),
            )
            con.executemany(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                [
                    ("model", "global_words_ingest_v2"),
                    ("last_shard_id", shard_id),
                    ("last_shard_path", str(shard_db)),
                ],
            )
            con.commit()
        finally:
            con.execute("DETACH DATABASE shard")

        cf_count = con.execute("SELECT COUNT(*) FROM global_cf_lexicon").fetchone()[0]
        raw_count = con.execute("SELECT COUNT(*) FROM global_raw_lexicon").fetchone()[0]
        elapsed = time.time() - t0
        print(
            f"Done shard={shard_id} total_words={rows_total} mapped={rows_with_global} "
            f"global_cf={cf_count} global_raw={raw_count} elapsed={elapsed:.1f}s"
        )
    finally:
        con.close()


def main() -> None:
    args = parse_args()
    global_db = Path(args.global_db)
    shard_db = Path(args.shard_db)
    if not shard_db.exists():
        raise SystemExit(f"Missing shard DB: {shard_db}")
    global_db.parent.mkdir(parents=True, exist_ok=True)

    shard_id = args.shard_id or shard_db.stem
    ingest(global_db=global_db, shard_db=shard_db, shard_id=shard_id)


if __name__ == "__main__":
    main()
