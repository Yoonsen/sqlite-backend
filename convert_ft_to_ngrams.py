#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
import struct
from typing import Dict, Iterable, List, Optional, Tuple


def varint_encode(n: int) -> bytes:
    if n < 0:
        raise ValueError("varint_encode expects non-negative integers")
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            break
    return bytes(out)


def pack_key(ids: Iterable[int]) -> bytes:
    return b"".join(struct.pack("<I", i) for i in ids)


def encode_deltas(positions: List[int]) -> bytes:
    last = 0
    out = bytearray()
    for pos in positions:
        delta = pos - last
        if delta < 0:
            raise ValueError("positions must be non-decreasing")
        out.extend(varint_encode(delta))
        last = pos
    return bytes(out)


def connect_ro(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def apply_build_pragmas(con: sqlite3.Connection) -> None:
    con.execute("PRAGMA journal_mode = OFF;")
    con.execute("PRAGMA synchronous = OFF;")
    con.execute("PRAGMA temp_store = MEMORY;")
    con.execute("PRAGMA cache_size = -2000000;")


def ensure_dst_schema(con: sqlite3.Connection, split_ngrams: bool) -> None:
    if split_ngrams:
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
                cf_id INTEGER NOT NULL,
                book_id INTEGER NOT NULL,
                tf INTEGER NOT NULL,
                post BLOB NOT NULL,
                PRIMARY KEY (cf_id, book_id)
            ) WITHOUT ROWID;

            CREATE INDEX IF NOT EXISTS unigrams_book_id_cf_id ON unigrams(book_id, cf_id);

            CREATE TABLE IF NOT EXISTS bigrams (
                key BLOB NOT NULL,
                book_id INTEGER NOT NULL,
                tf INTEGER NOT NULL,
                post BLOB NOT NULL,
                PRIMARY KEY (key, book_id)
            ) WITHOUT ROWID;

            CREATE INDEX IF NOT EXISTS bigrams_book_id_key ON bigrams(book_id, key);

            CREATE TABLE IF NOT EXISTS urns (
                book_id INTEGER NOT NULL PRIMARY KEY
            ) WITHOUT ROWID;
            """
        )
        return

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

        CREATE TABLE IF NOT EXISTS ngrams (
            key BLOB NOT NULL,
            book_id INTEGER NOT NULL,
            tf INTEGER NOT NULL,
            post BLOB NOT NULL,
            PRIMARY KEY (key, book_id)
        ) WITHOUT ROWID;

        CREATE INDEX IF NOT EXISTS ngrams_book_id_key ON ngrams(book_id, key);

        CREATE TABLE IF NOT EXISTS urns (
            book_id INTEGER NOT NULL PRIMARY KEY
        ) WITHOUT ROWID;
        """
    )


