from __future__ import annotations

import random
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple


def _normalize_namespace_token(token: str) -> Optional[str]:
    tok = (token or "").strip()
    if not tok.startswith("#") or len(tok) < 2:
        return None
    return tok[1:].casefold()


def parse_namespace_query_token(token: str) -> Optional[Tuple[str, Optional[str]]]:
    tok = (token or "").strip()
    if not tok.startswith("#"):
        return None
    m = re.match(r"^#([A-Za-z0-9_]+)(?::(.+))?$", tok)
    if not m:
        return None
    namespace = m.group(1).casefold()
    raw_value = m.group(2)
    if raw_value is None:
        return namespace, None
    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"\"", "'"}:
        value = value[1:-1]
    value = value.strip()
    return namespace, value or None


def parse_namespace_query(
    terms: Optional[List[str]],
    term_groups: Optional[List[List[str]]],
) -> Tuple[Optional[str], Optional[str], bool]:
    if term_groups:
        source: Iterable[str] = (t for g in term_groups for t in g)
    else:
        source = terms or []
    ns: Optional[str] = None
    ns_value: Optional[str] = None
    has_non_namespace = False
    for token in source:
        parsed = parse_namespace_query_token(token)
        if not parsed:
            if (token or "").strip():
                has_non_namespace = True
            continue
        cur_ns, cur_value = parsed
        if ns is None:
            ns = cur_ns
            ns_value = cur_value
        elif cur_ns != ns:
            raise ValueError("Only one annotation namespace per request is supported in v1")
        elif cur_value and ns_value and cur_value != ns_value:
            raise ValueError("Only one namespace value per request is supported in v1")
        elif cur_value and not ns_value:
            ns_value = cur_value
    return ns, ns_value, has_non_namespace


def extract_query_namespaces(
    terms: Optional[List[str]],
    term_groups: Optional[List[List[str]]],
) -> Tuple[Set[str], bool]:
    namespaces: Set[str] = set()
    has_non_namespace_terms = False
    if term_groups:
        source: Iterable[str] = (t for group in term_groups for t in group)
    else:
        source = terms or []
    for term in source:
        ns = _normalize_namespace_token(term)
        if ns:
            namespaces.add(ns)
        elif (term or "").strip():
            has_non_namespace_terms = True
    return namespaces, has_non_namespace_terms


def _connect_ro(db_path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def resolve_namespace(
    registry_db_path: str,
    namespace: str,
    base_dir: Optional[str] = None,
) -> Dict[str, Any]:
    con = _connect_ro(registry_db_path)
    try:
        row = con.execute(
            """
            SELECT namespace, db_path, version, resolver
            FROM annotation_namespaces
            WHERE namespace = ? AND active = 1
            LIMIT 1
            """,
            (namespace,),
        ).fetchone()
    finally:
        con.close()
    if not row:
        raise ValueError(f"Namespace not registered or inactive: #{namespace}")
    db_path_raw = str(row[1])
    db_path_obj = Path(db_path_raw)
    if not db_path_obj.is_absolute() and base_dir:
        db_path_obj = Path(base_dir) / db_path_obj
    db_path = str(db_path_obj)
    if not Path(db_path).exists():
        raise FileNotFoundError(f"Namespace DB not found for #{namespace}: {db_path}")
    return {
        "namespace": str(row[0]),
        "db_path": db_path,
        "version": str(row[2] or ""),
        "resolver": str(row[3] or ""),
    }


def resolve_namespace_books(
    registry_db_path: str,
    namespace: str,
    filter_ids: Optional[List[int]],
    doc_samples: int,
) -> List[int]:
    con = _connect_ro(registry_db_path)
    try:
        sql = """
            SELECT book_id
            FROM annotation_book_map
            WHERE namespace = ?
              AND coverage_status IN ('full', 'partial')
        """
        params: Sequence[Any]
        if filter_ids:
            placeholders = ",".join("?" for _ in filter_ids)
            sql += f" AND book_id IN ({placeholders})"
            params = (namespace, *filter_ids)
        else:
            params = (namespace,)
        rows = con.execute(sql, tuple(params)).fetchall()
    finally:
        con.close()
    book_ids = [int(r[0]) for r in rows]
    if doc_samples > 0 and len(book_ids) > doc_samples:
        return random.sample(book_ids, doc_samples)
    return book_ids


def fetch_geo_spans(
    geo_db_path: str,
    book_ids: List[int],
    total_limit: Optional[int],
    place_text_filter: Optional[str] = None,
) -> List[Dict[str, Any]]:
    if not book_ids:
        return []
    con = _connect_ro(geo_db_path)
    try:
        cur = con.cursor()
        cur.execute("CREATE TEMP TABLE _book_filter(book_id INTEGER PRIMARY KEY)")
        cur.executemany(
            "INSERT OR IGNORE INTO _book_filter(book_id) VALUES (?)",
            ((int(book_id),) for book_id in book_ids),
        )
        sql = """
            SELECT g.book_id,
                   g.seq_start,
                   g.token_len,
                   g.place_id,
                   g.variant_id,
                   g.surface_text,
                   g.score,
                   g.method,
                   p.canonical_name,
                   p.geonames_id,
                   p.lat,
                   p.lon,
                   p.country,
                   pv.variant_text
            FROM geo_spans g
            JOIN _book_filter f ON f.book_id = g.book_id
            LEFT JOIN places p ON p.place_id = g.place_id
            LEFT JOIN place_variants pv ON pv.variant_id = g.variant_id
        """
        params: List[Any] = []
        if place_text_filter:
            sql += """
            WHERE lower(COALESCE(g.surface_text, '')) = lower(?)
               OR lower(COALESCE(pv.variant_text, '')) = lower(?)
               OR lower(COALESCE(p.canonical_name, '')) = lower(?)
            """
            params.extend([place_text_filter, place_text_filter, place_text_filter])
        sql += " ORDER BY g.book_id, g.seq_start"
        if total_limit is not None and int(total_limit) > 0:
            sql += " LIMIT ?"
            params.append(int(total_limit))
        rows = cur.execute(sql, tuple(params)).fetchall()
    finally:
        con.close()
    out: List[Dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "bookId": int(row[0]),
                "seqStart": int(row[1]),
                "tokenLen": int(row[2]),
                "placeId": int(row[3]) if row[3] is not None else None,
                "variantId": int(row[4]) if row[4] is not None else None,
                "surfaceText": str(row[5]) if row[5] is not None else None,
                "score": float(row[6]) if row[6] is not None else None,
                "method": str(row[7]) if row[7] is not None else None,
                "place": {
                    "canonicalName": str(row[8]) if row[8] is not None else None,
                    "geonamesId": int(row[9]) if row[9] is not None else None,
                    "lat": float(row[10]) if row[10] is not None else None,
                    "lon": float(row[11]) if row[11] is not None else None,
                    "country": str(row[12]) if row[12] is not None else None,
                    "variantText": str(row[13]) if row[13] is not None else None,
                },
            }
        )
    return out


