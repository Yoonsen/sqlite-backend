#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path
from typing import Iterable, List


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
        description="Build token_blocks sidecar DB(s) from roaring shard tokens."
    )
    p.add_argument("--sources", nargs="+", required=True, help="Input shard DB files")
    p.add_argument("--out-dir", required=True, help="Output directory for sidecar DB files")
    p.add_argument(
        "--out-file",
        default="",
        help="Optional explicit output DB file path (requires exactly one source)",
    )
    p.add_argument(
        "--prefix",
        default="imag_roaring_blk128_sidecar",
        help="Output filename prefix",
    )
    p.add_argument("--block-size", type=int, default=128, help="Tokens per block")
    p.add_argument(
        "--commit-every",
        type=int,
        default=1000,
        help="Commit every N blocks",
    )
    return p.parse_args()


def open_dst(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA temp_store=FILE")
    con.execute("PRAGMA cache_size=-50000")
    con.execute("PRAGMA mmap_size=0")
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS token_blocks (
            book_id INTEGER NOT NULL,
            block_start INTEGER NOT NULL,
            block_len INTEGER NOT NULL,
            raw_ids BLOB NOT NULL,
            PRIMARY KEY (book_id, block_start)
        ) WITHOUT ROWID;

        CREATE TABLE IF NOT EXISTS meta (
            key TEXT NOT NULL PRIMARY KEY,
            value TEXT NOT NULL
        ) WITHOUT ROWID;
        """
    )
    return con


def flush_block(
    dst: sqlite3.Connection,
    book_id: int,
    block_start: int,
    raw_ids: List[int],
) -> None:
    if not raw_ids:
        return
    dst.execute(
        "INSERT INTO token_blocks(book_id, block_start, block_len, raw_ids) VALUES (?, ?, ?, ?)",
        (book_id, block_start, len(raw_ids), encode_values(raw_ids)),
    )


def build_sidecar(src_path: Path, dst_path: Path, block_size: int, commit_every: int) -> None:
    if dst_path.exists():
        dst_path.unlink()
    dst = open_dst(dst_path)
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    try:
        t0 = time.time()
        books = src.execute("SELECT COUNT(*) FROM urns").fetchone()[0]
        tokens = src.execute("SELECT COUNT(*) FROM tokens").fetchone()[0]
        print(
            f"[{src_path.name}] start sidecar build: books={books} tokens={tokens} block_size={block_size}"
        )
        dst.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('source_db', ?)", (str(src_path),)
        )
        dst.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('token_storage', 'blocks_sidecar_v1')"
        )
        dst.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('token_block_size', ?)",
            (str(block_size),),
        )
        dst.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('books', ?)",
            (str(books),),
        )
        dst.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('tokens', ?)",
            (str(tokens),),
        )
        dst.commit()

        cur = src.execute("SELECT book_id, seq, raw_id FROM tokens ORDER BY book_id, seq")
        current_book = None
        current_block_start = None
        raw_ids: List[int] = []
        built = 0

        for book_id, seq, raw_id in cur:
            bstart = (int(seq) // block_size) * block_size
            if (
                current_book is None
                or int(book_id) != current_book
                or bstart != current_block_start
            ):
                if current_book is not None and current_block_start is not None:
                    flush_block(dst, current_book, current_block_start, raw_ids)
                    built += 1
                    if built % commit_every == 0:
                        dst.commit()
                        print(f"[{src_path.name}] token_blocks built: {built}")
                current_book = int(book_id)
                current_block_start = bstart
                raw_ids = []
            raw_ids.append(int(raw_id))

        if current_book is not None and current_block_start is not None:
            flush_block(dst, current_book, current_block_start, raw_ids)
            built += 1

        dst.commit()
        dst.execute("ANALYZE")
        dst.commit()
        elapsed = time.time() - t0
        print(f"[{src_path.name}] done: token_blocks={built} elapsed_s={elapsed:.1f}")
    finally:
        src.close()
        dst.close()


def main() -> None:
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    sources = [Path(p) for p in args.sources]
    for p in sources:
        if not p.exists():
            raise SystemExit(f"Missing source db: {p}")

    if args.out_file:
        if len(sources) != 1:
            raise SystemExit("--out-file can only be used with exactly one source DB.")
        dst = Path(args.out_file)
        print(f"=== build sidecar {sources[0]} -> {dst}")
        build_sidecar(sources[0], dst, args.block_size, args.commit_every)
    else:
        for i, src in enumerate(sources):
            dst = out_dir / f"{args.prefix}_{i:02d}.db"
            print(f"=== build sidecar {src} -> {dst}")
            build_sidecar(src, dst, args.block_size, args.commit_every)
    print("All sidecar builds completed.")


if __name__ == "__main__":
    main()
