#!/usr/bin/env python3
from __future__ import annotations

import argparse
import random
import sqlite3
import struct
from typing import Iterable, List, Tuple


def decode_varints(blob: bytes) -> List[int]:
    positions: List[int] = []
    acc = 0
    i = 0
    n = len(blob)
    while i < n:
        shift = 0
        value = 0
        while True:
            if i >= n:
                break
            b = blob[i]
            i += 1
            value |= (b & 0x7F) << shift
            if (b & 0x80) == 0:
                break
            shift += 7
        acc += value
        positions.append(acc)
    return positions


def pack_key(ids: Iterable[int]) -> bytes:
    return b"".join(struct.pack("<I", i) for i in ids)


def fetch_cf_id(words_db: str, token: str) -> int:
    con = sqlite3.connect(words_db)
    cur = con.cursor()
    row = cur.execute(
        "SELECT cf_id FROM words WHERE word = ? LIMIT 1", (token,)
    ).fetchone()
    con.close()
    if not row:
        raise ValueError(f"Token not found in words db: {token!r}")
    return int(row[0])


def sample_positions(blob: bytes, count: int, rng: random.Random) -> List[int]:
    positions = decode_varints(blob)
    if not positions:
        return []
    if len(positions) <= count:
        return positions
    return rng.sample(positions, count)


def lookup_words(con: sqlite3.Connection, raw_ids: List[int]) -> List[str]:
    if not raw_ids:
        return []
    cur = con.cursor()
    words: List[str] = []
    for raw_id in raw_ids:
        row = cur.execute(
            "SELECT word FROM words WHERE raw_id = ? LIMIT 1", (raw_id,)
        ).fetchone()
        words.append(row[0] if row else "")
    return words


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sample sentence fragments after '.' from postings db."
    )
    parser.add_argument("--db", required=True, help="ngrams/tokens db path")
    parser.add_argument("--words-db", required=True, help="words db path")
    parser.add_argument("--books", type=int, default=10, help="max books to sample")
    parser.add_argument("--per-book", type=int, default=3, help="samples per book")
    parser.add_argument("--after", type=int, default=5, help="words after '.'")
    parser.add_argument(
        "--use-unigrams",
        action="store_true",
        help="Use unigrams table instead of ngrams",
    )
    parser.add_argument("--seed", type=int, default=13, help="random seed")
    args = parser.parse_args()

    cf_id = fetch_cf_id(args.words_db, ".")
    key = pack_key([cf_id])

    rng = random.Random(args.seed)
    con = sqlite3.connect(args.db)
    cur = con.cursor()
    words_con = sqlite3.connect(args.words_db)

    if args.use_unigrams:
        rows = cur.execute(
            """
            SELECT book_id, post
            FROM unigrams
            WHERE cf_id = ?
            ORDER BY book_id
            """,
            (cf_id,),
        ).fetchmany(args.books)
    else:
        rows = cur.execute(
            """
            SELECT book_id, post
            FROM ngrams
            WHERE key = ?
            ORDER BY book_id
            """,
            (key,),
        ).fetchmany(args.books)

    for book_id, post in rows:
        positions = sample_positions(post, args.per_book, rng)
        for pos in positions:
            raw_ids = cur.execute(
                """
                SELECT raw_id
                FROM tokens
                WHERE book_id = ?
                  AND seq > ?
                  AND seq <= ?
                ORDER BY seq
                """,
                (book_id, pos, pos + args.after),
            ).fetchall()
            raw_list = [r[0] for r in raw_ids]
            words = lookup_words(words_con, raw_list)
            fragment = " ".join(w for w in words if w)
            print(f"{book_id}\t{pos}\t{fragment}")

    con.close()
    words_con.close()


if __name__ == "__main__":
    main()
