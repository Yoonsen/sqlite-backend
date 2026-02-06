#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import struct
from typing import Iterable, List


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
        description="Sample fragments where word A is near word B."
    )
    parser.add_argument("--db", required=True, help="ngrams/tokens db path")
    parser.add_argument("--words-db", required=True, help="words db path")
    parser.add_argument("--ext", required=True, help="postings extension .so path")
    parser.add_argument("--word-a", default="og", help="anchor word (default: og)")
    parser.add_argument("--word-b", default="i", help="near word (default: i)")
    parser.add_argument("--window", type=int, default=10, help="distance window")
    parser.add_argument("--per-book", type=int, default=3, help="max fragments per book")
    parser.add_argument("--before", type=int, default=5, help="words before anchor")
    parser.add_argument("--after", type=int, default=5, help="words after anchor")
    args = parser.parse_args()

    cf_a = fetch_cf_id(args.words_db, args.word_a)
    cf_b = fetch_cf_id(args.words_db, args.word_b)
    key_a = pack_key([cf_a])
    key_b = pack_key([cf_b])

    con = sqlite3.connect(args.db)
    con.enable_load_extension(True)
    con.load_extension(args.ext)
    cur = con.cursor()
    words_con = sqlite3.connect(args.words_db)

    rows = cur.execute(
        """
        SELECT a.book_id,
               post_near_positions(a.post, b.post, ?, ?) AS positions
        FROM ngrams a
        JOIN ngrams b ON a.book_id = b.book_id
        WHERE a.key = ?
          AND b.key = ?
          AND positions != '[]'
        ORDER BY a.book_id
        """,
        (-args.window, args.window, key_a, key_b),
    )

    for book_id, positions_json in rows:
        try:
            positions = json.loads(positions_json)
        except Exception:
            positions = []
        if not positions:
            continue
        used = 0
        for pos in positions:
            raw_ids = cur.execute(
                """
                SELECT raw_id
                FROM tokens
                WHERE book_id = ?
                  AND seq >= ?
                  AND seq <= ?
                ORDER BY seq
                """,
                (book_id, pos - args.before, pos + args.after),
            ).fetchall()
            raw_list = [r[0] for r in raw_ids]
            words = lookup_words(words_con, raw_list)
            fragment = " ".join(w for w in words if w)
            print(f"{book_id}\t{pos}\t{fragment}")
            used += 1
            if used >= args.per_book:
                break

    con.close()
    words_con.close()


if __name__ == "__main__":
    main()
