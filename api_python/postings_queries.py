from __future__ import annotations

import json
import random
import sqlite3
from bisect import bisect_left, bisect_right
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

try:
    from pyroaring import BitMap as RoaringBitMap
except Exception:  # pragma: no cover - optional runtime dependency
    RoaringBitMap = None

_CODEC_CACHE: Dict[int, str] = {}


def connect_postings(
    db_path: str, ext_path: str, sidecar_path: Optional[str] = None
) -> sqlite3.Connection:
    if not Path(db_path).exists():
        raise FileNotFoundError(f"Postings DB not found: {db_path}")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.enable_load_extension(True)
    if ext_path:
        con.execute("SELECT load_extension(?, ?)", (ext_path, "sqlite3_postings_init"))
    if sidecar_path and Path(sidecar_path).exists():
        sidecar_uri = f"file:{sidecar_path}?mode=ro&immutable=1"
        try:
            con.execute("ATTACH DATABASE ? AS sidecar", (sidecar_uri,))
        except sqlite3.OperationalError:
            # Some SQLite builds may not treat ATTACH parameter as URI.
            # Fallback to plain path attach.
            con.execute("ATTACH DATABASE ? AS sidecar", (str(sidecar_path),))
    return con


def connect_words(db_path: str) -> sqlite3.Connection:
    if not Path(db_path).exists():
        raise FileNotFoundError(f"Words DB not found: {db_path}")
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def get_cf_id(curw: sqlite3.Cursor, word: str) -> Optional[int]:
    for variant in _term_exact_variants(word):
        row = curw.execute(
            "SELECT cf_id FROM words WHERE word = ? ORDER BY raw_id LIMIT 1", (variant,)
        ).fetchone()
        if row:
            return row[0]
    return None


def _term_exact_variants(term: str) -> List[str]:
    """
    Build ordered exact-match variants for robust lookup when words.word stores
    cased forms (for example only 'Øysterdalen' and not always a lower-case
    duplicate).
    """
    t = str(term or "")
    if not t:
        return []
    out: List[str] = []
    for candidate in (t.casefold(), t, t.lower(), t.title(), t.upper()):
        if candidate and candidate not in out:
            out.append(candidate)
    return out


def _postings_codec(cur: sqlite3.Cursor) -> str:
    key = id(cur.connection)
    cached = _CODEC_CACHE.get(key)
    if cached:
        return cached
    codec = "legacy_varint"
    try:
        row = cur.execute(
            "SELECT value FROM meta WHERE key = 'postings_codec' LIMIT 1"
        ).fetchone()
        if row and row[0]:
            codec = str(row[0]).strip()
    except sqlite3.OperationalError:
        pass
    _CODEC_CACHE[key] = codec
    return codec


def detect_postings_codec(cur: sqlite3.Cursor) -> str:
    return _postings_codec(cur)


def _decode_positions_blob(cur: sqlite3.Cursor, blob: bytes) -> List[int]:
    if not blob:
        return []
    codec = _postings_codec(cur)
    if codec == "roaring_v1":
        if RoaringBitMap is None:
            raise RuntimeError("pyroaring is required for roaring_v1 postings decode")
        return list(RoaringBitMap.deserialize(blob))
    row = cur.execute("SELECT post_positions(?)", (blob,)).fetchone()
    if not row or not row[0]:
        return []
    return [int(x) for x in json.loads(row[0])]


def _all_doc_ids(cur: sqlite3.Cursor) -> List[int]:
    # Fast path for shard-level doc universe.
    row = cur.execute("SELECT post FROM urns_postings WHERE id = 1").fetchone()
    if row and row[0]:
        return _decode_positions_blob(cur, row[0])
    rows = cur.execute("SELECT book_id FROM urns ORDER BY book_id").fetchall()
    return [int(r[0]) for r in rows]


