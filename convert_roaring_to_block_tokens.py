#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple


def encode_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def encode_values(values: Iterable[int]) -> bytes:
    out = bytearray()
    for v in values:
        iv = int(v)
        if iv < 0:
            raise ValueError("raw_id must be non-negative")
        out.extend(encode_varint(iv))
    return bytes(out)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Convert roaring shards to token-block storage (128 by default)."
    )
    p.add_argument(
        "--sources",
        nargs="+",
        required=True,
        help="Input roaring shard DB files",
    )
    p.add_argument(
        "--out-dir",
        required=True,
        help="Output directory for converted shards",
    )
    p.add_argument(
        "--prefix",
        default="imag_roaring_blk128",
        help="Output filename prefix",
    )
    p.add_argument(
        "--block-size",
        type=int,
        default=128,
        help="Tokens per block",
    )
    p.add_argument(
        "--commit-every",
        type=int,
        default=50000,
        help="Commit every N token blocks",
    )
    return p.parse_args()


def setup_dst_schema(dst: sqlite3.Connection) -> None:
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

        CREATE TABLE IF NOT EXISTS token_blocks (
            book_id INTEGER NOT NULL,
            block_start INTEGER NOT NULL,
            block_len INTEGER NOT NULL,
            raw_ids BLOB NOT NULL,
            PRIMARY KEY (book_id, block_start)
        ) WITHOUT ROWID;
        """
    )


def copy_core_tables(dst: sqlite3.Connection, src_path: Path) -> None:
    dst.execute("ATTACH DATABASE ? AS srcdb", (str(src_path),))
    try:
        dst.execute(
            "INSERT INTO unigrams(book_id, cf_id, tf, post) SELECT book_id, cf_id, tf, post FROM srcdb.unigrams"
        )
        dst.execute(
            """
            INSERT INTO words(word, raw_id, cf_id, global_id, docfreq, total_tf, docpost, docpost_is_complement)
            SELECT word, raw_id, cf_id, global_id, docfreq, total_tf, docpost, docpost_is_complement
            FROM srcdb.words
            """
        )
        dst.execute("INSERT INTO urns(book_id) SELECT book_id FROM srcdb.urns")
        dst.execute("INSERT INTO urns_postings(id, post) SELECT id, post FROM srcdb.urns_postings")
        dst.execute("INSERT INTO meta(key, value) SELECT key, value FROM srcdb.meta")
        dst.commit()
    finally:
        dst.execute("DETACH DATABASE srcdb")


def flush_block(
    dst: sqlite3.Connection,
    book_id: int,
    block_start: int,
    raw_ids: List[int],
) -> None:
    if not raw_ids:
        return
    blob = encode_values(raw_ids)
    dst.execute(
        "INSERT INTO token_blocks(book_id, block_start, block_len, raw_ids) VALUES (?, ?, ?, ?)",
        (book_id, block_start, len(raw_ids), blob),
    )


def build_token_blocks(
    dst: sqlite3.Connection,
    src_path: Path,
    block_size: int,
    commit_every: int,
) -> int:
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    built = 0
    try:
        cur = src.execute("SELECT book_id, seq, raw_id FROM tokens ORDER BY book_id, seq")
        current_book = None
        current_block_start = None
        raw_ids: List[int] = []

        for book_id, seq, raw_id in cur:
            block_start = (int(seq) // block_size) * block_size
            if (
                current_book is None
                or int(book_id) != current_book
                or block_start != current_block_start
            ):
                if current_book is not None and current_block_start is not None:
                    flush_block(dst, current_book, current_block_start, raw_ids)
                    built += 1
                    if built % commit_every == 0:
                        dst.commit()
                        print(f"  token_blocks built: {built}")
                current_book = int(book_id)
                current_block_start = block_start
                raw_ids = []
            raw_ids.append(int(raw_id))

        if current_book is not None and current_block_start is not None:
            flush_block(dst, current_book, current_block_start, raw_ids)
            built += 1
            dst.commit()
    finally:
        src.close()
    return built


def convert_one(
    src_path: Path,
    dst_path: Path,
    block_size: int,
    commit_every: int,
) -> None:
    if dst_path.exists():
        dst_path.unlink()

    dst = sqlite3.connect(str(dst_path))
    dst.execute("PRAGMA journal_mode=WAL")
    dst.execute("PRAGMA synchronous=NORMAL")
    # Keep temp objects on disk to avoid RAM spikes on long runs.
    dst.execute("PRAGMA temp_store=FILE")
    # Cache size is in KB when negative; keep this conservative.
    dst.execute("PRAGMA cache_size=-100000")
    dst.execute("PRAGMA mmap_size=134217728")

    try:
        setup_dst_schema(dst)
        print(f"[{src_path.name}] copy core tables ...")
        copy_core_tables(dst, src_path)
        print(f"[{src_path.name}] build token_blocks (block_size={block_size}) ...")
        blocks = build_token_blocks(dst, src_path, block_size, commit_every)
        dst.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('token_storage', 'blocks_v1')"
        )
        dst.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('token_block_size', ?)",
            (str(block_size),),
        )
        dst.execute("ANALYZE")
        dst.commit()
        print(f"[{src_path.name}] done. token_blocks={blocks}")
    finally:
        dst.close()


def main() -> None:
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    sources = [Path(p) for p in args.sources]
    for p in sources:
        if not p.exists():
            raise SystemExit(f"Missing source db: {p}")

    for i, src in enumerate(sources):
        dst = out_dir / f"{args.prefix}_{i:02d}.db"
        print(f"=== convert {src} -> {dst}")
        convert_one(src, dst, args.block_size, args.commit_every)
    print("All conversions completed.")


if __name__ == "__main__":
    main()
