#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
import struct
import time
from typing import Dict, Tuple


def word_for_cf(curw: sqlite3.Cursor, cf_id: int) -> str:
    row = curw.execute(
        "SELECT word FROM words WHERE cf_id = ? ORDER BY raw_id LIMIT 1",
        (cf_id,),
    ).fetchone()
    return row[0] if row else ""


def pick_candidate(
    cur: sqlite3.Cursor,
    min_bi: int,
    max_bi: int | None,
    min_uni_min: int,
    max_uni_min: int | None,
    limit: int,
) -> list[Tuple[bytes, int, int, int, int, int]]:
    rows = cur.execute(
        """
        SELECT key, book_id, tf
        FROM bigrams
        WHERE tf >= ?
        ORDER BY RANDOM()
        LIMIT ?
        """,
        (min_bi, limit),
    ).fetchall()

    results = []
    for key, book_id, bi_tf in rows:
        if key is None or len(key) != 8:
            continue
        cf_x, cf_y = struct.unpack("<II", key)
        rowx = cur.execute(
            "SELECT tf FROM unigrams WHERE book_id = ? AND cf_id = ? LIMIT 1",
            (book_id, cf_x),
        ).fetchone()
        rowy = cur.execute(
            "SELECT tf FROM unigrams WHERE book_id = ? AND cf_id = ? LIMIT 1",
            (book_id, cf_y),
        ).fetchone()
        if not rowx or not rowy:
            continue
        uni_min = min(rowx[0], rowy[0])
        if uni_min < min_uni_min:
            continue
        if max_uni_min is not None and uni_min > max_uni_min:
            continue
        if max_bi is not None and bi_tf > max_bi:
            continue
        results.append((key, book_id, cf_x, cf_y, rowx[0], rowy[0]))
        if len(results) >= 1:
            break
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sample bigram vs postings timing by frequency regime."
    )
    parser.add_argument("--db", required=True, help="ngrams/tokens db path")
    parser.add_argument("--words-db", required=True, help="words db path")
    parser.add_argument("--ext", required=True, help="postings extension .so path")
    parser.add_argument("--short", type=int, default=100, help="short threshold")
    parser.add_argument("--long", type=int, default=1000, help="long threshold")
    args = parser.parse_args()

    con = sqlite3.connect(args.db)
    con.enable_load_extension(True)
    con.load_extension(args.ext)
    cur = con.cursor()

    conw = sqlite3.connect(args.words_db)
    curw = conw.cursor()

    scenarios = [
        ("short-short", 1, None, 1, args.short),
        ("long-long", args.long, None, args.long, None),
        ("long-short", args.long, None, 1, args.short),
    ]

    for label, min_bi, max_bi, min_uni_min, max_uni_min in scenarios:
        candidates = pick_candidate(
            cur, min_bi, max_bi, min_uni_min, max_uni_min, limit=500
        )
        if not candidates:
            print(f"{label}: no match")
            continue

        key, book_id, cf_x, cf_y, tf_x, tf_y = candidates[0]
        wx = word_for_cf(curw, cf_x)
        wy = word_for_cf(curw, cf_y)

        # Bigram lookup timing
        start = time.perf_counter()
        row = cur.execute(
            "SELECT post, tf FROM bigrams WHERE book_id = ? AND key = ? LIMIT 1",
            (book_id, key),
        ).fetchone()
        t_bigram = time.perf_counter() - start
        post, bi_tf = row if row else (None, 0)

        # Postings near timing
        start = time.perf_counter()
        hits = cur.execute(
            """
            SELECT post_intersect_offset_sym(a.post, b.post, 1, 1)
            FROM unigrams a
            JOIN unigrams b ON a.book_id = b.book_id
            WHERE a.book_id = ? AND a.cf_id = ? AND b.cf_id = ?
            """,
            (book_id, cf_x, cf_y),
        ).fetchone()[0]
        t_post = time.perf_counter() - start

        # Sample next word after bigram
        next_word = ""
        if post is not None:
            seq = cur.execute("SELECT post_sample(?, 0)", (post,)).fetchone()[0]
            row_next = cur.execute(
                "SELECT raw_id FROM tokens WHERE book_id = ? AND seq = ?",
                (book_id, seq + 2),
            ).fetchone()
            if row_next:
                row_word = curw.execute(
                    "SELECT word FROM words WHERE raw_id = ? LIMIT 1",
                    (row_next[0],),
                ).fetchone()
                next_word = row_word[0] if row_word else ""

        print(
            f"{label}: book={book_id} bigram='{wx} {wy}' next='{next_word}' "
            f"uni_tf=({tf_x},{tf_y}) bi_tf={bi_tf} "
            f"time_bigram={t_bigram:.6f}s time_post={t_post:.6f}s hits={hits}"
        )

    con.close()
    conw.close()


if __name__ == "__main__":
    main()
