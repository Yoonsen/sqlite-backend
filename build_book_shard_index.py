#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Iterable, List, Optional, Tuple


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Build global book->shard index from shard urn lists."
    )
    p.add_argument(
        "--config",
        required=True,
        help="Config JSON with postings_dbs.",
    )
    p.add_argument(
        "--out-db",
        required=True,
        help="Output SQLite DB for global dispatcher tables.",
    )
    p.add_argument(
        "--if-exists",
        default="replace",
        choices=["replace", "append"],
        help="replace (default) recreates tables, append keeps existing rows.",
    )
    return p.parse_args()


def find_table(con: sqlite3.Connection, candidates: Iterable[str]) -> Optional[str]:
    cur = con.cursor()
    for name in candidates:
        row = cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        if row:
            return str(row[0])
    return None


def read_book_ids(shard_path: Path) -> List[int]:
    con = sqlite3.connect(f"file:{shard_path}?mode=ro", uri=True)
    try:
        table = find_table(con, ["urns", "tokens", "unigrams", "ngrams"])
        if table is None:
            raise RuntimeError("No urns/tokens/unigrams/ngrams table found.")
        if table == "urns":
            rows = con.execute("SELECT book_id FROM urns ORDER BY book_id").fetchall()
        elif table == "tokens":
            rows = con.execute(
                "SELECT DISTINCT book_id FROM tokens ORDER BY book_id"
            ).fetchall()
        else:
            rows = con.execute(
                f"SELECT DISTINCT book_id FROM {table} ORDER BY book_id"
            ).fetchall()
        return [int(r[0]) for r in rows]
    finally:
        con.close()


def ensure_schema(con: sqlite3.Connection, replace: bool) -> None:
    if replace:
        con.executescript(
            """
            DROP TABLE IF EXISTS book_shard;
            DROP TABLE IF EXISTS shards;
            DROP TABLE IF EXISTS build_meta;
            """
        )
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS shards (
            shard_id INTEGER NOT NULL PRIMARY KEY,
            shard_path TEXT NOT NULL UNIQUE,
            book_count INTEGER NOT NULL
        ) WITHOUT ROWID;

        CREATE TABLE IF NOT EXISTS book_shard (
            book_id INTEGER NOT NULL,
            shard_id INTEGER NOT NULL,
            PRIMARY KEY (book_id, shard_id)
        ) WITHOUT ROWID;

        CREATE INDEX IF NOT EXISTS book_shard_shard_idx
            ON book_shard(shard_id, book_id);

        CREATE TABLE IF NOT EXISTS build_meta (
            key TEXT NOT NULL PRIMARY KEY,
            value TEXT NOT NULL
        ) WITHOUT ROWID;
        """
    )


def main() -> int:
    args = parse_args()
    cfg_path = Path(args.config)
    out_db = Path(args.out_db)
    if not cfg_path.exists():
        raise SystemExit(f"Missing config: {cfg_path}")

    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    shard_paths = [Path(p) for p in cfg.get("postings_dbs", [])]
    if not shard_paths:
        raise SystemExit("No postings_dbs found in config.")

    for p in shard_paths:
        if not p.exists():
            raise SystemExit(f"Missing shard DB: {p}")

    out_db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(out_db))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    ensure_schema(con, replace=(args.if_exists == "replace"))

    rows_book_shard: List[Tuple[int, int]] = []
    rows_shards: List[Tuple[int, str, int]] = []
    for shard_id, shard_path in enumerate(shard_paths):
        book_ids = read_book_ids(shard_path)
        rows_shards.append((shard_id, str(shard_path), len(book_ids)))
        rows_book_shard.extend((book_id, shard_id) for book_id in book_ids)
        print(f"[shard {shard_id}] books={len(book_ids)} path={shard_path}")

    if args.if_exists == "append":
        con.executemany(
            "INSERT OR IGNORE INTO shards(shard_id, shard_path, book_count) VALUES (?, ?, ?)",
            rows_shards,
        )
        con.executemany(
            "INSERT OR IGNORE INTO book_shard(book_id, shard_id) VALUES (?, ?)",
            rows_book_shard,
        )
    else:
        con.executemany(
            "INSERT INTO shards(shard_id, shard_path, book_count) VALUES (?, ?, ?)",
            rows_shards,
        )
        con.executemany(
            "INSERT INTO book_shard(book_id, shard_id) VALUES (?, ?)",
            rows_book_shard,
        )

    overlap_count = con.execute(
        """
        SELECT COUNT(*)
        FROM (
            SELECT book_id
            FROM book_shard
            GROUP BY book_id
            HAVING COUNT(*) > 1
        )
        """
    ).fetchone()[0]

    total_pairs = con.execute("SELECT COUNT(*) FROM book_shard").fetchone()[0]
    total_books = con.execute("SELECT COUNT(DISTINCT book_id) FROM book_shard").fetchone()[0]

    con.executemany(
        "INSERT OR REPLACE INTO build_meta(key, value) VALUES (?, ?)",
        [
            ("source_config", str(cfg_path)),
            ("shard_count", str(len(shard_paths))),
            ("book_shard_rows", str(total_pairs)),
            ("distinct_books", str(total_books)),
            ("overlap_books", str(int(overlap_count))),
        ],
    )
    con.commit()
    con.close()

    print("")
    print(f"out_db={out_db}")
    print(f"book_shard rows={total_pairs}")
    print(f"distinct books={total_books}")
    print(f"books present in >1 shard={int(overlap_count)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
