#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path
from typing import List


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Extract main DB (without tokens) from roaring shard DB(s)."
    )
    p.add_argument("--sources", nargs="+", required=True, help="Input roaring shard DBs")
    p.add_argument("--out-dir", required=True, help="Output directory for main DBs")
    p.add_argument("--prefix", default="imag_roaring_main", help="Output filename prefix")
    p.add_argument(
        "--out-file",
        default="",
        help="Optional explicit output DB path (requires exactly one source)",
    )
    return p.parse_args()


def ensure_schema(dst: sqlite3.Connection) -> None:
    dst.executescript(
        """
        CREATE TABLE IF NOT EXISTS unigrams (
            book_id INTEGER NOT NULL,
            cf_id INTEGER NOT NULL,
            tf INTEGER NOT NULL,
            post BLOB NOT NULL,
            PRIMARY KEY (book_id, cf_id)
        ) WITHOUT ROWID;

        CREATE TABLE IF NOT EXISTS words (
            word TEXT NOT NULL PRIMARY KEY,
            raw_id INTEGER NOT NULL UNIQUE,
            cf_id INTEGER NOT NULL,
            global_id INTEGER,
            docfreq INTEGER DEFAULT 0,
            total_tf INTEGER DEFAULT 0,
            docpost BLOB,
            docpost_is_complement INTEGER DEFAULT 0
        ) WITHOUT ROWID;

        CREATE INDEX IF NOT EXISTS words_cf_id ON words(cf_id);
        CREATE INDEX IF NOT EXISTS words_global_id ON words(global_id);

        CREATE TABLE IF NOT EXISTS urns (
            book_id INTEGER NOT NULL PRIMARY KEY
        ) WITHOUT ROWID;

        CREATE TABLE IF NOT EXISTS urns_postings (
            id INTEGER NOT NULL PRIMARY KEY,
            post BLOB NOT NULL
        ) WITHOUT ROWID;

        CREATE TABLE IF NOT EXISTS meta (
            key TEXT NOT NULL PRIMARY KEY,
            value TEXT NOT NULL
        ) WITHOUT ROWID;
        """
    )


def extract_one(src_path: Path, dst_path: Path) -> None:
    if dst_path.exists():
        dst_path.unlink()

    dst = sqlite3.connect(str(dst_path))
    try:
        dst.execute("PRAGMA journal_mode=WAL")
        dst.execute("PRAGMA synchronous=NORMAL")
        dst.execute("PRAGMA temp_store=FILE")
        dst.execute("PRAGMA cache_size=-100000")
        dst.execute("PRAGMA mmap_size=0")
        ensure_schema(dst)

        t0 = time.time()
        dst.execute("ATTACH DATABASE ? AS srcdb", (str(src_path),))
        try:
            print(f"[{src_path.name}] copy urns ...")
            dst.execute("INSERT INTO urns(book_id) SELECT book_id FROM srcdb.urns")
            dst.commit()

            print(f"[{src_path.name}] copy urns_postings ...")
            dst.execute("INSERT INTO urns_postings(id, post) SELECT id, post FROM srcdb.urns_postings")
            dst.commit()

            print(f"[{src_path.name}] copy words ...")
            dst.execute(
                """
                INSERT INTO words(word, raw_id, cf_id, global_id, docfreq, total_tf, docpost, docpost_is_complement)
                SELECT word, raw_id, cf_id, global_id, docfreq, total_tf, docpost, docpost_is_complement
                FROM srcdb.words
                """
            )
            dst.commit()

            print(f"[{src_path.name}] copy unigrams ...")
            dst.execute("INSERT INTO unigrams(book_id, cf_id, tf, post) SELECT book_id, cf_id, tf, post FROM srcdb.unigrams")
            dst.commit()

            print(f"[{src_path.name}] copy meta ...")
            dst.execute("INSERT OR REPLACE INTO meta(key, value) SELECT key, value FROM srcdb.meta")
            dst.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('layout', 'main_only_v1')")
            dst.commit()
        finally:
            dst.execute("DETACH DATABASE srcdb")

        dst.execute("ANALYZE")
        dst.commit()
        elapsed = time.time() - t0
        print(f"[{src_path.name}] done in {elapsed:.1f}s -> {dst_path}")
    finally:
        dst.close()


def main() -> None:
    args = parse_args()
    sources = [Path(p) for p in args.sources]
    for p in sources:
        if not p.exists():
            raise SystemExit(f"Missing source DB: {p}")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.out_file:
        if len(sources) != 1:
            raise SystemExit("--out-file requires exactly one source.")
        extract_one(sources[0], Path(args.out_file))
        return

    for i, src in enumerate(sources):
        dst = out_dir / f"{args.prefix}_{i}.db"
        extract_one(src, dst)


if __name__ == "__main__":
    main()