def _docpost_union_ids(cur: sqlite3.Cursor, cf_ids: List[int]) -> Optional[List[int]]:
    if not cf_ids:
        return None
    placeholders = ",".join("?" for _ in cf_ids)
    try:
        rows = cur.execute(
            f"""
            SELECT cf_id, docpost, docpost_is_complement
            FROM words
            WHERE cf_id IN ({placeholders})
            GROUP BY cf_id
            """,
            tuple(cf_ids),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    if not rows:
        return None
    all_docs_cache: Optional[set[int]] = None
    out: set[int] = set()
    for _, blob, is_complement in rows:
        ids = set(_decode_positions_blob(cur, blob))
        if int(is_complement or 0) == 1:
            if all_docs_cache is None:
                all_docs_cache = set(_all_doc_ids(cur))
            ids = all_docs_cache - ids
        out |= ids
    return sorted(out)


def _intersect_sorted_lists(lists: List[List[int]]) -> List[int]:
    if not lists:
        return []
    out = set(lists[0])
    for arr in lists[1:]:
        out &= set(arr)
        if not out:
            return []
    return sorted(out)


def _positions_sample(positions: List[int], n: int) -> List[int]:
    if not positions:
        return []
    if n <= 0:
        return positions
    if len(positions) <= n:
        return positions
    return random.sample(positions, n)


def _union_roaring_posts(blobs: List[bytes]) -> List[int]:
    if not blobs:
        return []
    if RoaringBitMap is None:
        raise RuntimeError("pyroaring is required for roaring_v1 postings union")
    out = RoaringBitMap()
    for blob in blobs:
        if blob:
            out |= RoaringBitMap.deserialize(blob)
    return list(out)


def _union_positions_for_book(cur: sqlite3.Cursor, cf_ids: List[int], book_id: int) -> List[int]:
    placeholders = ",".join("?" for _ in cf_ids)
    rows = cur.execute(
        f"SELECT post FROM unigrams WHERE book_id = ? AND cf_id IN ({placeholders})",
        (book_id, *cf_ids),
    ).fetchall()
    blobs = [r[0] for r in rows if r and r[0]]
    if not blobs:
        return []
    if _postings_codec(cur) == "roaring_v1":
        return _union_roaring_posts(blobs)
    positions: List[int] = []
    seen = set()
    for blob in blobs:
        for p in _decode_positions_blob(cur, blob):
            if p not in seen:
                seen.add(p)
                positions.append(p)
    positions.sort()
    return positions


def candidate_books_for_groups(
    cur: sqlite3.Cursor,
    groups: List[List[int]],
    schema: str = "unigrams",
    base_filter_ids: Optional[List[int]] = None,
) -> List[int]:
    if not groups:
        return []
    filter_set = set(base_filter_ids) if base_filter_ids else None
    per_group: List[set[int]] = []
    for group in groups:
        if not group:
            return []
        placeholders = ",".join("?" for _ in group)
        rows = cur.execute(
            f"SELECT DISTINCT book_id FROM {schema} WHERE cf_id IN ({placeholders})",
            tuple(group),
        ).fetchall()
        s = {int(r[0]) for r in rows}
        if filter_set is not None:
            s &= filter_set
        per_group.append(s)
    out = per_group[0]
    for s in per_group[1:]:
        out &= s
        if not out:
            return []
    return sorted(out)


def group_positions_for_book(
    cur: sqlite3.Cursor,
    groups: List[List[int]],
    book_id: int,
    schema: str = "unigrams",
) -> List[List[int]]:
    if schema != "unigrams":
        # Current roaring runtime fallback is scoped to unigrams.
        return []
    return [_union_positions_for_book(cur, g, book_id) for g in groups]


def _has_pos_in_window(positions: List[int], center: int, off_min: int, off_max: int) -> bool:
    if not positions:
        return False
    lo = center + off_min
    hi = center + off_max
    i = bisect_left(positions, lo)
    return i < len(positions) and positions[i] <= hi


def near_positions_from_groups(
    group_positions: List[List[int]],
    off_min: int,
    off_max: int,
    exclude_self: bool = False,
) -> List[int]:
    if not group_positions or len(group_positions) < 2:
        return []
    anchor = group_positions[0]
    if not anchor:
        return []
    others = group_positions[1:]
    same_group_mode = (
        exclude_self
        and len(group_positions) == 2
        and off_min <= 0 <= off_max
        and group_positions[0] == group_positions[1]
    )
    out: List[int] = []
    for p in anchor:
        ok = True
        for g in others:
            if same_group_mode:
                lo = p + off_min
                hi = p + off_max
                i = bisect_left(g, lo)
                j = bisect_right(g, hi)
                # Require at least one neighbor different from anchor position.
                found_other = any(g[k] != p for k in range(i, j))
                if not found_other:
                    ok = False
                    break
                continue
            if not _has_pos_in_window(g, p, off_min, off_max):
                ok = False
                break
        if ok:
            out.append(int(p))
    return out


def near_count_from_groups(
    group_positions: List[List[int]],
    off_min: int,
    off_max: int,
    exclude_self: bool = False,
) -> int:
    return len(near_positions_from_groups(group_positions, off_min, off_max, exclude_self))


def sequence_positions_from_groups(
    group_positions: List[List[int]],
) -> List[int]:
    """
    Return anchor positions for strict sequence matching with step=1.
    Example for 3 groups: require p in g1, p+1 in g2, p+2 in g3.
    """
    if not group_positions or len(group_positions) < 2:
        return []
    anchor = group_positions[0]
    if not anchor:
        return []
    others = [set(g) for g in group_positions[1:]]
    out: List[int] = []
    for p in anchor:
        ok = True
        for i, s in enumerate(others, start=1):
            if (p + i) not in s:
                ok = False
                break
        if ok:
            out.append(int(p))
    return out


def sequence_count_from_groups(
    group_positions: List[List[int]],
) -> int:
    return len(sequence_positions_from_groups(group_positions))


def _sample_position_from_blob(cur: sqlite3.Cursor, blob: bytes, n: int) -> List[int]:
    positions = _decode_positions_blob(cur, blob)
    return _positions_sample(positions, n)


def _decode_count_from_blob(cur: sqlite3.Cursor, blob: bytes) -> int:
    return len(_decode_positions_blob(cur, blob))


def _decode_positions_intersect(cur: sqlite3.Cursor, blobs: List[bytes]) -> List[int]:
    if not blobs:
        return []
    lists = [_decode_positions_blob(cur, b) for b in blobs if b]
    if not lists:
        return []
    return _intersect_sorted_lists(lists)


def _docpost_book_ids_python(cur: sqlite3.Cursor, cf_groups: List[List[int]]) -> Optional[List[int]]:
    if not cf_groups:
        return []
    group_ids: List[List[int]] = []
    for group in cf_groups:
        ids = _docpost_union_ids(cur, group)
        if ids is None:
            continue
        group_ids.append(ids)
    if not group_ids:
        return None
    return _intersect_sorted_lists(group_ids)


def docpost_book_ids(cur: sqlite3.Cursor, cf_groups: List[List[int]]) -> Optional[List[int]]:
    return _docpost_book_ids_python(cur, cf_groups)


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


def _decode_varints(blob: bytes, max_items: Optional[int] = None) -> List[int]:
    out: List[int] = []
    i = 0
    n = len(blob)
    while i < n:
        shift = 0
        value = 0
        while True:
            if i >= n:
                return out
            b = blob[i]
            i += 1
            value |= (b & 0x7F) << shift
            if (b & 0x80) == 0:
                break
            shift += 7
        out.append(value)
        if max_items is not None and len(out) >= max_items:
            return out
    return out


def _has_sidecar(cur: sqlite3.Cursor) -> bool:
    try:
        rows = cur.execute("PRAGMA database_list").fetchall()
    except sqlite3.OperationalError:
        return False
    return any(str(r[1]) == "sidecar" for r in rows)


def _fetch_raw_window_rows_from_blocks(
    cur: sqlite3.Cursor, book_id: int, start: int, end: int
) -> List[Tuple[int, int]]:
    if not _has_sidecar(cur):
        raise sqlite3.OperationalError("tokens table missing and no sidecar attached")
    rows = cur.execute(
        """
        SELECT block_start, block_len, raw_ids
        FROM sidecar.token_blocks
        WHERE book_id = ?
          AND block_start <= ?
          AND (block_start + block_len - 1) >= ?
        ORDER BY block_start
        """,
        (book_id, end, start),
    ).fetchall()
    out: List[Tuple[int, int]] = []
    for block_start, block_len, raw_blob in rows:
        raw_ids = _decode_varints(raw_blob, int(block_len))
        bstart = int(block_start)
        blen = int(block_len)
        for i, raw_id in enumerate(raw_ids[:blen]):
            seq = bstart + i
            if seq < start:
                continue
            if seq > end:
                break
            out.append((seq, int(raw_id)))
    return out


def _fetch_raw_window_rows(
    cur: sqlite3.Cursor, book_id: int, start: int, end: int
) -> List[Tuple[int, int]]:
    try:
        return cur.execute(
            """
            SELECT seq, raw_id
            FROM tokens
            WHERE book_id = ? AND seq BETWEEN ? AND ?
            ORDER BY seq
            """,
            (book_id, start, end),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table: tokens" not in str(exc).lower():
            raise
        return _fetch_raw_window_rows_from_blocks(cur, book_id, start, end)


def _fetch_window_token_parts(
    cur: sqlite3.Cursor,
    curw: sqlite3.Cursor,
    book_id: int,
    center: int,
    before: int,
    after: int,
    span_len: int = 1,
) -> Tuple[List[str], List[str], List[str]]:
    span_len = max(int(span_len or 1), 1)
    start = max(center - before, 0)
    end = center + max(after, span_len - 1)
    rows = _fetch_raw_window_rows(cur, book_id, start, end)
    raw_map = raw_words(curw, [r[1] for r in rows])
    span_end = center + span_len - 1
    before_tokens: List[str] = []
    hit_tokens: List[str] = []
    after_tokens: List[str] = []
    for seq, raw_id in rows:
        w = raw_map.get(raw_id, "?")
        if seq < center:
            before_tokens.append(w)
        elif center <= seq <= span_end:
            hit_tokens.append(w)
        else:
            after_tokens.append(w)
    return before_tokens, hit_tokens, after_tokens


def fetch_window(
    cur: sqlite3.Cursor,
    curw: sqlite3.Cursor,
    book_id: int,
    center: int,
    before: int,
    after: int,
    span_len: int = 1,
) -> str:
    before_tokens, hit_tokens, after_tokens = _fetch_window_token_parts(
        cur, curw, book_id, center, before, after, span_len=span_len
    )
    tokens: List[str] = []
    if before_tokens:
        tokens.extend(before_tokens)
    if hit_tokens:
        tokens.append(f"[{' '.join(hit_tokens)}]")
    if after_tokens:
        tokens.extend(after_tokens)
    return " ".join(tokens)


def fetch_window_structured(
    cur: sqlite3.Cursor,
    curw: sqlite3.Cursor,
    book_id: int,
    center: int,
    before: int,
    after: int,
    span_len: int = 1,
) -> Dict[str, object]:
    before_tokens, hit_tokens, after_tokens = _fetch_window_token_parts(
        cur, curw, book_id, center, before, after, span_len=span_len
    )
    hit_text = " ".join(hit_tokens)
    return {
        "bookId": int(book_id),
        "seqStart": int(center),
        "len": max(int(span_len or 1), 1),
        "before": " ".join(before_tokens),
        "hit": hit_text,
        "after": " ".join(after_tokens),
        "surface": hit_text,
    }


def sample_concordance_single(
    cur: sqlite3.Cursor,
    curw: sqlite3.Cursor,
    cf_id: int,
    per_book: int,
    before: int,
    after: int,
    use_filter: bool,
    filter_json: Optional[str],
    render_mode: str = "legacy",
) -> List[Tuple[int, int, object]]:
    if use_filter:
        sql = """
            SELECT u.book_id, u.tf, u.post
            FROM json_each(?) f
            JOIN unigrams u ON u.book_id = f.value
            WHERE u.cf_id = ?
        """
    else:
        sql = "SELECT book_id, tf, post FROM unigrams WHERE cf_id = ?"
    out: List[Tuple[int, int, object]] = []
    params = (filter_json, cf_id) if use_filter else (cf_id,)
    for book_id, _, post in cur.execute(sql, params):
        positions = _decode_positions_blob(cur, post)
        if not positions:
            continue
        for pos in _positions_sample(positions, per_book):
            frag: object
            if render_mode == "structured":
                frag = fetch_window_structured(cur, curw, book_id, pos, before, after)
            else:
                frag = fetch_window(cur, curw, book_id, pos, before, after)
            out.append((int(book_id), int(pos), frag))
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
    render_mode: str = "legacy",
) -> List[Tuple[int, int, object]]:
    if not cf_ids:
        return []
    placeholders = ",".join("?" for _ in cf_ids)
    if use_filter:
        book_rows = cur.execute(
            f"""
            SELECT DISTINCT u.book_id
            FROM json_each(?) f
            JOIN unigrams u ON u.book_id = f.value
            WHERE u.cf_id IN ({placeholders})
            """,
            (filter_json, *cf_ids),
        ).fetchall()
    else:
        book_rows = cur.execute(
            f"SELECT DISTINCT book_id FROM unigrams WHERE cf_id IN ({placeholders})",
            tuple(cf_ids),
        ).fetchall()
    out: List[Tuple[int, int, object]] = []
    for (book_id,) in book_rows:
        positions = _union_positions_for_book(cur, cf_ids, int(book_id))
        for pos in _positions_sample(positions, per_book):
            frag: object
            if render_mode == "structured":
                frag = fetch_window_structured(cur, curw, book_id, pos, before, after)
            else:
                frag = fetch_window(cur, curw, book_id, pos, before, after)
            out.append((int(book_id), int(pos), frag))
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
    render_mode: str = "legacy",
) -> List[Tuple[int, int, object]]:
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
    out: List[Tuple[int, int, object]] = []
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
        if per_book <= 0:
            indices = range(total)
        else:
            samples = min(per_book, total)
            indices = random.sample(range(total), samples)
        for idx in indices:
            pos_row = inner.execute("SELECT post_sample(?, ?)", (blob, idx)).fetchone()
            if pos_row is None or pos_row[0] is None:
                continue
            pos = int(pos_row[0])
            frag: object
            if render_mode == "structured":
                frag = fetch_window_structured(cur, curw, book_id, pos, before, after)
            else:
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


