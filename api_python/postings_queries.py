from __future__ import annotations

import json
import random
import sqlite3
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


def connect_postings(db_path: str, ext_path: str) -> sqlite3.Connection:
    if not Path(db_path).exists():
        raise FileNotFoundError(f"Postings DB not found: {db_path}")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.enable_load_extension(True)
    if ext_path:
        con.execute("SELECT load_extension(?, ?)", (ext_path, "sqlite3_postings_init"))
    return con


def connect_words(db_path: str) -> sqlite3.Connection:
    if not Path(db_path).exists():
        raise FileNotFoundError(f"Words DB not found: {db_path}")
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def get_cf_id(curw: sqlite3.Cursor, word: str) -> Optional[int]:
    w = word.casefold()
    row = curw.execute(
        "SELECT cf_id FROM words WHERE word = ? ORDER BY raw_id LIMIT 1", (w,)
    ).fetchone()
    if row:
        return row[0]
    row = curw.execute(
        "SELECT cf_id FROM words WHERE word = ? ORDER BY raw_id LIMIT 1", (word,)
    ).fetchone()
    return row[0] if row else None


def _docpost_union_blob(cur: sqlite3.Cursor, cf_ids: List[int]) -> Optional[bytes]:
    if not cf_ids:
        return None
    placeholders = ",".join("?" for _ in cf_ids)
    try:
        row = cur.execute(
            f"""
            WITH u AS (SELECT post AS all_docs FROM urns_postings WHERE id = 1)
            SELECT post_union_agg(
                CASE
                    WHEN w.docpost_is_complement = 1 THEN post_complement(w.docpost, u.all_docs)
                    ELSE w.docpost
                END
            )
            FROM words w, u
            WHERE w.cf_id IN ({placeholders})
            """,
            cf_ids,
        ).fetchone()
    except sqlite3.OperationalError:
        try:
            row = cur.execute(
                f"""
                SELECT post_union_agg(docpost)
                FROM words
                WHERE cf_id IN ({placeholders})
                  AND docpost_is_complement = 0
                """,
                cf_ids,
            ).fetchone()
        except sqlite3.OperationalError:
            # Some local/legacy DBs do not have docpost columns at all.
            # In that case we disable docpost prefiltering and let callers
            # fall back to urn sampling / no prefilter.
            return None
    if not row:
        return None
    return row[0]


def _intersect_blobs(cur: sqlite3.Cursor, blobs: List[bytes]) -> Optional[bytes]:
    out: Optional[bytes] = None
    for blob in blobs:
        if not blob:
            return None
        if out is None:
            out = blob
            continue
        row = cur.execute("SELECT post_intersect_blob(?, ?)", (out, blob)).fetchone()
        if not row:
            return None
        out = row[0]
    return out


def docpost_book_ids(cur: sqlite3.Cursor, cf_groups: List[List[int]]) -> Optional[List[int]]:
    if not cf_groups:
        return []
    blobs: List[bytes] = []
    for group in cf_groups:
        blob = _docpost_union_blob(cur, group)
        if not blob:
            continue
        blobs.append(blob)
    if not blobs:
        return None
    inter = _intersect_blobs(cur, blobs)
    if not inter:
        return []
    row = cur.execute("SELECT post_positions(?)", (inter,)).fetchone()
    positions = json.loads(row[0]) if row and row[0] else []
    return [int(p) for p in positions]


def docpost_sample_book_ids(
    cur: sqlite3.Cursor,
    cf_groups: List[List[int]],
    sample_n: int,
    seed: int = 0,
) -> Tuple[List[int], int]:
    if not cf_groups:
        return [], 0
    blobs: List[bytes] = []
    for group in cf_groups:
        blob = _docpost_union_blob(cur, group)
        if not blob:
            continue
        blobs.append(blob)
    if not blobs:
        return [], -1
    inter = _intersect_blobs(cur, blobs)
    if not inter:
        return [], 0
    row = cur.execute("SELECT post_count(?)", (inter,)).fetchone()
    total = int(row[0] or 0) if row else 0
    if total <= 0:
        return [], 0
    n = min(sample_n, total)
    rng = random.Random(seed)
    if n == total:
        indices = list(range(total))
    else:
        indices = rng.sample(range(total), n)
    out: List[int] = []
    for idx in indices:
        row = cur.execute("SELECT post_sample(?, ?)", (inter, idx)).fetchone()
        if row is None or row[0] is None:
            continue
        out.append(int(row[0]))
    return out, total


def sample_urns(cur: sqlite3.Cursor, n: int) -> List[int]:
    if n <= 0:
        return []
    rows = cur.execute(
        "SELECT book_id FROM urns ORDER BY random() LIMIT ?", (n,)
    ).fetchall()
    return [int(r[0]) for r in rows]


def raw_words(curw: sqlite3.Cursor, raw_ids: Iterable[int]) -> Dict[int, str]:
    ids = list(raw_ids)
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = curw.execute(
        f"SELECT raw_id, word FROM words WHERE raw_id IN ({placeholders})", ids
    ).fetchall()
    return {rid: w for rid, w in rows}


