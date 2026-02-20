#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import time
from collections import defaultdict
from pathlib import Path
from typing import DefaultDict, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    from pyroaring import BitMap
except Exception as exc:  # pragma: no cover
    raise SystemExit(
        "Missing dependency: pyroaring. Install with: python -m pip install pyroaring"
    ) from exc


BookRef = Tuple[int, int]  # (source_index, book_id)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Rebuild shards with Roaring blobs for unigrams.post and words.docpost."
    )
    p.add_argument(
        "--config",
        default="config.local.json",
        help="Config JSON containing postings_dbs list",
    )
    p.add_argument(
        "--sources",
        nargs="*",
        default=None,
        help="Optional explicit source shard DB paths (overrides config)",
    )
    p.add_argument("--out-dir", required=True, help="Output directory for rebuilt shards")
    p.add_argument(
        "--prefix",
        default="imag_roaring",
        help="Output shard filename prefix",
    )
    p.add_argument(
        "--target-shards",
        type=int,
        default=3,
        help="Target number of output shards",
    )
    p.add_argument(
        "--complement-threshold",
        type=float,
        default=0.5,
        help="Use complement docpost when df/total_docs exceeds threshold",
    )
    p.add_argument(
        "--limit-books",
        type=int,
        default=0,
        help="Optional limit for dry-runs (0 = all books)",
    )
    p.add_argument(
        "--commit-every",
        type=int,
        default=25000,
        help="Commit interval for unigrams conversion",
    )
    return p.parse_args()


def roaring_bytes_from_sorted(ids: Sequence[int]) -> bytes:
    return BitMap(ids).serialize()


def decode_varint_deltas(blob: bytes) -> List[int]:
    out: List[int] = []
    prev = 0
    i = 0
    n = len(blob)
    while i < n:
        shift = 0
        val = 0
        while True:
            b = blob[i]
            i += 1
            val |= (b & 0x7F) << shift
            if (b & 0x80) == 0:
                break
            shift += 7
        prev += val
        out.append(prev)
    return out