def ensure_words_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS words (
            word TEXT NOT NULL PRIMARY KEY,
            raw_id INTEGER NOT NULL UNIQUE,
            cf_id INTEGER NOT NULL
        ) WITHOUT ROWID;

        CREATE INDEX IF NOT EXISTS words_cf_id ON words(cf_id);
        """
    )


def batch_insert(
    con: sqlite3.Connection, sql: str, rows: Iterable[Tuple], batch_size: int
) -> None:
    cur = con.cursor()
    batch = []
    for row in rows:
        batch.append(row)
        if len(batch) >= batch_size:
            cur.executemany(sql, batch)
            batch.clear()
    if batch:
        cur.executemany(sql, batch)


def prepare_urn_filter(con: sqlite3.Connection, urns: List[int]) -> None:
    con.execute("DROP TABLE IF EXISTS urn_filter;")
    con.execute("CREATE TEMP TABLE urn_filter (urn INTEGER PRIMARY KEY) WITHOUT ROWID;")
    con.executemany("INSERT INTO urn_filter(urn) VALUES (?)", [(u,) for u in urns])


def select_urns(src: sqlite3.Connection, urns: Optional[List[int]], urn_limit: int) -> Optional[List[int]]:
    if urns:
        return urns
    if urn_limit <= 0:
        return None
    cur = src.execute("SELECT urn FROM urns ORDER BY urn LIMIT ?", (urn_limit,))
    return [row[0] for row in cur.fetchall()]


def build_word_maps(
    src: sqlite3.Connection,
    words_con: sqlite3.Connection,
    urns: Optional[List[int]],
    batch_size: int,
) -> Dict[str, Tuple[int, int]]:
    ensure_words_schema(words_con)
    apply_build_pragmas(words_con)

    if urns is not None:
        prepare_urn_filter(src, urns)
        sql = """
            SELECT DISTINCT word
            FROM unigram
            JOIN urn_filter USING (urn)
            ORDER BY word
        """
    else:
        sql = "SELECT DISTINCT word FROM unigram ORDER BY word"

    raw_id = 0
    cf_id = 0
    cf_map: Dict[str, int] = {}
    word_map: Dict[str, Tuple[int, int]] = {}

    def rows() -> Iterable[Tuple[str, int, int]]:
        nonlocal raw_id, cf_id
        cur = src.execute(sql)
        for (word,) in cur:
            raw_id += 1
            cf = word.casefold()
            if cf not in cf_map:
                cf_id += 1
                cf_map[cf] = cf_id
            word_map[word] = (raw_id, cf_map[cf])
            yield (word, raw_id, cf_map[cf])

    words_con.execute("BEGIN;")
    batch_insert(
        words_con,
        "INSERT INTO words(word, raw_id, cf_id) VALUES (?, ?, ?)",
        rows(),
        batch_size,
    )
    words_con.commit()
    return word_map


def process_book(
    dst: sqlite3.Connection,
    book_id: int,
    seqs: List[int],
    cf_ids: List[int],
    raw_ids: List[int],
    paras: List[int],
    pages: List[int],
    ngram_max: int,
    batch_size: int,
    split_ngrams: bool,
    bigram_min_uni: int,
    bigram_min_tf: int,
) -> None:
    tokens_rows = [
        (book_id, seqs[i], cf_ids[i], raw_ids[i], paras[i], pages[i])
        for i in range(len(seqs))
    ]

    dst.execute("BEGIN;")
    batch_insert(
        dst,
        "INSERT INTO tokens(book_id, seq, cf_id, raw_id, para, page) VALUES (?, ?, ?, ?, ?, ?)",
        tokens_rows,
        batch_size,
    )
    dst.execute("INSERT OR IGNORE INTO urns(book_id) VALUES (?)", (book_id,))

    count = len(cf_ids)
    if split_ngrams:
        unigram_positions: Dict[int, List[int]] = {}
        bigram_positions: Dict[bytes, List[int]] = {}
        for i in range(count):
            unigram_positions.setdefault(cf_ids[i], []).append(seqs[i])
            if ngram_max >= 2 and i + 1 < count:
                key = pack_key(cf_ids[i : i + 2])
                bigram_positions.setdefault(key, []).append(seqs[i])

        unigram_rows = []
        for cf_id, pos in unigram_positions.items():
            blob = encode_deltas(pos)
            unigram_rows.append((cf_id, book_id, len(pos), blob))

        batch_insert(
            dst,
            "INSERT INTO unigrams(cf_id, book_id, tf, post) VALUES (?, ?, ?, ?)",
            unigram_rows,
            batch_size,
        )

        if ngram_max >= 2:
            bigram_rows = []
            for key, pos in bigram_positions.items():
                tf = len(pos)
                if bigram_min_tf > 0 and tf < bigram_min_tf:
                    continue
                if bigram_min_uni > 0:
                    cf_x, cf_y = struct.unpack("<II", key)
                    if min(
                        len(unigram_positions.get(cf_x, [])),
                        len(unigram_positions.get(cf_y, [])),
                    ) < bigram_min_uni:
                        continue
                blob = encode_deltas(pos)
                bigram_rows.append((key, book_id, tf, blob))

            batch_insert(
                dst,
                "INSERT INTO bigrams(key, book_id, tf, post) VALUES (?, ?, ?, ?)",
                bigram_rows,
                batch_size,
            )

        dst.commit()
        return

    positions: Dict[bytes, List[int]] = {}
    for i in range(count):
        for n in range(1, ngram_max + 1):
            if i + n > count:
                break
            key = pack_key(cf_ids[i : i + n])
            positions.setdefault(key, []).append(seqs[i])

    ngram_rows = []
    for key, pos in positions.items():
        blob = encode_deltas(pos)
        ngram_rows.append((key, book_id, len(pos), blob))

    batch_insert(
        dst,
        "INSERT INTO ngrams(key, book_id, tf, post) VALUES (?, ?, ?, ?)",
        ngram_rows,
        batch_size,
    )
    dst.commit()


def convert(
    src_path: str,
    dst_path: str,
    words_path: str,
    urns: Optional[List[int]],
    urn_limit: int,
    ngram_max: int,
    batch_size: int,
    split_ngrams: bool,
    bigram_min_uni: int,
    bigram_min_tf: int,
) -> None:
    src = connect_ro(src_path)
    dst = sqlite3.connect(dst_path)
    words = sqlite3.connect(words_path)

    apply_build_pragmas(dst)
    if split_ngrams and ngram_max > 2:
        ngram_max = 2
    ensure_dst_schema(dst, split_ngrams)

    selected_urns = select_urns(src, urns, urn_limit)
    word_map = build_word_maps(src, words, selected_urns, batch_size)

    if selected_urns is not None:
        prepare_urn_filter(src, selected_urns)
        sql = """
            SELECT ft.urn, ft.word, ft.seq, ft.para, ft.page
            FROM ft
            JOIN urn_filter ON urn_filter.urn = ft.urn
            ORDER BY ft.urn, ft.seq
        """
    else:
        sql = "SELECT urn, word, seq, para, page FROM ft ORDER BY urn, seq"

    cur = src.execute(sql)

    current_urn = None
    seqs: List[int] = []
    cf_ids: List[int] = []
    raw_ids: List[int] = []
    paras: List[int] = []
    pages: List[int] = []

    for urn, word, seq, para, page in cur:
        if current_urn is None:
            current_urn = urn
        if urn != current_urn:
            process_book(
                dst,
                current_urn,
                seqs,
                cf_ids,
                raw_ids,
                paras,
                pages,
                ngram_max,
                batch_size,
                split_ngrams,
                bigram_min_uni,
                bigram_min_tf,
            )
            seqs.clear()
            cf_ids.clear()
            raw_ids.clear()
            paras.clear()
            pages.clear()
            current_urn = urn

        raw_id, cf_id = word_map[word]
        seqs.append(seq)
        cf_ids.append(cf_id)
        raw_ids.append(raw_id)
        paras.append(para)
        pages.append(page)

    if current_urn is not None:
        process_book(
            dst,
            current_urn,
            seqs,
            cf_ids,
            raw_ids,
            paras,
            pages,
            ngram_max,
            batch_size,
            split_ngrams,
            bigram_min_uni,
            bigram_min_tf,
        )

    src.close()
    dst.close()
    words.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert ALTO ft table into tokens + ngrams shards."
    )
    parser.add_argument("--src", required=True, help="Source alto_*.db path")
    parser.add_argument("--dst", required=True, help="Output postings db path")
    parser.add_argument(
        "--words-db",
        default="",
        help="Output words db path (default: <dst>_words.db)",
    )
    parser.add_argument(
        "--urns",
        default="",
        help="Comma-separated list of urns to process (optional)",
    )
    parser.add_argument(
        "--urn-limit",
        type=int,
        default=0,
        help="Process first N urns from source (optional)",
    )
    parser.add_argument(
        "--ngram-max",
        type=int,
        default=3,
        help="Max n-gram length to store (default: 3)",
    )
    parser.add_argument(
        "--split-ngrams",
        action="store_true",
        help="Store unigrams/bigrams in separate tables",
    )
    parser.add_argument(
        "--bigram-min-uni",
        type=int,
        default=0,
        help="Minimum unigram tf (per token) to keep bigram (split mode)",
    )
    parser.add_argument(
        "--bigram-min-tf",
        type=int,
        default=0,
        help="Minimum bigram tf to keep (split mode)",
    )
    parser.add_argument(
        "--batch",
        type=int,
        default=10000,
        help="Batch size for inserts (default: 10000)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    words_db = args.words_db or f"{args.dst}_words.db"
    urns = [int(u) for u in args.urns.split(",") if u.strip()] or None
    convert(
        src_path=args.src,
        dst_path=args.dst,
        words_path=words_db,
        urns=urns,
        urn_limit=args.urn_limit,
        ngram_max=args.ngram_max,
        batch_size=args.batch,
        split_ngrams=args.split_ngrams,
        bigram_min_uni=args.bigram_min_uni,
        bigram_min_tf=args.bigram_min_tf,
    )


if __name__ == "__main__":
    main()
