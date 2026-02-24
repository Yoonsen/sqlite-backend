#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import multiprocessing as mp
import os
import random
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from api_python.postings_queries import (
    candidate_books_for_groups,
    connect_postings,
    connect_words,
    fetch_window,
    get_cf_id,
    group_positions_for_book,
    near_count_from_groups,
    near_positions_from_groups,
    sequence_count_from_groups,
    sequence_positions_from_groups,
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Local shard dispatcher using multiprocessing (one process per shard task)."
    )
    p.add_argument("--config", required=True, help="Config JSON with postings_dbs and optional words_db.")
    p.add_argument(
        "--shard-map-db",
        required=True,
        help="SQLite DB with table book_shard(book_id INTEGER, shard_id INTEGER).",
    )
    p.add_argument("--endpoint", required=True, choices=["/near_query", "/near_fragments", "/near_hits"])
    p.add_argument("--payload-json", required=True, help="Path to payload JSON.")
    p.add_argument("--filter-ids-csv", required=True, help="CSV with dhlabid/urn_seq/book_id.")
    p.add_argument(
        "--max-workers",
        type=int,
        default=0,
        help="Max worker processes (0 = one process per non-empty shard task).",
    )
    return p.parse_args()


def read_filter_ids_csv(path: Path) -> List[int]:
    with path.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        return []
    header = [c.strip().lower() for c in rows[0]]
    data_rows = rows
    idx = 0
    for name in ("dhlabid", "urn_seq", "book_id"):
        if name in header:
            idx = header.index(name)
            data_rows = rows[1:]
            break
    out: List[int] = []
    for row in data_rows:
        if not row or idx >= len(row):
            continue
        txt = row[idx].strip()
        if not txt:
            continue
        try:
            out.append(int(float(txt)))
        except ValueError:
            continue
    return out


def chunked(seq: List[int], n: int) -> Iterable[List[int]]:
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def split_ids_by_shard(shard_map_db: Path, book_ids: List[int]) -> Dict[int, List[int]]:
    con = sqlite3.connect(f"file:{shard_map_db}?mode=ro", uri=True)
    cur = con.cursor()
    by_shard: Dict[int, List[int]] = {}
    try:
        for part in chunked(book_ids, 900):
            placeholders = ",".join("?" for _ in part)
            sql = f"SELECT shard_id, book_id FROM book_shard WHERE book_id IN ({placeholders})"
            for shard_id, book_id in cur.execute(sql, tuple(part)):
                sid = int(shard_id)
                by_shard.setdefault(sid, []).append(int(book_id))
    finally:
        con.close()
    return by_shard


def shard_words_path(postings_path: str, words_db: Optional[str]) -> str:
    return words_db or postings_path


def _resolve_term_groups_local(curw, payload: Dict[str, Any]) -> List[List[int]]:
    max_variants = int(payload.get("maxVariants", 10) or 10)
    term_groups = payload.get("termGroups")
    terms = payload.get("terms")
    raw_groups: List[List[str]]
    if isinstance(term_groups, list) and term_groups:
        raw_groups = [[str(t) for t in g] for g in term_groups if g]
    elif isinstance(terms, list) and terms:
        raw_groups = [[str(t)] for t in terms if str(t).strip()]
    else:
        return []

    groups: List[List[int]] = []
    for g in raw_groups:
        cf_ids: List[int] = []
        for term in g:
            t = term.strip()
            if not t:
                continue
            if t.endswith("*"):
                prefix = t[:-1]
                if not prefix:
                    continue
                rows = curw.execute(
                    """
                    SELECT cf_id
                    FROM words
                    WHERE word >= ? AND word < ?
                    GROUP BY cf_id
                    ORDER BY total_tf DESC
                    LIMIT ?
                    """,
                    (prefix, f"{prefix}\uffff", max_variants),
                ).fetchall()
                cf_ids.extend(int(r[0]) for r in rows)
            else:
                cf = get_cf_id(curw, t)
                if cf is not None:
                    cf_ids.append(int(cf))
        cf_ids = sorted(set(cf_ids))
        if not cf_ids:
            return []
        groups.append(cf_ids)
    return groups