def connect_rw(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=OFF")
    con.execute("PRAGMA temp_store=MEMORY")
    con.execute("PRAGMA cache_size=-400000")
    con.execute("PRAGMA mmap_size=268435456")
    return con


def connect_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def ensure_schema(dst: sqlite3.Connection) -> None:
    dst.executescript(
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
        """
    )


def resolve_sources(args: argparse.Namespace) -> List[Path]:
    if args.sources:
        return [Path(p) for p in args.sources]
    cfg = json.loads(Path(args.config).read_text())
    srcs = [Path(p) for p in cfg.get("postings_dbs", [])]
    if not srcs:
        raise SystemExit("No source shards found (use --sources or config postings_dbs).")
    return srcs


def fetch_books(sources: Sequence[Path], limit_books: int = 0) -> List[BookRef]:
    refs: List[BookRef] = []
    for idx, src_path in enumerate(sources):
        con = connect_ro(src_path)
        try:
            rows = [r[0] for r in con.execute("SELECT book_id FROM urns ORDER BY book_id")]
        finally:
            con.close()
        refs.extend((idx, b) for b in rows)
    refs.sort()
    if limit_books > 0:
        refs = refs[:limit_books]
    return refs


def partition_books(refs: Sequence[BookRef], target_shards: int) -> List[List[BookRef]]:
    if target_shards < 1:
        raise ValueError("target_shards must be >= 1")
    if not refs:
        return [[]]
    chunk = int(math.ceil(len(refs) / float(target_shards)))
    return [list(refs[i : i + chunk]) for i in range(0, len(refs), chunk)]


def insert_selected_books(dst: sqlite3.Connection, selected: Sequence[int]) -> None:
    dst.executemany("INSERT INTO urns(book_id) VALUES (?)", ((b,) for b in selected))


def copy_tokens_for_source(
    dst: sqlite3.Connection, src_path: Path, selected_books: Sequence[int]
) -> None:
    if not selected_books:
        return
    dst.commit()
    dst.execute("ATTACH DATABASE ? AS srcdb", (str(src_path),))
    try:
        dst.execute("CREATE TEMP TABLE sel(book_id INTEGER PRIMARY KEY) WITHOUT ROWID")
        dst.executemany("INSERT INTO sel(book_id) VALUES (?)", ((b,) for b in selected_books))
        dst.execute(
            """
            INSERT INTO tokens(book_id, seq, cf_id, raw_id, para, page)
            SELECT t.book_id, t.seq, t.cf_id, t.raw_id, t.para, t.page
            FROM srcdb.tokens t
            JOIN sel s ON s.book_id = t.book_id
            """
        )
        dst.execute("DROP TABLE sel")
        dst.commit()
    finally:
        dst.commit()
        dst.execute("DETACH DATABASE srcdb")


def copy_and_convert_unigrams_for_source(
    dst: sqlite3.Connection,
    src_path: Path,
    selected_books: Sequence[int],
    commit_every: int,
) -> int:
    if not selected_books:
        return 0
    src = connect_ro(src_path)
    written = 0
    try:
        src.execute("CREATE TEMP TABLE sel(book_id INTEGER PRIMARY KEY) WITHOUT ROWID")
        src.executemany("INSERT INTO sel(book_id) VALUES (?)", ((b,) for b in selected_books))
        cur = src.execute(
            """
            SELECT u.book_id, u.cf_id, u.tf, u.post
            FROM unigrams u
            JOIN sel s ON s.book_id = u.book_id
            ORDER BY u.book_id, u.cf_id
            """
        )
        batch: List[Tuple[int, int, int, bytes]] = []
        for book_id, cf_id, tf, post_blob in cur:
            seqs = decode_varint_deltas(post_blob)
            rb = roaring_bytes_from_sorted(seqs)
            batch.append((book_id, cf_id, int(tf), rb))
            if len(batch) >= commit_every:
                dst.executemany(
                    "INSERT INTO unigrams(book_id, cf_id, tf, post) VALUES (?, ?, ?, ?)", batch
                )
                dst.commit()
                written += len(batch)
                batch.clear()
        if batch:
            dst.executemany(
                "INSERT INTO unigrams(book_id, cf_id, tf, post) VALUES (?, ?, ?, ?)", batch
            )
            dst.commit()
            written += len(batch)
    finally:
        src.close()
    return written


def copy_words_lexicon_for_source(
    dst: sqlite3.Connection, src_path: Path, selected_books: Sequence[int]
) -> None:
    if not selected_books:
        return
    dst.commit()
    dst.execute("ATTACH DATABASE ? AS srcdb", (str(src_path),))
    try:
        dst.execute("CREATE TEMP TABLE sel(book_id INTEGER PRIMARY KEY) WITHOUT ROWID")
        dst.executemany("INSERT INTO sel(book_id) VALUES (?)", ((b,) for b in selected_books))
        dst.execute(
            """
            INSERT OR IGNORE INTO words(word, raw_id, cf_id, global_id)
            SELECT w.word, w.raw_id, w.cf_id, w.cf_id
            FROM srcdb.words w
            JOIN (
                SELECT DISTINCT u.cf_id
                FROM srcdb.unigrams u
                JOIN sel s ON s.book_id = u.book_id
            ) x ON x.cf_id = w.cf_id
            """
        )
        dst.execute("DROP TABLE sel")
        dst.commit()
    finally:
        dst.commit()
        dst.execute("DETACH DATABASE srcdb")


def build_docposts_and_stats(
    dst: sqlite3.Connection, complement_threshold: float
) -> Tuple[int, int]:
    all_books = [r[0] for r in dst.execute("SELECT book_id FROM urns ORDER BY book_id")]
    total_docs = len(all_books)
    all_books_set = set(all_books)
    dst.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", ("total_docs", str(total_docs))
    )
    dst.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", ("postings_codec", "roaring_v1")
    )
    dst.execute(
        "INSERT OR REPLACE INTO urns_postings(id, post) VALUES (?, ?)",
        (1, roaring_bytes_from_sorted(all_books)),
    )

    updated = 0
    current_cf: Optional[int] = None
    ids: List[int] = []
    tf_sum = 0

    def flush(cf_id: Optional[int], doc_ids: List[int], total_tf: int) -> int:
        if cf_id is None:
            return 0
        df = len(doc_ids)
        if total_docs > 0 and (df / float(total_docs)) > complement_threshold:
            present = set(doc_ids)
            payload_ids = sorted(all_books_set - present)
            is_complement = 1
        else:
            payload_ids = doc_ids
            is_complement = 0
        blob = roaring_bytes_from_sorted(payload_ids)
        dst.execute(
            """
            UPDATE words
            SET docfreq = ?, total_tf = ?, docpost = ?, docpost_is_complement = ?
            WHERE cf_id = ?
            """,
            (df, total_tf, blob, is_complement, cf_id),
        )
        return 1

    cur = dst.execute("SELECT cf_id, book_id, tf FROM unigrams ORDER BY cf_id, book_id")
    for cf_id, book_id, tf in cur:
        if current_cf is None:
            current_cf = int(cf_id)
        if int(cf_id) != current_cf:
            updated += flush(current_cf, ids, tf_sum)
            ids = []
            tf_sum = 0
            current_cf = int(cf_id)
        ids.append(int(book_id))
        tf_sum += int(tf)
    updated += flush(current_cf, ids, tf_sum)
    dst.commit()
    return total_docs, updated


def build_one_shard(
    shard_idx: int,
    refs: Sequence[BookRef],
    sources: Sequence[Path],
    out_dir: Path,
    prefix: str,
    complement_threshold: float,
    commit_every: int,
) -> Path:
    out_path = out_dir / f"{prefix}_{shard_idx:02d}.db"
    if out_path.exists():
        out_path.unlink()
    dst = connect_rw(out_path)
    ensure_schema(dst)
    source_to_books: DefaultDict[int, List[int]] = defaultdict(list)
    for sidx, book_id in refs:
        source_to_books[sidx].append(book_id)

    t0 = time.time()
    insert_selected_books(dst, [b for _, b in refs])
    dst.commit()

    for sidx, books in sorted(source_to_books.items()):
        src_path = sources[sidx]
        print(f"[shard {shard_idx}] copy tokens from {src_path.name} ({len(books)} books)")
        copy_tokens_for_source(dst, src_path, books)
        dst.commit()

    total_uni = 0
    for sidx, books in sorted(source_to_books.items()):
        src_path = sources[sidx]
        print(f"[shard {shard_idx}] convert unigrams from {src_path.name} ({len(books)} books)")
        total_uni += copy_and_convert_unigrams_for_source(dst, src_path, books, commit_every)
        print(f"[shard {shard_idx}] converted unigrams rows so far: {total_uni}")

    for sidx, books in sorted(source_to_books.items()):
        src_path = sources[sidx]
        print(f"[shard {shard_idx}] copy words lexicon from {src_path.name}")
        copy_words_lexicon_for_source(dst, src_path, books)
        dst.commit()

    total_docs, updated = build_docposts_and_stats(dst, complement_threshold)
    elapsed = time.time() - t0
    print(
        f"[shard {shard_idx}] done: docs={total_docs}, unigrams={total_uni}, "
        f"updated_cf={updated}, elapsed_s={elapsed:.1f}"
    )
    dst.execute("ANALYZE")
    dst.commit()
    dst.close()
    return out_path


def main() -> None:
    args = parse_args()
    sources = resolve_sources(args)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for p in sources:
        if not p.exists():
            raise SystemExit(f"Missing source shard: {p}")

    refs = fetch_books(sources, args.limit_books)
    if not refs:
        raise SystemExit("No books found in source shards.")
    splits = partition_books(refs, args.target_shards)
    print(
        f"books={len(refs)} target_shards={args.target_shards} produced_shards={len(splits)} "
        f"avg_books={len(refs)/len(splits):.1f}"
    )

    out_paths: List[Path] = []
    for idx, shard_refs in enumerate(splits):
        print(f"=== building shard {idx} books={len(shard_refs)} ===")
        out_paths.append(
            build_one_shard(
                idx,
                shard_refs,
                sources,
                out_dir,
                args.prefix,
                args.complement_threshold,
                args.commit_every,
            )
        )
    print("Rebuild complete:")
    for p in out_paths:
        print(f" - {p}")


if __name__ == "__main__":
    main()
