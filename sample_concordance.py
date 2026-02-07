#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
import struct
from typing import Iterable, List


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


def pack_key(ids: Iterable[int]) -> bytes:
    return b"".join(struct.pack("<I", i) for i in ids)


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
    parser = argparse.ArgumentParser(description="Sample concordance fragments.")
    parser.add_argument("--db", required=True, help="ngrams/tokens db path")
    parser.add_argument("--words-db", required=True, help="words db path")
    parser.add_argument("--ext", default="", help="postings extension .so path")
    parser.add_argument("--token", default="og", help="token to sample")
    parser.add_argument("--limit", type=int, default=30, help="max fragments")
    parser.add_argument("--per-book", type=int, default=0, help="samples per book")
    parser.add_argument(
        "--use-unigrams",
        action="store_true",
        help="Sample from unigrams table instead of ngrams",
    )
    parser.add_argument("--before", type=int, default=5, help="words before")
    parser.add_argument("--after", type=int, default=5, help="words after")
    args = parser.parse_args()

    cf_id = fetch_cf_id(args.words_db, args.token)
    key = pack_key([cf_id])

    con = sqlite3.connect(args.db)
    if args.ext:
        con.enable_load_extension(True)
        con.load_extension(args.ext)
    cur = con.cursor()
    words_con = sqlite3.connect(args.words_db)

    if args.per_book > 0:
        if not args.ext:
            raise ValueError("--per-book requires --ext to use post_sample")
        values = ",".join(f"({i})" for i in range(args.per_book))
        if args.use_unigrams:
            query = f"""
                WITH nums(n) AS (VALUES {values})
                SELECT unigrams.book_id,
                       post_sample(unigrams.post, abs(random()) % unigrams.tf) AS seq
                FROM unigrams
                JOIN nums
                WHERE unigrams.cf_id = ?
                  AND unigrams.tf > 0
                ORDER BY unigrams.book_id, nums.n
            """
            rows = cur.execute(
                query,
                (cf_id,),
            ).fetchall()
        else:
            query = f"""
                WITH nums(n) AS (VALUES {values})
                SELECT ngrams.book_id,
                       post_sample(ngrams.post, abs(random()) % ngrams.tf) AS seq
                FROM ngrams
                JOIN nums
                WHERE ngrams.key = ?
                  AND ngrams.tf > 0
                ORDER BY ngrams.book_id, nums.n
            """
            rows = cur.execute(
                query,
                (key,),
            ).fetchall()
    else:
        rows = cur.execute(
            """
            SELECT book_id, seq
            FROM tokens
            WHERE cf_id = ?
            ORDER BY book_id, seq
            LIMIT ?
            """,
            (cf_id, args.limit),
        ).fetchall()

    for book_id, seq in rows:
        if seq is None:
            continue
        raw_ids = cur.execute(
            """
            SELECT raw_id
            FROM tokens
            WHERE book_id = ?
              AND seq >= ?
              AND seq <= ?
            ORDER BY seq
            """,
            (book_id, seq - args.before, seq + args.after),
        ).fetchall()
        raw_list = [r[0] for r in raw_ids]
        words = lookup_words(words_con, raw_list)
        fragment = " ".join(w for w in words if w)
        print(f"{book_id}\t{seq}\t{fragment}")

    con.close()
    words_con.close()


if __name__ == "__main__":
    main()
