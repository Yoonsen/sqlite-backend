#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Tuple, Set, Optional

import convert_ft_to_ngrams as conv


def load_csv_groups(path: str) -> Dict[str, List[int]]:
    groups: Dict[str, List[int]] = defaultdict(list)
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            db_path = (row.get("db_path") or "").strip()
            urn_seq = row.get("urn_seq")
            if not db_path or not urn_seq:
                continue
            try:
                book_id = int(urn_seq)
            except ValueError:
                continue
            groups[db_path].append(book_id)
    return groups


def iter_distinct_words(src: sqlite3.Connection, urns: List[int]) -> Iterable[str]:
    conv.prepare_urn_filter(src, urns)
    sql = """
        SELECT DISTINCT word
        FROM unigram
        JOIN urn_filter USING (urn)
        ORDER BY word
    """
    for (word,) in src.execute(sql):
        yield word


def ensure_words_schema(words_con: sqlite3.Connection) -> None:
    conv.ensure_words_schema(words_con)
    conv.apply_build_pragmas(words_con)


def load_existing_words(words_con: sqlite3.Connection) -> Tuple[Dict[str, Tuple[int, int]], Dict[str, int], int, int]:
    word_map: Dict[str, Tuple[int, int]] = {}
    cf_map: Dict[str, int] = {}
    max_raw = 0
    max_cf = 0
    for word, raw_id, cf_id in words_con.execute("SELECT word, raw_id, cf_id FROM words"):
        word_map[word] = (raw_id, cf_id)
        if raw_id > max_raw:
            max_raw = raw_id
        if cf_id > max_cf:
            max_cf = cf_id
        cf = word.casefold()
        if cf not in cf_map:
            cf_map[cf] = cf_id
    return word_map, cf_map, max_raw, max_cf


def update_words(
    src: sqlite3.Connection,
    words_con: sqlite3.Connection,
    urns: List[int],
    word_map: Dict[str, Tuple[int, int]],
    cf_map: Dict[str, int],
    raw_id: int,
    cf_id: int,
    batch_size: int,
) -> Tuple[int, int]:
    words_con.execute("BEGIN;")
    rows: List[Tuple[str, int, int]] = []
    for word in iter_distinct_words(src, urns):
        if word in word_map:
            continue
        raw_id += 1
        cf = word.casefold()
        if cf not in cf_map:
            cf_id += 1
            cf_map[cf] = cf_id
        word_map[word] = (raw_id, cf_map[cf])
        rows.append((word, raw_id, cf_map[cf]))
        if len(rows) >= batch_size:
            words_con.executemany(
                "INSERT INTO words(word, raw_id, cf_id) VALUES (?, ?, ?)", rows
            )
            rows.clear()
    if rows:
        words_con.executemany(
            "INSERT INTO words(word, raw_id, cf_id) VALUES (?, ?, ?)", rows
        )
    words_con.commit()
    return raw_id, cf_id


def process_db(
    src_path: str,
    dst: sqlite3.Connection,
    words_con: sqlite3.Connection,
    urns: List[int],
    word_map: Dict[str, Tuple[int, int]],
    cf_map: Dict[str, int],
    raw_id: int,
    cf_id: int,
    batch_size: int,
    max_tokens: Optional[int],
    total_tokens: int,
) -> Tuple[int, int]:
    src = conv.connect_ro(src_path)
    raw_id, cf_id = update_words(
        src, words_con, urns, word_map, cf_map, raw_id, cf_id, batch_size
    )

    conv.prepare_urn_filter(src, urns)
    sql = """
        SELECT ft.urn, ft.word, ft.seq, ft.para, ft.page
        FROM ft
        JOIN urn_filter ON urn_filter.urn = ft.urn
        ORDER BY ft.urn, ft.seq
    """

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
            conv.process_book(
                dst,
                current_urn,
                seqs,
                cf_ids,
                raw_ids,
                paras,
                pages,
                ngram_max=1,
                batch_size=batch_size,
                split_ngrams=True,
                bigram_min_uni=0,
                bigram_min_tf=0,
            )
            total_tokens += len(seqs)
            if max_tokens is not None and total_tokens >= max_tokens:
                src.close()
                return raw_id, cf_id, total_tokens, True
            seqs.clear()
            cf_ids.clear()
            raw_ids.clear()
            paras.clear()
            pages.clear()
            current_urn = urn

        raw_word_id, cf_word_id = word_map[word]
        seqs.append(seq)
        cf_ids.append(cf_word_id)
        raw_ids.append(raw_word_id)
        paras.append(para)
        pages.append(page)

    if current_urn is not None:
        conv.process_book(
            dst,
            current_urn,
            seqs,
            cf_ids,
            raw_ids,
            paras,
            pages,
            ngram_max=1,
            batch_size=batch_size,
            split_ngrams=True,
            bigram_min_uni=0,
            bigram_min_tf=0,
        )
        total_tokens += len(seqs)

    src.close()
    return raw_id, cf_id, total_tokens, False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build imagination shard from CSV list of urn_seq + db_path."
    )
    parser.add_argument("--csv", required=True, help="Input CSV with urn_seq + db_path")
    parser.add_argument("--src-root", required=True, help="Root directory for db_path")
    parser.add_argument("--dst", required=True, help="Output postings db path")
    parser.add_argument("--words-db", required=True, help="Output words db path")
    parser.add_argument("--batch", type=int, default=10000, help="Batch size")
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=0,
        help="Stop after writing this many tokens (0 = no limit)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    groups = load_csv_groups(args.csv)
    if not groups:
        raise SystemExit("No rows found in CSV.")

    dst = sqlite3.connect(args.dst)
    conv.apply_build_pragmas(dst)
    conv.ensure_dst_schema(dst, split_ngrams=True)

    words_con = sqlite3.connect(args.words_db)
    ensure_words_schema(words_con)
    word_map, cf_map, raw_id, cf_id = load_existing_words(words_con)

    processed: Set[int] = set(
        row[0] for row in dst.execute("SELECT book_id FROM urns")
    )
    total_tokens = 0
    max_tokens = args.max_tokens if args.max_tokens > 0 else None

    for db_path in sorted(groups.keys()):
        urns = [u for u in groups[db_path] if u not in processed]
        if not urns:
            continue
        src_path = db_path
        if not Path(db_path).is_absolute():
            src_path = str(Path(args.src_root) / db_path)
        if not Path(src_path).exists():
            print(f"skip missing db: {src_path}")
            continue
        print(f"processing {src_path} ({len(urns)} urns)")
        raw_id, cf_id, total_tokens, stop = process_db(
            src_path,
            dst,
            words_con,
            urns,
            word_map,
            cf_map,
            raw_id,
            cf_id,
            args.batch,
            max_tokens,
            total_tokens,
        )
        processed.update(urns)
        if stop:
            print(f"Reached max tokens: {total_tokens}")
            break

    dst.close()
    words_con.close()


if __name__ == "__main__":
    main()