def near_partner_popcount(
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
    """
    Count matching partner tokens from the right-hand query term.

    For ordered near searches this counts B positions that have an A within the
    requested window to the left. For symmetric searches it counts B positions
    that have an A anywhere in [-window, +window].

    This is intentionally only well-defined for two different terms. Same-term
    queries should stay on the existing anchor-hit path until we add an explicit
    self-excluding participant metric.
    """
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
    if symmetric:
        off_min, off_max = -window, window
    else:
        # Count B positions that have an A to the left within the requested
        # ordered near window.
        off_min, off_max = -window, -1
    total = 0
    docs = 0
    params = (filter_json, cf_a, cf_b) if use_filter else (cf_a, cf_b)
    for _, post_a, post_b in cur.execute(sql, params):
        if cf_a == cf_b:
            cnt = 0
        elif exclude_self and off_min <= 0 <= off_max:
            cnt_left = inner.execute(
                "SELECT post_near_count(?, ?, ?, ?)", (post_b, post_a, off_min, -1)
            ).fetchone()[0]
            cnt_right = inner.execute(
                "SELECT post_near_count(?, ?, ?, ?)", (post_b, post_a, 1, off_max)
            ).fetchone()[0]
            cnt = cnt_left + cnt_right
        else:
            cnt = inner.execute(
                "SELECT post_near_count(?, ?, ?, ?)",
                (post_b, post_a, off_min, off_max),
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
            window_rows = _fetch_raw_window_rows(cur, book_id, max(pos - before, 0), pos + after)
            raw_map = raw_words(curw, [r[1] for r in window_rows])
            for _, raw_id in window_rows:
                w = raw_map.get(raw_id, "?").casefold()
                counts[w] = counts.get(w, 0) + 1
    return counts
