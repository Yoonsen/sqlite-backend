#!/usr/bin/env python3
from __future__ import annotations

import argparse
import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Tuple


@dataclass
class LocalWord:
    shard_id: str
    cf_id: int
    word: str
    docfreq: int
    total_tf: int


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Build global words catalog from shard main DBs."
    )
    p.add_argument(
        "--sources",
        nargs="+",
        required=True,
        help="Shard main DB files (imag_roaring_main_*.db)",
    )
    p.add_argument("--dst", required=True, help="Output global words DB file")
    p.add_argument(
        "--keep-percent",
        type=float,
        default=70.0,
        help="Per-shard keep ratio in percent after filtering (default: 70)",
    )
    p.add_argument(
        "--min-docfreq",
        type=int,
        default=2,
        help="Per-shard minimum docfreq (default: 2 = non-hapax)",
    )
    return p.parse_args()


def ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS global_words (
            word TEXT NOT NULL PRIMARY KEY,
            global_id INTEGER NOT NULL UNIQUE,
            shard_count INTEGER NOT NULL,
            sum_docfreq INTEGER NOT NULL,
            sum_total_tf INTEGER NOT NULL
        ) WITHOUT ROWID;

        CREATE TABLE IF NOT EXISTS shard_word_map (
            shard_id TEXT NOT NULL,
            cf_id INTEGER NOT NULL,
            word TEXT NOT NULL,
            global_id INTEGER NOT NULL,
            docfreq INTEGER NOT NULL,
            total_tf INTEGER NOT NULL,
            PRIMARY KEY (shard_id, cf_id)
        ) WITHOUT ROWID;

        CREATE INDEX IF NOT EXISTS shard_word_map_global_id
            ON shard_word_map(global_id);

        CREATE TABLE IF NOT EXISTS shards (
            shard_id TEXT NOT NULL PRIMARY KEY,
            main_path TEXT NOT NULL
        ) WITHOUT ROWID;

        CREATE TABLE IF NOT EXISTS meta (
            key TEXT NOT NULL PRIMARY KEY,
            value TEXT NOT NULL
        ) WITHOUT ROWID;
        """
    )


def shard_id_from_path(path: Path) -> str:
    return path.stem


def fetch_keep_count(src: sqlite3.Connection, min_docfreq: int, keep_percent: float) -> int:
    (n_rows,) = src.execute(
        "SELECT COUNT(*) FROM words WHERE docfreq >= ?",
        (min_docfreq,),
    ).fetchone()
    if n_rows <= 0:
        return 0
    keep_n = int(math.ceil(n_rows * (keep_percent / 100.0)))
    return max(1, keep_n)


def fetch_words_for_shard(
    src_path: Path, min_docfreq: int, keep_percent: float
) -> List[LocalWord]:
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    try:
        keep_n = fetch_keep_count(src, min_docfreq=min_docfreq, keep_percent=keep_percent)
        if keep_n == 0:
            return []

        rows = src.execute(
            """
            SELECT cf_id, word, docfreq, total_tf
            FROM words
            WHERE docfreq >= ?
            ORDER BY docfreq DESC, total_tf DESC, word ASC
            LIMIT ?
            """,
            (min_docfreq, keep_n),
        ).fetchall()

        sid = shard_id_from_path(src_path)
        out: List[LocalWord] = []
        seen_cf: set[int] = set()
        for cf_id, word, docfreq, total_tf in rows:
            cfi = int(cf_id)
            # Keep the first row per cf_id (rows are already ranked by frequency).
            if cfi in seen_cf:
                continue
            seen_cf.add(cfi)
            out.append(
                LocalWord(
                    shard_id=sid,
                    cf_id=cfi,
                    word=str(word),
                    docfreq=int(docfreq or 0),
                    total_tf=int(total_tf or 0),
                )
            )
        return out
    finally:
        src.close()


def build_global_ids(all_local_rows: Iterable[LocalWord]) -> Tuple[Dict[str, int], Dict[str, List[LocalWord]]]:
    by_word: Dict[str, List[LocalWord]] = {}
    for r in all_local_rows:
        by_word.setdefault(r.word, []).append(r)

    ordered_words = sorted(
        by_word.keys(),
        key=lambda w: (
            -sum(x.docfreq for x in by_word[w]),
            -sum(x.total_tf for x in by_word[w]),
            w,
        ),
    )
    global_id_by_word = {w: i + 1 for i, w in enumerate(ordered_words)}
    return global_id_by_word, by_word


def main() -> None:
    args = parse_args()
    if args.keep_percent <= 0 or args.keep_percent > 100:
        raise SystemExit("--keep-percent must be in (0, 100].")

    src_paths = [Path(p) for p in args.sources]
    for p in src_paths:
        if not p.exists():
            raise SystemExit(f"Missing source shard: {p}")

    dst_path = Path(args.dst)
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    if dst_path.exists():
        dst_path.unlink()

    all_local_rows: List[LocalWord] = []
    for src_path in src_paths:
        shard_rows = fetch_words_for_shard(
            src_path, min_docfreq=args.min_docfreq, keep_percent=args.keep_percent
        )
        print(
            f"[{src_path.name}] selected {len(shard_rows)} rows "
            f"(docfreq>={args.min_docfreq}, keep={args.keep_percent:.1f}%)"
        )
        all_local_rows.extend(shard_rows)

    global_id_by_word, by_word = build_global_ids(all_local_rows)

    dst = sqlite3.connect(str(dst_path))
    try:
        dst.execute("PRAGMA journal_mode=WAL")
        dst.execute("PRAGMA synchronous=NORMAL")
        dst.execute("PRAGMA temp_store=FILE")
        dst.execute("PRAGMA cache_size=-80000")
        dst.execute("PRAGMA mmap_size=0")
        ensure_schema(dst)

        dst.executemany(
            "INSERT INTO shards(shard_id, main_path) VALUES (?, ?)",
            [(shard_id_from_path(p), str(p)) for p in src_paths],
        )

        global_rows = []
        for word, gid in global_id_by_word.items():
            group = by_word[word]
            global_rows.append(
                (
                    word,
                    gid,
                    len({x.shard_id for x in group}),
                    sum(x.docfreq for x in group),
                    sum(x.total_tf for x in group),
                )
            )
        dst.executemany(
            """
            INSERT INTO global_words(word, global_id, shard_count, sum_docfreq, sum_total_tf)
            VALUES (?, ?, ?, ?, ?)
            """,
            global_rows,
        )

        map_rows = [
            (
                r.shard_id,
                r.cf_id,
                r.word,
                global_id_by_word[r.word],
                r.docfreq,
                r.total_tf,
            )
            for r in all_local_rows
        ]
        dst.executemany(
            """
            INSERT INTO shard_word_map(shard_id, cf_id, word, global_id, docfreq, total_tf)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            map_rows,
        )

        dst.executemany(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            [
                ("model", "global_words_v1"),
                ("min_docfreq", str(args.min_docfreq)),
                ("keep_percent", f"{args.keep_percent:.4f}"),
                ("source_shards", str(len(src_paths))),
                ("global_word_count", str(len(global_id_by_word))),
                ("local_rows_kept", str(len(all_local_rows))),
            ],
        )
        dst.commit()
        print(
            f"Done: {dst_path} "
            f"(global_words={len(global_id_by_word)}, local_rows={len(all_local_rows)})"
        )
    finally:
        dst.close()


if __name__ == "__main__":
    main()