def fetch_window(
    cur: sqlite3.Cursor,
    curw: sqlite3.Cursor,
    book_id: int,
    center: int,
    before: int,
    after: int,
) -> str:
    inner = cur.connection.cursor()
    start = max(center - before, 0)
    end = center + after
    rows = inner.execute(
        """
        SELECT seq, raw_id
        FROM tokens
        WHERE book_id = ? AND seq BETWEEN ? AND ?
        ORDER BY seq
        """,
        (book_id, start, end),
    ).fetchall()
    raw_map = raw_words(curw, [r[1] for r in rows])
    tokens = []
    for seq, raw_id in rows:
        w = raw_map.get(raw_id, "?")
        if seq == center:
            tokens.append(f"[{w}]")
        else:
            tokens.append(w)
    return " ".join(tokens)


def sample_concordance_single(
    cur: sqlite3.Cursor,
    curw: sqlite3.Cursor,
    cf_id: int,
    per_book: int,
    before: int,
    after: int,
    use_filter: bool,
    filter_json: Optional[str],
) -> List[Tuple[int, int, str]]:
    inner = cur.connection.cursor()
    if use_filter:
        sql = """
            SELECT u.book_id, u.tf, u.post
            FROM json_each(?) f
            JOIN unigrams u ON u.book_id = f.value
            WHERE u.cf_id = ?
        """
    else:
        sql = "SELECT book_id, tf, post FROM unigrams WHERE cf_id = ?"
    out = []
    params = (filter_json, cf_id) if use_filter else (cf_id,)
    for book_id, tf, post in cur.execute(sql, params):
        if tf <= 0:
            continue
        samples = min(per_book, tf)
        for _ in range(samples):
            idx = random.randrange(tf)
            row = inner.execute("SELECT post_sample(?, ?)", (post, idx)).fetchone()
            if row is None or row[0] is None:
                continue
            pos = int(row[0])
            frag = fetch_window(cur, curw, book_id, pos, before, after)
            out.append((book_id, pos, frag))
    return out


def sample_concordance_union(
    cur: sqlite3.Cursor,
    curw: sqlite3.Cursor,
    cf_ids: List[int],
    per_book: int,
    before: int,
    after: int,
    use_filter: bool,
    filter_json: Optional[str],
) -> List[Tuple[int, int, str]]:
    if not cf_ids:
        return []
    inner = cur.connection.cursor()
    placeholders = ",".join("?" for _ in cf_ids)
    if use_filter:
        sql = f"""
            SELECT u.book_id, post_union_agg(u.post) AS post
            FROM json_each(?) f
            JOIN unigrams u ON u.book_id = f.value
            WHERE u.cf_id IN ({placeholders})
            GROUP BY u.book_id
        """
        params = (filter_json, *cf_ids)
    else:
        sql = f"""
            SELECT u.book_id, post_union_agg(u.post) AS post
            FROM unigrams u
            WHERE u.cf_id IN ({placeholders})
            GROUP BY u.book_id
        """
        params = tuple(cf_ids)
    out: List[Tuple[int, int, str]] = []
    for book_id, post in cur.execute(sql, params):
        if not post:
            continue
        cnt_row = inner.execute("SELECT post_count(?)", (post,)).fetchone()
        total = int(cnt_row[0] or 0) if cnt_row else 0
        if total <= 0:
            continue
        samples = min(per_book, total)
        indices = random.sample(range(total), samples)
        for idx in indices:
            pos_row = inner.execute("SELECT post_sample(?, ?)", (post, idx)).fetchone()
            if pos_row is None or pos_row[0] is None:
                continue
            pos = int(pos_row[0])
            frag = fetch_window(cur, curw, book_id, pos, before, after)
            out.append((book_id, pos, frag))
    return out


def sample_concordance_near(
    cur: sqlite3.Cursor,
    curw: sqlite3.Cursor,
    cf_a: int,
    cf_b: int,
    per_book: int,
    before: int,
    after: int,
    use_filter: bool,
    filter_json: Optional[str],
    ngrams_table: str,
    off_min: int,
    off_max: int,
    exclude_self: bool,
) -> List[Tuple[int, int, str]]:
    inner = cur.connection.cursor()
    if use_filter:
        sql = f"""
            SELECT a.book_id, a.post, b.post
            FROM json_each(?) f
            JOIN {ngrams_table} a ON a.book_id = f.value
            JOIN {ngrams_table} b ON b.book_id = f.value
            WHERE a.cf_id = ? AND b.cf_id = ?
        """
    else:
        sql = f"""
            SELECT a.book_id, a.post, b.post
            FROM {ngrams_table} a
            JOIN {ngrams_table} b ON a.book_id = b.book_id
            WHERE a.cf_id = ? AND b.cf_id = ?
        """
    out = []
    params = (filter_json, cf_a, cf_b) if use_filter else (cf_a, cf_b)
    for book_id, post_a, post_b in cur.execute(sql, params):
        if exclude_self and cf_a == cf_b and off_min == 0 and off_max == 0:
            row = inner.execute(
                "SELECT post_near_positions_blob(?, ?, ?, ?)", (post_a, post_b, 1, 1)
            ).fetchone()
        else:
            row = inner.execute(
                "SELECT post_near_positions_blob(?, ?, ?, ?)",
                (post_a, post_b, off_min, off_max),
            ).fetchone()
        if row is None or row[0] is None:
            continue
        blob = row[0]
        cnt_row = inner.execute("SELECT post_count(?)", (blob,)).fetchone()
        total = int(cnt_row[0] or 0) if cnt_row else 0
        if total <= 0:
            continue
        samples = min(per_book, total)
        indices = random.sample(range(total), samples)
        for idx in indices:
            pos_row = inner.execute("SELECT post_sample(?, ?)", (blob, idx)).fetchone()
            if pos_row is None or pos_row[0] is None:
                continue
            pos = int(pos_row[0])
            frag = fetch_window(cur, curw, book_id, pos, before, after)
            out.append((book_id, int(pos), frag))
    return out


