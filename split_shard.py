#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

import convert_ft_to_ngrams as conv


def get_book_counts(src: sqlite3.Connection) -> List[Tuple[int, int]]:
    cur = src.execute(
        "SELECT book_id, SUM(tf) AS tokens FROM unigrams GROUP BY book_id ORDER BY book_id"
    )
    return [(int(book_id), int(tokens or 0)) for book_id, tokens in cur.fetchall()]


def chunk_books(
    counts: List[Tuple[int, int]],
    max_books: Optional[int],
    max_tokens: Optional[int],
) -> List[List[int]]:
    chunks: List[List[int]] = []
    current: List[int] = []
    current_tokens = 0
    for book_id, tokens in counts:
        if max_books and len(current) >= max_books:
            chunks.append(current)
            current = []
            current_tokens = 0
        if max_tokens and current and current_tokens + tokens > max_tokens:
            chunks.append(current)
            current = []
            current_tokens = 0
        current.append(book_id)
        current_tokens += tokens
    if current:
        chunks.append(current)
    return chunks


def insert_book_filter(dst: sqlite3.Connection, book_ids: Iterable[int]) -> None:
    dst.execute("DROP TABLE IF EXISTS book_filter;")
    dst.execute("CREATE TEMP TABLE book_filter (book_id INTEGER PRIMARY KEY) WITHOUT ROWID;")
    dst.executemany(
        "INSERT INTO book_filter(book_id) VALUES (?)", [(int(b),) for b in book_ids]
    )


def copy_subset(
    src_path: str,
    dst_path: str,
    book_ids: List[int],
    copy_bigrams: bool,
) -> None:
    dst = sqlite3.connect(dst_path)
    conv.apply_build_pragmas(dst)
    conv.ensure_dst_schema(dst, split_ngrams=True)
    dst.execute("ATTACH DATABASE ? AS srcdb", (src_path,))

    insert_book_filter(dst, book_ids)

    dst.execute("BEGIN;")
    dst.execute(
        """
        INSERT INTO tokens(book_id, seq, cf_id, raw_id, para, page)
        SELECT t.book_id, t.seq, t.cf_id, t.raw_id, t.para, t.page
        FROM srcdb.tokens t
        JOIN book_filter f ON f.book_id = t.book_id
        """
    )
    dst.execute(
        """
        INSERT INTO unigrams(cf_id, book_id, tf, post)
        SELECT u.cf_id, u.book_id, u.tf, u.post
        FROM srcdb.unigrams u
        JOIN book_filter f ON f.book_id = u.book_id
        """
    )
    if copy_bigrams:
        dst.execute(
            """
            INSERT INTO bigrams(key, book_id, tf, post)
            SELECT b.key, b.book_id, b.tf, b.post
            FROM srcdb.bigrams b
            JOIN book_filter f ON f.book_id = b.book_id
            """
        )
    dst.execute(
        """
        INSERT INTO urns(book_id)
        SELECT f.book_id FROM book_filter f
        """
    )
    dst.commit()
    dst.execute("DETACH DATABASE srcdb")
    dst.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Split a postings shard into smaller DBs.")
    parser.add_argument("--src", required=True, help="Source shard db path")
    parser.add_argument("--dst-prefix", required=True, help="Output prefix (suffix _NNN.db added)")
    parser.add_argument("--max-books", type=int, default=0, help="Max books per shard")
    parser.add_argument("--max-tokens", type=int, default=0, help="Max tokens per shard")
    parser.add_argument("--copy-bigrams", action="store_true", help="Copy bigrams table")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    src_path = args.src
    if not Path(src_path).exists():
        raise SystemExit(f"Source db not found: {src_path}")

    max_books = args.max_books if args.max_books > 0 else None
    max_tokens = args.max_tokens if args.max_tokens > 0 else None
    if max_books is None and max_tokens is None:
        raise SystemExit("Provide --max-books or --max-tokens")

    src = sqlite3.connect(src_path)
    counts = get_book_counts(src)
    src.close()
    chunks = chunk_books(counts, max_books, max_tokens)

    for idx, book_ids in enumerate(chunks, start=1):
        dst_path = f"{args.dst_prefix}_{idx:03d}.db"
        print(f"writing {dst_path} ({len(book_ids)} books)")
        copy_subset(src_path, dst_path, book_ids, args.copy_bigrams)


if __name__ == "__main__":
    main()