def fetch_geo_books_from_imagination(
    imagination_db_path: str,
    place_text_filter: str,
    filter_ids: Optional[List[int]],
    total_limit: Optional[int],
) -> List[Dict[str, Any]]:
    token = (place_text_filter or "").strip()
    if not token:
        return []
    con = _connect_ro(imagination_db_path)
    try:
        cur = con.cursor()
        params: List[Any] = [token, token, token]
        sql = """
            WITH wanted_tokens AS (
                SELECT DISTINCT lower(token) AS norm_token
                FROM places
                WHERE lower(token) = lower(?)
                   OR lower(modern) = lower(?)
                UNION
                SELECT lower(?)
            )
            SELECT
                b.dhlabid AS book_id,
                SUM(COALESCE(b.book_count, 0)) AS mention_count,
                MIN(b.token) AS any_token,
                MAX(b.geonameid) AS geonameid
            FROM books b
            JOIN wanted_tokens wt
              ON lower(b.token) = wt.norm_token
        """
        if filter_ids:
            placeholders = ",".join("?" for _ in filter_ids)
            sql += f" WHERE b.dhlabid IN ({placeholders})"
            params.extend(int(x) for x in filter_ids)
        sql += """
            GROUP BY b.dhlabid
            ORDER BY mention_count DESC, b.dhlabid
        """
        if total_limit is not None and int(total_limit) > 0:
            sql += " LIMIT ?"
            params.append(int(total_limit))
        rows = cur.execute(sql, tuple(params)).fetchall()

        place_meta = cur.execute(
            """
            SELECT modern, latitude, longitude, area, location_type
            FROM places
            WHERE lower(token) = lower(?) OR lower(modern) = lower(?)
            LIMIT 1
            """,
            (token, token),
        ).fetchone()
    finally:
        con.close()

    modern = str(place_meta[0]) if place_meta and place_meta[0] is not None else None
    lat = float(place_meta[1]) if place_meta and place_meta[1] not in (None, "") else None
    lon = float(place_meta[2]) if place_meta and place_meta[2] not in (None, "") else None
    country = str(place_meta[3]) if place_meta and place_meta[3] is not None else None
    method = "imagination_books_fallback"

    normalized_surface = token
    normalized_token_len = len([x for x in normalized_surface.split() if x])
    out: List[Dict[str, Any]] = []
    for book_id, mention_count, any_token, geonameid in rows:
        surface_variant = str(any_token) if any_token is not None else token
        out.append(
            {
                "bookId": int(book_id),
                # Positional data is unavailable in imagination.db (book-level aggregates only).
                "seqStart": -1,
                "tokenLen": normalized_token_len,
                "placeId": None,
                "variantId": None,
                "surfaceText": normalized_surface,
                "score": float(mention_count or 0),
                "method": method,
                "place": {
                    "canonicalName": modern or token,
                    "geonamesId": int(geonameid) if geonameid is not None else None,
                    "lat": lat,
                    "lon": lon,
                    "country": country,
                    "variantText": surface_variant,
                },
                "bookMentionCount": int(mention_count or 0),
                "coverageLevel": "book",
            }
        )
    return out