def near_frequency(
    cur: sqlite3.Cursor,
    cf_a: int,
    cf_b: int,
    window: int,
    use_filter: bool,
    filter_json: Optional[str],
    ngrams_table: str,
    symmetric: bool,
    exclude_self: bool,
) -> Tuple[int, int]:
    inner = cur.connection.cursor()
    if use_filter:
        sql = f"""
            SELECT a.book_id, a.post, b.post
            FROM json_each(?) f
            JOIN {ngrams_table} a ON a.book_id = f.value
            JOIN {ngrams_table} b ON b.book_id = f.value
            WHERE a.cf_id = ? AND b.cf_id = ?
        """
    else:
        sql = f"""
            SELECT a.book_id, a.post, b.post
            FROM {ngrams_table} a
            JOIN {ngrams_table} b ON a.book_id = b.book_id
            WHERE a.cf_id = ? AND b.cf_id = ?
        """
    total = 0
    docs = 0
    params = (filter_json, cf_a, cf_b) if use_filter else (cf_a, cf_b)
    for _, post_a, post_b in cur.execute(sql, params):
        if cf_a == cf_b:
            if exclude_self:
                cnt = inner.execute(
                    "SELECT post_near_count(?, ?, 1, ?)", (post_a, post_b, window)
                ).fetchone()[0]
            elif symmetric:
                cnt = inner.execute(
                    "SELECT post_intersect_offset_sym(?, ?, ?, ?)",
                    (post_a, post_b, -window, window),
                ).fetchone()[0]
            else:
                cnt = inner.execute(
                    "SELECT post_near_count(?, ?, 1, ?)", (post_a, post_b, window)
                ).fetchone()[0]
        else:
            if symmetric:
                cnt_ab = inner.execute(
                    "SELECT post_near_count(?, ?, 1, ?)", (post_a, post_b, window)
                ).fetchone()[0]
                cnt_ba = inner.execute(
                    "SELECT post_near_count(?, ?, 1, ?)", (post_b, post_a, window)
                ).fetchone()[0]
                cnt = cnt_ab + cnt_ba
            else:
                cnt = inner.execute(
                    "SELECT post_near_count(?, ?, 1, ?)", (post_a, post_b, window)
                ).fetchone()[0]
        total += cnt
        if cnt > 0:
            docs += 1
    return total, docs


def sample_collocations(
    cur: sqlite3.Cursor,
    curw: sqlite3.Cursor,
    cf_id: int,
    per_book: int,
    before: int,
    after: int,
    use_filter: bool,
    filter_json: Optional[str],
    ngrams_table: str,
) -> Dict[str, int]:
    inner = cur.connection.cursor()
    if use_filter:
        sql = f"""
            SELECT u.book_id, u.tf, u.post
            FROM json_each(?) f
            JOIN {ngrams_table} u ON u.book_id = f.value
            WHERE u.cf_id = ?
        """
    else:
        sql = f"SELECT book_id, tf, post FROM {ngrams_table} WHERE cf_id = ?"
    counts: Dict[str, int] = {}
    params = (filter_json, cf_id) if use_filter else (cf_id,)
    for book_id, tf, post in cur.execute(sql, params):
        if tf <= 0:
            continue
        samples = min(per_book, tf)
        for _ in range(samples):
            idx = random.randrange(tf)
            row = inner.execute("SELECT post_sample(?, ?)", (post, idx)).fetchone()
            if row is None or row[0] is None:
                continue
            pos = int(row[0])
            rows = inner.execute(
                """
                SELECT raw_id
                FROM tokens
                WHERE book_id = ? AND seq BETWEEN ? AND ?
                ORDER BY seq
                """,
                (book_id, max(pos - before, 0), pos + after),
            ).fetchall()
            raw_map = raw_words(curw, [r[0] for r in rows])
            for (raw_id,) in rows:
                w = raw_map.get(raw_id, "?").casefold()
                counts[w] = counts.get(w, 0) + 1
    return counts
