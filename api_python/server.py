from __future__ import annotations

import json
import os
import random
import re
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from html import escape as html_escape
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from api_python.annotations import (
    fetch_geo_spans,
    fetch_geo_books_from_imagination,
    parse_namespace_query,
    resolve_namespace,
    resolve_namespace_books,
)
from api_python.config import load_config
from api_python.postings_queries import (
    candidate_books_for_groups,
    connect_postings,
    connect_words,
    detect_postings_codec,
    docpost_book_ids,
    fetch_window,
    group_positions_for_book,
    get_cf_id,
    near_count_from_groups,
    near_frequency,
    near_positions_from_groups,
    sequence_count_from_groups,
    sequence_positions_from_groups,
    sample_urns,
    sample_collocations,
    sample_concordance_near,
    sample_concordance_single,
    sample_concordance_union,
)

try:
    from pyroaring import BitMap as RoaringBitMap
except Exception:  # pragma: no cover - optional dependency
    RoaringBitMap = None

app = FastAPI(title="Postings API", version="0.1.0")

USE_BITMAP_NEAR = os.environ.get("POSTINGS_BITMAP_NEAR", "").strip() == "1"
BITMAP_CHUNK_SIZE = int(os.environ.get("POSTINGS_BITMAP_CHUNK", "4096"))
PROFILE_NEAR = os.environ.get("POSTINGS_PROFILE_NEAR", "").strip() == "1"
PREUNION_GROUPS = os.environ.get("POSTINGS_PREUNION_GROUPS", "").strip() == "1"
QUERY_ENGINE_DEFAULT = os.environ.get("POSTINGS_QUERY_ENGINE", "python").strip().lower()
JULIA_HYBRID_ENABLED = os.environ.get("POSTINGS_JULIA_HYBRID", "").strip() == "1"
JULIA_BIN = os.environ.get("JULIA_BIN", "julia")
_JULIA_PROBE_SCRIPT_ENV = os.environ.get("JULIA_PROBE_SCRIPT", "").strip()
if _JULIA_PROBE_SCRIPT_ENV:
    JULIA_PROBE_SCRIPT = _JULIA_PROBE_SCRIPT_ENV
elif os.path.exists("/app/api_julia/sqlite_blob_julia_probe.jl"):
    JULIA_PROBE_SCRIPT = "/app/api_julia/sqlite_blob_julia_probe.jl"
else:
    JULIA_PROBE_SCRIPT = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "api_julia", "sqlite_blob_julia_probe.jl")
    )
JULIA_TIMEOUT_SECONDS = int(os.environ.get("POSTINGS_JULIA_TIMEOUT", "120"))
JULIA_PARALLEL_SHARDS_DEFAULT = (
    os.environ.get("POSTINGS_JULIA_PARALLEL_SHARDS", "").strip() == "1"
)
JULIA_THREADS = os.environ.get("POSTINGS_JULIA_THREADS", "").strip()
JULIA_PROXY_URL = os.environ.get("POSTINGS_JULIA_PROXY_URL", "").strip().rstrip("/")
_PY_PAR = os.environ.get("POSTINGS_PYTHON_PARALLEL_SHARDS", "1").strip().lower()
PYTHON_PARALLEL_SHARDS_DEFAULT = _PY_PAR not in {"0", "false", "no", "off"}
PYTHON_SHARD_WORKERS = int(os.environ.get("POSTINGS_PYTHON_SHARD_WORKERS", "0") or 0)