def _worker(task: Dict[str, Any]) -> Dict[str, Any]:
    endpoint = task["endpoint"]
    payload = task["payload"]
    postings_path = task["postings_path"]
    words_path = task["words_path"]
    ext_path = task["ext_path"]
    sidecar_path = task.get("sidecar_path")
    filter_ids = task["filter_ids"]
    schema = str(payload.get("schema", "unigrams") or "unigrams")
    match_mode = str(payload.get("matchMode", "near") or "near").lower()
    window = int(payload.get("window", 5) or 5)
    symmetric = bool(payload.get("symmetric", True))
    exclude_self = bool(payload.get("excludeSelf", False))
    before = int(payload.get("before", 5) or 5)
    after = int(payload.get("after", 5) or 5)
    per_book = int(payload.get("perBook", 3) or 3)
    total_limit = int(payload.get("totalLimit", 0) or 0)
    doc_samples = int(payload.get("docSamples", 0) or 0)

    con = connect_postings(postings_path, ext_path, sidecar_path)
    conw = connect_words(words_path)
    try:
        cur = con.cursor()
        curw = conw.cursor()
        groups = _resolve_term_groups_local(curw, payload)
        if not groups:
            return {"status": "ok", "total": 0, "docs": 0, "rows": [], "pid": os.getpid(), "shard": postings_path}

        book_ids = candidate_books_for_groups(cur, groups, schema=schema, base_filter_ids=filter_ids)
        if doc_samples > 0 and len(book_ids) > doc_samples:
            book_ids = random.sample(book_ids, doc_samples)

        off_min = -window if symmetric else 1
        off_max = window
        total = 0
        docs = 0
        rows: List[Dict[str, Any]] = []

        for book_id in book_ids:
            gp = group_positions_for_book(cur, groups, int(book_id), schema=schema)
            if match_mode == "sequence":
                positions = sequence_positions_from_groups(gp)
            else:
                positions = near_positions_from_groups(gp, off_min, off_max, exclude_self)
            if not positions:
                continue
            if endpoint == "/near_query":
                if match_mode == "sequence":
                    c = sequence_count_from_groups(gp)
                else:
                    c = near_count_from_groups(gp, off_min, off_max, exclude_self)
                total += int(c)
                docs += 1
                continue

            docs += 1
            total += len(positions)
            sampled = positions if len(positions) <= per_book else random.sample(positions, per_book)
            for pos in sampled:
                row: Dict[str, Any] = {"bookId": int(book_id), "pos": int(pos)}
                if endpoint == "/near_fragments":
                    row["frag"] = fetch_window(cur, curw, int(book_id), int(pos), before, after)
                rows.append(row)
                if total_limit and len(rows) >= total_limit:
                    break
            if total_limit and len(rows) >= total_limit:
                break

        return {
            "status": "ok",
            "total": int(total),
            "docs": int(docs),
            "rows": rows,
            "pid": os.getpid(),
            "shard": postings_path,
        }
    finally:
        con.close()
        conw.close()


def main() -> int:
    args = parse_args()
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    postings_dbs = [str(p) for p in cfg.get("postings_dbs", [])]
    if not postings_dbs:
        raise SystemExit("No postings_dbs in config.")
    words_db = str(cfg.get("words_db") or "").strip() or None
    ext_path = str(cfg.get("ext_path") or "")
    sidecar_dbs = cfg.get("sidecar_dbs") or []

    payload = json.loads(Path(args.payload_json).read_text(encoding="utf-8"))
    filter_ids = read_filter_ids_csv(Path(args.filter_ids_csv))
    by_shard: Dict[int, List[int]] = {}
    if filter_ids:
        by_shard = split_ids_by_shard(Path(args.shard_map_db), filter_ids)
    tasks: List[Dict[str, Any]] = []
    for shard_index, postings_path in enumerate(postings_dbs):
        shard_filter = by_shard.get(shard_index, [])
        # Empty corpus/filter means fullscan: dispatch every shard with no filter.
        if filter_ids and not shard_filter:
            continue
        task = {
            "endpoint": args.endpoint,
            "payload": payload,
            "postings_path": postings_path,
            "words_path": shard_words_path(postings_path, words_db),
            "ext_path": ext_path,
            "sidecar_path": sidecar_dbs[shard_index] if shard_index < len(sidecar_dbs) else None,
            "filter_ids": shard_filter,
        }
        tasks.append(task)

    if not tasks:
        print(json.dumps({"total": 0, "docs": 0, "rows": [], "_meta": {"tasks": 0}}, ensure_ascii=False, indent=2))
        return 0

    workers = args.max_workers if args.max_workers > 0 else len(tasks)
    workers = max(1, min(workers, len(tasks)))
    ctx = mp.get_context("spawn")
    with ctx.Pool(processes=workers) as pool:
        results = pool.map(_worker, tasks)

    total = sum(int(r.get("total", 0)) for r in results)
    docs = sum(int(r.get("docs", 0)) for r in results)
    rows: List[Dict[str, Any]] = []
    if args.endpoint in {"/near_fragments", "/near_hits"}:
        for r in results:
            rr = r.get("rows")
            if isinstance(rr, list):
                rows.extend(rr)
        total_limit = int(payload.get("totalLimit", 0) or 0)
        if total_limit > 0 and len(rows) > total_limit:
            rows = rows[:total_limit]

    out: Dict[str, Any] = {"total": int(total), "docs": int(docs)}
    if args.endpoint in {"/near_fragments", "/near_hits"}:
        out["rows"] = rows
    out["_perf"] = {
        "workers": [{"pid": r.get("pid"), "shard": r.get("shard")} for r in results],
        "tasks": len(tasks),
        "requested_filter_ids": len(filter_ids),
        "fullscan_mode": len(filter_ids) == 0,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
