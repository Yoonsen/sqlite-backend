#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Iterable, List, Tuple

import convert_ft_to_ngrams as conv


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


def encode_deltas(values: Iterable[int]) -> bytes:
    out = bytearray()
    prev = 0
    for v in values:
        delta = v - prev
        prev = v
        out.extend(encode_varint(delta))
    return bytes(out)


def complement_list(all_ids: List[int], present: List[int]) -> List[int]:
    out: List[int] = []
    i = 0
    j = 0
    while i < len(all_ids) and j < len(present):
        a = all_ids[i]
        b = present[j]
        if a == b:
            i += 1
            j += 1
        elif a < b:
            out.append(a)
            i += 1
        else:
            j += 1
    if i < len(all_ids):
        out.extend(all_ids[i:])
    return out


def ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS tokens (
            book_id INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            cf_id INTEGER NOT NULL,
            raw_id INTEGER NOT NULL,
            para INTEGER,
            page INTEGER,
            PRIMARY KEY (book_id, seq)
        ) WITHOUT ROWID;

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
            docfreq INTEGER DEFAULT 0,
            total_tf INTEGER DEFAULT 0,
            docpost BLOB,
            docpost_is_complement INTEGER DEFAULT 0
        ) WITHOUT ROWID;

        CREATE INDEX IF NOT EXISTS words_cf_id ON words(cf_id);

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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build words+unigrams index db from a shard."
    )
    parser.add_argument("--src", required=True, help="Source shard DB path")
    parser.add_argument("--dst", required=True, help="Output DB path")
    parser.add_argument(
        "--complement-threshold",
        type=float,
        default=0.5,
        help="Use complement postings when df/total_docs exceeds this ratio",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    src_path = Path(args.src)
    dst_path = Path(args.dst)
    if not src_path.exists():
        raise SystemExit(f"Missing src DB: {src_path}")

    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    dst = sqlite3.connect(dst_path)
    conv.apply_build_pragmas(dst)
    ensure_schema(dst)

    dst.execute("ATTACH DATABASE ? AS src", (str(src_path),))
    dst.execute("INSERT OR IGNORE INTO urns SELECT book_id FROM src.urns")
    dst.execute(
        """
        INSERT OR IGNORE INTO words(word, raw_id, cf_id)
        SELECT word, raw_id, cf_id FROM src.words
        """
    )
    dst.execute(
        """
        INSERT OR IGNORE INTO tokens(book_id, seq, cf_id, raw_id, para, page)
        SELECT book_id, seq, cf_id, raw_id, para, page FROM src.tokens
        """
    )
    dst.execute(
        """
        INSERT OR IGNORE INTO unigrams(book_id, cf_id, tf, post)
        SELECT book_id, cf_id, tf, post FROM src.unigrams
        """
    )
    dst.commit()
    dst.execute("DETACH DATABASE src")

    all_books = [row[0] for row in src.execute("SELECT book_id FROM urns ORDER BY book_id")]
    total_docs = len(all_books)
    dst.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", ("total_docs", str(total_docs)))
    dst.execute(
        "INSERT OR REPLACE INTO urns_postings(id, post) VALUES (?, ?)",
        (1, encode_deltas(all_books)),
    )
    dst.commit()

    update_sql = """
        UPDATE words
        SET docfreq = ?, total_tf = ?, docpost = ?, docpost_is_complement = ?
        WHERE cf_id = ?
    """

    cur = src.execute(
        "SELECT cf_id, book_id, tf FROM unigrams ORDER BY cf_id, book_id"
    )
    current_cf = None
    book_ids: List[int] = []
    total_tf = 0

    def flush(cf_id: int, ids: List[int], tf_sum: int) -> None:
        if cf_id is None:
            return
        df = len(ids)
        if total_docs > 0 and df / total_docs > args.complement_threshold:
            docpost_ids = complement_list(all_books, ids)
            is_complement = 1
        else:
            docpost_ids = ids
            is_complement = 0
        blob = encode_deltas(docpost_ids)
        dst.execute(update_sql, (df, tf_sum, blob, is_complement, cf_id))

    for cf_id, book_id, tf in cur:
        if current_cf is None:
            current_cf = cf_id
        if cf_id != current_cf:
            flush(current_cf, book_ids, total_tf)
            book_ids = []
            total_tf = 0
            current_cf = cf_id
        book_ids.append(book_id)
        total_tf += int(tf)

    if current_cf is not None:
        flush(current_cf, book_ids, total_tf)

    dst.commit()
    src.close()
    dst.close()


if __name__ == "__main__":
    main()