@app.middleware("http")
async def allow_all_cors(request: Request, call_next):
    if request.method == "OPTIONS":
        return Response(
            status_code=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
                "Access-Control-Allow-Headers": "*",
            },
        )
    response = await call_next(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "*"
    return response
CONFIG = load_config()


def shard_words_path(postings_path: str) -> str:
    return CONFIG.words_db or postings_path


def shard_sidecar_path(postings_path: str, shard_index: int) -> Optional[str]:
    if CONFIG.sidecar_dbs and shard_index < len(CONFIG.sidecar_dbs):
        return CONFIG.sidecar_dbs[shard_index]

    main_name = Path(postings_path).name
    main_prefix = os.environ.get("POSTINGS_MAIN_PREFIX", "imag_roaring_main_")
    sidecar_prefix = os.environ.get("POSTINGS_SIDECAR_PREFIX", "imag_roaring_blk128_sidecar_")
    sidecar_dir = os.environ.get("POSTINGS_SIDECAR_DIR", "").strip()
    if not main_name.startswith(main_prefix):
        return None
    m = re.search(r"(\d+)\.db$", main_name)
    if not m:
        return None
    side_name = f"{sidecar_prefix}{m.group(1)}.db"
    if sidecar_dir:
        return str(Path(sidecar_dir) / side_name)
    return str(Path(postings_path).with_name(side_name))


class ConcordanceRequest(BaseModel):
    wordA: str
    wordB: Optional[str] = ""
    window: int = Field(5, ge=1, le=50)
    before: int = Field(5, ge=1, le=25)
    after: int = Field(5, ge=1, le=25)
    perBook: int = Field(3, ge=1, le=20)
    docSamples: Optional[int] = Field(None, ge=0, le=50000)
    totalLimit: int = Field(200, ge=1, le=5000)
    schema: Optional[str] = None
    useFilter: bool = False
    filterIds: List[int] = []
    symmetric: bool = True
    excludeSelf: bool = False


class NearFrequencyRequest(BaseModel):
    wordA: str
    wordB: str
    window: int = Field(5, ge=1, le=50)
    schema: Optional[str] = None
    symmetric: bool = True
    excludeSelf: bool = False
    useFilter: bool = False
    filterIds: List[int] = []
    docSamples: Optional[int] = Field(None, ge=0, le=50000)


class NearQueryRequest(BaseModel):
    terms: Optional[List[str]] = None
    termGroups: Optional[List[List[str]]] = None
    window: int = Field(5, ge=1, le=50)
    schema: Optional[str] = None
    symmetric: bool = True
    excludeSelf: bool = False
    useFilter: bool = False
    filterIds: List[int] = []
    docSamples: Optional[int] = Field(None, ge=0, le=50000)
    maxVariants: int = Field(10, ge=1, le=100)
    engine: Optional[str] = None
    parallelShards: Optional[bool] = None
    matchMode: Optional[str] = None


class OrQueryRequest(BaseModel):
    terms: List[str] = []
    termGroups: Optional[List[List[str]]] = None
    before: int = Field(5, ge=1, le=25)
    after: int = Field(5, ge=1, le=25)
    perBook: int = Field(3, ge=1, le=20)
    docSamples: Optional[int] = Field(None, ge=0, le=50000)
    totalLimit: int = Field(200, ge=1, le=5000)
    schema: Optional[str] = None
    useFilter: bool = False
    filterIds: List[int] = []
    maxVariants: int = Field(10, ge=1, le=100)
    parallelShards: Optional[bool] = None
    renderHits: bool = False


class PlacesRequest(BaseModel):
    dhlabids: List[int] = []
    maxPlaces: int = Field(5000, ge=1, le=20000)


class PlaceDetailsRequest(BaseModel):
    dhlabids: List[int] = []
    token: str
    limit: int = Field(1000, ge=1, le=20000)


def _connect_imagination_ro() -> sqlite3.Connection:
    db_path = (CONFIG.imagination_db or "").strip()
    if not db_path:
        raise HTTPException(
            status_code=500,
            detail="imagination_db is not configured in POSTINGS_CONFIG",
        )
    if not Path(db_path).exists():
        raise HTTPException(status_code=500, detail=f"imagination_db not found: {db_path}")
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def _table_columns(cur: sqlite3.Cursor, table: str) -> set[str]:
    cols: set[str] = set()
    try:
        for row in cur.execute(f"PRAGMA table_info({table})"):
            cols.add(str(row[1]))
    except sqlite3.Error:
        return set()
    return cols


def _pick_col(cols: set[str], candidates: List[str]) -> Optional[str]:
    for c in candidates:
        if c in cols:
            return c
    return None


def _optional_expr(cols: set[str], candidates: List[str], alias: str, cast: Optional[str] = None) -> str:
    col = _pick_col(cols, candidates)
    if not col:
        return f"NULL AS {alias}"
    if cast:
        return f"CAST({col} AS {cast}) AS {alias}"
    return f"{col} AS {alias}"


@app.get("/api/metadata/all")
def imagination_metadata_all():
    con = _connect_imagination_ro()
    try:
        cur = con.cursor()
        corpus_cols = _table_columns(cur, "corpus")
        if not corpus_cols:
            raise HTTPException(status_code=500, detail="imagination_db is missing table: corpus")
        id_col = _pick_col(corpus_cols, ["dhlabid", "book_id"])
        if not id_col:
            raise HTTPException(
                status_code=500,
                detail="corpus table must contain dhlabid (or book_id)",
            )
        year_expr = _optional_expr(corpus_cols, ["year", "pub_year"], "year", cast="INTEGER")
        author_expr = _optional_expr(corpus_cols, ["author", "forfatter"], "author")
        title_expr = _optional_expr(corpus_cols, ["title", "titel"], "title")
        category_expr = _optional_expr(corpus_cols, ["category", "genre"], "category")
        urn_expr = _optional_expr(corpus_cols, ["urn"], "urn")
        unique_places_expr = _optional_expr(corpus_cols, ["unique_places"], "unique_places_src", cast="INTEGER")
        total_mentions_expr = _optional_expr(
            corpus_cols, ["total_mentions"], "total_mentions_src", cast="INTEGER"
        )
        books_cols = _table_columns(cur, "books")
        books_dhlab_col = _pick_col(books_cols, ["dhlabid", "book_id"]) if books_cols else None
        books_token_col = _pick_col(books_cols, ["token", "place_token"]) if books_cols else None
        books_count_col = _pick_col(books_cols, ["book_count", "mentions", "count"]) if books_cols else None
        books_join_sql = ""
        unique_places_calc_expr = "NULL AS unique_places_calc"
        total_mentions_calc_expr = "NULL AS total_mentions_calc"
        if books_dhlab_col and books_token_col:
            books_count_expr = f"COALESCE({books_count_col}, 1)" if books_count_col else "1"
            books_join_sql = f"""
            LEFT JOIN (
                SELECT
                    {books_dhlab_col} AS dhlabid,
                    COUNT(DISTINCT {books_token_col}) AS unique_places_calc,
                    SUM({books_count_expr}) AS total_mentions_calc
                FROM books
                GROUP BY {books_dhlab_col}
            ) bm ON bm.dhlabid = c.{id_col}
            """
            unique_places_calc_expr = "CAST(bm.unique_places_calc AS INTEGER) AS unique_places_calc"
            total_mentions_calc_expr = "CAST(bm.total_mentions_calc AS INTEGER) AS total_mentions_calc"
        year_order = "year IS NULL, year, dhlabid" if _pick_col(corpus_cols, ["year", "pub_year"]) else "dhlabid"
        sql = f"""
        SELECT
            CAST(c.{id_col} AS INTEGER) AS dhlabid,
            {urn_expr},
            {author_expr},
            {year_expr},
            {category_expr},
            {title_expr},
            {unique_places_expr},
            {total_mentions_expr},
            {unique_places_calc_expr},
            {total_mentions_calc_expr}
        FROM corpus c
        {books_join_sql}
        ORDER BY {year_order}
        """
        rows = cur.execute(sql).fetchall()
        books: List[Dict[str, Any]] = []
        for r in rows:
            unique_places = int(r[6]) if r[6] is not None else (int(r[8]) if r[8] is not None else None)
            total_mentions = int(r[7]) if r[7] is not None else (int(r[9]) if r[9] is not None else None)
            books.append(
                {
                    "dhlabid": int(r[0]),
                    "urn": str(r[1]) if r[1] is not None else "",
                    "author": str(r[2]) if r[2] is not None else None,
                    "year": int(r[3]) if r[3] is not None else None,
                    "category": str(r[4]) if r[4] is not None else None,
                    "title": str(r[5]) if r[5] is not None else None,
                    "unique_places": unique_places,
                    "total_mentions": total_mentions,
                }
            )
        return {"books": books}
    finally:
        con.close()


@app.post("/api/places")
def imagination_places(req: PlacesRequest):
    if not req.dhlabids:
        return {"places": [], "total_places": 0}
    con = _connect_imagination_ro()
    try:
        cur = con.cursor()
        books_cols = _table_columns(cur, "books")
        places_cols = _table_columns(cur, "places")
        if not books_cols:
            raise HTTPException(status_code=500, detail="imagination_db is missing table: books")
        if not places_cols:
            raise HTTPException(status_code=500, detail="imagination_db is missing table: places")
        dhlab_col = _pick_col(books_cols, ["dhlabid", "book_id"])
        token_col = _pick_col(books_cols, ["token", "place_token"])
        count_col = _pick_col(books_cols, ["book_count", "mentions", "count"])
        places_token_col = _pick_col(places_cols, ["token", "place_token"])
        lat_col = _pick_col(places_cols, ["latitude", "lat"])
        lon_col = _pick_col(places_cols, ["longitude", "lon"])
        name_col = _pick_col(places_cols, ["modern", "name", "canonical_name"])
        id_col_places = _pick_col(places_cols, ["geonameid", "geonames_id", "place_id"])
        id_col_books = _pick_col(books_cols, ["geonameid", "geonames_id", "place_id"])
        if not dhlab_col or not token_col:
            raise HTTPException(
                status_code=500,
                detail="books table must contain dhlabid/book_id and token/place_token",
            )
        if not places_token_col or not lat_col or not lon_col:
            raise HTTPException(
                status_code=500,
                detail="places table must contain token/place_token and latitude/longitude",
            )
        count_expr = f"COALESCE(b.{count_col}, 1)" if count_col else "1"
        name_expr = f"COALESCE(p.{name_col}, b.{token_col})" if name_col else f"b.{token_col}"
        if id_col_places:
            id_expr = f"COALESCE(CAST(p.{id_col_places} AS TEXT), b.{token_col})"
        elif id_col_books:
            id_expr = f"COALESCE(CAST(b.{id_col_books} AS TEXT), b.{token_col})"
        else:
            id_expr = f"b.{token_col}"
        dhlabids_json = json.dumps([int(x) for x in req.dhlabids])
        sql_rows = f"""
        WITH filter AS (
            SELECT CAST(value AS INTEGER) AS dhlabid
            FROM json_each(?)
        ),
        agg AS (
            SELECT
                {id_expr} AS id,
                b.{token_col} AS token,
                {name_expr} AS name,
                CAST(p.{lat_col} AS REAL) AS lat,
                CAST(p.{lon_col} AS REAL) AS lon,
                SUM({count_expr}) AS frequency,
                COUNT(DISTINCT b.{dhlab_col}) AS doc_count
            FROM books b
            JOIN filter f ON f.dhlabid = b.{dhlab_col}
            LEFT JOIN places p ON p.{places_token_col} = b.{token_col}
            WHERE p.{lat_col} IS NOT NULL
              AND p.{lon_col} IS NOT NULL
            GROUP BY id, b.{token_col}, name, p.{lat_col}, p.{lon_col}
        )
        SELECT id, token, name, lat, lon, frequency, doc_count
        FROM agg
        ORDER BY frequency DESC
        LIMIT ?
        """
        rows = cur.execute(sql_rows, (dhlabids_json, int(req.maxPlaces))).fetchall()
        sql_total = f"""
        WITH filter AS (
            SELECT CAST(value AS INTEGER) AS dhlabid
            FROM json_each(?)
        )
        SELECT COUNT(*)
        FROM (
            SELECT b.{token_col}
            FROM books b
            JOIN filter f ON f.dhlabid = b.{dhlab_col}
            LEFT JOIN places p ON p.{places_token_col} = b.{token_col}
            WHERE p.{lat_col} IS NOT NULL
              AND p.{lon_col} IS NOT NULL
            GROUP BY b.{token_col}
        )
        """
        total_places = int(cur.execute(sql_total, (dhlabids_json,)).fetchone()[0] or 0)
        places: List[Dict[str, Any]] = []
        for r in rows:
            places.append(
                {
                    "id": str(r[0]),
                    "token": str(r[1]),
                    "name": str(r[2]) if r[2] is not None else None,
                    "lat": float(r[3]),
                    "lon": float(r[4]),
                    "frequency": int(r[5] or 0),
                    "doc_count": int(r[6] or 0),
                }
            )
        return {"places": places, "total_places": total_places}
    finally:
        con.close()


@app.post("/api/places/details")
def imagination_places_details(req: PlaceDetailsRequest):
    token = (req.token or "").strip()
    if not req.dhlabids or not token:
        return {"books": []}
    con = _connect_imagination_ro()
    try:
        cur = con.cursor()
        books_cols = _table_columns(cur, "books")
        corpus_cols = _table_columns(cur, "corpus")
        if not books_cols:
            raise HTTPException(status_code=500, detail="imagination_db is missing table: books")
        if not corpus_cols:
            raise HTTPException(status_code=500, detail="imagination_db is missing table: corpus")
        b_dhlab_col = _pick_col(books_cols, ["dhlabid", "book_id"])
        c_dhlab_col = _pick_col(corpus_cols, ["dhlabid", "book_id"])
        token_col = _pick_col(books_cols, ["token", "place_token"])
        count_col = _pick_col(books_cols, ["book_count", "mentions", "count"])
        if not b_dhlab_col or not c_dhlab_col or not token_col:
            raise HTTPException(
                status_code=500,
                detail="books/corpus tables must contain dhlabid/book_id and books token/place_token",
            )
        count_expr = f"COALESCE(b.{count_col}, 1)" if count_col else "1"
        urn_expr = _optional_expr(corpus_cols, ["urn"], "urn")
        author_expr = _optional_expr(corpus_cols, ["author", "forfatter"], "author")
        year_expr = _optional_expr(corpus_cols, ["year", "pub_year"], "year", cast="INTEGER")
        title_expr = _optional_expr(corpus_cols, ["title", "titel"], "title")
        category_expr = _optional_expr(corpus_cols, ["category", "genre"], "category")
        dhlabids_json = json.dumps([int(x) for x in req.dhlabids])
        sql = f"""
        WITH filter AS (
            SELECT CAST(value AS INTEGER) AS dhlabid
            FROM json_each(?)
        )
        SELECT
            CAST(c.{c_dhlab_col} AS INTEGER) AS dhlabid,
            {urn_expr},
            {author_expr},
            {year_expr},
            {title_expr},
            {category_expr},
            SUM({count_expr}) AS mentions
        FROM books b
        JOIN filter f ON f.dhlabid = b.{b_dhlab_col}
        JOIN corpus c ON c.{c_dhlab_col} = b.{b_dhlab_col}
        WHERE lower(COALESCE(b.{token_col}, '')) = lower(?)
        GROUP BY c.{c_dhlab_col}, urn, author, year, title, category
        ORDER BY mentions DESC, year IS NULL, year, dhlabid
        LIMIT ?
        """
        rows = cur.execute(sql, (dhlabids_json, token, int(req.limit))).fetchall()
        books: List[Dict[str, Any]] = []
        for r in rows:
            books.append(
                {
                    "dhlabid": int(r[0]),
                    "urn": str(r[1]) if r[1] is not None else "",
                    "author": str(r[2]) if r[2] is not None else None,
                    "year": int(r[3]) if r[3] is not None else None,
                    "title": str(r[4]) if r[4] is not None else None,
                    "category": str(r[5]) if r[5] is not None else None,
                    "mentions": int(r[6] or 0),
                }
            )
        return {"books": books}
    finally:
        con.close()


def _geo_annotation_attrs(row: Dict[str, Any]) -> str:
    place = row.get("place") or {}
    attrs = [("data-layer", "geo")]
    attrs.append(("data-geo-place-id", row.get("placeId")))
    attrs.append(("data-geo-variant-id", row.get("variantId")))
    attrs.append(("data-geo-canonical", place.get("canonicalName")))
    attrs.append(("data-geo-geonames-id", place.get("geonamesId")))
    attrs.append(("data-geo-lat", place.get("lat")))
    attrs.append(("data-geo-lon", place.get("lon")))
    attrs.append(("data-geo-country", place.get("country")))
    attrs.append(("data-geo-variant-text", place.get("variantText")))
    attrs.append(("data-geo-method", row.get("method")))
    attrs.append(("data-geo-score", row.get("score")))
    parts: List[str] = []
    for key, value in attrs:
        if value is None:
            continue
        parts.append(f'{key}="{html_escape(str(value), quote=True)}"')
    return " ".join(parts)


def _extract_plain_terms_from_or_query(req: OrQueryRequest) -> List[str]:
    if req.termGroups:
        source = [t for g in req.termGroups for t in g]
    else:
        source = req.terms or []
    out: List[str] = []
    for token in source:
        tok = (token or "").strip()
        if not tok:
            continue
        if tok.startswith("#"):
            continue
        out.append(tok)
    return out


def _extract_plain_term_groups_from_or_query(req: OrQueryRequest) -> List[List[str]]:
    raw_groups = req.termGroups if req.termGroups else [[t] for t in (req.terms or [])]
    out: List[List[str]] = []
    for group in raw_groups:
        g: List[str] = []
        for token in group:
            tok = (token or "").strip()
            if not tok or tok.startswith("#"):
                continue
            g.append(tok)
        if g:
            out.append(g)
    return out


def _decode_roaring_positions(blob: bytes) -> List[int]:
    if not blob:
        return []
    if RoaringBitMap is None:
        raise HTTPException(status_code=500, detail="pyroaring is required for geo roaring decode")
    return list(RoaringBitMap.deserialize(blob))


def _try_parse_geo_key(value: Optional[str]) -> Optional[Tuple[str, str]]:
    if value is None:
        return None
    v = str(value).strip()
    if not v:
        return None
    if v.isdigit():
        return ("geonames", v)
    m = re.match(r"^(geonames|internal)\s*:\s*(.+)$", v, flags=re.IGNORECASE)
    if m:
        return (m.group(1).strip().casefold(), m.group(2).strip())
    return None


def _load_geo_anchor_positions(
    geo_db_path: str,
    book_ids: List[int],
    geo_key: Optional[Tuple[str, str]],
) -> Dict[int, List[int]]:
    if not book_ids:
        return {}
    con = sqlite3.connect(f"file:{geo_db_path}?mode=ro", uri=True)
    cur = con.cursor()
    try:
        cur.execute("CREATE TEMP TABLE _book_filter(book_id INTEGER PRIMARY KEY)")
        cur.executemany(
            "INSERT OR IGNORE INTO _book_filter(book_id) VALUES (?)",
            ((int(book_id),) for book_id in book_ids),
        )
        rows: List[Tuple[int, bytes]] = []
        if geo_key:
            rows = cur.execute(
                """
                SELECT p.book_id, p.starts_roaring
                FROM geo_postings_v2 p
                JOIN _book_filter f ON f.book_id = p.book_id
                WHERE p.token_len = 0
                  AND p.place_key_type = ?
                  AND p.place_key = ?
                """,
                (geo_key[0], geo_key[1]),
            ).fetchall()
        else:
            rows = cur.execute(
                """
                SELECT p.book_id, p.post_blob
                FROM geo_postings_all p
                JOIN _book_filter f ON f.book_id = p.book_id
                """
            ).fetchall()
        out: Dict[int, List[int]] = {}
        for book_id, blob in rows:
            pos = _decode_roaring_positions(blob)
            if pos:
                out[int(book_id)] = sorted(int(x) for x in pos)
        return out
    finally:
        con.close()


def _lookup_geo_span_meta(
    geo_db_path: str,
    hits: List[Tuple[int, int]],
    geo_key: Optional[Tuple[str, str]],
) -> Dict[Tuple[int, int], Dict[str, Any]]:
    if not hits:
        return {}
    con = sqlite3.connect(f"file:{geo_db_path}?mode=ro", uri=True)
    cur = con.cursor()
    try:
        out: Dict[Tuple[int, int], Dict[str, Any]] = {}
        for book_id, pos in hits:
            params: List[Any] = [int(book_id), int(pos)]
            sql = """
                SELECT token_len, surface_text, place_key_type, place_key
                FROM geo_mentions_v2
                WHERE book_id = ? AND seq_start = ?
            """
            if geo_key:
                sql += " AND place_key_type = ? AND place_key = ?"
                params.extend([geo_key[0], geo_key[1]])
            sql += " ORDER BY token_len DESC LIMIT 1"
            row = cur.execute(sql, tuple(params)).fetchone()
            if not row:
                continue
            out[(int(book_id), int(pos))] = {
                "tokenLen": int(row[0]),
                "surfaceText": str(row[1]) if row[1] is not None else None,
                "placeKeyType": str(row[2]) if row[2] is not None else None,
                "placeKey": str(row[3]) if row[3] is not None else None,
            }
        return out
    finally:
        con.close()


def _run_geo_namespace_near_or(
    req: OrQueryRequest,
    ns_db_path: str,
    book_ids: List[int],
    plain_term_groups: List[List[str]],
    geo_key: Optional[Tuple[str, str]],
) -> Dict[str, Any]:
    req_t0 = time.perf_counter()
    perf: Dict[str, Any] = {
        "mode": "geo_near_term_groups",
        "shards": [],
    }
    anchor_t0 = time.perf_counter()
    anchor_positions = _load_geo_anchor_positions(ns_db_path, book_ids, geo_key)
    perf["anchor_load_ms"] = round((time.perf_counter() - anchor_t0) * 1000.0, 3)
    perf["anchor_books"] = len(anchor_positions)
    if not anchor_positions:
        raise HTTPException(status_code=404, detail="No geo anchor positions for requested namespace/filter")
    rows_hits: List[Tuple[int, int]] = []
    target_books = set(anchor_positions.keys())
    for shard_index, path in enumerate(CONFIG.postings_dbs):
        if req.totalLimit and len(rows_hits) >= req.totalLimit:
            break
        shard_t0 = time.perf_counter()
        shard_perf: Dict[str, Any] = {
            "shard": path,
            "index": int(shard_index),
        }
        con = connect_postings(path, CONFIG.ext_path, shard_sidecar_path(path, shard_index))
        conw = connect_words(shard_words_path(path))
        try:
            cur = con.cursor()
            curw = conw.cursor()
            groups_t0 = time.perf_counter()
            cf_groups = _resolve_term_groups(
                curw, None, plain_term_groups, int(req.maxVariants), symmetric=False
            )
            shard_perf["groups_ms"] = round((time.perf_counter() - groups_t0) * 1000.0, 3)
            if not cf_groups:
                shard_perf["cf_groups_empty"] = True
                shard_perf["total_ms"] = round((time.perf_counter() - shard_t0) * 1000.0, 3)
                perf["shards"].append(shard_perf)
                continue
            shard_book_rows = cur.execute("SELECT book_id FROM urns").fetchall()
            shard_books = [int(r[0]) for r in shard_book_rows if int(r[0]) in target_books]
            shard_perf["candidate_books"] = len(shard_books)
            rows_before = len(rows_hits)
            book_scan_t0 = time.perf_counter()
            for book_id in shard_books:
                if req.totalLimit and len(rows_hits) >= req.totalLimit:
                    break
                group_pos = group_positions_for_book(
                    cur, cf_groups, int(book_id), req.schema or CONFIG.default_schema
                )
                if not group_pos or any(not g for g in group_pos):
                    continue
                off_min = -int(req.before)
                off_max = int(req.after)
                near_pos = near_positions_from_groups(
                    [anchor_positions[int(book_id)], *group_pos],
                    off_min,
                    off_max,
                    exclude_self=False,
                )
                if not near_pos:
                    continue
                picks = near_pos[: int(req.perBook)]
                for pos in picks:
                    rows_hits.append((int(book_id), int(pos)))
                    if req.totalLimit and len(rows_hits) >= req.totalLimit:
                        break
            shard_perf["match_ms"] = round((time.perf_counter() - book_scan_t0) * 1000.0, 3)
            shard_perf["rows_added"] = len(rows_hits) - rows_before
            shard_perf["total_ms"] = round((time.perf_counter() - shard_t0) * 1000.0, 3)
            perf["shards"].append(shard_perf)
        finally:
            con.close()
            conw.close()
    if not rows_hits:
        raise HTTPException(status_code=404, detail="No near hits for #geo + term groups")
    meta_t0 = time.perf_counter()
    meta_map = _lookup_geo_span_meta(ns_db_path, rows_hits, geo_key)
    perf["meta_lookup_ms"] = round((time.perf_counter() - meta_t0) * 1000.0, 3)
    rows: List[Dict[str, Any]] = []
    for book_id, pos in rows_hits[: int(req.totalLimit)]:
        meta = meta_map.get((book_id, pos), {})
        rows.append(
            {
                "bookId": int(book_id),
                "seqStart": int(pos),
                "pos": int(pos),
                "tokenLen": int(meta.get("tokenLen") or 1),
                "surfaceText": meta.get("surfaceText"),
                "placeKeyType": meta.get("placeKeyType"),
                "placeKey": meta.get("placeKey"),
                "method": "geo_near_term_groups",
            }
        )
    rendered_rows: List[Dict[str, object]] = []
    unresolved_render: List[Dict[str, int]] = []
    if bool(req.renderHits):
        render_t0 = time.perf_counter()
        render_input = [{"bookId": int(r["bookId"]), "pos": int(r["pos"])} for r in rows]
        rendered_rows, unresolved_render = _render_book_pos_rows(
            render_input, int(req.before), int(req.after)
        )
        perf["render_ms"] = round((time.perf_counter() - render_t0) * 1000.0, 3)
    out: Dict[str, Any] = {
        "namespace": "geo",
        "resolver": "geo_resolver",
        "mode": "geo_near",
        "rows": rows,
    }
    if rendered_rows:
        out["rendered"] = rendered_rows
    if unresolved_render:
        out["render_unresolved"] = unresolved_render
    if PROFILE_NEAR:
        perf["rows_total"] = len(rows)
        perf["hits_total"] = len(rows_hits)
        perf["total_ms"] = round((time.perf_counter() - req_t0) * 1000.0, 3)
        out["_perf"] = perf
    return out


def _attach_geo_surface_fragments(rows: List[Dict[str, Any]]) -> None:
    """
    Keep namespace/geo responses independent from fulltext internals.
    Fragments are derived from geo surface_text only.
    """
    for row in rows:
        surface = str(row.get("surfaceText") or "")
        row["fragRaw"] = surface
        if surface:
            attrs = _geo_annotation_attrs(row)
            row["fragHtml"] = f"<annotation {attrs}>{html_escape(surface)}</annotation>"
        else:
            row["fragHtml"] = ""


def _render_book_pos_rows(rows: List[Dict[str, int]], before: int, after: int) -> Tuple[List[Dict[str, object]], List[Dict[str, int]]]:
    out_rows: List[Dict[str, object]] = []
    unresolved = [{"bookId": int(r["bookId"]), "pos": int(r["pos"])} for r in rows]
    for shard_index, path in enumerate(CONFIG.postings_dbs):
        if not unresolved:
            break
        con = connect_postings(path, CONFIG.ext_path, shard_sidecar_path(path, shard_index))
        conw = connect_words(shard_words_path(path))
        cur = con.cursor()
        curw = conw.cursor()
        next_unresolved: List[Dict[str, int]] = []
        for row in unresolved:
            book_id = int(row["bookId"])
            pos = int(row["pos"])
            exists = cur.execute(
                "SELECT 1 FROM urns WHERE book_id = ? LIMIT 1",
                (book_id,),
            ).fetchone()
            if not exists:
                next_unresolved.append(row)
                continue
            frag = fetch_window(cur, curw, book_id, pos, before, after)
            out_rows.append({"bookId": book_id, "pos": pos, "frag": frag})
        con.close()
        conw.close()
        unresolved = next_unresolved
    return out_rows, unresolved


def _run_annotation_namespace_query_or(req: OrQueryRequest) -> Optional[Dict[str, Any]]:
    try:
        namespace, namespace_value, has_non_namespace_terms = parse_namespace_query(
            req.terms, req.termGroups
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not namespace:
        return None
    plain_terms = _extract_plain_terms_from_or_query(req)
    plain_term_groups = _extract_plain_term_groups_from_or_query(req)
    if has_non_namespace_terms and not plain_terms:
        raise HTTPException(status_code=400, detail="Invalid non-namespace term(s) in request")
    near_term = plain_terms[0] if plain_terms else None
    if near_term and namespace_value and not plain_term_groups:
        raise HTTPException(
            status_code=400,
            detail="Use either #namespace:value OR #namespace plus one plain term, not both",
        )
    if not CONFIG.annotation_registry_db:
        raise HTTPException(
            status_code=500,
            detail="annotation_registry_db is not configured in POSTINGS_CONFIG",
        )

    try:
        ns_meta = resolve_namespace(
            CONFIG.annotation_registry_db,
            namespace,
            base_dir=CONFIG.annotation_base_dir,
        )
        filter_ids = req.filterIds if req.useFilter and req.filterIds else None
        book_ids = resolve_namespace_books(
            CONFIG.annotation_registry_db,
            namespace,
            filter_ids,
            int(req.docSamples or 0),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if not book_ids:
        raise HTTPException(status_code=404, detail=f"No covered books for #{namespace}")

    resolver = str(ns_meta.get("resolver", "")).casefold()
    if resolver != "geo_resolver":
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported resolver for #{namespace}: {resolver}",
        )
    geo_key = _try_parse_geo_key(namespace_value) if namespace == "geo" else None
    if plain_term_groups:
        if namespace != "geo":
            raise HTTPException(status_code=400, detail="Near mode is only supported for #geo namespace")
        if namespace_value and not geo_key:
            raise HTTPException(
                status_code=400,
                detail="Near mode with #geo:value expects geoname id (e.g. #geo:3143244) or explicit geonames/internal prefix",
            )
        return _run_geo_namespace_near_or(
            req,
            ns_meta["db_path"],
            book_ids,
            plain_term_groups,
            geo_key,
        )

    rows = fetch_geo_spans(
        ns_meta["db_path"],
        book_ids,
        int(req.totalLimit),
        place_text_filter=namespace_value,
    )
    fallback_mode: Optional[str] = None
    if (
        not rows
        and namespace == "geo"
        and namespace_value
        and CONFIG.imagination_db
        and Path(CONFIG.imagination_db).exists()
    ):
        rows = fetch_geo_books_from_imagination(
            CONFIG.imagination_db,
            namespace_value,
            filter_ids if filter_ids else None,
            int(req.totalLimit),
        )
        if rows:
            fallback_mode = "imagination_book_level"
    if not rows:
        if namespace_value:
            raise HTTPException(
                status_code=404,
                detail=f'No annotation hits for #{namespace}:"{namespace_value}"',
            )
        raise HTTPException(status_code=404, detail=f"No annotation hits for #{namespace}")
    for row in rows:
        if row.get("seqStart") is not None:
            try:
                row["pos"] = int(row["seqStart"])
            except Exception:
                pass
    rendered_rows: List[Dict[str, object]] = []
    unresolved_render: List[Dict[str, int]] = []
    if bool(req.renderHits):
        render_input = []
        for row in rows:
            if "bookId" not in row or "pos" not in row:
                continue
            pos = int(row["pos"])
            if pos < 0:
                continue
            render_input.append({"bookId": int(row["bookId"]), "pos": pos})
        if render_input:
            rendered_rows, unresolved_render = _render_book_pos_rows(
                render_input, int(req.before), int(req.after)
            )
    _attach_geo_surface_fragments(rows)
    out = {
        "namespace": namespace,
        "resolver": resolver,
        "rows": rows,
    }
    if rendered_rows:
        out["rendered"] = rendered_rows
    if unresolved_render:
        out["render_unresolved"] = unresolved_render
    if fallback_mode:
        out["coverageMode"] = fallback_mode
    return out


class NearFragmentsRequest(BaseModel):
    terms: Optional[List[str]] = None
    termGroups: Optional[List[List[str]]] = None
    window: int = Field(5, ge=1, le=50)
    before: int = Field(5, ge=1, le=50)
    after: int = Field(5, ge=1, le=50)
    perBook: int = Field(3, ge=1, le=20)
    docSamples: Optional[int] = Field(None, ge=0, le=50000)
    totalLimit: int = Field(200, ge=1, le=5000)
    schema: Optional[str] = None
    symmetric: bool = True
    excludeSelf: bool = False
    useFilter: bool = False
    filterIds: List[int] = []
    maxVariants: int = Field(10, ge=1, le=100)
    includeFragments: bool = True
    engine: Optional[str] = None
    parallelShards: Optional[bool] = None
    matchMode: Optional[str] = None


class CollocationsRequest(BaseModel):
    word: str
    before: int = Field(5, ge=1, le=50)
    after: int = Field(5, ge=1, le=50)
    perBook: int = Field(3, ge=1, le=20)
    docSamples: Optional[int] = Field(None, ge=0, le=50000)
    schema: Optional[str] = None
    useFilter: bool = False
    filterIds: List[int] = []


class RenderHitsRequest(BaseModel):
    rows: List[Dict[str, int]]
    before: int = Field(5, ge=1, le=25)
    after: int = Field(5, ge=1, le=25)
    totalLimit: int = Field(200, ge=1, le=5000)


# ----- Imagination Models -----

class CorpusFilters(BaseModel):
    category: Optional[str] = None
    yearRange: Optional[Tuple[int, int]] = None
    author: Optional[str] = None

class CorpusBuildRequest(BaseModel):
    filters: Optional[CorpusFilters] = None
    contentKeywords: Optional[List[str]] = None
    baseCorpus: Optional[List[int]] = None

class PlacesRequest(BaseModel):
    dhlabids: List[int]
    maxPlaces: Optional[int] = 2000

class PlaceDetailsRequest(BaseModel):
    dhlabids: List[int]
    token: str

class MetadataRequest(BaseModel):
    dhlabids: List[int]
    placeFilter: Optional[str] = None

# ----- Database Helpers -----

def get_imagination_db():
    if not CONFIG.imagination_db:
        # Fallback for dev if not in config: look for imagination.db in current or parent dir
        possible_paths = [
            Path("imagination.db"),
            Path("data/imagination.db"),
            Path("../imagination.db")
        ]
        active_path = None
        for p in possible_paths:
            if p.exists():
                active_path = str(p)
                break
        if not active_path:
            raise HTTPException(status_code=500, detail="Imagination database not configured and not found in default paths.")
    else:
        active_path = CONFIG.imagination_db

    conn = sqlite3.connect(active_path)
    conn.row_factory = sqlite3.Row
    return conn


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok", "version": app.version}


@app.post("/concordance")
def concordance(req: ConcordanceRequest):
    postings_paths = CONFIG.postings_dbs
    max_variants = 1000

    def run_once(doc_samples: Optional[int]) -> Tuple[List[Tuple[int, int, str]], bool, bool]:
        local_rows: List[Tuple[int, int, str]] = []
        local_word_a_found = False
        local_word_b_found = True
        for shard_index, path in enumerate(postings_paths):
            con = connect_postings(path, CONFIG.ext_path, shard_sidecar_path(path, shard_index))
            conw = connect_words(shard_words_path(path))
            cur = con.cursor()
            curw = conw.cursor()
            base_filter_ids = req.filterIds if req.useFilter and req.filterIds else None
            use_filter = False
            filter_json = None
            cf_a = None
            cf_ids_a: Optional[List[int]] = None
            if req.wordA.endswith("*"):
                cf_ids_a, _ = expand_term_cf_ids_with_df(curw, req.wordA, max_variants)
                if not cf_ids_a:
                    con.close()
                    conw.close()
                    continue
            else:
                cf_a = get_cf_id(curw, req.wordA)
                if cf_a is None:
                    con.close()
                    conw.close()
                    continue
            local_word_a_found = True
            if req.wordB and req.wordB.strip():
                cf_b = get_cf_id(curw, req.wordB)
                if cf_b is None:
                    local_word_b_found = False
                    con.close()
                    conw.close()
                    continue
                if not use_filter:
                    filter_ids = _apply_docpost_filter_and_sample(
                        cur,
                        [[cf_a], [cf_b]],
                        base_filter_ids,
                        doc_samples,
                        req.totalLimit,
                        req.perBook,
                    )
                    if filter_ids == []:
                        con.close()
                        conw.close()
                        continue
                    if filter_ids:
                        use_filter = True
                        filter_json = json.dumps(filter_ids)
                if req.symmetric:
                    off_min, off_max = -req.before, req.after
                else:
                    off_min, off_max = 1, req.after
                local_rows.extend(
                    sample_concordance_near(
                        cur,
                        curw,
                        cf_a,
                        cf_b,
                        req.perBook,
                        req.before,
                        req.after,
                        use_filter,
                        filter_json,
                        (req.schema or CONFIG.default_schema),
                        off_min,
                        off_max,
                        req.excludeSelf,
                    )
                )
            else:
                if not use_filter:
                    if cf_ids_a:
                        cf_groups = [cf_ids_a]
                    else:
                        cf_groups = [[cf_a]]
                    filter_ids = _apply_docpost_filter_and_sample(
                        cur,
                        cf_groups,
                        base_filter_ids,
                        doc_samples,
                        req.totalLimit,
                        req.perBook,
                    )
                    if filter_ids == []:
                        con.close()
                        conw.close()
                        continue
                    if filter_ids:
                        use_filter = True
                        filter_json = json.dumps(filter_ids)
                if cf_ids_a:
                    local_rows.extend(
                        sample_concordance_union(
                            cur,
                            curw,
                            cf_ids_a,
                            req.perBook,
                            req.before,
                            req.after,
                            use_filter,
                            filter_json,
                        )
                    )
                else:
                    local_rows.extend(
                        sample_concordance_single(
                            cur,
                            curw,
                            cf_a,
                            req.perBook,
                            req.before,
                            req.after,
                            use_filter,
                            filter_json,
                        )
                    )
            con.close()
            conw.close()
        return local_rows, local_word_a_found, local_word_b_found

    rows, word_a_found, word_b_found = run_once(req.docSamples)
    has_word_b = bool(req.wordB and req.wordB.strip())
    if has_word_b and int(req.docSamples or 0) > 0 and not rows and word_a_found and word_b_found:
        rows, retry_word_a_found, retry_word_b_found = run_once(0)
        word_a_found = word_a_found or retry_word_a_found
        word_b_found = word_b_found and retry_word_b_found
    if not word_a_found:
        raise HTTPException(status_code=404, detail="Word A not found")
    if req.wordB and req.wordB.strip() and not word_b_found:
        raise HTTPException(status_code=404, detail="Word B not found")
    if req.totalLimit and len(rows) > req.totalLimit:
        rows = rows[: req.totalLimit]
    return {"rows": [{"bookId": b, "pos": p, "frag": f} for b, p, f in rows]}


@app.post("/near_frequency")
def near_freq(req: NearFrequencyRequest):
    postings_paths = CONFIG.postings_dbs
    total = 0
    docs = 0
    found_any = False
    for shard_index, path in enumerate(postings_paths):
        con = connect_postings(path, CONFIG.ext_path, shard_sidecar_path(path, shard_index))
        conw = connect_words(shard_words_path(path))
        cur = con.cursor()
        curw = conw.cursor()
        base_filter_ids = req.filterIds if req.useFilter and req.filterIds else None
        use_filter = False
        filter_json = None
        cf_a = get_cf_id(curw, req.wordA)
        cf_b = get_cf_id(curw, req.wordB)
        if cf_a is None or cf_b is None:
            con.close()
            conw.close()
            continue
        if not use_filter:
            filter_ids = _apply_docpost_filter_and_sample(
                cur,
                [[cf_a], [cf_b]],
                base_filter_ids,
                0,
                0,
                0,
            )
            if filter_ids == []:
                con.close()
                conw.close()
                continue
            if filter_ids:
                use_filter = True
                filter_json = json.dumps(filter_ids)
        found_any = True
        shard_total, shard_docs = near_frequency(
            cur,
            cf_a,
            cf_b,
            req.window,
            use_filter,
            filter_json,
            (req.schema or CONFIG.default_schema),
            req.symmetric,
            req.excludeSelf,
        )
        total += shard_total
        docs += shard_docs
        con.close()
        conw.close()
    if not found_any:
        raise HTTPException(status_code=404, detail="Word not found")
    return {"total": total, "docs": docs}


def _words_columns(curw) -> set:
    cols = set()
    try:
        for row in curw.execute("PRAGMA table_info(words)"):
            cols.add(row[1])
    except Exception:
        pass
    return cols


def _term_exact_variants(term: str) -> List[str]:
    t = str(term or "")
    if not t:
        return []
    out: List[str] = []
    for candidate in (t.casefold(), t, t.lower(), t.title(), t.upper()):
        if candidate and candidate not in out:
            out.append(candidate)
    return out


def expand_term_cf_ids_with_df(
    curw, term: str, max_variants: int
) -> Tuple[List[int], int]:
    term = term.strip()
    if not term:
        return [], 0
    cols = _words_columns(curw)
    if "total_tf" not in cols:
        raise RuntimeError("words.total_tf is required for prefix expansion")
    has_docfreq = "docfreq" in cols
    order_col = "total_tf"
    if term.endswith("*"):
        prefix = term[:-1]
        if not prefix:
            return [], 0
        select_cols = "cf_id" + (", docfreq" if has_docfreq else "")
        rows = curw.execute(
            f"""
            SELECT {select_cols}
            FROM words
            WHERE word >= ? AND word < ?
            GROUP BY cf_id
            ORDER BY {order_col} DESC
            LIMIT ?
            """,
            (prefix, f"{prefix}\uffff", max_variants),
        ).fetchall()
        cf_ids = [r[0] for r in rows]
        df_sum = sum(r[1] for r in rows) if has_docfreq else len(cf_ids)
        return cf_ids, int(df_sum)
    for variant in _term_exact_variants(term):
        if has_docfreq:
            row = curw.execute(
                "SELECT cf_id, docfreq FROM words WHERE word = ? ORDER BY raw_id LIMIT 1",
                (variant,),
            ).fetchone()
        else:
            row = curw.execute(
                "SELECT cf_id FROM words WHERE word = ? ORDER BY raw_id LIMIT 1",
                (variant,),
            ).fetchone()
        if row:
            return [row[0]], int(row[1]) if has_docfreq else 1
    return [], 0


def _resolve_term_groups(
    curw,
    terms: Optional[List[str]],
    term_groups: Optional[List[List[str]]],
    max_variants: int,
    symmetric: bool,
) -> List[List[int]]:
    base_terms = terms or []
    raw_groups = term_groups if term_groups else [[t] for t in base_terms]
    term_infos: List[Tuple[List[int], int]] = []
    for group in raw_groups:
        group_cf_ids: List[int] = []
        df_sum = 0
        for term in group:
            cf_ids, df = expand_term_cf_ids_with_df(curw, term, max_variants)
            if not cf_ids:
                # In OR groups, missing variants should not invalidate the
                # full query. We only fail if the entire group has no hits.
                continue
            group_cf_ids.extend(cf_ids)
            df_sum += df
        group_cf_ids = sorted(set(group_cf_ids))
        if not group_cf_ids:
            return []
        term_infos.append((group_cf_ids, df_sum))
    if symmetric:
        term_infos.sort(key=lambda x: x[1])
    return [info[0] for info in term_infos]


def expand_term_cf_ids(
    curw, term: str, max_variants: int
) -> List[int]:
    return expand_term_cf_ids_with_df(curw, term, max_variants)[0]


def prepare_term_cf_table(cur, groups: List[List[int]]) -> None:
    cur.execute("DROP TABLE IF EXISTS term_cf;")
    cur.execute(
        "CREATE TEMP TABLE term_cf (grp INTEGER NOT NULL, cf_id INTEGER NOT NULL)"
    )
    rows: List[Tuple[int, int]] = []
    for idx, group in enumerate(groups, start=1):
        for cf_id in group:
            rows.append((idx, cf_id))
    cur.executemany("INSERT INTO term_cf(grp, cf_id) VALUES (?, ?)", rows)


def union_cte(
    table: str,
    grp: int,
    use_filter: bool,
) -> str:
    from_clause = (
        "FROM filter f JOIN {table} u ON u.book_id = f.urn"
        if use_filter
        else "FROM {table} u"
    )
    return f"""
    g{grp} AS (
        SELECT u.book_id, post_union_agg(u.post) AS blob
        {from_clause.format(table=table)}
        JOIN term_cf t ON t.cf_id = u.cf_id AND t.grp = {grp}
        GROUP BY u.book_id
    )
    """


def groups_sql(groups: List[List[int]], table: str, use_filter: bool) -> Tuple[str, str, List[str]]:
    ctes = []
    for i in range(1, len(groups) + 1):
        ctes.append(union_cte(table, i, use_filter))
    join_clause = "FROM g1"
    cols = ["g1.blob AS b1"]
    for i in range(2, len(groups) + 1):
        join_clause += f" JOIN g{i} ON g{i}.book_id = g1.book_id"
        cols.append(f"g{i}.blob AS b{i}")
    select_sql = f"SELECT g1.book_id, {', '.join(cols)} {join_clause}"
    return ", ".join(ctes), select_sql, cols


def _sample_book_target(total_limit: int, per_book: int) -> int:
    if total_limit <= 0:
        return 0
    if per_book <= 0:
        return total_limit
    return max(1, (total_limit + per_book - 1) // per_book)


def _resolve_doc_samples(
    doc_samples: Optional[int],
    total_limit: int,
    per_book: int,
) -> int:
    if doc_samples is None:
        return _sample_book_target(total_limit, per_book)
    return int(doc_samples)


def _has_sql_function(cur, fn_name: str) -> bool:
    try:
        row = cur.execute(
            "SELECT 1 FROM pragma_function_list WHERE name = ? LIMIT 1",
            (fn_name,),
        ).fetchone()
        return bool(row)
    except sqlite3.Error:
        return False


def _normal_groups_for_sampling(cur, groups: List[List[int]]) -> List[List[int]]:
    """
    Return groups that have at least one non-complement docpost source.
    These are typically useful for reducing candidate docs before near.
    """
    out: List[List[int]] = []
    for g in groups:
        if not g:
            continue
        placeholders = ",".join("?" for _ in g)
        try:
            rows = cur.execute(
                f"""
                SELECT docpost, docpost_is_complement
                FROM words
                WHERE cf_id IN ({placeholders})
                GROUP BY cf_id
                """,
                tuple(g),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        has_normal = any((r[0] is not None) and int(r[1] or 0) == 0 for r in rows)
        if has_normal:
            out.append(g)
    return out


def _plan_near_downsample_filter_ids(
    cur,
    groups: List[List[int]],
    base_filter_ids: Optional[List[int]],
    doc_samples: int,
) -> Tuple[Optional[List[int]], str]:
    """
    Planner heuristic for multi-term near:
    1) Use intersection of normal groups (non-complement docpost) when available.
    2) If no normal groups exist, sample from corpus/base filter directly.
    """
    if doc_samples <= 0:
        return None, "disabled"

    normal_groups = _normal_groups_for_sampling(cur, groups)
    if normal_groups:
        ids = _apply_docpost_filter_and_sample(
            cur,
            normal_groups,
            base_filter_ids,
            0,  # do not sample here; use full reduced intersection first
            0,
            0,
        )
        if ids == []:
            return [], "normal_intersection_empty"
        if ids:
            if len(ids) > doc_samples:
                return random.sample(ids, doc_samples), "normal_intersection_sampled"
            return ids, "normal_intersection"

    # No normal groups (or no usable reduction): explicit sample fallback.
    if base_filter_ids:
        if len(base_filter_ids) > doc_samples:
            return random.sample(base_filter_ids, doc_samples), "fallback_sample"
        return list(base_filter_ids), "fallback_full_base"
    return sample_urns(cur, doc_samples), "fallback_sample"


def _apply_docpost_filter_and_sample(
    cur,
    cf_groups: List[List[int]],
    base_filter_ids: Optional[List[int]],
    doc_samples: Optional[int],
    total_limit: int,
    per_book: int,
    sample_only_when_no_docpost: bool = False,
) -> Optional[List[int]]:
    filter_ids = list(base_filter_ids) if base_filter_ids else None
    docpost_ids = docpost_book_ids(cur, cf_groups)
    has_docpost_prefilter = docpost_ids is not None
    if docpost_ids is not None:
        if filter_ids:
            filter_set = set(filter_ids)
            filter_ids = [bid for bid in docpost_ids if bid in filter_set]
        else:
            filter_ids = docpost_ids

    sample_n = _resolve_doc_samples(doc_samples, total_limit, per_book)
    should_sample = sample_n > 0 and (
        not sample_only_when_no_docpost or not has_docpost_prefilter
    )
    if should_sample:
        if filter_ids:
            if len(filter_ids) > sample_n:
                filter_ids = random.sample(filter_ids, sample_n)
        else:
            filter_ids = sample_urns(cur, sample_n)

    return filter_ids


def _select_engine(engine: Optional[str]) -> str:
    selected = (engine or QUERY_ENGINE_DEFAULT or "python").strip().lower()
    if selected not in {"python", "julia"}:
        raise HTTPException(status_code=400, detail="engine must be one of: python, julia")
    if selected == "julia" and not JULIA_HYBRID_ENABLED:
        raise HTTPException(
            status_code=400,
            detail="Julia engine is disabled. Set POSTINGS_JULIA_HYBRID=1 to enable.",
        )
    return selected


def _resolve_match_mode(match_mode: Optional[str]) -> str:
    mode = (match_mode or "near").strip().lower()
    if mode not in {"near", "sequence"}:
        raise HTTPException(status_code=400, detail="matchMode must be one of: near, sequence")
    return mode


def _run_julia_probe_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    env = os.environ.copy()
    if JULIA_THREADS:
        env["JULIA_NUM_THREADS"] = JULIA_THREADS
    payload_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as tf:
            json.dump(payload, tf, ensure_ascii=False)
            payload_path = tf.name
        cp = subprocess.run(
            [JULIA_BIN, JULIA_PROBE_SCRIPT, payload_path],
            capture_output=True,
            text=True,
            timeout=JULIA_TIMEOUT_SECONDS,
            env=env,
            check=True,
        )
        out = cp.stdout.strip()
        if not out:
            raise HTTPException(status_code=500, detail="Julia probe returned empty output")
        try:
            return json.loads(out)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=500, detail=f"Julia probe returned invalid JSON: {exc}"
            ) from exc
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail="Julia probe timed out") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=f"Julia binary not found: {JULIA_BIN}") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        msg = f"Julia probe failed (exit {exc.returncode})"
        if stderr:
            lines = [ln for ln in stderr.splitlines() if ln.strip()]
            tail = " | ".join(lines[-3:]) if lines else stderr
            msg += f": {tail}"
        raise HTTPException(status_code=500, detail=msg) from exc
    finally:
        if payload_path:
            try:
                os.unlink(payload_path)
            except OSError:
                pass


def _run_julia_proxy_request(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if not JULIA_PROXY_URL:
        raise HTTPException(
            status_code=500,
            detail="Julia proxy URL is not configured (POSTINGS_JULIA_PROXY_URL).",
        )
    url = f"{JULIA_PROXY_URL}{path}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=JULIA_TIMEOUT_SECONDS) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            if not body:
                return {}
            try:
                return json.loads(body)
            except json.JSONDecodeError as exc:
                raise HTTPException(
                    status_code=500, detail=f"Julia proxy returned invalid JSON: {exc}"
                ) from exc
    except urllib.error.HTTPError as exc:
        detail = exc.reason
        try:
            body = exc.read().decode("utf-8", errors="replace")
            if body:
                parsed = json.loads(body)
                if isinstance(parsed, dict) and "detail" in parsed:
                    detail = parsed["detail"]
        except Exception:
            pass
        raise HTTPException(status_code=exc.code, detail=detail) from exc
    except urllib.error.URLError as exc:
        raise HTTPException(
            status_code=502, detail=f"Could not reach Julia proxy at {url}"
        ) from exc


def _julia_payload_from_near_query(req: NearQueryRequest) -> Dict[str, Any]:
    return {
        "terms": req.terms,
        "termGroups": req.termGroups,
        "window": req.window,
        "symmetric": req.symmetric,
        "maxVariants": req.maxVariants,
        "docSamples": int(req.docSamples or 0),
        "perBook": 0,
        "totalLimit": 0,
        "mode": "count",
        "repeats": 1,
        "useFilter": bool(req.useFilter),
        "filterIds": req.filterIds or [],
        "parallelShards": req.parallelShards
        if req.parallelShards is not None
        else JULIA_PARALLEL_SHARDS_DEFAULT,
    }


def _julia_payload_from_near_fragments(req: NearFragmentsRequest) -> Dict[str, Any]:
    mode = "fragments" if req.includeFragments else "hits"
    return {
        "terms": req.terms,
        "termGroups": req.termGroups,
        "window": req.window,
        "before": req.before,
        "after": req.after,
        "perBook": req.perBook,
        "docSamples": int(req.docSamples or 0),
        "totalLimit": req.totalLimit,
        "maxVariants": req.maxVariants,
        "symmetric": req.symmetric,
        "mode": mode,
        "repeats": 1,
        "useFilter": bool(req.useFilter),
        "filterIds": req.filterIds or [],
        "parallelShards": req.parallelShards
        if req.parallelShards is not None
        else JULIA_PARALLEL_SHARDS_DEFAULT,
    }


def _extract_julia_last_run(result: Dict[str, Any]) -> Dict[str, Any]:
    last = result.get("last_run")
    if isinstance(last, dict):
        return last
    return result


def _python_parallel_shards_enabled(parallel_shards: Optional[bool]) -> bool:
    return (
        parallel_shards
        if parallel_shards is not None
        else PYTHON_PARALLEL_SHARDS_DEFAULT
    )


def _max_python_workers(task_count: int) -> int:
    if task_count <= 1:
        return 1
    if PYTHON_SHARD_WORKERS > 0:
        return max(1, min(task_count, PYTHON_SHARD_WORKERS))
    return task_count


def _near_query_roaring_worker(task: Dict[str, Any]) -> Dict[str, Any]:
    con = connect_postings(task["postings_path"], task["ext_path"], task.get("sidecar_path"))
    try:
        cur = con.cursor()
        groups = task["groups"]
        schema = task["schema"]
        off_min = int(task["off_min"])
        off_max = int(task["off_max"])
        exclude_self = bool(task.get("exclude_self", False))
        match_mode = str(task.get("match_mode", "near"))
        filter_ids = task.get("filter_ids")
        base_filter_ids = task.get("base_filter_ids")
        book_ids = (
            list(filter_ids)
            if filter_ids is not None
            else candidate_books_for_groups(cur, groups, schema=schema, base_filter_ids=base_filter_ids)
        )
        total = 0
        docs = 0
        for book_id in book_ids:
            gp = group_positions_for_book(cur, groups, int(book_id), schema=schema)
            if match_mode == "sequence":
                c = sequence_count_from_groups(gp)
            else:
                c = near_count_from_groups(gp, off_min, off_max, exclude_self)
            if c > 0:
                total += c
                docs += 1
        return {
            "total": int(total),
            "docs": int(docs),
            "_worker": {"pid": os.getpid(), "shard": task["postings_path"]},
        }
    finally:
        con.close()


def _near_fragments_roaring_worker(task: Dict[str, Any]) -> Dict[str, Any]:
    con = connect_postings(task["postings_path"], task["ext_path"], task.get("sidecar_path"))
    conw = connect_words(task["words_path"])
    try:
        cur = con.cursor()
        curw = conw.cursor()
        groups = task["groups"]
        schema = task["schema"]
        off_min = int(task["off_min"])
        off_max = int(task["off_max"])
        before = int(task["before"])
        after = int(task["after"])
        per_book = int(task["per_book"])
        total_limit = int(task["total_limit"])
        include_fragments = bool(task["include_fragments"])
        exclude_self = bool(task.get("exclude_self", False))
        match_mode = str(task.get("match_mode", "near"))
        filter_ids = task.get("filter_ids")
        base_filter_ids = task.get("base_filter_ids")
        doc_samples = int(task.get("doc_samples") or 0)
        book_ids = (
            list(filter_ids)
            if filter_ids is not None
            else candidate_books_for_groups(cur, groups, schema=schema, base_filter_ids=base_filter_ids)
        )
        near_hits: List[Tuple[int, List[int]]] = []
        for book_id in book_ids:
            gp = group_positions_for_book(cur, groups, int(book_id), schema=schema)
            if match_mode == "sequence":
                pos = sequence_positions_from_groups(gp)
            else:
                pos = near_positions_from_groups(gp, off_min, off_max, exclude_self)
            if pos:
                near_hits.append((int(book_id), pos))
        if doc_samples > 0 and len(near_hits) > doc_samples:
            near_hits = random.sample(near_hits, doc_samples)
        rows: List[Dict[str, object]] = []
        for book_id, positions in near_hits:
            sampled = positions if len(positions) <= per_book else random.sample(positions, per_book)
            for ipos in sampled:
                frag = (
                    fetch_window(cur, curw, book_id, int(ipos), before, after)
                    if include_fragments
                    else ""
                )
                rows.append({"bookId": book_id, "pos": int(ipos), "frag": frag})
                if total_limit and len(rows) >= total_limit:
                    break
            if total_limit and len(rows) >= total_limit:
                break
        return {
            "rows": rows,
            "_worker": {"pid": os.getpid(), "shard": task["postings_path"]},
        }
    finally:
        con.close()
        conw.close()


def _or_query_worker(task: Dict[str, Any]) -> Dict[str, Any]:
    con = connect_postings(task["postings_path"], task["ext_path"], task.get("sidecar_path"))
    conw = connect_words(task["words_path"])
    try:
        cur = con.cursor()
        curw = conw.cursor()
        groups = _resolve_term_groups(
            curw, task.get("terms"), task.get("term_groups"), int(task["max_variants"]), symmetric=False
        )
        if not groups:
            return {"rows": [], "found_any": False, "_worker": {"pid": os.getpid(), "shard": task["postings_path"]}}
        or_cf_ids = sorted(set(cf for g in groups for cf in g))
        if not or_cf_ids:
            return {"rows": [], "found_any": False, "_worker": {"pid": os.getpid(), "shard": task["postings_path"]}}
        base_filter_ids = task.get("base_filter_ids")
        filter_ids = _apply_docpost_filter_and_sample(
            cur,
            [or_cf_ids],
            base_filter_ids,
            task.get("doc_samples"),
            int(task.get("total_limit") or 0),
            int(task.get("per_book") or 0),
        )
        if filter_ids == []:
            return {"rows": [], "found_any": False, "_worker": {"pid": os.getpid(), "shard": task["postings_path"]}}
        use_filter = bool(filter_ids)
        filter_json = json.dumps(filter_ids) if use_filter else None
        rows = sample_concordance_union(
            cur,
            curw,
            or_cf_ids,
            int(task["per_book"]),
            int(task["before"]),
            int(task["after"]),
            use_filter,
            filter_json,
        )
        local_limit = int(task.get("total_limit") or 0)
        if local_limit and len(rows) > local_limit:
            rows = rows[:local_limit]
        return {
            "rows": rows,
            "found_any": True,
            "_worker": {"pid": os.getpid(), "shard": task["postings_path"]},
        }
    finally:
        con.close()
        conw.close()


@app.post("/near_query")
def near_query(req: NearQueryRequest):
    if req.termGroups:
        if len(req.termGroups) < 2:
            raise HTTPException(status_code=400, detail="termGroups must contain at least two items")
    elif not req.terms or len(req.terms) < 2:
        raise HTTPException(status_code=400, detail="terms must contain at least two items")
    match_mode = _resolve_match_mode(req.matchMode)
    engine = _select_engine(req.engine)
    if engine == "julia":
        if match_mode == "sequence":
            raise HTTPException(
                status_code=400,
                detail="matchMode=sequence is currently supported only for engine=python",
            )
        payload = _julia_payload_from_near_query(req)
        if JULIA_PROXY_URL:
            last = _run_julia_proxy_request("/near_query", payload)
            out = {
                "total": int(last.get("total", 0)),
                "docs": int(last.get("docs", 0)),
                "_engine": "julia",
            }
            if PROFILE_NEAR and isinstance(last.get("_perf"), dict):
                out["_perf"] = last["_perf"]
            return out
        julia_out = _run_julia_probe_payload(payload)
        last = _extract_julia_last_run(julia_out)
        out = {
            "total": int(last.get("total", 0)),
            "docs": int(last.get("docs", 0)),
            "_engine": "julia",
        }
        if PROFILE_NEAR and isinstance(last.get("timings_ms"), dict):
            out["_perf"] = last["timings_ms"]
        return out
    postings_paths = CONFIG.postings_dbs
    total = 0
    docs = 0
    found_any = False
    parallel_shards = _python_parallel_shards_enabled(req.parallelShards)
    parallel_tasks: List[Dict[str, Any]] = []
    perf_workers: List[Dict[str, Any]] = []
    perf_prefilter: List[Dict[str, Any]] = []
    off_min = -req.window if req.symmetric else 1
    off_max = req.window
    schema_name = req.schema or CONFIG.default_schema
    if match_mode == "sequence" and schema_name != "unigrams":
        raise HTTPException(
            status_code=400,
            detail="matchMode=sequence currently requires schema=unigrams",
        )
    for shard_index, path in enumerate(postings_paths):
        con = connect_postings(path, CONFIG.ext_path, shard_sidecar_path(path, shard_index))
        conw = connect_words(shard_words_path(path))
        cur = con.cursor()
        curw = conw.cursor()
        base_filter_ids = req.filterIds if req.useFilter and req.filterIds else None
        use_filter = False
        filter_json = None

        groups = _resolve_term_groups(
            curw, req.terms, req.termGroups, req.maxVariants, symmetric=False
        )
        if not groups:
            con.close()
            conw.close()
            continue
        if not use_filter:
            ds = int(req.docSamples or 0)
            prefilter_strategy = "docpost_only"
            if ds > 0:
                filter_ids, prefilter_strategy = _plan_near_downsample_filter_ids(
                    cur, groups, base_filter_ids, ds
                )
            else:
                filter_ids = _apply_docpost_filter_and_sample(
                    cur,
                    groups,
                    base_filter_ids,
                    0,
                    0,
                    0,
                    sample_only_when_no_docpost=True,
                )
            if PROFILE_NEAR:
                perf_prefilter.append(
                    {
                        "shard": path,
                        "strategy": prefilter_strategy,
                        "doc_samples": ds,
                        "base_pool": len(base_filter_ids) if base_filter_ids else None,
                        "selected_docs": len(filter_ids) if filter_ids else 0,
                    }
                )
            if filter_ids == []:
                con.close()
                conw.close()
                continue
            if filter_ids:
                use_filter = True
                filter_json = json.dumps(filter_ids)
        found_any = True
        codec = detect_postings_codec(cur)
        if schema_name == "unigrams" and (codec == "roaring_v1" or match_mode == "sequence"):
            task = {
                "postings_path": path,
                "sidecar_path": shard_sidecar_path(path, shard_index),
                "ext_path": CONFIG.ext_path,
                "groups": groups,
                "schema": schema_name,
                "off_min": off_min,
                "off_max": off_max,
                "exclude_self": req.excludeSelf,
                "filter_ids": filter_ids if use_filter else None,
                "base_filter_ids": base_filter_ids,
                "match_mode": match_mode,
            }
            if parallel_shards and len(postings_paths) > 1:
                parallel_tasks.append(task)
            else:
                shard_res = _near_query_roaring_worker(task)
                total += int(shard_res.get("total", 0))
                docs += int(shard_res.get("docs", 0))
                if PROFILE_NEAR:
                    worker = shard_res.get("_worker")
                    if isinstance(worker, dict):
                        perf_workers.append(worker)
            con.close()
            conw.close()
            continue
        if match_mode == "sequence":
            raise HTTPException(
                status_code=400,
                detail="matchMode=sequence currently requires python worker path",
            )
        prepare_term_cf_table(cur, groups)
        use_bitmap_fn = USE_BITMAP_NEAR and _has_sql_function(
            cur, "post_near_count_bitmap_multi_groups"
        )
        if use_bitmap_fn:
            if use_filter:
                from_clause = "FROM filter f JOIN {table} u ON u.book_id = f.urn"
            else:
                from_clause = "FROM {table} u"
            if PREUNION_GROUPS:
                sql = f"""
                WITH
                {("filter AS (SELECT value AS urn FROM json_each(?))," if use_filter else "")}
                grouped AS (
                    SELECT u.book_id, t.grp, post_union_agg(u.post) AS gblob
                    {from_clause.format(table=(req.schema or CONFIG.default_schema))}
                    JOIN term_cf t ON t.cf_id = u.cf_id
                    GROUP BY u.book_id, t.grp
                ),
                combined AS (
                    SELECT book_id,
                           post_near_count_bitmap_multi_groups(
                               grp, gblob, {off_min}, {off_max}, {BITMAP_CHUNK_SIZE}
                           ) AS c
                    FROM grouped
                    GROUP BY book_id
                )
                SELECT
                    SUM(CASE WHEN c > 0 THEN c ELSE 0 END) AS total,
                    SUM(CASE WHEN c > 0 THEN 1 ELSE 0 END) AS docs
                FROM combined
                """
            else:
                sql = f"""
                WITH
                {("filter AS (SELECT value AS urn FROM json_each(?))," if use_filter else "")}
                combined AS (
                    SELECT u.book_id,
                           post_near_count_bitmap_multi_groups(
                               t.grp, u.post, {off_min}, {off_max}, {BITMAP_CHUNK_SIZE}
                           ) AS c
                    {from_clause.format(table=(req.schema or CONFIG.default_schema))}
                    JOIN term_cf t ON t.cf_id = u.cf_id
                    GROUP BY u.book_id
                )
                SELECT
                    SUM(CASE WHEN c > 0 THEN c ELSE 0 END) AS total,
                    SUM(CASE WHEN c > 0 THEN 1 ELSE 0 END) AS docs
                FROM combined
                """
            params = (filter_json,) if use_filter else ()
            row = cur.execute(sql, params).fetchone()
            if row:
                total += int(row[0] or 0)
                docs += int(row[1] or 0)
            if PROFILE_NEAR:
                perf_workers.append({"pid": os.getpid(), "shard": path, "mode": "sqlite_bitmap"})
        else:
            ctes = []
            if use_filter:
                ctes.append("filter AS (SELECT value AS urn FROM json_each(?))")
            for i in range(1, len(groups) + 1):
                ctes.append(union_cte(req.schema or CONFIG.default_schema, i, use_filter))

            select_cols = []
            join_clause = "FROM g1"
            for i in range(2, len(groups) + 1):
                join_clause += f" JOIN g{i} ON g{i}.book_id = g1.book_id"
            for i in range(2, len(groups) + 1):
                select_cols.append(
                    f"post_near_count(g1.blob, g{i}.blob, {off_min}, {off_max}) AS c{i}"
                )
            select_cols_sql = ", ".join(select_cols)
            where_all = " AND ".join([f"c{i} > 0" for i in range(2, len(groups) + 1)])
            sum_cols = " + ".join([f"c{i}" for i in range(2, len(groups) + 1)])
            sql = f"""
            WITH
            {", ".join(ctes)}
            SELECT
                SUM(CASE WHEN {where_all} THEN {sum_cols} ELSE 0 END) AS total,
                SUM(CASE WHEN {where_all} THEN 1 ELSE 0 END) AS docs
            FROM (
                SELECT g1.book_id, {select_cols_sql}
                {join_clause}
            )
            """
            params = (filter_json,) if use_filter else ()
            row = cur.execute(sql, params).fetchone()
            if row:
                total += int(row[0] or 0)
                docs += int(row[1] or 0)
            if PROFILE_NEAR:
                perf_workers.append({"pid": os.getpid(), "shard": path, "mode": "sqlite_join"})
        con.close()
        conw.close()
    if parallel_tasks:
        try:
            max_workers = _max_python_workers(len(parallel_tasks))
            with ProcessPoolExecutor(max_workers=max_workers) as ex:
                for shard_res in ex.map(_near_query_roaring_worker, parallel_tasks):
                    total += int(shard_res.get("total", 0))
                    docs += int(shard_res.get("docs", 0))
                    if PROFILE_NEAR:
                        worker = shard_res.get("_worker")
                        if isinstance(worker, dict):
                            perf_workers.append(worker)
        except Exception:
            # Safety fallback for environments where process spawning is restricted.
            for task in parallel_tasks:
                shard_res = _near_query_roaring_worker(task)
                total += int(shard_res.get("total", 0))
                docs += int(shard_res.get("docs", 0))
                if PROFILE_NEAR:
                    worker = shard_res.get("_worker")
                    if isinstance(worker, dict):
                        perf_workers.append(worker)
    if not found_any:
        raise HTTPException(status_code=404, detail="No terms matched")
    out: Dict[str, Any] = {"total": total, "docs": docs}
    if PROFILE_NEAR:
        out["_perf"] = {
            "workers": perf_workers,
            "prefilter": perf_prefilter,
            "parallel_requested": bool(parallel_shards),
            "parallel_tasks": len(parallel_tasks),
        }
    return out


@app.post("/or_query")
def or_query(req: OrQueryRequest):
    annotation_response = _run_annotation_namespace_query_or(req)
    if annotation_response is not None:
        return annotation_response

    if req.termGroups:
        if len(req.termGroups) < 1:
            raise HTTPException(status_code=400, detail="termGroups must contain at least one item")
    elif not req.terms:
        raise HTTPException(status_code=400, detail="terms must contain at least one item")

    postings_paths = CONFIG.postings_dbs
    rows: List[Tuple[int, int, str]] = []
    found_any = False
    parallel_shards = _python_parallel_shards_enabled(req.parallelShards)
    perf_workers: List[Dict[str, Any]] = []
    tasks: List[Dict[str, Any]] = []
    base_filter_ids = req.filterIds if req.useFilter and req.filterIds else None
    for shard_index, path in enumerate(postings_paths):
        tasks.append(
            {
                "postings_path": path,
                "words_path": shard_words_path(path),
                "sidecar_path": shard_sidecar_path(path, shard_index),
                "ext_path": CONFIG.ext_path,
                "terms": req.terms,
                "term_groups": req.termGroups,
                "max_variants": req.maxVariants,
                "before": req.before,
                "after": req.after,
                "per_book": req.perBook,
                "doc_samples": req.docSamples,
                "total_limit": req.totalLimit,
                "base_filter_ids": base_filter_ids,
            }
        )
    if parallel_shards and len(tasks) > 1:
        try:
            max_workers = _max_python_workers(len(tasks))
            with ProcessPoolExecutor(max_workers=max_workers) as ex:
                for shard_res in ex.map(_or_query_worker, tasks):
                    found_any = found_any or bool(shard_res.get("found_any"))
                    if PROFILE_NEAR:
                        worker = shard_res.get("_worker")
                        if isinstance(worker, dict):
                            perf_workers.append(worker)
                    shard_rows = shard_res.get("rows", [])
                    if req.totalLimit:
                        remaining = max(req.totalLimit - len(rows), 0)
                        if remaining <= 0:
                            break
                        if len(shard_rows) > remaining:
                            shard_rows = shard_rows[:remaining]
                    rows.extend(shard_rows)
                    if req.totalLimit and len(rows) >= req.totalLimit:
                        break
        except Exception:
            for task in tasks:
                shard_res = _or_query_worker(task)
                found_any = found_any or bool(shard_res.get("found_any"))
                if PROFILE_NEAR:
                    worker = shard_res.get("_worker")
                    if isinstance(worker, dict):
                        perf_workers.append(worker)
                shard_rows = shard_res.get("rows", [])
                if req.totalLimit:
                    remaining = max(req.totalLimit - len(rows), 0)
                    if remaining <= 0:
                        break
                    if len(shard_rows) > remaining:
                        shard_rows = shard_rows[:remaining]
                rows.extend(shard_rows)
                if req.totalLimit and len(rows) >= req.totalLimit:
                    break
    else:
        for task in tasks:
            shard_res = _or_query_worker(task)
            found_any = found_any or bool(shard_res.get("found_any"))
            if PROFILE_NEAR:
                worker = shard_res.get("_worker")
                if isinstance(worker, dict):
                    perf_workers.append(worker)
            shard_rows = shard_res.get("rows", [])
            if req.totalLimit:
                remaining = max(req.totalLimit - len(rows), 0)
                if remaining <= 0:
                    break
                if len(shard_rows) > remaining:
                    shard_rows = shard_rows[:remaining]
            rows.extend(shard_rows)
            if req.totalLimit and len(rows) >= req.totalLimit:
                break

    if not found_any:
        raise HTTPException(status_code=404, detail="No terms matched")
    if not rows:
        raise HTTPException(status_code=404, detail="No results found")
    if req.totalLimit and len(rows) > req.totalLimit:
        rows = rows[: req.totalLimit]
    out: Dict[str, Any] = {"rows": [{"bookId": b, "pos": p, "frag": f} for b, p, f in rows]}
    if PROFILE_NEAR:
        out["_perf"] = {
            "workers": perf_workers,
            "parallel_requested": bool(parallel_shards),
            "parallel_tasks": len(tasks),
        }
    return out


@app.post("/near_fragments")
def near_fragments(req: NearFragmentsRequest):
    if req.termGroups:
        if len(req.termGroups) < 2:
            raise HTTPException(status_code=400, detail="termGroups must contain at least two items")
    elif not req.terms or len(req.terms) < 2:
        raise HTTPException(status_code=400, detail="terms must contain at least two items")
    match_mode = _resolve_match_mode(req.matchMode)
    engine = _select_engine(req.engine)
    if engine == "julia":
        if match_mode == "sequence":
            raise HTTPException(
                status_code=400,
                detail="matchMode=sequence is currently supported only for engine=python",
            )
        payload = _julia_payload_from_near_fragments(req)
        if JULIA_PROXY_URL:
            out = _run_julia_proxy_request("/near_fragments", payload)
            if not isinstance(out, dict):
                raise HTTPException(status_code=500, detail="Julia proxy returned invalid response")
            out.setdefault("_engine", "julia")
            if req.totalLimit and isinstance(out.get("rows"), list) and len(out["rows"]) > req.totalLimit:
                out["rows"] = out["rows"][: req.totalLimit]
            return out
        julia_out = _run_julia_probe_payload(payload)
        last = _extract_julia_last_run(julia_out)
        raw_rows = last.get("rows", [])
        rows: List[Dict[str, object]] = []
        if isinstance(raw_rows, list):
            for r in raw_rows:
                if not isinstance(r, dict):
                    continue
                try:
                    book_id = int(r.get("bookId"))
                    pos = int(r.get("seq", r.get("pos")))
                except (TypeError, ValueError):
                    continue
                row = {"bookId": book_id, "pos": pos}
                row["frag"] = str(r.get("frag", "")) if req.includeFragments else ""
                rows.append(row)
        if req.totalLimit and len(rows) > req.totalLimit:
            rows = rows[: req.totalLimit]
        if not rows:
            raise HTTPException(status_code=404, detail="No near fragments found")
        out: Dict[str, object] = {"rows": rows, "_engine": "julia"}
        if PROFILE_NEAR and isinstance(last.get("timings_ms"), dict):
            out["_perf"] = last["timings_ms"]
        return out
    postings_paths = CONFIG.postings_dbs
    rows: List[Dict[str, object]] = []
    off_min = -req.window if req.symmetric else 1
    off_max = req.window
    schema_name = req.schema or CONFIG.default_schema
    if match_mode == "sequence" and schema_name != "unigrams":
        raise HTTPException(
            status_code=400,
            detail="matchMode=sequence currently requires schema=unigrams",
        )
    req_t0 = time.perf_counter()
    perf_shards: List[Dict[str, object]] = []
    perf_workers: List[Dict[str, Any]] = []
    parallel_shards = _python_parallel_shards_enabled(req.parallelShards)
    parallel_tasks: List[Dict[str, Any]] = []
    for shard_index, path in enumerate(postings_paths):
        shard_t0 = time.perf_counter()
        rows_before_shard = len(rows)
        shard_perf: Dict[str, object] = {
            "shard": path,
            "groups_ms": 0.0,
            "prefilter_ms": 0.0,
            "prefilter_strategy": "",
            "near_sql_ms": 0.0,
            "post_ms": 0.0,
            "candidates_json": 0,
            "candidates_blob": 0,
            "rows_added": 0,
            "total_ms": 0.0,
        }
        con = connect_postings(path, CONFIG.ext_path, shard_sidecar_path(path, shard_index))
        conw = connect_words(shard_words_path(path))
        cur = con.cursor()
        curw = conw.cursor()
        base_filter_ids = req.filterIds if req.useFilter and req.filterIds else None
        use_filter = False
        filter_json = None

        groups_t0 = time.perf_counter()
        groups = _resolve_term_groups(
            curw,
            req.terms,
            req.termGroups,
            req.maxVariants,
            False if match_mode == "sequence" else req.symmetric,
        )
        shard_perf["groups_ms"] = round((time.perf_counter() - groups_t0) * 1000.0, 3)
        if not groups:
            con.close()
            conw.close()
            shard_perf["rows_added"] = len(rows) - rows_before_shard
            shard_perf["total_ms"] = round((time.perf_counter() - shard_t0) * 1000.0, 3)
            perf_shards.append(shard_perf)
            continue
        pre_t0 = time.perf_counter()
        if not use_filter:
            ds = int(req.docSamples or 0)
            prefilter_strategy = "docpost_only"
            if ds > 0:
                filter_ids, prefilter_strategy = _plan_near_downsample_filter_ids(
                    cur, groups, base_filter_ids, ds
                )
            else:
                filter_ids = _apply_docpost_filter_and_sample(
                    cur,
                    groups,
                    base_filter_ids,
                    0,
                    req.totalLimit,
                    req.perBook,
                    sample_only_when_no_docpost=True,
                )
            if filter_ids == []:
                con.close()
                conw.close()
                continue
            if filter_ids:
                use_filter = True
                filter_json = json.dumps(filter_ids)
            shard_perf["prefilter_strategy"] = prefilter_strategy
        shard_perf["prefilter_ms"] = round((time.perf_counter() - pre_t0) * 1000.0, 3)
        codec = detect_postings_codec(cur)
        if schema_name == "unigrams" and (codec == "roaring_v1" or match_mode == "sequence"):
            task = {
                "postings_path": path,
                "words_path": shard_words_path(path),
                "sidecar_path": shard_sidecar_path(path, shard_index),
                "ext_path": CONFIG.ext_path,
                "groups": groups,
                "schema": schema_name,
                "off_min": off_min,
                "off_max": off_max,
                "before": req.before,
                "after": req.after,
                "per_book": req.perBook,
                "doc_samples": int(req.docSamples or 0),
                "total_limit": req.totalLimit,
                "include_fragments": req.includeFragments,
                "exclude_self": req.excludeSelf,
                "filter_ids": filter_ids if use_filter else None,
                "base_filter_ids": base_filter_ids,
                "match_mode": match_mode,
            }
            if parallel_shards and len(postings_paths) > 1:
                parallel_tasks.append(task)
                con.close()
                conw.close()
                shard_perf["rows_added"] = 0
                shard_perf["total_ms"] = round((time.perf_counter() - shard_t0) * 1000.0, 3)
                perf_shards.append(shard_perf)
                continue
            py_t0 = time.perf_counter()
            shard_res = _near_fragments_roaring_worker(task)
            shard_rows = shard_res.get("rows", [])
            worker = shard_res.get("_worker")
            if PROFILE_NEAR and isinstance(worker, dict):
                perf_workers.append(worker)
            if req.totalLimit:
                remaining = max(req.totalLimit - len(rows), 0)
                if remaining <= 0:
                    shard_rows = []
                elif len(shard_rows) > remaining:
                    shard_rows = shard_rows[:remaining]
            rows.extend(shard_rows)
            shard_perf["post_ms"] = round((time.perf_counter() - py_t0) * 1000.0, 3)
            con.close()
            conw.close()
            shard_perf["rows_added"] = len(rows) - rows_before_shard
            shard_perf["total_ms"] = round((time.perf_counter() - shard_t0) * 1000.0, 3)
            perf_shards.append(shard_perf)
            continue

        if match_mode == "sequence":
            raise HTTPException(
                status_code=400,
                detail="matchMode=sequence currently requires python worker path",
            )

        # No two-term special path: always use the general grouped near flow.

        sql_t0 = time.perf_counter()
        prepare_term_cf_table(cur, groups)
        inner = con.cursor()
        use_bitmap_fn = USE_BITMAP_NEAR and _has_sql_function(
            cur, "post_near_positions_bitmap_multi_groups"
        )
        candidates_blob: List[Tuple[int, bytes]] = []
        candidates_json: List[Tuple[int, str]] = []
        if use_bitmap_fn:
            if use_filter:
                from_clause = "FROM filter f JOIN {table} u ON u.book_id = f.urn"
            else:
                from_clause = "FROM {table} u"
            use_sampled_agg_fn = _has_sql_function(
                cur, "post_near_sample_positions_json_bitmap_multi_groups"
            )
            if use_sampled_agg_fn:
                if PREUNION_GROUPS:
                    sql = f"""
                    WITH
                    {("filter AS (SELECT value AS urn FROM json_each(?))," if use_filter else "")}
                    grouped AS (
                        SELECT u.book_id, t.grp, post_union_agg(u.post) AS gblob
                        {from_clause.format(table=(req.schema or CONFIG.default_schema))}
                        JOIN term_cf t ON t.cf_id = u.cf_id
                        GROUP BY u.book_id, t.grp
                    ),
                    combined AS (
                        SELECT book_id,
                               post_near_sample_positions_json_bitmap_multi_groups(
                                   grp, gblob, {off_min}, {off_max}, {BITMAP_CHUNK_SIZE}, {req.perBook}
                               ) AS pos_json
                        FROM grouped
                        GROUP BY book_id
                    )
                    SELECT book_id, pos_json FROM combined
                    """
                else:
                    sql = f"""
                    WITH
                    {("filter AS (SELECT value AS urn FROM json_each(?))," if use_filter else "")}
                    combined AS (
                        SELECT u.book_id,
                               post_near_sample_positions_json_bitmap_multi_groups(
                                   t.grp, u.post, {off_min}, {off_max}, {BITMAP_CHUNK_SIZE}, {req.perBook}
                               ) AS pos_json
                        {from_clause.format(table=(req.schema or CONFIG.default_schema))}
                        JOIN term_cf t ON t.cf_id = u.cf_id
                        GROUP BY u.book_id
                    )
                    SELECT book_id, pos_json FROM combined
                    """
            else:
                if PREUNION_GROUPS:
                    sql = f"""
                    WITH
                    {("filter AS (SELECT value AS urn FROM json_each(?))," if use_filter else "")}
                    grouped AS (
                        SELECT u.book_id, t.grp, post_union_agg(u.post) AS gblob
                        {from_clause.format(table=(req.schema or CONFIG.default_schema))}
                        JOIN term_cf t ON t.cf_id = u.cf_id
                        GROUP BY u.book_id, t.grp
                    ),
                    combined AS (
                        SELECT book_id,
                               post_near_positions_bitmap_multi_groups(
                                   grp, gblob, {off_min}, {off_max}, {BITMAP_CHUNK_SIZE}
                               ) AS blob
                        FROM grouped
                        GROUP BY book_id
                    )
                    SELECT book_id, blob FROM combined
                    """
                else:
                    sql = f"""
                    WITH
                    {("filter AS (SELECT value AS urn FROM json_each(?))," if use_filter else "")}
                    combined AS (
                        SELECT u.book_id,
                               post_near_positions_bitmap_multi_groups(
                                   t.grp, u.post, {off_min}, {off_max}, {BITMAP_CHUNK_SIZE}
                               ) AS blob
                        {from_clause.format(table=(req.schema or CONFIG.default_schema))}
                        JOIN term_cf t ON t.cf_id = u.cf_id
                        GROUP BY u.book_id
                    )
                    SELECT book_id, blob FROM combined
                    """
            params = (filter_json,) if use_filter else ()
            for row in cur.execute(sql, params):
                book_id = row[0]
                if use_sampled_agg_fn:
                    pos_json = row[1]
                    if not pos_json or pos_json == "[]":
                        continue
                    candidates_json.append((book_id, pos_json))
                else:
                    common_blob = row[1]
                    if not common_blob:
                        continue
                    candidates_blob.append((book_id, common_blob))
        else:
            cte_sql, select_sql, _ = groups_sql(
                groups, schema_name, use_filter
            )
            if use_filter:
                cte_sql = "filter AS (SELECT value AS urn FROM json_each(?)), " + cte_sql
            sql = f"""
            WITH
            {cte_sql}
            {select_sql}
            """
            params = (filter_json,) if use_filter else ()
            for row in cur.execute(sql, params):
                book_id = row[0]
                blobs = row[1:]
                # compute near positions blob from anchor (b1) to each other group
                common_blob = None
                for idx in range(1, len(blobs)):
                    res = inner.execute(
                        "SELECT post_near_positions_blob(?, ?, ?, ?)",
                        (blobs[0], blobs[idx], off_min, off_max),
                    ).fetchone()
                    if not res or res[0] is None:
                        common_blob = None
                        break
                    if common_blob is None:
                        common_blob = res[0]
                    else:
                        inter = inner.execute(
                            "SELECT post_intersect_blob(?, ?)", (common_blob, res[0])
                        ).fetchone()
                        common_blob = inter[0] if inter else None
                    if not common_blob:
                        break
                if not common_blob:
                    continue
                candidates_blob.append((book_id, common_blob))
        shard_perf["near_sql_ms"] = round((time.perf_counter() - sql_t0) * 1000.0, 3)
        if req.docSamples and req.docSamples > 0:
            if len(candidates_json) > req.docSamples:
                candidates_json = random.sample(candidates_json, req.docSamples)
            if len(candidates_blob) > req.docSamples:
                candidates_blob = random.sample(candidates_blob, req.docSamples)
        shard_perf["candidates_json"] = len(candidates_json)
        shard_perf["candidates_blob"] = len(candidates_blob)
        post_t0 = time.perf_counter()
        for book_id, pos_json in candidates_json:
            try:
                positions = json.loads(pos_json)
            except Exception:
                positions = []
            for pos in positions:
                ipos = int(pos)
                frag = (
                    fetch_window(cur, curw, book_id, ipos, req.before, req.after)
                    if req.includeFragments
                    else ""
                )
                rows.append({"bookId": book_id, "pos": ipos, "frag": frag})
                if req.totalLimit and len(rows) >= req.totalLimit:
                    break
            if req.totalLimit and len(rows) >= req.totalLimit:
                break
        use_sample_json_fn = _has_sql_function(inner, "post_sample_positions_json")
        for book_id, common_blob in candidates_blob:
            if use_sample_json_fn:
                srow = inner.execute(
                    "SELECT post_sample_positions_json(?, ?)", (common_blob, req.perBook)
                ).fetchone()
                if not srow or not srow[0]:
                    continue
                try:
                    positions = json.loads(srow[0])
                except Exception:
                    positions = []
                for pos in positions:
                    ipos = int(pos)
                    frag = (
                        fetch_window(cur, curw, book_id, ipos, req.before, req.after)
                        if req.includeFragments
                        else ""
                    )
                    rows.append({"bookId": book_id, "pos": ipos, "frag": frag})
                    if req.totalLimit and len(rows) >= req.totalLimit:
                        break
            else:
                cnt_row = inner.execute("SELECT post_count(?)", (common_blob,)).fetchone()
                total = int(cnt_row[0] or 0) if cnt_row else 0
                if total <= 0:
                    continue
                samples = min(req.perBook, total)
                indices = random.sample(range(total), samples)
                for idx in indices:
                    pos_row = inner.execute("SELECT post_sample(?, ?)", (common_blob, idx)).fetchone()
                    if pos_row is None or pos_row[0] is None:
                        continue
                    pos = int(pos_row[0])
                    frag = (
                        fetch_window(cur, curw, book_id, pos, req.before, req.after)
                        if req.includeFragments
                        else ""
                    )
                    rows.append({"bookId": book_id, "pos": int(pos), "frag": frag})
                    if req.totalLimit and len(rows) >= req.totalLimit:
                        break
            if req.totalLimit and len(rows) >= req.totalLimit:
                break
        shard_perf["post_ms"] = round((time.perf_counter() - post_t0) * 1000.0, 3)
        con.close()
        conw.close()
        shard_perf["rows_added"] = len(rows) - rows_before_shard
        shard_perf["total_ms"] = round((time.perf_counter() - shard_t0) * 1000.0, 3)
        perf_shards.append(shard_perf)
        if req.totalLimit and len(rows) >= req.totalLimit:
            break
    if parallel_tasks:
        try:
            max_workers = _max_python_workers(len(parallel_tasks))
            with ProcessPoolExecutor(max_workers=max_workers) as ex:
                for shard_res in ex.map(_near_fragments_roaring_worker, parallel_tasks):
                    shard_rows = shard_res.get("rows", [])
                    worker = shard_res.get("_worker")
                    if PROFILE_NEAR and isinstance(worker, dict):
                        perf_workers.append(worker)
                    if req.totalLimit:
                        remaining = max(req.totalLimit - len(rows), 0)
                        if remaining <= 0:
                            break
                        if len(shard_rows) > remaining:
                            shard_rows = shard_rows[:remaining]
                    rows.extend(shard_rows)
                    if req.totalLimit and len(rows) >= req.totalLimit:
                        break
        except Exception:
            # Safety fallback for environments where process spawning is restricted.
            for task in parallel_tasks:
                shard_res = _near_fragments_roaring_worker(task)
                shard_rows = shard_res.get("rows", [])
                worker = shard_res.get("_worker")
                if PROFILE_NEAR and isinstance(worker, dict):
                    perf_workers.append(worker)
                if req.totalLimit:
                    remaining = max(req.totalLimit - len(rows), 0)
                    if remaining <= 0:
                        break
                    if len(shard_rows) > remaining:
                        shard_rows = shard_rows[:remaining]
                rows.extend(shard_rows)
                if req.totalLimit and len(rows) >= req.totalLimit:
                    break
    if not rows:
        raise HTTPException(status_code=404, detail="No near fragments found")
    if PROFILE_NEAR:
        return {
            "rows": rows,
            "_perf": {
                "total_ms": round((time.perf_counter() - req_t0) * 1000.0, 3),
                "shards": perf_shards,
                "workers": perf_workers,
                "parallel_requested": bool(parallel_shards),
                "parallel_tasks": len(parallel_tasks),
            },
        }
    return {"rows": rows}


@app.post("/near_hits")
def near_hits(req: NearFragmentsRequest):
    req_hits = req.model_copy(update={"includeFragments": False})
    out = near_fragments(req_hits)
    rows = out.get("rows", [])
    hits = [{"bookId": int(r["bookId"]), "pos": int(r["pos"])} for r in rows]
    if "_perf" in out:
        return {"rows": hits, "_perf": out["_perf"]}
    return {"rows": hits}


@app.post("/render_hits")
def render_hits(req: RenderHitsRequest):
    if not req.rows:
        raise HTTPException(status_code=400, detail="rows must contain at least one item")

    target_rows = req.rows[: req.totalLimit]
    pending: List[Dict[str, int]] = []
    for row in target_rows:
        if "bookId" not in row or "pos" not in row:
            continue
        pending.append({"bookId": int(row["bookId"]), "pos": int(row["pos"])})

    if not pending:
        raise HTTPException(status_code=400, detail="rows must contain bookId and pos")

    out_rows, unresolved = _render_book_pos_rows(pending, int(req.before), int(req.after))

    if not out_rows:
        raise HTTPException(status_code=404, detail="No rows could be rendered")

    return {"rows": out_rows, "unresolved": unresolved}


@app.post("/collocations")
def collocations(req: CollocationsRequest):
    postings_paths = CONFIG.postings_dbs
    combined: Dict[str, int] = {}
    found_any = False
    for shard_index, path in enumerate(postings_paths):
        con = connect_postings(path, CONFIG.ext_path, shard_sidecar_path(path, shard_index))
        conw = connect_words(shard_words_path(path))
        cur = con.cursor()
        curw = conw.cursor()
        base_filter_ids = req.filterIds if req.useFilter and req.filterIds else None
        use_filter = False
        filter_json = None
        cf_id = get_cf_id(curw, req.word)
        if cf_id is None:
            con.close()
            conw.close()
            continue
        if not use_filter:
            filter_ids = _apply_docpost_filter_and_sample(
                cur,
                [[cf_id]],
                base_filter_ids,
                req.docSamples,
                50,
                req.perBook,
            )
            if filter_ids == []:
                con.close()
                conw.close()
                continue
            if filter_ids:
                use_filter = True
                filter_json = json.dumps(filter_ids)
        found_any = True
        counts = sample_collocations(
            cur,
            curw,
            cf_id,
            req.perBook,
            req.before,
            req.after,
            use_filter,
            filter_json,
            (req.schema or CONFIG.default_schema),
        )
        con.close()
        conw.close()
        for w, c in counts.items():
            combined[w] = combined.get(w, 0) + c
    if not found_any:
        raise HTTPException(status_code=404, detail="Word not found")
    top = sorted(combined.items(), key=lambda x: x[1], reverse=True)[:50]
    return {"rows": [{"word": w, "count": c} for w, c in top]}


# ----- Unified Imagination Endpoints -----

@app.post("/api/corpus/build")
def build_corpus(req: CorpusBuildRequest):
    conn = get_imagination_db()
    cursor = conn.cursor()

    # 1. Metadata Filtering
    query = "SELECT dhlabid, author, category, year FROM corpus WHERE 1=1"
    params = []

    if req.filters:
        if req.filters.category:
            query += " AND category = ?"
            params.append(req.filters.category)
        if req.filters.yearRange:
            query += " AND year >= ? AND year <= ?"
            params.extend(req.filters.yearRange)
        if req.filters.author:
            query += " AND author LIKE ?"
            params.append(f"%{req.filters.author}%")
            
    cursor.execute(query, params)
    rows = cursor.fetchall()
    dhlabids = [row["dhlabid"] for row in rows]
    
    # NOTE: Content keyword filtering (contentKeywords) is not implemented here 
    # to maintain strict decoupling between metadata and fulltext search.
    # Users should perform search via existing search endpoints and pass results 
    # as baseCorpus for intersection.

    # 2. Base Corpus Intersection (used for combining with search hits)
    if req.baseCorpus is not None:
        base_set = set(req.baseCorpus)
        dhlabids = [id for id in dhlabids if id in base_set]
        
    stats = {
        "totalBooks": len(dhlabids),
        "uniqueAuthors": len(set([row["author"] for row in rows if row["dhlabid"] in dhlabids])),
    }

    conn.close()
    return {"dhlabids": dhlabids, "stats": stats}


@app.post("/api/places")
def get_places(req: PlacesRequest):
    if not req.dhlabids:
        return {"places": []}
        
    conn = get_imagination_db()
    cursor = conn.cursor()
    
    import json
    try:
        # First, count total distinct places
        total_query = """
            WITH ids AS (
                SELECT value AS dhlabid FROM json_each(?)
            )
            SELECT COUNT(DISTINCT p.token) as total
            FROM ids
            JOIN books b ON b.dhlabid = ids.dhlabid
            JOIN places p ON p.token = b.token
            WHERE p.latitude IS NOT NULL 
                AND p.longitude IS NOT NULL
                AND p.latitude != ''
                AND p.longitude != ''
        """
        cursor.execute(total_query, [json.dumps(req.dhlabids)])
        total_places = cursor.fetchone()["total"]

        query = f"""
            WITH ids AS (
                SELECT value AS dhlabid FROM json_each(?)
            )
            SELECT 
                p.token, 
                p.modern as name, 
                CAST(p.latitude AS FLOAT) as lat, 
                CAST(p.longitude AS FLOAT) as lon, 
                COUNT(b.dhlabid) as doc_count, 
                SUM(b.book_count) as frequency
            FROM ids
            JOIN books b ON b.dhlabid = ids.dhlabid
            JOIN places p ON p.token = b.token
            WHERE p.latitude IS NOT NULL 
                AND p.longitude IS NOT NULL
                AND p.latitude != ''
                AND p.longitude != ''
            GROUP BY p.token, p.modern, p.latitude, p.longitude
            ORDER BY frequency DESC
            LIMIT ?
        """
        params = [json.dumps(req.dhlabids), req.maxPlaces]
        cursor.execute(query, params)
        rows = cursor.fetchall()
        places = [
            {"id": r["token"], "token": r["token"], "name": r["name"], "lat": r["lat"], "lon": r["lon"], 
             "frequency": r["frequency"], "doc_count": r["doc_count"]} 
            for r in rows
        ]
    except Exception as e:
        print(f"Places query error: {e}")
        places = []
        total_places = 0
    
    conn.close()
    return {"places": places, "total_places": total_places}


@app.post("/api/places/details")
def get_place_details(req: PlaceDetailsRequest):
    if not req.dhlabids:
        return {"books": []}
        
    conn = get_imagination_db()
    cursor = conn.cursor()
    
    import json
    try:
        query = f"""
            WITH ids AS (
                SELECT value AS dhlabid FROM json_each(?)
            )
            SELECT 
                c.dhlabid, c.urn, c.author, c.year, c.title, c.category,
                b.book_count as mentions
            FROM ids
            JOIN books b ON b.dhlabid = ids.dhlabid
            JOIN corpus c ON c.dhlabid = b.dhlabid
            WHERE b.token = ?
            ORDER BY b.book_count DESC
            LIMIT 500
        """
        params = [json.dumps(req.dhlabids), req.token]
        cursor.execute(query, params)
        books = [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        print(f"Fetch details error: {e}")
        books = []
        
    conn.close()
    return {"books": books}


@app.post("/api/books/metadata")
def get_metadata(req: MetadataRequest):
    if not req.dhlabids:
        return {"books": []}
    
    placeholders = ",".join(["?"] * len(req.dhlabids))
    conn = get_imagination_db()
    cursor = conn.cursor()
    
    query = f"SELECT dhlabid, urn, author, year, category FROM corpus WHERE dhlabid IN ({placeholders})"
    cursor.execute(query, req.dhlabids)
    rows = cursor.fetchall()
    
    books = [dict(r) for r in rows]
    conn.close()
    return {"books": books}


@app.get("/api/metadata/all")
def get_all_metadata():
    conn = get_imagination_db()
    cursor = conn.cursor()
    query = """
        SELECT 
            c.dhlabid, c.urn, c.author, c.year, c.category, c.title, 
            COUNT(DISTINCT b.token) as unique_places, 
            SUM(b.book_count) as total_mentions
        FROM corpus c
        LEFT JOIN books b ON c.dhlabid = b.dhlabid
        GROUP BY c.dhlabid
    """
    cursor.execute(query)
    rows = cursor.fetchall()
    
    books = [dict(r) for r in rows]
    conn.close()
    return {"books": books}
