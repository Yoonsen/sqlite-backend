#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Validate local shard consistency and optional global-id sync."
    )
    p.add_argument(
        "--config",
        default="config.main_sidecar.local.json",
        help="Config JSON with postings_dbs.",
    )
    p.add_argument(
        "--master-words-db",
        default="",
        help="Optional global/master words DB for global_id checks.",
    )
    p.add_argument(
        "--sample",
        type=int,
        default=5,
        help="How many example rows to print per failed check.",
    )
    p.add_argument(
        "--check-tokens-raw",
        action="store_true",
        help="Enable expensive full check: tokens.raw_id must exist in words.raw_id.",
    )
    return p.parse_args()


def q1(cur: sqlite3.Cursor, sql: str, params: Tuple = ()) -> int:
    row = cur.execute(sql, params).fetchone()
    return int(row[0]) if row else 0


def has_table(cur: sqlite3.Cursor, table: str, schema: str = "main") -> bool:
    row = cur.execute(
        f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return bool(row)


def has_column(cur: sqlite3.Cursor, table: str, column: str) -> bool:
    cols = {r[1] for r in cur.execute(f"PRAGMA table_info({table})")}
    return column in cols


def detect_master_model(master_cur: sqlite3.Cursor) -> str:
    if has_table(master_cur, "global_words", schema="masterdb"):
        return "global_words"
    if has_table(master_cur, "global_cf_lexicon", schema="masterdb"):
        return "global_cf_lexicon"
    return "unknown"


def validate_one_shard(
    shard_path: Path,
    sample_n: int,
    master_path: Optional[Path],
    check_tokens_raw: bool,
) -> Dict[str, object]:
    out: Dict[str, object] = {"shard": str(shard_path), "ok": True, "checks": {}}
    con = sqlite3.connect(f"file:{shard_path}?mode=ro", uri=True)
    cur = con.cursor()
    try:
        if master_path is not None:
            cur.execute("ATTACH DATABASE ? AS masterdb", (str(master_path),))

        required = ("words", "unigrams", "urns")
        missing = [t for t in required if not has_table(cur, t)]
        if missing:
            out["ok"] = False
            out["checks"]["missing_required_tables"] = missing
            return out

        # 1) Every unigrams.cf_id must exist in words.cf_id.
        missing_cf_count = q1(
            cur,
            """
            SELECT COUNT(*)
            FROM (
                SELECT DISTINCT u.cf_id
                FROM unigrams u
                LEFT JOIN words w ON w.cf_id = u.cf_id
                WHERE w.cf_id IS NULL
            )
            """,
        )
        out["checks"]["unigrams_cf_missing_in_words"] = missing_cf_count
        if missing_cf_count > 0:
            out["ok"] = False
            out["checks"]["unigrams_cf_missing_examples"] = cur.execute(
                """
                SELECT DISTINCT u.cf_id
                FROM unigrams u
                LEFT JOIN words w ON w.cf_id = u.cf_id
                WHERE w.cf_id IS NULL
                ORDER BY u.cf_id
                LIMIT ?
                """,
                (sample_n,),
            ).fetchall()

        # 2) cf_id should map to one casefold-family per shard.
        ambiguous_cf_count = q1(
            cur,
            """
            SELECT COUNT(*)
            FROM (
                SELECT cf_id
                FROM words
                GROUP BY cf_id
                HAVING COUNT(DISTINCT lower(word)) > 1
            )
            """,
        )
        out["checks"]["cf_id_multiple_casefolds"] = ambiguous_cf_count
        if ambiguous_cf_count > 0:
            out["ok"] = False
            out["checks"]["cf_id_multiple_casefolds_examples"] = cur.execute(
                """
                SELECT cf_id, COUNT(DISTINCT lower(word)) AS n_lemmas
                FROM words
                GROUP BY cf_id
                HAVING COUNT(DISTINCT lower(word)) > 1
                ORDER BY n_lemmas DESC, cf_id
                LIMIT ?
                """,
                (sample_n,),
            ).fetchall()

        # 3) Optional expensive check on tokens/raw coverage.
        if check_tokens_raw and has_table(cur, "tokens"):
            missing_raw_count = q1(
                cur,
                """
                SELECT COUNT(*)
                FROM (
                    SELECT DISTINCT t.raw_id
                    FROM tokens t
                    LEFT JOIN words w ON w.raw_id = t.raw_id
                    WHERE w.raw_id IS NULL
                )
                """,
            )
            out["checks"]["tokens_raw_missing_in_words"] = missing_raw_count
            if missing_raw_count > 0:
                out["ok"] = False
                out["checks"]["tokens_raw_missing_examples"] = cur.execute(
                    """
                    SELECT DISTINCT t.raw_id
                    FROM tokens t
                    LEFT JOIN words w ON w.raw_id = t.raw_id
                    WHERE w.raw_id IS NULL
                    ORDER BY t.raw_id
                    LIMIT ?
                    """,
                    (sample_n,),
                ).fetchall()

        # 4) Optional global sync checks against master words DB.
        if master_path is not None:
            model = detect_master_model(cur)
            out["checks"]["master_model"] = model
            if not has_column(cur, "words", "global_id"):
                out["checks"]["missing_global_id_column"] = True
                out["ok"] = False
            else:
                missing_global_id = q1(
                    cur,
                    "SELECT COUNT(*) FROM words WHERE global_id IS NULL",
                )
                out["checks"]["words_missing_global_id"] = missing_global_id
                if model == "global_words":
                    missing_in_master = q1(
                        cur,
                        """
                        SELECT COUNT(*)
                        FROM words w
                        LEFT JOIN masterdb.global_words g ON g.global_id = w.global_id
                        WHERE w.global_id IS NOT NULL AND g.global_id IS NULL
                        """,
                    )
                    out["checks"]["global_id_missing_in_master"] = missing_in_master
                    if missing_in_master > 0:
                        out["ok"] = False

                    word_mismatch = q1(
                        cur,
                        """
                        SELECT COUNT(*)
                        FROM words w
                        JOIN masterdb.global_words g ON g.global_id = w.global_id
                        WHERE lower(g.word) != lower(w.word)
                        """,
                    )
                    out["checks"]["global_id_word_mismatch"] = word_mismatch
                    if word_mismatch > 0:
                        out["ok"] = False
                elif model == "global_cf_lexicon":
                    missing_in_master = q1(
                        cur,
                        """
                        SELECT COUNT(*)
                        FROM words w
                        LEFT JOIN masterdb.global_cf_lexicon g ON g.global_cf_id = w.global_id
                        WHERE w.global_id IS NOT NULL AND g.global_cf_id IS NULL
                        """,
                    )
                    out["checks"]["global_id_missing_in_master"] = missing_in_master
                    if missing_in_master > 0:
                        out["ok"] = False

                    cf_mismatch = q1(
                        cur,
                        """
                        SELECT COUNT(*)
                        FROM words w
                        JOIN masterdb.global_cf_lexicon g ON g.global_cf_id = w.global_id
                        WHERE lower(w.word) != g.cf_word
                        """,
                    )
                    out["checks"]["global_cf_word_mismatch"] = cf_mismatch
                    if cf_mismatch > 0:
                        out["ok"] = False
                else:
                    out["checks"]["master_model_error"] = "unknown master schema"
                    out["ok"] = False
    finally:
        if master_path is not None:
            try:
                cur.execute("DETACH DATABASE masterdb")
            except sqlite3.Error:
                pass
        con.close()
    return out


def main() -> int:
    args = parse_args()
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    shard_paths = [Path(p) for p in cfg.get("postings_dbs", [])]
    if not shard_paths:
        print("FAIL: no postings_dbs in config")
        return 2

    master_path: Optional[Path] = None
    if args.master_words_db.strip():
        master_path = Path(args.master_words_db)
        if not master_path.exists():
            print(f"FAIL: master words db not found: {master_path}")
            return 2

    print(f"Config: {args.config}")
    print(f"Shards: {len(shard_paths)}")
    if master_path is not None:
        print(f"Master words DB: {master_path}")
    print("")

    failed = 0
    for p in shard_paths:
        if not p.exists():
            print(f"[FAIL] {p} (missing file)")
            failed += 1
            continue
        res = validate_one_shard(
            p,
            sample_n=args.sample,
            master_path=master_path,
            check_tokens_raw=bool(args.check_tokens_raw),
        )
        status = "PASS" if res.get("ok") else "FAIL"
        print(f"[{status}] {res['shard']}")
        checks = res.get("checks", {})
        for key, value in checks.items():
            print(f"  - {key}: {value}")
        if status == "FAIL":
            failed += 1
        print("")

    if failed:
        print(f"Validation finished with failures: {failed}/{len(shard_paths)} shards")
        return 1
    print("Validation OK: all shards passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
