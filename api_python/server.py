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
from typing import Any, Dict, List, Literal, Optional, Tuple

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from api_python.annotations import (
    fetch_geo_book_sequence,
    fetch_geo_spans,
    fetch_geo_spans_by_key,
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
    fetch_window_structured,
    group_frequency,
    group_positions_for_book,
    get_cf_id,
    near_count_from_groups,
    near_frequency,
    near_partner_popcount,
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
GEO_REQUIRE_CAPITALIZED = os.environ.get("POSTINGS_GEO_REQUIRE_CAPITALIZED", "1").strip() != "0"
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
AUTO_DOC_SAMPLE_MIN_CANDIDATES = int(
    os.environ.get("POSTINGS_AUTO_DOC_SAMPLE_MIN_CANDIDATES", "1000") or 1000
)
AUTO_DOC_SAMPLE_MULTIPLIER = int(
    os.environ.get("POSTINGS_AUTO_DOC_SAMPLE_MULTIPLIER", "20") or 20
)


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
    perBook: int = Field(3, ge=0, le=20)
    docSamples: Optional[int] = Field(None, ge=0, le=50000)
    totalLimit: int = Field(200, ge=0, le=5000)
    schema: Optional[str] = None
    useFilter: bool = False
    filterIds: List[int] = []
    symmetric: bool = True
    excludeSelf: bool = False
    renderMode: Literal["legacy", "structured"] = "legacy"


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
    before: int = Field(5, ge=1, le=50)
    after: int = Field(5, ge=1, le=50)
    perBook: int = Field(3, ge=0, le=20)
    totalLimit: int = Field(200, ge=0, le=5000)
    schema: Optional[str] = None
    symmetric: bool = True
    excludeSelf: bool = False
    useFilter: bool = False
    filterIds: List[int] = []
    docSamples: Optional[int] = Field(None, ge=0, le=50000)
    maxVariants: int = Field(10, ge=1, le=100)
    mode: Literal["count", "hits", "render"] = "count"
    countMode: Literal["auto", "anchor", "partner_popcount"] = "auto"
    engine: Optional[str] = None
    parallelShards: Optional[bool] = None
    matchMode: Optional[str] = None
    renderMode: Literal["legacy", "structured"] = "legacy"


class OrQueryRequest(BaseModel):
    terms: List[str] = []
    termGroups: Optional[List[List[str]]] = None
    before: int = Field(5, ge=1, le=25)
    after: int = Field(5, ge=1, le=25)
    perBook: int = Field(3, ge=0, le=20)
    docSamples: Optional[int] = Field(None, ge=0, le=50000)
    totalLimit: int = Field(200, ge=0, le=5000)
    schema: Optional[str] = None
    useFilter: bool = False
    filterIds: List[int] = []
    maxVariants: int = Field(10, ge=1, le=100)
    parallelShards: Optional[bool] = None
    renderHits: bool = False
    renderMode: Literal["legacy", "structured"] = "legacy"


class PlacesRequest(BaseModel):
    dhlabids: List[int] = []
    maxPlaces: int = Field(5000, ge=1, le=20000)


class PlaceDetailsRequest(BaseModel):
    dhlabids: List[int] = []
    token: str
    limit: int = Field(1000, ge=1, le=20000)


class PlaceFirstYearRequest(BaseModel):
    dhlabids: List[int] = []


class PlaceStatsRequest(BaseModel):
    dhlabids: List[int] = []
    maxFeatureCodes: int = Field(100, ge=1, le=1000)


class PlaceResolveRequest(BaseModel):
    query: Optional[str] = None
    id: Optional[str] = None
    limit: int = Field(10, ge=1, le=100)


class PlaceQaRequest(BaseModel):
    dhlabids: List[int] = []
    query: Optional[str] = None
    id: Optional[str] = None
    limit: int = Field(10, ge=1, le=100)
    maxSurfaces: int = Field(5, ge=1, le=20)


class GeoBookSequenceRequest(BaseModel):
    bookId: int
    namespace: str = "geo"
    limit: int = Field(50000, ge=1, le=500000)


class GeoAnnotationEditRequest(BaseModel):
    namespace: str = "geo"
    bookId: int
    seqStart: int = Field(..., ge=0)
    action: Literal["set_place", "clear"]
    nbPlaceId: Optional[int] = Field(None, ge=1)
    note: Optional[str] = None
    editor: Optional[str] = None
    rebuild: bool = True
    dropExisting: bool = False


class GeoAnnotationEditItem(BaseModel):
    bookId: int
    seqStart: int = Field(..., ge=0)
    action: Literal["set_place", "clear"]
    nbPlaceId: Optional[int] = Field(None, ge=1)
    note: Optional[str] = None
    editor: Optional[str] = None


class GeoAnnotationBatchEditRequest(BaseModel):
    namespace: str = "geo"
    edits: List[GeoAnnotationEditItem] = Field(..., min_length=1, max_length=2000)
    rebuild: bool = True
    dropExisting: bool = False


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


def _place_catalog_spec(cur: sqlite3.Cursor) -> Dict[str, Optional[str]]:
    places_cols = _table_columns(cur, "places")
    if not places_cols:
        raise HTTPException(status_code=500, detail="imagination_db is missing table: places")
    token_col = _pick_col(places_cols, ["token", "place_token"])
    name_col = _pick_col(places_cols, ["modern", "name", "canonical_name"])
    lat_col = _pick_col(places_cols, ["latitude", "lat"])
    lon_col = _pick_col(places_cols, ["longitude", "lon"])
    country_col = _pick_col(places_cols, ["area", "country", "country_code"])
    id_col = _pick_col(
        places_cols,
        ["mock_id", "nb_place_id", "geonameid", "geonames_id", "place_id", "id"],
    )
    if not token_col and name_col:
        # New geo_imagination.db is name/id-first and may not keep token column.
        token_col = name_col
    if not token_col:
        raise HTTPException(status_code=500, detail="places table must contain token/place_token")
    return {
        "token_col": token_col,
        "name_col": name_col,
        "lat_col": lat_col,
        "lon_col": lon_col,
        "country_col": country_col,
        "id_col": id_col,
    }


def _place_catalog_exprs(spec: Dict[str, Optional[str]]) -> Dict[str, str]:
    token_col = spec["token_col"]
    name_col = spec["name_col"]
    lat_col = spec["lat_col"]
    lon_col = spec["lon_col"]
    country_col = spec["country_col"]
    id_col = spec["id_col"]
    token_expr = f"COALESCE(p.{token_col}, '')"
    canonical_expr = f"COALESCE(p.{name_col}, p.{token_col})" if name_col else token_expr
    lat_expr = f"CAST(p.{lat_col} AS REAL)" if lat_col else "NULL"
    lon_expr = f"CAST(p.{lon_col} AS REAL)" if lon_col else "NULL"
    country_expr = f"p.{country_col}" if country_col else "NULL"
    if id_col:
        id_expr = f"COALESCE(CAST(p.{id_col} AS TEXT), {canonical_expr}, {token_expr})"
    else:
        id_expr = f"COALESCE({canonical_expr}, {token_expr})"
    return {
        "token": token_expr,
        "canonical": canonical_expr,
        "lat": lat_expr,
        "lon": lon_expr,
        "country": country_expr,
        "id": id_expr,
    }


def _place_kind_case_sql(
    place_alias: str = "p",
    feature_code_col: Optional[str] = "feature_code",
    feature_class_col: Optional[str] = "feature_class",
) -> str:
    feature_code = (
        f"COALESCE({place_alias}.{feature_code_col}, '')"
        if feature_code_col
        else "''"
    )
    feature_class = (
        f"COALESCE({place_alias}.{feature_class_col}, '')"
        if feature_class_col
        else "''"
    )
    return f"""
        CASE
            WHEN {feature_code} IN ('MT', 'MTS', 'PK', 'PKS', 'HLL', 'PASS', 'RDGE')
              THEN 'mountain'
            WHEN {feature_code} IN ('STM', 'STMI', 'STMX', 'STMH', 'WADI')
              THEN 'river'
            WHEN {feature_code} IN ('LK', 'LKS', 'RSV', 'SEA', 'GULF', 'BAY', 'COVE', 'OCN', 'CHN', 'CNL')
              THEN 'water'
            WHEN {feature_code} GLOB 'PPL*' OR {feature_class} = 'P'
              THEN 'settlement'
            WHEN {feature_code} IN ('PCLI', 'PCL', 'PCLS', 'PCLD', 'PCLF', 'ADM1', 'ADM2', 'ADM3', 'ADM4', 'ADM5', 'CONT')
              OR {feature_class} = 'A'
              THEN 'admin'
            WHEN {feature_code} IN ('ISL', 'ISLS')
              THEN 'island'
            WHEN {feature_class} = 'V'
              THEN 'vegetation'
            WHEN {feature_class} = 'T'
              THEN 'terrain'
            WHEN {feature_class} = 'H'
              THEN 'water'
            WHEN {feature_class} = 'R'
              THEN 'transport'
            WHEN {feature_class} = 'S'
              THEN 'spot'
            WHEN {feature_class} = 'L'
              THEN 'area'
            WHEN {feature_class} = 'U'
              THEN 'undersea'
            ELSE 'other'
        END
    """


def _place_kind_label(kind: str) -> str:
    labels = {
        "mountain": "Mountain",
        "river": "River",
        "water": "Water",
        "settlement": "Settlement",
        "admin": "Administrative",
        "island": "Island",
        "vegetation": "Vegetation",
        "terrain": "Terrain",
        "transport": "Transport",
        "spot": "Spot",
        "area": "Area",
        "undersea": "Undersea",
        "other": "Other",
    }
    return labels.get(kind, kind.title())


def _ranked_place_match_type(rank: int) -> str:
    if rank <= 1:
        return "exact"
    if rank == 2:
        return "prefix"
    return "contains"


def _format_place_matches(
    rows: List[Tuple[Any, ...]], prefer_canonical_matched_form: bool = False
) -> List[Dict[str, Any]]:
    grouped: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for row in rows:
        place_id = str(row[0] or "")
        canonical_name = str(row[1]) if row[1] is not None else ""
        matched_form = str(row[2]) if row[2] is not None else canonical_name
        lat = float(row[3]) if row[3] is not None else None
        lon = float(row[4]) if row[4] is not None else None
        country = str(row[5]) if row[5] is not None else None
        rank = int(row[6]) if row[6] is not None else 99
        if place_id not in grouped:
            grouped[place_id] = {
                "id": place_id,
                "canonicalName": canonical_name or matched_form or None,
                "matchedForm": matched_form or canonical_name or None,
                "alternateForms": [],
                "lat": lat,
                "lon": lon,
                "country": country,
                "matchType": _ranked_place_match_type(rank),
                "_forms": set(),
                "_rank": rank,
            }
            order.append(place_id)
        item = grouped[place_id]
        if rank < int(item["_rank"]):
            item["_rank"] = rank
            item["matchType"] = _ranked_place_match_type(rank)
            item["matchedForm"] = matched_form or canonical_name or None
        if item["canonicalName"] is None and canonical_name:
            item["canonicalName"] = canonical_name
        for form in (matched_form, canonical_name):
            clean = str(form or "").strip()
            if not clean:
                continue
            if clean == item["matchedForm"]:
                continue
            if clean not in item["_forms"]:
                item["_forms"].add(clean)
                item["alternateForms"].append(clean)
    out: List[Dict[str, Any]] = []
    for place_id in order:
        item = grouped[place_id]
        if prefer_canonical_matched_form and item.get("canonicalName"):
            canonical_name = str(item["canonicalName"])
            if canonical_name in item["alternateForms"]:
                item["alternateForms"] = [
                    form for form in item["alternateForms"] if form != canonical_name
                ]
            item["matchedForm"] = canonical_name
        item.pop("_forms", None)
        item.pop("_rank", None)
        out.append(item)
    return out


def _resolve_places_by_query(cur: sqlite3.Cursor, query: str, limit: int) -> List[Dict[str, Any]]:
    spec = _place_catalog_spec(cur)
    exprs = _place_catalog_exprs(spec)
    token_expr = exprs["token"]
    canonical_expr = exprs["canonical"]
    sql = f"""
        SELECT
            {exprs["id"]} AS id,
            {canonical_expr} AS canonical_name,
            {token_expr} AS matched_form,
            {exprs["lat"]} AS lat,
            {exprs["lon"]} AS lon,
            {exprs["country"]} AS country,
            CASE
                WHEN lower({token_expr}) = lower(?) THEN 0
                WHEN lower({canonical_expr}) = lower(?) THEN 1
                WHEN lower({token_expr}) LIKE lower(?) THEN 2
                ELSE 3
            END AS rank
        FROM places p
        WHERE lower({token_expr}) = lower(?)
           OR lower({canonical_expr}) = lower(?)
           OR lower({token_expr}) LIKE lower(?)
           OR lower({canonical_expr}) LIKE lower(?)
        ORDER BY rank, canonical_name, matched_form
        LIMIT ?
    """
    pattern_prefix = f"{query}%"
    pattern_contains = f"%{query}%"
    rows = cur.execute(
        sql,
        (
            query,
            query,
            pattern_prefix,
            query,
            query,
            pattern_prefix,
            pattern_contains,
            max(int(limit) * 10, int(limit)),
        ),
    ).fetchall()
    matches = _format_place_matches(rows)
    return matches[: int(limit)]


def _resolve_places_by_id(cur: sqlite3.Cursor, raw_id: str, limit: int) -> List[Dict[str, Any]]:
    spec = _place_catalog_spec(cur)
    exprs = _place_catalog_exprs(spec)
    token_expr = exprs["token"]
    canonical_expr = exprs["canonical"]
    sql = f"""
        SELECT
            {exprs["id"]} AS id,
            {canonical_expr} AS canonical_name,
            {token_expr} AS matched_form,
            {exprs["lat"]} AS lat,
            {exprs["lon"]} AS lon,
            {exprs["country"]} AS country,
            0 AS rank
        FROM places p
        WHERE {exprs["id"]} = ?
        ORDER BY canonical_name, matched_form
        LIMIT ?
    """
    rows = cur.execute(sql, (raw_id, max(int(limit) * 10, int(limit)))).fetchall()
    matches = _format_place_matches(rows, prefer_canonical_matched_form=True)
    return matches[: int(limit)]


def _load_nb_surface_tokens_for_places(
    dhlabids: List[int],
    nb_place_ids: List[int],
) -> Dict[int, str]:
    """
    Best-effort enrichment for /api/places in NB mode:
    choose the most frequent surface form from annotation rows in the requested books.
    """
    if not dhlabids or not nb_place_ids or not CONFIG.annotation_registry_db:
        return {}
    try:
        ns_meta = resolve_namespace(
            CONFIG.annotation_registry_db,
            "geo",
            base_dir=CONFIG.annotation_base_dir,
        )
    except Exception:
        return {}
    geo_db_path = str(ns_meta.get("db_path") or "").strip()
    if not geo_db_path:
        return {}
    con = sqlite3.connect(f"file:{geo_db_path}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        cur.execute("CREATE TEMP TABLE _book_filter_tokens(book_id INTEGER PRIMARY KEY)")
        cur.executemany(
            "INSERT OR IGNORE INTO _book_filter_tokens(book_id) VALUES (?)",
            ((int(book_id),) for book_id in dhlabids),
        )
        cur.execute("CREATE TEMP TABLE _place_filter_tokens(place_id INTEGER PRIMARY KEY)")
        cur.executemany(
            "INSERT OR IGNORE INTO _place_filter_tokens(place_id) VALUES (?)",
            ((int(pid),) for pid in nb_place_ids),
        )
        has_geo_spans = cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='geo_spans' LIMIT 1"
        ).fetchone()
        has_mentions_v2 = cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='geo_mentions_v2' LIMIT 1"
        ).fetchone()
        if has_geo_spans:
            sql = """
                SELECT
                    s.place_id AS place_id,
                    s.surface_text AS surface_text,
                    COUNT(*) AS cnt
                FROM geo_spans s
                JOIN _book_filter_tokens b ON b.book_id = s.book_id
                JOIN _place_filter_tokens p ON p.place_id = s.place_id
                WHERE trim(COALESCE(s.surface_text, '')) <> ''
                GROUP BY s.place_id, s.surface_text
                ORDER BY s.place_id, cnt DESC, LENGTH(s.surface_text) ASC, s.surface_text
            """
        elif has_mentions_v2:
            sql = """
                SELECT
                    m.place_id AS place_id,
                    m.surface_text AS surface_text,
                    COUNT(*) AS cnt
                FROM geo_mentions_v2 m
                JOIN _book_filter_tokens b ON b.book_id = m.book_id
                JOIN _place_filter_tokens p ON p.place_id = m.place_id
                WHERE trim(COALESCE(m.surface_text, '')) <> ''
                GROUP BY m.place_id, m.surface_text
                ORDER BY m.place_id, cnt DESC, LENGTH(m.surface_text) ASC, m.surface_text
            """
        else:
            return {}
        rows = cur.execute(sql).fetchall()
        out: Dict[int, str] = {}
        for place_id, surface_text, _cnt in rows:
            pid = int(place_id)
            if pid in out:
                continue
            out[pid] = str(surface_text)
        return out
    except Exception:
        return {}
    finally:
        con.close()


def _load_place_surface_tokens_from_imagination(
    cur: sqlite3.Cursor,
    place_ids: List[int],
) -> Dict[int, str]:
    if not place_ids:
        return {}
    psf_cols = _table_columns(cur, "place_surface_forms")
    if not psf_cols:
        return {}
    psf_place_col = _pick_col(psf_cols, ["mock_id", "nb_place_id", "place_id", "id"])
    surface_col = _pick_col(psf_cols, ["surface_text", "surface"])
    rank_col = _pick_col(psf_cols, ["rank"])
    if not psf_place_col or not surface_col or not rank_col:
        return {}
    ids_json = json.dumps([int(pid) for pid in place_ids])
    sql = f"""
        WITH ids AS (
            SELECT CAST(value AS INTEGER) AS place_id
            FROM json_each(?)
        )
        SELECT
            CAST(psf.{psf_place_col} AS INTEGER) AS place_id,
            psf.{surface_col} AS surface_text
        FROM place_surface_forms psf
        JOIN ids i ON i.place_id = psf.{psf_place_col}
        WHERE psf.{rank_col} = 1
          AND trim(COALESCE(psf.{surface_col}, '')) <> ''
        ORDER BY psf.{psf_place_col}
    """
    rows = cur.execute(sql, (ids_json,)).fetchall()
    return {int(place_id): str(surface_text) for place_id, surface_text in rows}


def _load_place_surface_stats_from_imagination(
    cur: sqlite3.Cursor,
    place_ids: List[int],
    max_surfaces: int = 5,
) -> Dict[int, List[Dict[str, Any]]]:
    if not place_ids:
        return {}
    psf_cols = _table_columns(cur, "place_surface_forms")
    if not psf_cols:
        return {}
    psf_place_col = _pick_col(psf_cols, ["mock_id", "nb_place_id", "place_id", "id"])
    surface_col = _pick_col(psf_cols, ["surface_text", "surface"])
    mentions_col = _pick_col(psf_cols, ["mentions", "count"])
    rank_col = _pick_col(psf_cols, ["rank"])
    if not psf_place_col or not surface_col or not mentions_col or not rank_col:
        return {}
    ids_json = json.dumps([int(pid) for pid in place_ids])
    sql = f"""
        WITH ids AS (
            SELECT CAST(value AS INTEGER) AS place_id
            FROM json_each(?)
        )
        SELECT
            CAST(psf.{psf_place_col} AS INTEGER) AS place_id,
            psf.{surface_col} AS surface_text,
            CAST(psf.{mentions_col} AS INTEGER) AS mentions
        FROM place_surface_forms psf
        JOIN ids i ON i.place_id = psf.{psf_place_col}
        WHERE trim(COALESCE(psf.{surface_col}, '')) <> ''
          AND psf.{rank_col} <= ?
        ORDER BY psf.{psf_place_col}, psf.{rank_col}
    """
    rows = cur.execute(sql, (ids_json, int(max_surfaces))).fetchall()
    out: Dict[int, List[Dict[str, Any]]] = {}
    for place_id, surface_text, mentions in rows:
        pid = int(place_id)
        items = out.setdefault(pid, [])
        items.append(
            {
                "surface": str(surface_text),
                "mentions": int(mentions or 0),
            }
        )
    return out


def _load_nb_surface_stats_for_places(
    dhlabids: List[int],
    nb_place_ids: List[int],
    max_surfaces: int = 5,
) -> Dict[int, List[Dict[str, Any]]]:
    if not dhlabids or not nb_place_ids or not CONFIG.annotation_registry_db:
        return {}
    try:
        ns_meta = resolve_namespace(
            CONFIG.annotation_registry_db,
            "geo",
            base_dir=CONFIG.annotation_base_dir,
        )
    except Exception:
        return {}
    geo_db_path = str(ns_meta.get("db_path") or "").strip()
    if not geo_db_path:
        return {}
    con = sqlite3.connect(f"file:{geo_db_path}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        cur.execute("CREATE TEMP TABLE _book_filter_surface(book_id INTEGER PRIMARY KEY)")
        cur.executemany(
            "INSERT OR IGNORE INTO _book_filter_surface(book_id) VALUES (?)",
            ((int(book_id),) for book_id in dhlabids),
        )
        cur.execute("CREATE TEMP TABLE _place_filter_surface(place_id INTEGER PRIMARY KEY)")
        cur.executemany(
            "INSERT OR IGNORE INTO _place_filter_surface(place_id) VALUES (?)",
            ((int(pid),) for pid in nb_place_ids),
        )
        has_geo_spans = cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='geo_spans' LIMIT 1"
        ).fetchone()
        has_mentions_v2 = cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='geo_mentions_v2' LIMIT 1"
        ).fetchone()
        if has_geo_spans:
            sql = """
                SELECT
                    s.place_id AS place_id,
                    s.surface_text AS surface_text,
                    COUNT(*) AS cnt
                FROM geo_spans s
                JOIN _book_filter_surface b ON b.book_id = s.book_id
                JOIN _place_filter_surface p ON p.place_id = s.place_id
                WHERE trim(COALESCE(s.surface_text, '')) <> ''
                GROUP BY s.place_id, s.surface_text
                ORDER BY s.place_id, cnt DESC, LENGTH(s.surface_text) ASC, s.surface_text
            """
        elif has_mentions_v2:
            sql = """
                SELECT
                    m.place_id AS place_id,
                    m.surface_text AS surface_text,
                    COUNT(*) AS cnt
                FROM geo_mentions_v2 m
                JOIN _book_filter_surface b ON b.book_id = m.book_id
                JOIN _place_filter_surface p ON p.place_id = m.place_id
                WHERE trim(COALESCE(m.surface_text, '')) <> ''
                GROUP BY m.place_id, m.surface_text
                ORDER BY m.place_id, cnt DESC, LENGTH(m.surface_text) ASC, m.surface_text
            """
        else:
            return {}
        rows = cur.execute(sql).fetchall()
        out: Dict[int, List[Dict[str, Any]]] = {}
        for place_id, surface_text, cnt in rows:
            pid = int(place_id)
            items = out.setdefault(pid, [])
            if len(items) >= int(max_surfaces):
                continue
            items.append(
                {
                    "surface": str(surface_text),
                    "mentions": int(cnt or 0),
                }
            )
        return out
    except Exception:
        return {}
    finally:
        con.close()


def _geo_db_path_for_namespace(namespace: str = "geo") -> Optional[str]:
    if not CONFIG.annotation_registry_db:
        return None
    try:
        ns_meta = resolve_namespace(
            CONFIG.annotation_registry_db,
            namespace,
            base_dir=CONFIG.annotation_base_dir,
        )
    except Exception:
        return None
    geo_db_path = str(ns_meta.get("db_path") or "").strip()
    return geo_db_path or None


def _count_exact_surface_mentions(
    dhlabids: List[int],
    surface: str,
    place_ids: Optional[List[int]] = None,
) -> int:
    if place_ids:
        counts = _count_exact_surface_mentions_by_place(dhlabids, surface, place_ids)
        return sum(int(v or 0) for v in counts.values())
    geo_db_path = _geo_db_path_for_namespace("geo")
    if not geo_db_path or not dhlabids or not str(surface or "").strip():
        return 0
    con = sqlite3.connect(f"file:{geo_db_path}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        cur.execute("CREATE TEMP TABLE _book_filter_surface_count(book_id INTEGER PRIMARY KEY)")
        cur.executemany(
            "INSERT OR IGNORE INTO _book_filter_surface_count(book_id) VALUES (?)",
            ((int(book_id),) for book_id in dhlabids),
        )
        params: List[Any] = [str(surface).strip()]
        place_join = ""
        place_where = ""
        if place_ids:
            cur.execute("CREATE TEMP TABLE _place_filter_surface_count(place_id INTEGER PRIMARY KEY)")
            cur.executemany(
                "INSERT OR IGNORE INTO _place_filter_surface_count(place_id) VALUES (?)",
                ((int(pid),) for pid in place_ids),
            )
            place_join = "JOIN _place_filter_surface_count p ON p.place_id = s.place_id"
        has_geo_spans = cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='geo_spans' LIMIT 1"
        ).fetchone()
        has_mentions_v2 = cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='geo_mentions_v2' LIMIT 1"
        ).fetchone()
        if has_geo_spans:
            sql = f"""
                SELECT COUNT(*)
                FROM geo_spans s
                JOIN _book_filter_surface_count b ON b.book_id = s.book_id
                {place_join}
                WHERE lower(COALESCE(s.surface_text, '')) = lower(?)
            """
        elif has_mentions_v2:
            if place_ids:
                place_join = "JOIN _place_filter_surface_count p ON p.place_id = m.place_id"
            sql = f"""
                SELECT COUNT(*)
                FROM geo_mentions_v2 m
                JOIN _book_filter_surface_count b ON b.book_id = m.book_id
                {place_join}
                WHERE lower(COALESCE(m.surface_text, '')) = lower(?)
            """
        else:
            return 0
        row = cur.execute(sql, tuple(params)).fetchone()
        return int(row[0] or 0) if row else 0
    except Exception:
        return 0
    finally:
        con.close()


def _count_exact_surface_mentions_by_place(
    dhlabids: List[int],
    surface: str,
    place_ids: List[int],
) -> Dict[int, int]:
    geo_db_path = _geo_db_path_for_namespace("geo")
    if not geo_db_path or not dhlabids or not str(surface or "").strip() or not place_ids:
        return {}
    con = sqlite3.connect(f"file:{geo_db_path}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        cur.execute("CREATE TEMP TABLE _book_filter_surface_count(book_id INTEGER PRIMARY KEY)")
        cur.executemany(
            "INSERT OR IGNORE INTO _book_filter_surface_count(book_id) VALUES (?)",
            ((int(book_id),) for book_id in dhlabids),
        )
        cur.execute("CREATE TEMP TABLE _place_filter_surface_count(place_id INTEGER PRIMARY KEY)")
        cur.executemany(
            "INSERT OR IGNORE INTO _place_filter_surface_count(place_id) VALUES (?)",
            ((int(pid),) for pid in place_ids),
        )
        has_geo_spans = cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='geo_spans' LIMIT 1"
        ).fetchone()
        has_mentions_v2 = cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='geo_mentions_v2' LIMIT 1"
        ).fetchone()
        if has_geo_spans:
            sql = """
                SELECT s.place_id, COUNT(*) AS mentions
                FROM geo_spans s
                JOIN _book_filter_surface_count b ON b.book_id = s.book_id
                JOIN _place_filter_surface_count p ON p.place_id = s.place_id
                WHERE lower(COALESCE(s.surface_text, '')) = lower(?)
                GROUP BY s.place_id
            """
        elif has_mentions_v2:
            sql = """
                SELECT m.place_id, COUNT(*) AS mentions
                FROM geo_mentions_v2 m
                JOIN _book_filter_surface_count b ON b.book_id = m.book_id
                JOIN _place_filter_surface_count p ON p.place_id = m.place_id
                WHERE lower(COALESCE(m.surface_text, '')) = lower(?)
                GROUP BY m.place_id
            """
        else:
            return {}
        out = {int(pid): 0 for pid in place_ids}
        for pid, mentions in cur.execute(sql, (str(surface).strip(),)).fetchall():
            if pid is None:
                continue
            out[int(pid)] = int(mentions or 0)
        return out
    except Exception:
        return {}
    finally:
        con.close()


def _term_exact_variants_local(term: str) -> List[str]:
    t = str(term or "")
    if not t:
        return []
    out: List[str] = []
    for candidate in (t.casefold(), t, t.lower(), t.title(), t.upper()):
        if candidate and candidate not in out:
            out.append(candidate)
    return out


def _count_word_frequency_for_corpus(dhlabids: List[int], term: str) -> int:
    if not dhlabids or not str(term or "").strip():
        return 0
    variants = _term_exact_variants_local(term)
    if not variants:
        return 0
    total = 0
    for db_path in CONFIG.postings_dbs:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            cur = con.cursor()
            placeholders = ",".join("?" for _ in variants)
            sql = f"""
                WITH filter AS (
                    SELECT CAST(value AS INTEGER) AS book_id
                    FROM json_each(?)
                ),
                term_cf AS (
                    SELECT DISTINCT cf_id
                    FROM words
                    WHERE word IN ({placeholders})
                )
                SELECT COALESCE(SUM(u.tf), 0)
                FROM unigrams u
                JOIN filter f ON f.book_id = u.book_id
                WHERE u.cf_id IN (SELECT cf_id FROM term_cf)
            """
            row = cur.execute(sql, (json.dumps([int(x) for x in dhlabids]), *variants)).fetchone()
            total += int(row[0] or 0) if row else 0
        finally:
            con.close()
    return total


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
        book_places_cols = _table_columns(cur, "book_places")
        books_dhlab_col = _pick_col(books_cols, ["dhlabid", "book_id"]) if books_cols else None
        books_token_col = _pick_col(books_cols, ["token", "place_token"]) if books_cols else None
        books_count_col = _pick_col(books_cols, ["book_count", "mentions", "count"]) if books_cols else None
        bp_dhlab_col = _pick_col(book_places_cols, ["dhlabid", "book_id"]) if book_places_cols else None
        bp_place_col = _pick_col(book_places_cols, ["mock_id", "nb_place_id", "place_id"]) if book_places_cols else None
        bp_count_col = _pick_col(book_places_cols, ["mentions", "book_count", "count"]) if book_places_cols else None
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
        elif bp_dhlab_col and bp_place_col:
            bp_count_expr = f"COALESCE({bp_count_col}, 1)" if bp_count_col else "1"
            books_join_sql = f"""
            LEFT JOIN (
                SELECT
                    {bp_dhlab_col} AS dhlabid,
                    COUNT(DISTINCT {bp_place_col}) AS unique_places_calc,
                    SUM({bp_count_expr}) AS total_mentions_calc
                FROM book_places
                GROUP BY {bp_dhlab_col}
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
        book_places_cols = _table_columns(cur, "book_places")
        places_cols = _table_columns(cur, "places")
        place_names_cols = _table_columns(cur, "place_names")
        if not places_cols:
            raise HTTPException(status_code=500, detail="imagination_db is missing table: places")
        dhlab_col = _pick_col(books_cols, ["dhlabid", "book_id"]) if books_cols else None
        token_col = _pick_col(books_cols, ["token", "place_token"]) if books_cols else None
        count_col = _pick_col(books_cols, ["book_count", "mentions", "count"]) if books_cols else None
        bp_dhlab_col = _pick_col(book_places_cols, ["dhlabid", "book_id"]) if book_places_cols else None
        bp_place_col = _pick_col(book_places_cols, ["mock_id", "nb_place_id", "place_id"]) if book_places_cols else None
        bp_count_col = _pick_col(book_places_cols, ["mentions", "book_count", "count"]) if book_places_cols else None
        places_token_col = _pick_col(places_cols, ["token", "place_token"])
        places_nb_col = _pick_col(places_cols, ["mock_id", "nb_place_id", "place_id", "id"])
        lat_col = _pick_col(places_cols, ["latitude", "lat"])
        lon_col = _pick_col(places_cols, ["longitude", "lon"])
        name_col = _pick_col(places_cols, ["modern", "name", "canonical_name"])
        id_col_places = _pick_col(
            places_cols,
            ["mock_id", "nb_place_id", "geonameid", "geonames_id", "place_id"],
        )
        id_col_books = _pick_col(books_cols, ["geonameid", "geonames_id", "place_id"]) if books_cols else None
        if not lat_col or not lon_col:
            raise HTTPException(
                status_code=500,
                detail="places table must contain latitude/longitude",
            )
        uses_place_id_model = False
        if books_cols and dhlab_col and token_col and places_token_col:
            count_expr = f"COALESCE(b.{count_col}, 1)" if count_col else "1"
            name_expr = f"COALESCE(p.{name_col}, b.{token_col})" if name_col else f"b.{token_col}"
            feature_code_expr = "NULLIF(TRIM(COALESCE(p.feature_code, '')), '')"
            kind_expr = _place_kind_case_sql(
                "p",
                feature_code_col=("feature_code" if "feature_code" in places_cols else None),
                feature_class_col=("feature_class" if "feature_class" in places_cols else None),
            )
            if id_col_places:
                id_expr = f"COALESCE(CAST(p.{id_col_places} AS TEXT), b.{token_col})"
            elif id_col_books:
                id_expr = f"COALESCE(CAST(b.{id_col_books} AS TEXT), b.{token_col})"
            else:
                id_expr = f"b.{token_col}"
            token_out_expr = f"b.{token_col}"
            sql_rows = f"""
            WITH filter AS (
                SELECT CAST(value AS INTEGER) AS dhlabid
                FROM json_each(?)
            ),
            agg AS (
                SELECT
                    {id_expr} AS id,
                    {token_out_expr} AS token,
                    {name_expr} AS name,
                    CAST(p.{lat_col} AS REAL) AS lat,
                    CAST(p.{lon_col} AS REAL) AS lon,
                    {feature_code_expr} AS feature_code,
                    {kind_expr} AS kind,
                    SUM({count_expr}) AS frequency,
                    COUNT(DISTINCT b.{dhlab_col}) AS doc_count
                FROM books b
                JOIN filter f ON f.dhlabid = b.{dhlab_col}
                LEFT JOIN places p ON p.{places_token_col} = b.{token_col}
                WHERE p.{lat_col} IS NOT NULL
                  AND p.{lon_col} IS NOT NULL
                GROUP BY 1, 2, 3, 4, 5, 6, 7
            )
            SELECT id, token, name, lat, lon, feature_code, kind, frequency, doc_count
            FROM agg
            ORDER BY frequency DESC
            LIMIT ?
            """
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
        elif book_places_cols and bp_dhlab_col and bp_place_col and places_nb_col:
            uses_place_id_model = True
            bp_count_expr = f"COALESCE(bp.{bp_count_col}, 1)" if bp_count_col else "1"
            token_expr = f"COALESCE(p.{name_col}, CAST(p.{places_nb_col} AS TEXT))" if name_col else f"CAST(p.{places_nb_col} AS TEXT)"
            place_names_id_col = _pick_col(place_names_cols, ["place_id", "nb_place_id", "mock_id"]) if place_names_cols else None
            place_names_canonical_col = (
                _pick_col(place_names_cols, ["canonical_name", "name"]) if place_names_cols else None
            )
            place_names_norwegian_col = (
                _pick_col(place_names_cols, ["norwegian_name", "primary_surface", "surface_text", "name"])
                if place_names_cols
                else None
            )
            place_names_surface_forms_col = (
                _pick_col(place_names_cols, ["surface_forms_json"]) if place_names_cols else None
            )
            place_names_join_sql = ""
            canonical_expr = (
                f"COALESCE(pn.{place_names_canonical_col}, p.{name_col}, CAST(p.{places_nb_col} AS TEXT))"
                if place_names_canonical_col and name_col
                else (
                    f"COALESCE(pn.{place_names_canonical_col}, CAST(p.{places_nb_col} AS TEXT))"
                    if place_names_canonical_col
                    else (
                        f"COALESCE(p.{name_col}, CAST(p.{places_nb_col} AS TEXT))"
                        if name_col
                        else f"CAST(p.{places_nb_col} AS TEXT)"
                    )
                )
            )
            norwegian_expr = (
                f"COALESCE(pn.{place_names_norwegian_col}, p.{name_col}, CAST(p.{places_nb_col} AS TEXT))"
                if place_names_norwegian_col and name_col
                else (
                    f"COALESCE(pn.{place_names_norwegian_col}, CAST(p.{places_nb_col} AS TEXT))"
                    if place_names_norwegian_col
                    else (
                        f"COALESCE(p.{name_col}, CAST(p.{places_nb_col} AS TEXT))"
                        if name_col
                        else f"CAST(p.{places_nb_col} AS TEXT)"
                    )
                )
            )
            surface_forms_expr = (
                f"COALESCE(pn.{place_names_surface_forms_col}, '[]')"
                if place_names_surface_forms_col
                else "'[]'"
            )
            if place_names_id_col:
                place_names_join_sql = f"LEFT JOIN place_names pn ON pn.{place_names_id_col} = p.{places_nb_col}"
            feature_code_expr = "NULLIF(TRIM(COALESCE(p.feature_code, '')), '')"
            kind_expr = _place_kind_case_sql(
                "p",
                feature_code_col=("feature_code" if "feature_code" in places_cols else None),
                feature_class_col=("feature_class" if "feature_class" in places_cols else None),
            )
            id_expr = f"CAST(p.{places_nb_col} AS TEXT)"
            sql_rows = f"""
            WITH filter AS (
                SELECT CAST(value AS INTEGER) AS dhlabid
                FROM json_each(?)
            ),
            agg AS (
                SELECT
                    {id_expr} AS id,
                    {token_expr} AS token,
                    {canonical_expr} AS canonical_name,
                    {norwegian_expr} AS norwegian_name,
                    {surface_forms_expr} AS surface_forms_json,
                    CAST(p.{lat_col} AS REAL) AS lat,
                    CAST(p.{lon_col} AS REAL) AS lon,
                    {feature_code_expr} AS feature_code,
                    {kind_expr} AS kind,
                    SUM({bp_count_expr}) AS frequency,
                    COUNT(DISTINCT bp.{bp_dhlab_col}) AS doc_count
                FROM book_places bp
                JOIN filter f ON f.dhlabid = bp.{bp_dhlab_col}
                JOIN places p ON p.{places_nb_col} = bp.{bp_place_col}
                {place_names_join_sql}
                WHERE p.{lat_col} IS NOT NULL
                  AND p.{lon_col} IS NOT NULL
                GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9
            )
            SELECT id, token, canonical_name, norwegian_name, surface_forms_json, lat, lon, feature_code, kind, frequency, doc_count
            FROM agg
            ORDER BY frequency DESC
            LIMIT ?
            """
            sql_total = f"""
            WITH filter AS (
                SELECT CAST(value AS INTEGER) AS dhlabid
                FROM json_each(?)
            )
            SELECT COUNT(*)
            FROM (
                SELECT bp.{bp_place_col}
                FROM book_places bp
                JOIN filter f ON f.dhlabid = bp.{bp_dhlab_col}
                JOIN places p ON p.{places_nb_col} = bp.{bp_place_col}
                WHERE p.{lat_col} IS NOT NULL
                  AND p.{lon_col} IS NOT NULL
                GROUP BY bp.{bp_place_col}
            )
            """
        else:
            raise HTTPException(
                status_code=500,
                detail="imagination_db must contain either books+places(token join) or book_places+places(nb_place_id join)",
            )
        dhlabids_json = json.dumps([int(x) for x in req.dhlabids])
        rows = cur.execute(sql_rows, (dhlabids_json, int(req.maxPlaces))).fetchall()
        total_places = int(cur.execute(sql_total, (dhlabids_json,)).fetchone()[0] or 0)
        place_ids_for_rows: List[int] = []
        if uses_place_id_model:
            for r in rows:
                try:
                    place_ids_for_rows.append(int(r[0]))
                except Exception:
                    continue
        surface_by_place_id = _load_place_surface_tokens_from_imagination(cur, place_ids_for_rows)
        if not surface_by_place_id and place_ids_for_rows:
            surface_by_place_id = _load_nb_surface_tokens_for_places(
                [int(x) for x in req.dhlabids],
                place_ids_for_rows,
            )
        places: List[Dict[str, Any]] = []
        for r in rows:
            place_id_text = str(r[0])
            place_id_value: Optional[int] = None
            if uses_place_id_model:
                try:
                    place_id_value = int(r[0])
                except Exception:
                    place_id_value = None
            token_value = str(r[1])
            if place_id_value is not None:
                token_value = surface_by_place_id.get(place_id_value, token_value)
            canonical_value = str(r[2]) if r[2] is not None else None
            norwegian_value = canonical_value
            surface_forms_value: List[Dict[str, Any]] = []
            if uses_place_id_model:
                norwegian_value = str(r[3]) if r[3] is not None else canonical_value
                if r[4]:
                    try:
                        parsed_surface_forms = json.loads(str(r[4]))
                        if isinstance(parsed_surface_forms, list):
                            surface_forms_value = parsed_surface_forms
                    except Exception:
                        surface_forms_value = []
            places.append(
                {
                    "id": place_id_text,
                    "nb_place_id": place_id_value,
                    "mock_id": place_id_value if uses_place_id_model and "mock_id" in places_cols else None,
                    "surface": token_value,
                    "canonicalName": canonical_value,
                    "norwegianName": norwegian_value,
                    "surfaceForms": surface_forms_value,
                    "token": token_value,
                    "name": norwegian_value if norwegian_value is not None else canonical_value,
                    "lat": float(r[5] if uses_place_id_model else r[3]),
                    "lon": float(r[6] if uses_place_id_model else r[4]),
                    "featureCode": str(r[7] if uses_place_id_model else r[5]) if (r[7] if uses_place_id_model else r[5]) is not None else None,
                    "kind": str(r[8] if uses_place_id_model else r[6]) if (r[8] if uses_place_id_model else r[6]) is not None else None,
                    "frequency": int((r[9] if uses_place_id_model else r[7]) or 0),
                    "doc_count": int((r[10] if uses_place_id_model else r[8]) or 0),
                }
            )
        return {"places": places, "total_places": total_places}
    finally:
        con.close()


@app.post("/api/places/first-year")
def imagination_places_first_year(req: PlaceFirstYearRequest):
    if not req.dhlabids:
        return {"rows": []}
    con = _connect_imagination_ro()
    try:
        cur = con.cursor()
        book_places_cols = _table_columns(cur, "book_places")
        places_cols = _table_columns(cur, "places")
        corpus_cols = _table_columns(cur, "corpus")
        if not book_places_cols or not places_cols or not corpus_cols:
            raise HTTPException(
                status_code=500,
                detail="imagination_db must contain book_places, places, and corpus for first-year",
            )
        bp_dhlab_col = _pick_col(book_places_cols, ["dhlabid", "book_id"])
        bp_place_col = _pick_col(book_places_cols, ["mock_id", "nb_place_id", "place_id"])
        places_id_col = _pick_col(places_cols, ["mock_id", "nb_place_id", "place_id", "id"])
        places_name_col = _pick_col(places_cols, ["name", "modern", "canonical_name"])
        corpus_dhlab_col = _pick_col(corpus_cols, ["dhlabid", "book_id"])
        corpus_year_col = _pick_col(corpus_cols, ["year", "pub_year"])
        if not bp_dhlab_col or not bp_place_col or not places_id_col or not corpus_dhlab_col or not corpus_year_col:
            raise HTTPException(
                status_code=500,
                detail="imagination_db schema is missing required columns for first-year",
            )
        token_expr = (
            f"COALESCE(p.{places_name_col}, CAST(p.{places_id_col} AS TEXT))"
            if places_name_col
            else f"CAST(p.{places_id_col} AS TEXT)"
        )
        dhlabids_json = json.dumps([int(x) for x in req.dhlabids])
        sql = f"""
            WITH filter AS (
                SELECT CAST(value AS INTEGER) AS dhlabid
                FROM json_each(?)
            ),
            place_years AS (
                SELECT
                    CAST(p.{places_id_col} AS TEXT) AS place_id,
                    {token_expr} AS token,
                    CAST(c.{corpus_year_col} AS INTEGER) AS year
                FROM book_places bp
                JOIN filter f ON f.dhlabid = bp.{bp_dhlab_col}
                JOIN corpus c ON c.{corpus_dhlab_col} = bp.{bp_dhlab_col}
                JOIN places p ON p.{places_id_col} = bp.{bp_place_col}
                WHERE c.{corpus_year_col} IS NOT NULL
            )
            SELECT
                place_id,
                token,
                MIN(year) AS year
            FROM place_years
            GROUP BY place_id, token
            ORDER BY year, token
        """
        rows = cur.execute(sql, (dhlabids_json,)).fetchall()
        place_ids_for_surface: List[int] = []
        for row in rows:
            try:
                place_ids_for_surface.append(int(row[0]))
            except Exception:
                continue
        surface_by_place_id = _load_place_surface_tokens_from_imagination(cur, place_ids_for_surface)
        if not surface_by_place_id and place_ids_for_surface:
            surface_by_place_id = _load_nb_surface_tokens_for_places(
                [int(x) for x in req.dhlabids],
                place_ids_for_surface,
            )
        out_rows: List[Dict[str, Any]] = []
        for place_id, token, year in rows:
            token_value = str(token) if token is not None else str(place_id)
            try:
                pid_int = int(place_id)
            except Exception:
                pid_int = None
            if pid_int is not None:
                token_value = surface_by_place_id.get(pid_int, token_value)
            out_rows.append(
                {
                    "place_id": str(place_id),
                    "token": token_value,
                    "year": int(year),
                }
            )
        return {"rows": out_rows}
    finally:
        con.close()


@app.post("/api/places/stats")
def imagination_places_stats(req: PlaceStatsRequest):
    if not req.dhlabids:
        return {
            "totals": {"uniquePlaces": 0, "mentions": 0, "docCount": 0},
            "kinds": [],
            "featureClasses": [],
            "featureCodes": [],
        }
    con = _connect_imagination_ro()
    try:
        cur = con.cursor()
        book_places_cols = _table_columns(cur, "book_places")
        places_cols = _table_columns(cur, "places")
        if not book_places_cols or not places_cols:
            raise HTTPException(
                status_code=500,
                detail="imagination_db must contain book_places and places for place stats",
            )
        bp_dhlab_col = _pick_col(book_places_cols, ["dhlabid", "book_id"])
        bp_place_col = _pick_col(book_places_cols, ["mock_id", "nb_place_id", "place_id"])
        bp_count_col = _pick_col(book_places_cols, ["mentions", "book_count", "count"])
        places_id_col = _pick_col(places_cols, ["mock_id", "nb_place_id", "place_id", "id"])
        if not bp_dhlab_col or not bp_place_col or not places_id_col:
            raise HTTPException(
                status_code=500,
                detail="imagination_db book_places/places schema is missing required id columns",
            )
        bp_count_expr = f"COALESCE(bp.{bp_count_col}, 1)" if bp_count_col else "1"
        dhlabids_json = json.dumps([int(x) for x in req.dhlabids])
        feature_code_col = "feature_code" if "feature_code" in places_cols else None
        feature_class_col = "feature_class" if "feature_class" in places_cols else None
        kind_expr = _place_kind_case_sql(
            "p",
            feature_code_col=feature_code_col,
            feature_class_col=feature_class_col,
        )
        feature_class_select_expr = (
            "COALESCE(NULLIF(TRIM(COALESCE(p.feature_class, '')), ''), 'unknown')"
            if feature_class_col
            else "'unknown'"
        )
        feature_code_select_expr = (
            "COALESCE(NULLIF(TRIM(COALESCE(p.feature_code, '')), ''), 'unknown')"
            if feature_code_col
            else "'unknown'"
        )

        totals_sql = f"""
            WITH filter AS (
                SELECT CAST(value AS INTEGER) AS dhlabid
                FROM json_each(?)
            )
            SELECT
                COUNT(DISTINCT bp.{bp_place_col}) AS unique_places,
                COALESCE(SUM({bp_count_expr}), 0) AS mentions,
                COUNT(DISTINCT bp.{bp_dhlab_col}) AS doc_count
            FROM book_places bp
            JOIN filter f ON f.dhlabid = bp.{bp_dhlab_col}
            JOIN places p ON p.{places_id_col} = bp.{bp_place_col}
        """
        feature_class_sql = f"""
            WITH filter AS (
                SELECT CAST(value AS INTEGER) AS dhlabid
                FROM json_each(?)
            )
            SELECT
                {feature_class_select_expr} AS feature_class,
                COUNT(DISTINCT bp.{bp_place_col}) AS unique_places,
                COALESCE(SUM({bp_count_expr}), 0) AS mentions,
                COUNT(DISTINCT bp.{bp_dhlab_col}) AS doc_count
            FROM book_places bp
            JOIN filter f ON f.dhlabid = bp.{bp_dhlab_col}
            JOIN places p ON p.{places_id_col} = bp.{bp_place_col}
            GROUP BY 1
            ORDER BY mentions DESC, unique_places DESC, feature_class
        """
        feature_code_sql = f"""
            WITH filter AS (
                SELECT CAST(value AS INTEGER) AS dhlabid
                FROM json_each(?)
            )
            SELECT
                {feature_code_select_expr} AS feature_code,
                {feature_class_select_expr} AS feature_class,
                {kind_expr} AS kind,
                COUNT(DISTINCT bp.{bp_place_col}) AS unique_places,
                COALESCE(SUM({bp_count_expr}), 0) AS mentions,
                COUNT(DISTINCT bp.{bp_dhlab_col}) AS doc_count
            FROM book_places bp
            JOIN filter f ON f.dhlabid = bp.{bp_dhlab_col}
            JOIN places p ON p.{places_id_col} = bp.{bp_place_col}
            GROUP BY 1, 2, 3
            ORDER BY mentions DESC, unique_places DESC, feature_code
            LIMIT ?
        """
        kind_sql = f"""
            WITH filter AS (
                SELECT CAST(value AS INTEGER) AS dhlabid
                FROM json_each(?)
            )
            SELECT
                {kind_expr} AS kind,
                COUNT(DISTINCT bp.{bp_place_col}) AS unique_places,
                COALESCE(SUM({bp_count_expr}), 0) AS mentions,
                COUNT(DISTINCT bp.{bp_dhlab_col}) AS doc_count
            FROM book_places bp
            JOIN filter f ON f.dhlabid = bp.{bp_dhlab_col}
            JOIN places p ON p.{places_id_col} = bp.{bp_place_col}
            GROUP BY 1
            ORDER BY mentions DESC, unique_places DESC, kind
        """

        totals_row = cur.execute(totals_sql, (dhlabids_json,)).fetchone()
        feature_class_rows = cur.execute(feature_class_sql, (dhlabids_json,)).fetchall()
        feature_code_rows = cur.execute(
            feature_code_sql,
            (dhlabids_json, int(req.maxFeatureCodes)),
        ).fetchall()
        kind_rows = cur.execute(kind_sql, (dhlabids_json,)).fetchall()

        return {
            "totals": {
                "uniquePlaces": int(totals_row[0] or 0),
                "mentions": int(totals_row[1] or 0),
                "docCount": int(totals_row[2] or 0),
            },
            "kinds": [
                {
                    "kind": str(row[0]),
                    "label": _place_kind_label(str(row[0])),
                    "uniquePlaces": int(row[1] or 0),
                    "mentions": int(row[2] or 0),
                    "docCount": int(row[3] or 0),
                }
                for row in kind_rows
            ],
            "featureClasses": [
                {
                    "featureClass": str(row[0]),
                    "uniquePlaces": int(row[1] or 0),
                    "mentions": int(row[2] or 0),
                    "docCount": int(row[3] or 0),
                }
                for row in feature_class_rows
            ],
            "featureCodes": [
                {
                    "featureCode": str(row[0]),
                    "featureClass": str(row[1]),
                    "kind": str(row[2]),
                    "label": _place_kind_label(str(row[2])),
                    "uniquePlaces": int(row[3] or 0),
                    "mentions": int(row[4] or 0),
                    "docCount": int(row[5] or 0),
                }
                for row in feature_code_rows
            ],
        }
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
        book_places_cols = _table_columns(cur, "book_places")
        places_cols = _table_columns(cur, "places")
        corpus_cols = _table_columns(cur, "corpus")
        if not corpus_cols:
            raise HTTPException(status_code=500, detail="imagination_db is missing table: corpus")
        b_dhlab_col = _pick_col(books_cols, ["dhlabid", "book_id"]) if books_cols else None
        c_dhlab_col = _pick_col(corpus_cols, ["dhlabid", "book_id"])
        token_col = _pick_col(books_cols, ["token", "place_token"]) if books_cols else None
        count_col = _pick_col(books_cols, ["book_count", "mentions", "count"]) if books_cols else None
        bp_dhlab_col = _pick_col(book_places_cols, ["dhlabid", "book_id"]) if book_places_cols else None
        bp_place_col = _pick_col(book_places_cols, ["mock_id", "nb_place_id", "place_id"]) if book_places_cols else None
        bp_count_col = _pick_col(book_places_cols, ["mentions", "book_count", "count"]) if book_places_cols else None
        places_name_col = _pick_col(places_cols, ["name", "modern", "canonical_name"]) if places_cols else None
        places_id_col = _pick_col(
            places_cols,
            ["mock_id", "nb_place_id", "place_id", "id", "geonames_id"],
        ) if places_cols else None
        if not c_dhlab_col:
            raise HTTPException(status_code=500, detail="corpus table must contain dhlabid/book_id")
        urn_expr = _optional_expr(corpus_cols, ["urn"], "urn")
        author_expr = _optional_expr(corpus_cols, ["author", "forfatter"], "author")
        year_expr = _optional_expr(corpus_cols, ["year", "pub_year"], "year", cast="INTEGER")
        title_expr = _optional_expr(corpus_cols, ["title", "titel"], "title")
        category_expr = _optional_expr(corpus_cols, ["category", "genre"], "category")
        dhlabids_json = json.dumps([int(x) for x in req.dhlabids])
        if books_cols and b_dhlab_col and token_col:
            count_expr = f"COALESCE(b.{count_col}, 1)" if count_col else "1"
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
        elif book_places_cols and bp_dhlab_col and bp_place_col and places_id_col:
            bp_count_expr = f"COALESCE(bp.{bp_count_col}, 1)" if bp_count_col else "1"
            place_match_clause = "CAST(p.{id_col} AS TEXT) = ?".format(id_col=places_id_col)
            params: List[Any] = [dhlabids_json, token]
            if places_name_col:
                place_match_clause += " OR lower(COALESCE(p.{name_col}, '')) = lower(?)".format(
                    name_col=places_name_col
                )
                params.append(token)
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
                SUM({bp_count_expr}) AS mentions
            FROM book_places bp
            JOIN filter f ON f.dhlabid = bp.{bp_dhlab_col}
            JOIN places p ON p.{places_id_col} = bp.{bp_place_col}
            JOIN corpus c ON c.{c_dhlab_col} = bp.{bp_dhlab_col}
            WHERE ({place_match_clause})
            GROUP BY c.{c_dhlab_col}, urn, author, year, title, category
            ORDER BY mentions DESC, year IS NULL, year, dhlabid
            LIMIT ?
            """
            params.append(int(req.limit))
            rows = cur.execute(sql, tuple(params)).fetchall()
        else:
            raise HTTPException(
                status_code=500,
                detail="imagination_db must contain either books token model or book_places place-id model",
            )
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


@app.post("/api/place/resolve")
def resolve_place(req: PlaceResolveRequest):
    query = str(req.query or "").strip()
    raw_id = str(req.id or "").strip()
    if bool(query) == bool(raw_id):
        raise HTTPException(status_code=400, detail="Provide exactly one of: query or id")
    con = _connect_imagination_ro()
    try:
        cur = con.cursor()
        if query:
            matches = _resolve_places_by_query(cur, query, int(req.limit))
        else:
            matches = _resolve_places_by_id(cur, raw_id, int(req.limit))
        return {"matches": matches}
    finally:
        con.close()


@app.post("/api/place/qa")
def place_qa(req: PlaceQaRequest):
    query = str(req.query or "").strip()
    raw_id = str(req.id or "").strip()
    if bool(query) == bool(raw_id):
        raise HTTPException(status_code=400, detail="Provide exactly one of: query or id")
    dhlabids = [int(x) for x in req.dhlabids]
    con = _connect_imagination_ro()
    try:
        cur = con.cursor()
        book_places_cols = _table_columns(cur, "book_places")
        places_cols = _table_columns(cur, "places")
        if not book_places_cols or not places_cols:
            raise HTTPException(
                status_code=500,
                detail="imagination_db must contain book_places and places for place QA",
            )
        bp_dhlab_col = _pick_col(book_places_cols, ["dhlabid", "book_id"])
        bp_place_col = _pick_col(book_places_cols, ["mock_id", "nb_place_id", "place_id"])
        bp_count_col = _pick_col(book_places_cols, ["mentions", "book_count", "count"])
        places_id_col = _pick_col(places_cols, ["mock_id", "nb_place_id", "place_id", "id"])
        name_col = _pick_col(places_cols, ["modern", "name", "canonical_name"])
        country_col = _pick_col(places_cols, ["area", "country", "country_code"])
        lat_col = _pick_col(places_cols, ["latitude", "lat"])
        lon_col = _pick_col(places_cols, ["longitude", "lon"])
        feature_code_col = _pick_col(places_cols, ["feature_code", "featureCode"])
        feature_class_col = _pick_col(places_cols, ["feature_class", "featureClass"])
        if not bp_dhlab_col or not bp_place_col or not places_id_col:
            raise HTTPException(
                status_code=500,
                detail="imagination_db book_places/places schema is missing required id columns",
            )
        if query:
            matches = _resolve_places_by_query(cur, query, int(req.limit))
        else:
            matches = _resolve_places_by_id(cur, raw_id, int(req.limit))

        dhlabids_json = json.dumps(dhlabids)
        bp_count_expr = f"COALESCE(bp.{bp_count_col}, 1)" if bp_count_col else "1"
        coverage_sql = f"""
            WITH filter AS (
                SELECT CAST(value AS INTEGER) AS dhlabid
                FROM json_each(?)
            ),
            geo_books AS (
                SELECT
                    bp.{bp_dhlab_col} AS dhlabid,
                    SUM({bp_count_expr}) AS mentions
                FROM book_places bp
                JOIN filter f ON f.dhlabid = bp.{bp_dhlab_col}
                GROUP BY bp.{bp_dhlab_col}
            )
            SELECT
                (SELECT COUNT(*) FROM filter) AS books_in_corpus,
                COUNT(DISTINCT g.dhlabid) AS books_with_geo,
                COALESCE(SUM(g.mentions), 0) AS total_geo_mentions,
                (
                    SELECT COUNT(DISTINCT bp2.{bp_place_col})
                    FROM book_places bp2
                    JOIN filter f2 ON f2.dhlabid = bp2.{bp_dhlab_col}
                ) AS unique_places
            FROM geo_books g
        """
        coverage_row = cur.execute(coverage_sql, (dhlabids_json,)).fetchone()
        books_in_corpus = int(coverage_row[0] or 0)
        books_with_geo = int(coverage_row[1] or 0)
        total_geo_mentions = int(coverage_row[2] or 0)
        unique_places = int(coverage_row[3] or 0)

        matched_ids = [str(m.get("id") or "").strip() for m in matches if str(m.get("id") or "").strip()]
        stats_by_id: Dict[str, Dict[str, Any]] = {}
        query_word_frequency = _count_word_frequency_for_corpus(dhlabids, query) if query else 0
        if matched_ids:
            ids_json = json.dumps(matched_ids)
            canonical_expr = f"COALESCE(p.{name_col}, CAST(p.{places_id_col} AS TEXT))" if name_col else f"CAST(p.{places_id_col} AS TEXT)"
            country_expr = f"p.{country_col}" if country_col else "NULL"
            lat_expr = f"CAST(p.{lat_col} AS REAL)" if lat_col else "NULL"
            lon_expr = f"CAST(p.{lon_col} AS REAL)" if lon_col else "NULL"
            feature_code_expr = f"NULLIF(TRIM(COALESCE(p.{feature_code_col}, '')), '')" if feature_code_col else "NULL"
            feature_class_expr = f"NULLIF(TRIM(COALESCE(p.{feature_class_col}, '')), '')" if feature_class_col else "NULL"
            kind_expr = _place_kind_case_sql(
                "p",
                feature_code_col=feature_code_col,
                feature_class_col=feature_class_col,
            )
            stats_sql = f"""
                WITH filter AS (
                    SELECT CAST(value AS INTEGER) AS dhlabid
                    FROM json_each(?)
                ),
                match_ids AS (
                    SELECT CAST(value AS TEXT) AS place_id
                    FROM json_each(?)
                ),
                agg AS (
                    SELECT
                        bp.{bp_place_col} AS place_id,
                        COUNT(DISTINCT bp.{bp_dhlab_col}) AS doc_count,
                        COALESCE(SUM({bp_count_expr}), 0) AS mentions
                    FROM book_places bp
                    JOIN filter f ON f.dhlabid = bp.{bp_dhlab_col}
                    GROUP BY bp.{bp_place_col}
                )
                SELECT
                    CAST(p.{places_id_col} AS TEXT) AS id,
                    {canonical_expr} AS canonical_name,
                    {country_expr} AS country,
                    {lat_expr} AS lat,
                    {lon_expr} AS lon,
                    {feature_code_expr} AS feature_code,
                    {feature_class_expr} AS feature_class,
                    {kind_expr} AS kind,
                    COALESCE(a.mentions, 0) AS mentions,
                    COALESCE(a.doc_count, 0) AS doc_count
                FROM match_ids m
                JOIN places p ON CAST(p.{places_id_col} AS TEXT) = m.place_id
                LEFT JOIN agg a ON CAST(a.place_id AS TEXT) = m.place_id
                ORDER BY canonical_name, id
            """
            for row in cur.execute(stats_sql, (dhlabids_json, ids_json)).fetchall():
                stats_by_id[str(row[0])] = {
                    "canonicalName": str(row[1]) if row[1] is not None else None,
                    "country": str(row[2]) if row[2] is not None else None,
                    "lat": float(row[3]) if row[3] is not None else None,
                    "lon": float(row[4]) if row[4] is not None else None,
                    "featureCode": str(row[5]) if row[5] is not None else None,
                    "featureClass": str(row[6]) if row[6] is not None else None,
                    "kind": str(row[7]) if row[7] is not None else None,
                    "placeMentions": int(row[8] or 0),
                    "docCount": int(row[9] or 0),
                }
        numeric_matched_ids = [int(mid) for mid in matched_ids if mid.isdigit()]
        tagged_surface_mentions_by_place = (
            _count_exact_surface_mentions_by_place(dhlabids, query, numeric_matched_ids)
            if query and numeric_matched_ids
            else {}
        )
        tagged_surface_mentions = (
            sum(int(v or 0) for v in tagged_surface_mentions_by_place.values())
            if query
            else 0
        )
        surface_stats = _load_place_surface_stats_from_imagination(
            cur,
            numeric_matched_ids,
            max_surfaces=int(req.maxSurfaces),
        ) if matched_ids and dhlabids else {}
        if not surface_stats and matched_ids and dhlabids:
            surface_stats = _load_nb_surface_stats_for_places(
                dhlabids,
                numeric_matched_ids,
                max_surfaces=int(req.maxSurfaces),
            )

        out_matches: List[Dict[str, Any]] = []
        for m in matches:
            place_id = str(m.get("id") or "").strip()
            meta = stats_by_id.get(place_id, {})
            doc_count = int(meta.get("docCount") or 0)
            tagged_surface_for_place = (
                int(tagged_surface_mentions_by_place.get(int(place_id), 0))
                if query and place_id.isdigit()
                else 0
            )
            out_matches.append(
                {
                    "id": place_id,
                    "canonicalName": meta.get("canonicalName") or m.get("canonicalName"),
                    "matchedForm": m.get("matchedForm"),
                    "alternateForms": m.get("alternateForms") or [],
                    "country": meta.get("country") or m.get("country"),
                    "lat": meta.get("lat") if meta.get("lat") is not None else m.get("lat"),
                    "lon": meta.get("lon") if meta.get("lon") is not None else m.get("lon"),
                    "featureCode": meta.get("featureCode"),
                    "featureClass": meta.get("featureClass"),
                    "kind": meta.get("kind"),
                    "placeMentions": int(meta.get("placeMentions") or 0),
                    "surfacePlaceMentions": tagged_surface_for_place,
                    "docCount": doc_count,
                    "docCoverageRate": (doc_count / books_in_corpus) if books_in_corpus else 0.0,
                    "wordFrequency": query_word_frequency if query else None,
                    "nonPlaceWordFrequency": (
                        max(query_word_frequency - tagged_surface_for_place, 0)
                        if query
                        else None
                    ),
                    "surfaceTagRatio": (
                        (tagged_surface_for_place / query_word_frequency)
                        if query and query_word_frequency > 0
                        else None
                    ),
                    "surfaceShareWithinTagged": (
                        (tagged_surface_for_place / tagged_surface_mentions)
                        if query and tagged_surface_mentions > 0
                        else None
                    ),
                    "topSurfaces": surface_stats.get(int(place_id), []) if place_id.isdigit() else [],
                }
            )

        return {
            "corpus": {
                "booksInCorpus": books_in_corpus,
                "booksWithGeo": books_with_geo,
                "coverageRate": (books_with_geo / books_in_corpus) if books_in_corpus else 0.0,
                "totalGeoMentions": total_geo_mentions,
                "uniquePlaces": unique_places,
                "queryWordFrequency": query_word_frequency if query else None,
                "queryTaggedSurfaceMentions": tagged_surface_mentions if query else None,
                "queryNonPlaceWordFrequency": (
                    max(query_word_frequency - tagged_surface_mentions, 0)
                    if query
                    else None
                ),
                "querySurfaceTagRatio": (
                    (tagged_surface_mentions / query_word_frequency)
                    if query and query_word_frequency > 0
                    else None
                ),
            },
            "matches": out_matches,
        }
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
        # New default contract: bare numeric #geo:<id> means NB internal place id.
        # Legacy geonames/internal forms remain supported when explicitly prefixed.
        return ("nb", v)
    m = re.match(r"^(geonames|internal|nb)\s*:\s*(.+)$", v, flags=re.IGNORECASE)
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
        def _table_exists(name: str) -> bool:
            row = cur.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
                (name,),
            ).fetchone()
            return bool(row)

        cur.execute("CREATE TEMP TABLE _book_filter(book_id INTEGER PRIMARY KEY)")
        cur.executemany(
            "INSERT OR IGNORE INTO _book_filter(book_id) VALUES (?)",
            ((int(book_id),) for book_id in book_ids),
        )
        rows_blob: List[Tuple[int, bytes]] = []
        rows_pos: List[Tuple[int, int]] = []
        if geo_key:
            if _table_exists("geo_postings_v2"):
                rows_blob = cur.execute(
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
                if not rows_blob:
                    # Some exports may not materialize token_len=0 rollups.
                    # Fallback: merge all token_len rows for the requested key.
                    rows_blob = cur.execute(
                        """
                        SELECT p.book_id, p.starts_roaring
                        FROM geo_postings_v2 p
                        JOIN _book_filter f ON f.book_id = p.book_id
                        WHERE p.place_key_type = ?
                          AND p.place_key = ?
                        """,
                        (geo_key[0], geo_key[1]),
                    ).fetchall()
            elif _table_exists("geo_mentions_v2"):
                rows_pos = cur.execute(
                    """
                    SELECT m.book_id, m.seq_start
                    FROM geo_mentions_v2 m
                    JOIN _book_filter f ON f.book_id = m.book_id
                    WHERE m.place_key_type = ?
                      AND m.place_key = ?
                    ORDER BY m.book_id, m.seq_start
                    """,
                    (geo_key[0], geo_key[1]),
                ).fetchall()
            elif _table_exists("geo_spans"):
                if geo_key[0] == "geonames" and _table_exists("places"):
                    rows_pos = cur.execute(
                        """
                        SELECT s.book_id, s.seq_start
                        FROM geo_spans s
                        JOIN _book_filter f ON f.book_id = s.book_id
                        JOIN places p ON p.place_id = s.place_id
                        WHERE CAST(p.geonames_id AS TEXT) = ?
                        ORDER BY s.book_id, s.seq_start
                        """,
                        (geo_key[1],),
                    ).fetchall()
                elif geo_key[0] == "internal":
                    rows_pos = cur.execute(
                        """
                        SELECT s.book_id, s.seq_start
                        FROM geo_spans s
                        JOIN _book_filter f ON f.book_id = s.book_id
                        WHERE CAST(s.place_id AS TEXT) = ?
                        ORDER BY s.book_id, s.seq_start
                        """,
                        (geo_key[1],),
                    ).fetchall()
        else:
            if _table_exists("geo_postings_all"):
                rows_blob = cur.execute(
                    """
                    SELECT p.book_id, p.post_blob
                    FROM geo_postings_all p
                    JOIN _book_filter f ON f.book_id = p.book_id
                    """
                ).fetchall()
            elif _table_exists("geo_book_index_v2"):
                rows_blob = cur.execute(
                    """
                    SELECT b.book_id, b.all_places_roaring
                    FROM geo_book_index_v2 b
                    JOIN _book_filter f ON f.book_id = b.book_id
                    """
                ).fetchall()
            elif _table_exists("geo_spans"):
                rows_pos = cur.execute(
                    """
                    SELECT s.book_id, s.seq_start
                    FROM geo_spans s
                    JOIN _book_filter f ON f.book_id = s.book_id
                    ORDER BY s.book_id, s.seq_start
                    """
                ).fetchall()
        out: Dict[int, List[int]] = {}
        for book_id, blob in rows_blob:
            pos = _decode_roaring_positions(blob)
            if pos:
                bid = int(book_id)
                arr = out.get(bid)
                if arr is None:
                    arr = []
                    out[bid] = arr
                arr.extend(int(x) for x in pos)
        if rows_blob:
            for bid, arr in out.items():
                out[bid] = sorted(set(arr))
        if rows_pos:
            for book_id, seq_start in rows_pos:
                bid = int(book_id)
                arr = out.get(bid)
                if arr is None:
                    arr = []
                    out[bid] = arr
                arr.append(int(seq_start))
            for bid, arr in out.items():
                out[bid] = sorted(set(arr))
        return out
    finally:
        con.close()


def _is_surface_capitalized(surface_text: Optional[str]) -> bool:
    s = str(surface_text or "").strip()
    if not s:
        return False
    for ch in s:
        if not ch.isalpha():
            continue
        return ch.isupper()
    return False


def _load_geo_capitalized_positions_from_mentions(
    geo_db_path: str,
    book_ids: List[int],
    geo_key: Tuple[str, str],
) -> Dict[int, List[int]]:
    if not book_ids:
        return {}
    con = sqlite3.connect(f"file:{geo_db_path}?mode=ro", uri=True)
    cur = con.cursor()
    try:
        has_mentions = cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='geo_mentions_v2' LIMIT 1"
        ).fetchone()
        if not has_mentions:
            return {}
        cur.execute("CREATE TEMP TABLE _book_filter_cap(book_id INTEGER PRIMARY KEY)")
        cur.executemany(
            "INSERT OR IGNORE INTO _book_filter_cap(book_id) VALUES (?)",
            ((int(book_id),) for book_id in book_ids),
        )
        rows = cur.execute(
            """
            SELECT m.book_id, m.seq_start, m.surface_text
            FROM geo_mentions_v2 m
            JOIN _book_filter_cap f ON f.book_id = m.book_id
            WHERE m.place_key_type = ?
              AND m.place_key = ?
            ORDER BY m.book_id, m.seq_start
            """,
            (geo_key[0], geo_key[1]),
        ).fetchall()
        out: Dict[int, List[int]] = {}
        for book_id, seq_start, surface_text in rows:
            if not _is_surface_capitalized(surface_text):
                continue
            bid = int(book_id)
            arr = out.get(bid)
            if arr is None:
                arr = []
                out[bid] = arr
            arr.append(int(seq_start))
        for bid, arr in out.items():
            out[bid] = sorted(set(arr))
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
        table_ok = cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='geo_mentions_v2' LIMIT 1"
        ).fetchone()
        if not table_ok:
            # Some prod exports intentionally omit geo_mentions_v2 to stay compact.
            # In that case we return empty meta and let callers use safe defaults.
            return {}
        cur.execute(
            "CREATE TEMP TABLE _hit_filter_meta(book_id INTEGER NOT NULL, seq_start INTEGER NOT NULL, PRIMARY KEY(book_id, seq_start))"
        )
        cur.executemany(
            "INSERT OR IGNORE INTO _hit_filter_meta(book_id, seq_start) VALUES (?, ?)",
            ((int(book_id), int(pos)) for book_id, pos in hits),
        )
        params: List[Any] = []
        sql = """
            SELECT
              h.book_id,
              h.seq_start,
              m.token_len,
              m.surface_text,
              m.place_key_type,
              m.place_key,
              m.place_id,
              COALESCE(m.geonames_id, p.geonames_id) AS geonames_id,
              p.canonical_name,
              p.lat,
              p.lon,
              p.country,
              pv.variant_text
            FROM _hit_filter_meta h
            JOIN geo_mentions_v2 m
              ON m.book_id = h.book_id
             AND m.seq_start = h.seq_start
            LEFT JOIN places p ON p.place_id = m.place_id
            LEFT JOIN place_variants pv ON pv.variant_id = m.variant_id
        """
        if geo_key:
            sql += """
            WHERE m.place_key_type = ?
              AND m.place_key = ?
            """
            params.extend([geo_key[0], geo_key[1]])
        sql += " ORDER BY h.book_id, h.seq_start, m.token_len DESC"
        rows = cur.execute(sql, tuple(params)).fetchall()
        out: Dict[Tuple[int, int], Dict[str, Any]] = {}
        for row in rows:
            key = (int(row[0]), int(row[1]))
            if key in out:
                # Highest token_len row comes first due to ORDER BY.
                continue
            out[key] = {
                "tokenLen": int(row[2]),
                "surfaceText": str(row[3]) if row[3] is not None else None,
                "placeKeyType": str(row[4]) if row[4] is not None else None,
                "placeKey": str(row[5]) if row[5] is not None else None,
                "placeId": int(row[6]) if row[6] is not None else None,
                "geonamesId": int(row[7]) if row[7] is not None else None,
                "place": {
                    "canonicalName": str(row[8]) if row[8] is not None else None,
                    "geonamesId": int(row[7]) if row[7] is not None else None,
                    "lat": float(row[9]) if row[9] is not None else None,
                    "lon": float(row[10]) if row[10] is not None else None,
                    "country": str(row[11]) if row[11] is not None else None,
                    "variantText": str(row[12]) if row[12] is not None else None,
                },
            }
        return out
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        if "no such table" in msg and "geo_mentions_v2" in msg:
            return {}
        raise
    finally:
        con.close()


def _lookup_geo_place_meta(
    geo_db_path: str,
    geo_key: Tuple[str, str],
) -> Dict[str, Any]:
    con = sqlite3.connect(f"file:{geo_db_path}?mode=ro", uri=True)
    cur = con.cursor()
    try:
        table_ok = cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='places' LIMIT 1"
        ).fetchone()
        if not table_ok:
            return {
                "placeId": int(geo_key[1]) if geo_key[0] in {"internal", "nb"} and str(geo_key[1]).isdigit() else None,
                "geonamesId": int(geo_key[1]) if geo_key[0] == "geonames" and str(geo_key[1]).isdigit() else None,
                "place": {
                    "canonicalName": None,
                    "lat": None,
                    "lon": None,
                    "country": None,
                    "variantText": None,
                },
            }
        if geo_key[0] == "geonames":
            row = cur.execute(
                """
                SELECT place_id, geonames_id, canonical_name, lat, lon, country
                FROM places
                WHERE CAST(geonames_id AS TEXT) = ?
                ORDER BY place_id
                LIMIT 1
                """,
                (geo_key[1],),
            ).fetchone()
        else:
            row = cur.execute(
                """
                SELECT place_id, geonames_id, canonical_name, lat, lon, country
                FROM places
                WHERE CAST(place_id AS TEXT) = ?
                LIMIT 1
                """,
                (geo_key[1],),
            ).fetchone()
        if not row:
            return {
                "placeId": int(geo_key[1]) if geo_key[0] in {"internal", "nb"} and str(geo_key[1]).isdigit() else None,
                "geonamesId": int(geo_key[1]) if geo_key[0] == "geonames" and str(geo_key[1]).isdigit() else None,
                "place": {
                    "canonicalName": None,
                    "lat": None,
                    "lon": None,
                    "country": None,
                    "variantText": None,
                },
            }
        return {
            "placeId": int(row[0]) if row[0] is not None else None,
            "geonamesId": int(row[1]) if row[1] is not None else None,
            "place": {
                "canonicalName": str(row[2]) if row[2] is not None else None,
                "lat": float(row[3]) if row[3] is not None else None,
                "lon": float(row[4]) if row[4] is not None else None,
                "country": str(row[5]) if row[5] is not None else None,
                "variantText": None,
            },
        }
    finally:
        con.close()


def _fetch_geo_rows_by_key_mentions_fast(
    geo_db_path: str,
    book_ids: List[int],
    geo_key: Tuple[str, str],
    total_limit: int,
) -> Optional[List[Dict[str, Any]]]:
    """
    Fast path for #geo:key concordance rows:
    resolve rows directly from geo_mentions_v2 (+ place joins) in one query.
    Returns None when geo_mentions_v2 is unavailable so callers can fallback.
    """
    if not book_ids:
        return []
    con = sqlite3.connect(f"file:{geo_db_path}?mode=ro", uri=True)
    cur = con.cursor()
    try:
        has_mentions = cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='geo_mentions_v2' LIMIT 1"
        ).fetchone()
        if not has_mentions:
            return None
        cur.execute("CREATE TEMP TABLE _book_filter_fast(book_id INTEGER PRIMARY KEY)")
        cur.executemany(
            "INSERT OR IGNORE INTO _book_filter_fast(book_id) VALUES (?)",
            ((int(book_id),) for book_id in book_ids),
        )
        params: List[Any] = [geo_key[0], geo_key[1]]
        sql = """
            WITH ranked AS (
              SELECT
                m.book_id,
                m.seq_start,
                m.token_len,
                m.place_key_type,
                m.place_key,
                m.place_id,
                COALESCE(m.geonames_id, p.geonames_id) AS geonames_id,
                m.variant_id,
                m.surface_text,
                p.canonical_name,
                p.lat,
                p.lon,
                p.country,
                pv.variant_text,
                ROW_NUMBER() OVER (
                  PARTITION BY m.book_id, m.seq_start
                  ORDER BY m.token_len DESC
                ) AS rn
              FROM geo_mentions_v2 m
              JOIN _book_filter_fast f ON f.book_id = m.book_id
              LEFT JOIN places p ON p.place_id = m.place_id
              LEFT JOIN place_variants pv ON pv.variant_id = m.variant_id
              WHERE m.place_key_type = ?
                AND m.place_key = ?
            )
            SELECT
              book_id,
              seq_start,
              token_len,
              place_key_type,
              place_key,
              place_id,
              geonames_id,
              variant_id,
              surface_text,
              canonical_name,
              lat,
              lon,
              country,
              variant_text
            FROM ranked
            WHERE rn = 1
            ORDER BY book_id, seq_start
        """
        if total_limit > 0:
            sql += " LIMIT ?"
            params.append(int(total_limit))
        rows = cur.execute(sql, tuple(params)).fetchall()
        out: List[Dict[str, Any]] = []
        for row in rows:
            out.append(
                {
                    "bookId": int(row[0]),
                    "seqStart": int(row[1]),
                    "tokenLen": int(row[2]) if row[2] is not None else 1,
                    "placeKeyType": str(row[3]) if row[3] is not None else geo_key[0],
                    "placeKey": str(row[4]) if row[4] is not None else geo_key[1],
                    "placeId": int(row[5]) if row[5] is not None else None,
                    "variantId": int(row[7]) if row[7] is not None else None,
                    "surfaceText": str(row[8]) if row[8] is not None else None,
                    "method": "geo_postings_fastpath",
                    "score": None,
                    "geonamesId": int(row[6]) if row[6] is not None else None,
                    "place": {
                        "canonicalName": str(row[9]) if row[9] is not None else None,
                        "geonamesId": int(row[6]) if row[6] is not None else None,
                        "lat": float(row[10]) if row[10] is not None else None,
                        "lon": float(row[11]) if row[11] is not None else None,
                        "country": str(row[12]) if row[12] is not None else None,
                        "variantText": str(row[13]) if row[13] is not None else None,
                    },
                }
            )
        return out
    except sqlite3.OperationalError:
        # Some exports may not include v2 tables/cols; caller handles fallback.
        return None
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
    if geo_key:
        perf["capital_filter_enabled"] = False
        perf["capital_filter_applied"] = False
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
                picks = near_pos if int(req.perBook) <= 0 else near_pos[: int(req.perBook)]
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
        key_type = meta.get("placeKeyType")
        key_val = meta.get("placeKey")
        if not key_type and geo_key:
            key_type = geo_key[0]
        if not key_val and geo_key:
            key_val = geo_key[1]
        place = meta.get("place") or {}
        rows.append(
            {
                "bookId": int(book_id),
                "seqStart": int(pos),
                "pos": int(pos),
                "tokenLen": int(meta.get("tokenLen") or 1),
                "surfaceText": meta.get("surfaceText"),
                "placeKeyType": key_type,
                "placeKey": key_val,
                "placeId": meta.get("placeId"),
                "geonamesId": meta.get("geonamesId"),
                "place": {
                    "canonicalName": place.get("canonicalName"),
                    "geonamesId": place.get("geonamesId"),
                    "lat": place.get("lat"),
                    "lon": place.get("lon"),
                    "country": place.get("country"),
                    "variantText": place.get("variantText"),
                },
                "method": "geo_near_term_groups",
            }
        )
    rendered_rows: List[Dict[str, object]] = []
    unresolved_render: List[Dict[str, int]] = []
    if bool(req.renderHits):
        render_t0 = time.perf_counter()
        render_input = [
            {
                "bookId": int(r["bookId"]),
                "pos": int(r["pos"]),
                "tokenLen": int(r.get("tokenLen") or 1),
            }
            for r in rows
        ]
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


def _count_geo_namespace_near_or(
    req: OrQueryRequest,
    ns_db_path: str,
    book_ids: List[int],
    plain_term_groups: List[List[str]],
    geo_key: Optional[Tuple[str, str]],
) -> Dict[str, Any]:
    anchor_positions = _load_geo_anchor_positions(ns_db_path, book_ids, geo_key)
    if not anchor_positions:
        raise HTTPException(status_code=404, detail="No geo anchor positions for requested namespace/filter")
    total = 0
    docs = 0
    target_books = set(anchor_positions.keys())
    for shard_index, path in enumerate(CONFIG.postings_dbs):
        con = connect_postings(path, CONFIG.ext_path, shard_sidecar_path(path, shard_index))
        conw = connect_words(shard_words_path(path))
        try:
            cur = con.cursor()
            curw = conw.cursor()
            cf_groups = _resolve_term_groups(
                curw, None, plain_term_groups, int(req.maxVariants), symmetric=False
            )
            if not cf_groups:
                continue
            shard_book_rows = cur.execute("SELECT book_id FROM urns").fetchall()
            shard_books = [int(r[0]) for r in shard_book_rows if int(r[0]) in target_books]
            for book_id in shard_books:
                group_pos = group_positions_for_book(
                    cur, cf_groups, int(book_id), req.schema or CONFIG.default_schema
                )
                if not group_pos or any(not g for g in group_pos):
                    continue
                near_pos = near_positions_from_groups(
                    [anchor_positions[int(book_id)], *group_pos],
                    -int(req.before),
                    int(req.after),
                    exclude_self=False,
                )
                if not near_pos:
                    continue
                total += len(near_pos)
                docs += 1
        finally:
            con.close()
            conw.close()
    if total <= 0:
        raise HTTPException(status_code=404, detail="No near hits for #geo + term groups")
    return {"total": int(total), "docs": int(docs)}


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


def _build_text_fragment_row(
    cur: sqlite3.Cursor,
    curw: sqlite3.Cursor,
    book_id: int,
    seq_start: int,
    before: int,
    after: int,
    render_mode: str = "legacy",
    span_len: int = 1,
) -> Dict[str, object]:
    if render_mode == "structured":
        return fetch_window_structured(
            cur,
            curw,
            int(book_id),
            int(seq_start),
            int(before),
            int(after),
            span_len=max(int(span_len or 1), 1),
        )
    frag = fetch_window(
        cur,
        curw,
        int(book_id),
        int(seq_start),
        int(before),
        int(after),
        span_len=max(int(span_len or 1), 1),
    )
    return {"bookId": int(book_id), "pos": int(seq_start), "frag": frag}


def _render_book_pos_rows(
    rows: List[Dict[str, int]],
    before: int,
    after: int,
    render_mode: str = "legacy",
) -> Tuple[List[Dict[str, object]], List[Dict[str, int]]]:
    out_rows: List[Dict[str, object]] = []
    unresolved = [
        {
            "bookId": int(r["bookId"]),
            "pos": int(r["pos"]),
            "tokenLen": max(int(r.get("tokenLen") or 1), 1),
        }
        for r in rows
    ]
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
            token_len = max(int(row.get("tokenLen") or 1), 1)
            exists = cur.execute(
                "SELECT 1 FROM urns WHERE book_id = ? LIMIT 1",
                (book_id,),
            ).fetchone()
            if not exists:
                next_unresolved.append(row)
                continue
            out_rows.append(
                _build_text_fragment_row(
                    cur,
                    curw,
                    book_id,
                    pos,
                    before,
                    after,
                    render_mode=render_mode,
                    span_len=token_len,
                )
            )
        con.close()
        conw.close()
        unresolved = next_unresolved
    return out_rows, unresolved


def _run_annotation_namespace_query_or(req: OrQueryRequest) -> Optional[Dict[str, Any]]:
    perf_ns: Dict[str, Any] = {}
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
        if filter_ids:
            # Fast path: when caller already scopes books, avoid registry book-map scan.
            seen: set[int] = set()
            book_ids = []
            for raw_id in filter_ids:
                bid = int(raw_id)
                if bid in seen:
                    continue
                seen.add(bid)
                book_ids.append(bid)
            doc_samples = _effective_namespace_doc_samples(namespace_value, req.docSamples)
            if doc_samples > 0 and len(book_ids) > doc_samples:
                book_ids = random.sample(book_ids, doc_samples)
        else:
            book_ids = resolve_namespace_books(
                CONFIG.annotation_registry_db,
                namespace,
                filter_ids,
                _effective_namespace_doc_samples(namespace_value, req.docSamples),
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
                detail="Near mode with #geo:value expects an id (e.g. #geo:3143244) or explicit geonames/internal/nb prefix",
            )
        return _run_geo_namespace_near_or(
            req,
            ns_meta["db_path"],
            book_ids,
            plain_term_groups,
            geo_key,
        )

    if namespace == "geo" and geo_key:
        geo_fast_t0 = time.perf_counter()
        try:
            fast_rows_t0 = time.perf_counter()
            fast_rows = _fetch_geo_rows_by_key_mentions_fast(
                ns_meta["db_path"], book_ids, geo_key, int(req.totalLimit)
            )
            perf_ns["geo_fast_mentions_ms"] = round((time.perf_counter() - fast_rows_t0) * 1000.0, 3)
            if fast_rows is not None:
                rows = fast_rows
                perf_ns["geo_fast_strategy"] = "mentions_direct"
            else:
                anchor_t0 = time.perf_counter()
                anchor_positions = _load_geo_anchor_positions(ns_meta["db_path"], book_ids, geo_key)
                perf_ns["geo_anchor_ms"] = round((time.perf_counter() - anchor_t0) * 1000.0, 3)
                rows_hits: List[Tuple[int, int]] = []
                max_hits = int(req.totalLimit)
                for bid in sorted(anchor_positions.keys()):
                    arr = anchor_positions[bid]
                    if max_hits > 0:
                        remaining = max_hits - len(rows_hits)
                        if remaining <= 0:
                            break
                        if len(arr) <= remaining:
                            rows_hits.extend((int(bid), int(pos)) for pos in arr)
                        else:
                            rows_hits.extend((int(bid), int(pos)) for pos in arr[:remaining])
                            break
                    else:
                        rows_hits.extend((int(bid), int(pos)) for pos in arr)
                place_t0 = time.perf_counter()
                place_meta = _lookup_geo_place_meta(ns_meta["db_path"], geo_key)
                perf_ns["geo_place_meta_ms"] = round((time.perf_counter() - place_t0) * 1000.0, 3)
                meta_t0 = time.perf_counter()
                meta_map = _lookup_geo_span_meta(ns_meta["db_path"], rows_hits, geo_key)
                perf_ns["geo_span_meta_ms"] = round((time.perf_counter() - meta_t0) * 1000.0, 3)
                place_defaults = place_meta.get("place") or {}
                rows = []
                for book_id, pos in rows_hits:
                    meta = meta_map.get((book_id, pos), {})
                    row_place = meta.get("place") or {}
                    rows.append(
                        {
                            "bookId": int(book_id),
                            "seqStart": int(pos),
                            "tokenLen": int(meta.get("tokenLen") or 1),
                            "placeKeyType": meta.get("placeKeyType") or geo_key[0],
                            "placeKey": meta.get("placeKey") or geo_key[1],
                            "placeId": meta.get("placeId") or place_meta.get("placeId"),
                            "variantId": meta.get("variantId"),
                            "surfaceText": meta.get("surfaceText"),
                            "method": "geo_postings_fastpath",
                            "score": None,
                            "geonamesId": meta.get("geonamesId") or place_meta.get("geonamesId"),
                            "place": {
                                "canonicalName": row_place.get("canonicalName") or place_defaults.get("canonicalName"),
                                "geonamesId": row_place.get("geonamesId") or place_meta.get("geonamesId"),
                                "lat": row_place.get("lat")
                                if row_place.get("lat") is not None
                                else place_defaults.get("lat"),
                                "lon": row_place.get("lon")
                                if row_place.get("lon") is not None
                                else place_defaults.get("lon"),
                                "country": row_place.get("country") or place_defaults.get("country"),
                                "variantText": row_place.get("variantText") or place_defaults.get("variantText"),
                            },
                        }
                    )
                perf_ns["geo_fast_strategy"] = "postings_plus_meta"
        except HTTPException as exc:
            # Fallback for environments without roaring decode support.
            if "pyroaring" not in str(getattr(exc, "detail", "")).lower():
                raise
            rows = fetch_geo_spans_by_key(
                ns_meta["db_path"],
                book_ids,
                geo_key[0],
                geo_key[1],
                int(req.totalLimit),
            )
            perf_ns["geo_fast_strategy"] = "spans_by_key_fallback"
        perf_ns["geo_fast_total_ms"] = round((time.perf_counter() - geo_fast_t0) * 1000.0, 3)
    else:
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
        and not geo_key
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
            render_input.append(
                {
                    "bookId": int(row["bookId"]),
                    "pos": pos,
                    "tokenLen": int(row.get("tokenLen") or 1),
                }
            )
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
    if PROFILE_NEAR and perf_ns:
        perf_ns["rows"] = len(rows)
        out["_perf"] = perf_ns
    return out


def _resolve_geo_namespace_meta(namespace: str) -> Tuple[Dict[str, Any], str]:
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
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    resolver = str(ns_meta.get("resolver", "")).casefold()
    if resolver != "geo_resolver":
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported resolver for #{namespace}: {resolver}",
        )
    return ns_meta, resolver


def _run_geo_nb_rebuild(annotation_db_path: str, drop_existing: bool = False) -> Dict[str, Any]:
    script_candidates = [
        Path(__file__).resolve().parent.parent / "build_geo_nb_contract_v1.py",
        Path("/app/build_geo_nb_contract_v1.py"),
    ]
    script_path = next((p for p in script_candidates if p.exists()), None)
    if script_path is None:
        raise HTTPException(
            status_code=500,
            detail="build_geo_nb_contract_v1.py not found; cannot rebuild resolved geo tables",
        )
    cmd = [
        "python",
        str(script_path),
        "--annotation-db",
        annotation_db_path,
    ]
    if drop_existing:
        cmd.append("--drop-existing")
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=900,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=500, detail=f"Geo rebuild timed out: {exc}") from exc
    if proc.returncode != 0:
        stderr_text = (proc.stderr or "").strip()
        stdout_text = (proc.stdout or "").strip()
        detail = stderr_text or stdout_text or f"geo rebuild failed (exit code {proc.returncode})"
        raise HTTPException(status_code=500, detail=detail)
    return {
        "script": str(script_path),
        "command": cmd,
        "stdout": (proc.stdout or "").strip(),
    }


def _ensure_geo_edit_tables(cur: sqlite3.Cursor) -> None:
    edits_table_ok = cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='geo_annotations_edits' LIMIT 1"
    ).fetchone()
    if not edits_table_ok:
        raise HTTPException(
            status_code=500,
            detail="geo_annotations_edits table is missing; initialize annotation_geo_nb schema first",
        )
    places_ok = cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='nb_places' LIMIT 1"
    ).fetchone()
    if not places_ok:
        raise HTTPException(
            status_code=500,
            detail="nb_places table is missing; initialize annotation_geo_nb schema first",
        )


def _insert_geo_edit_row(
    cur: sqlite3.Cursor,
    book_id: int,
    seq_start: int,
    action: str,
    nb_place_id: Optional[int],
    note: Optional[str],
    editor: Optional[str],
) -> int:
    if action == "set_place":
        if nb_place_id is None:
            raise HTTPException(status_code=400, detail="nbPlaceId is required when action='set_place'")
        place_row = cur.execute(
            "SELECT 1 FROM nb_places WHERE nb_place_id = ? LIMIT 1",
            (int(nb_place_id),),
        ).fetchone()
        if not place_row:
            raise HTTPException(status_code=404, detail=f"nb_place_id not found: {int(nb_place_id)}")

    cur.execute(
        """
        INSERT INTO geo_annotations_edits(
          dhlabid, seq_start, action, nb_place_id, note, editor
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            int(book_id),
            int(seq_start),
            str(action),
            int(nb_place_id) if nb_place_id is not None and action == "set_place" else None,
            note,
            editor,
        ),
    )
    return int(cur.lastrowid)


@app.post("/api/geo/book/sequence")
def api_geo_book_sequence(req: GeoBookSequenceRequest) -> Dict[str, Any]:
    namespace = (req.namespace or "geo").strip().casefold() or "geo"
    ns_meta, resolver = _resolve_geo_namespace_meta(namespace)
    rows = fetch_geo_book_sequence(
        ns_meta["db_path"],
        int(req.bookId),
        int(req.limit),
    )
    return {
        "namespace": namespace,
        "resolver": resolver,
        "bookId": int(req.bookId),
        "rows": rows,
        "count": len(rows),
    }


@app.post("/api/geo/annotation/edit")
def api_geo_annotation_edit(req: GeoAnnotationEditRequest) -> Dict[str, Any]:
    namespace = (req.namespace or "geo").strip().casefold() or "geo"
    ns_meta, resolver = _resolve_geo_namespace_meta(namespace)

    geo_db_path = str(ns_meta["db_path"])
    con = sqlite3.connect(geo_db_path)
    try:
        cur = con.cursor()
        _ensure_geo_edit_tables(cur)
        edit_id = _insert_geo_edit_row(
            cur=cur,
            book_id=int(req.bookId),
            seq_start=int(req.seqStart),
            action=str(req.action),
            nb_place_id=(int(req.nbPlaceId) if req.nbPlaceId is not None else None),
            note=req.note,
            editor=req.editor,
        )
        con.commit()
    finally:
        con.close()

    rebuild_info: Optional[Dict[str, Any]] = None
    if bool(req.rebuild):
        rebuild_info = _run_geo_nb_rebuild(geo_db_path, drop_existing=bool(req.dropExisting))

    out: Dict[str, Any] = {
        "ok": True,
        "namespace": namespace,
        "resolver": resolver,
        "dbPath": geo_db_path,
        "edit": {
            "editId": edit_id,
            "bookId": int(req.bookId),
            "seqStart": int(req.seqStart),
            "action": str(req.action),
            "nbPlaceId": int(req.nbPlaceId) if req.nbPlaceId is not None and req.action == "set_place" else None,
            "note": req.note,
            "editor": req.editor,
        },
    }
    if rebuild_info is not None:
        out["rebuild"] = rebuild_info
    return out


@app.post("/api/geo/annotation/edit/batch")
def api_geo_annotation_edit_batch(req: GeoAnnotationBatchEditRequest) -> Dict[str, Any]:
    namespace = (req.namespace or "geo").strip().casefold() or "geo"
    ns_meta, resolver = _resolve_geo_namespace_meta(namespace)

    geo_db_path = str(ns_meta["db_path"])
    con = sqlite3.connect(geo_db_path)
    inserted: List[Dict[str, Any]] = []
    try:
        cur = con.cursor()
        _ensure_geo_edit_tables(cur)
        for item in req.edits:
            edit_id = _insert_geo_edit_row(
                cur=cur,
                book_id=int(item.bookId),
                seq_start=int(item.seqStart),
                action=str(item.action),
                nb_place_id=(int(item.nbPlaceId) if item.nbPlaceId is not None else None),
                note=item.note,
                editor=item.editor,
            )
            inserted.append(
                {
                    "editId": edit_id,
                    "bookId": int(item.bookId),
                    "seqStart": int(item.seqStart),
                    "action": str(item.action),
                    "nbPlaceId": int(item.nbPlaceId) if item.nbPlaceId is not None and item.action == "set_place" else None,
                    "note": item.note,
                    "editor": item.editor,
                }
            )
        con.commit()
    finally:
        con.close()

    rebuild_info: Optional[Dict[str, Any]] = None
    if bool(req.rebuild):
        rebuild_info = _run_geo_nb_rebuild(geo_db_path, drop_existing=bool(req.dropExisting))

    out: Dict[str, Any] = {
        "ok": True,
        "namespace": namespace,
        "resolver": resolver,
        "dbPath": geo_db_path,
        "count": len(inserted),
        "edits": inserted,
    }
    if rebuild_info is not None:
        out["rebuild"] = rebuild_info
    return out


class NearFragmentsRequest(BaseModel):
    terms: Optional[List[str]] = None
    termGroups: Optional[List[List[str]]] = None
    window: int = Field(5, ge=1, le=50)
    before: int = Field(5, ge=1, le=50)
    after: int = Field(5, ge=1, le=50)
    perBook: int = Field(3, ge=0, le=20)
    docSamples: Optional[int] = Field(None, ge=0, le=50000)
    totalLimit: int = Field(200, ge=0, le=5000)
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
    renderMode: Literal["legacy", "structured"] = "legacy"


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
    contentOperator: Optional[str] = "AND"
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

    def run_once(doc_samples: Optional[int]) -> Tuple[List[Tuple[int, int, object]], bool, bool]:
        local_rows: List[Tuple[int, int, object]] = []
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
                        req.renderMode,
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
                            req.renderMode,
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
                            req.renderMode,
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
    if req.renderMode == "structured":
        structured_rows: List[Dict[str, object]] = []
        for book_id, pos, payload in rows:
            if isinstance(payload, dict):
                row = dict(payload)
            else:
                row = {
                    "bookId": int(book_id),
                    "seqStart": int(pos),
                    "len": 1,
                    "before": "",
                    "hit": str(payload),
                    "after": "",
                    "surface": str(payload),
                }
            row.setdefault("bookId", int(book_id))
            row.setdefault("seqStart", int(pos))
            row.setdefault("len", 1)
            structured_rows.append(row)
        return {"renderMode": req.renderMode, "rows": structured_rows}
    return {
        "renderMode": req.renderMode,
        "rows": [{"bookId": b, "pos": p, "frag": f} for b, p, f in rows],
    }


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
    # Each inner group is OR; groups are combined as AND across the query.
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


def _auto_doc_sample_threshold(total_limit: int, per_book: int) -> int:
    sample_target = _sample_book_target(total_limit, per_book)
    return max(AUTO_DOC_SAMPLE_MIN_CANDIDATES, sample_target * AUTO_DOC_SAMPLE_MULTIPLIER)


def _effective_namespace_doc_samples(
    namespace_value: Optional[str], requested_doc_samples: Optional[int]
) -> int:
    # For targeted namespace queries like #geo:geonames:<id>, resolve the concrete
    # annotation key first; sampling the namespace coverage set up front turns
    # sparse lookups into a random "needle in a haystack" search.
    if namespace_value:
        return 0
    return int(requested_doc_samples or 0)


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
    auto_sampling = doc_samples is None
    should_sample = sample_n > 0 and (
        not sample_only_when_no_docpost or not has_docpost_prefilter
    )
    if auto_sampling and should_sample and filter_ids is not None:
        if len(filter_ids) <= _auto_doc_sample_threshold(total_limit, per_book):
            should_sample = False
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


def _near_fragments_request_from_query(req: NearQueryRequest) -> NearFragmentsRequest:
    return NearFragmentsRequest(
        terms=req.terms,
        termGroups=req.termGroups,
        window=req.window,
        before=req.before,
        after=req.after,
        perBook=req.perBook,
        docSamples=req.docSamples,
        totalLimit=req.totalLimit,
        schema=req.schema,
        symmetric=req.symmetric,
        excludeSelf=req.excludeSelf,
        useFilter=req.useFilter,
        filterIds=req.filterIds,
        maxVariants=req.maxVariants,
        includeFragments=(req.mode == "render"),
        engine=req.engine,
        parallelShards=req.parallelShards,
        matchMode=req.matchMode,
        renderMode=req.renderMode,
    )


def _or_query_request_from_near_query(req: NearQueryRequest) -> OrQueryRequest:
    return OrQueryRequest(
        terms=req.terms or [],
        termGroups=req.termGroups,
        before=req.before,
        after=req.after,
        perBook=req.perBook,
        docSamples=req.docSamples,
        totalLimit=req.totalLimit,
        schema=req.schema,
        useFilter=req.useFilter,
        filterIds=req.filterIds,
        maxVariants=req.maxVariants,
        parallelShards=req.parallelShards,
        renderHits=(req.mode == "render"),
        renderMode=req.renderMode,
    )


def _normalize_namespace_near_response(
    req: NearQueryRequest, out: Dict[str, Any]
) -> Dict[str, Any]:
    rows_in = out.get("rows", [])
    rows = [dict(r) for r in rows_in] if isinstance(rows_in, list) else []
    if req.mode == "count":
        docs = len({int(r["bookId"]) for r in rows if "bookId" in r})
        res: Dict[str, Any] = {"total": len(rows), "docs": docs}
        if "_perf" in out:
            res["_perf"] = out["_perf"]
        return res
    if req.mode == "render":
        rendered_map: Dict[Tuple[int, int], str] = {}
        rendered_rows = out.get("rendered", [])
        if isinstance(rendered_rows, list):
            for r in rendered_rows:
                if not isinstance(r, dict):
                    continue
                if "bookId" not in r or "pos" not in r:
                    continue
                rendered_map[(int(r["bookId"]), int(r["pos"]))] = str(r.get("frag") or "")
        for row in rows:
            if "bookId" in row and "pos" in row:
                row["frag"] = rendered_map.get((int(row["bookId"]), int(row["pos"])), "")
            else:
                row["frag"] = ""
    res = {"rows": rows}
    for key in ("namespace", "resolver", "coverageMode", "_perf"):
        if key in out:
            res[key] = out[key]
    return res


def _count_annotation_namespace_query_or(req: OrQueryRequest) -> Optional[Dict[str, Any]]:
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
        if filter_ids:
            seen: set[int] = set()
            book_ids = []
            for raw_id in filter_ids:
                bid = int(raw_id)
                if bid in seen:
                    continue
                seen.add(bid)
                book_ids.append(bid)
            doc_samples = _effective_namespace_doc_samples(namespace_value, req.docSamples)
            if doc_samples > 0 and len(book_ids) > doc_samples:
                book_ids = random.sample(book_ids, doc_samples)
        else:
            book_ids = resolve_namespace_books(
                CONFIG.annotation_registry_db,
                namespace,
                filter_ids,
                _effective_namespace_doc_samples(namespace_value, req.docSamples),
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
                detail="Near mode with #geo:value expects an id (e.g. #geo:3143244) or explicit geonames/internal/nb prefix",
            )
        return _count_geo_namespace_near_or(
            req,
            ns_meta["db_path"],
            book_ids,
            plain_term_groups,
            geo_key,
        )
    if namespace == "geo":
        try:
            if geo_key:
                anchor_positions = _load_geo_anchor_positions(ns_meta["db_path"], book_ids, geo_key)
            elif namespace_value:
                rows = fetch_geo_spans(
                    ns_meta["db_path"],
                    book_ids,
                    None,
                    place_text_filter=namespace_value,
                )
                if not rows and CONFIG.imagination_db and Path(CONFIG.imagination_db).exists():
                    rows = fetch_geo_books_from_imagination(
                        CONFIG.imagination_db,
                        namespace_value,
                        filter_ids if filter_ids else None,
                        None,
                    )
                if not rows:
                    raise HTTPException(
                        status_code=404,
                        detail=f'No annotation hits for #{namespace}:"{namespace_value}"',
                    )
                return {
                    "total": len(rows),
                    "docs": len({int(r["bookId"]) for r in rows if "bookId" in r}),
                }
            else:
                anchor_positions = _load_geo_anchor_positions(ns_meta["db_path"], book_ids, None)
        except HTTPException as exc:
            if "pyroaring" not in str(getattr(exc, "detail", "")).lower():
                raise
            rows = (
                fetch_geo_spans_by_key(ns_meta["db_path"], book_ids, geo_key[0], geo_key[1], None)
                if geo_key
                else fetch_geo_spans(ns_meta["db_path"], book_ids, None)
            )
            if not rows:
                raise HTTPException(status_code=404, detail=f"No annotation hits for #{namespace}")
            return {
                "total": len(rows),
                "docs": len({int(r["bookId"]) for r in rows if "bookId" in r}),
            }
        total = sum(len(arr) for arr in anchor_positions.values())
        docs = len(anchor_positions)
        if total <= 0:
            if namespace_value:
                raise HTTPException(
                    status_code=404,
                    detail=f'No annotation hits for #{namespace}:"{namespace_value}"',
                )
            raise HTTPException(status_code=404, detail=f"No annotation hits for #{namespace}")
        return {"total": int(total), "docs": int(docs)}
    out = _run_annotation_namespace_query_or(req)
    if out is None:
        return None
    rows = out.get("rows", [])
    return {
        "total": len(rows) if isinstance(rows, list) else 0,
        "docs": len({int(r["bookId"]) for r in rows if isinstance(r, dict) and "bookId" in r})
        if isinstance(rows, list)
        else 0,
    }


def _run_namespace_near_query(req: NearQueryRequest) -> Optional[Dict[str, Any]]:
    try:
        namespace, _, _ = parse_namespace_query(req.terms, req.termGroups)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not namespace:
        return None
    or_req = _or_query_request_from_near_query(req)
    if req.mode == "count":
        return _count_annotation_namespace_query_or(or_req)
    out = _run_annotation_namespace_query_or(or_req)
    if out is None:
        return None
    return _normalize_namespace_near_response(req, out)


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
        render_mode = str(task.get("render_mode", "legacy"))
        span_len = max(int(task.get("span_len") or 1), 1)
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
            if per_book <= 0 or len(positions) <= per_book:
                sampled = positions
            else:
                sampled = random.sample(positions, per_book)
            for ipos in sampled:
                if include_fragments:
                    rows.append(
                        _build_text_fragment_row(
                            cur,
                            curw,
                            book_id,
                            int(ipos),
                            before,
                            after,
                            render_mode=render_mode,
                            span_len=span_len,
                        )
                    )
                else:
                    rows.append({"bookId": book_id, "pos": int(ipos)})
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
            str(task.get("render_mode", "legacy")),
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
    namespace_response = _run_namespace_near_query(req)
    if namespace_response is not None:
        return namespace_response
    min_group_count = 2 if req.mode in {"hits", "render"} else 1
    if req.termGroups:
        if len(req.termGroups) < min_group_count:
            raise HTTPException(
                status_code=400,
                detail=(
                    "termGroups must contain at least two items"
                    if min_group_count == 2
                    else "termGroups must contain at least one item"
                ),
            )
    elif not req.terms or len(req.terms) < min_group_count:
        raise HTTPException(
            status_code=400,
            detail=(
                "terms must contain at least two items"
                if min_group_count == 2
                else "terms must contain at least one item"
            ),
        )
    if req.mode in {"hits", "render"}:
        out = near_fragments(_near_fragments_request_from_query(req))
        if req.mode == "hits":
            rows = out.get("rows", [])
            hits = [{"bookId": int(r["bookId"]), "pos": int(r["pos"])} for r in rows]
            if "_perf" in out:
                return {"rows": hits, "_perf": out["_perf"]}
            return {"rows": hits}
        return out
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
    count_mode = req.countMode or "auto"
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
        if len(groups) == 1:
            shard_total, shard_docs = group_frequency(
                cur,
                groups[0],
                use_filter,
                filter_json,
                schema_name,
            )
            total += int(shard_total)
            docs += int(shard_docs)
            if PROFILE_NEAR:
                perf_workers.append(
                    {
                        "pid": os.getpid(),
                        "shard": path,
                        "mode": "single_group_frequency_fastpath",
                    }
                )
            con.close()
            conw.close()
            continue
        pairwise_fastpath = (
            match_mode == "near"
            and schema_name == "unigrams"
            and len(groups) == 2
            and len(groups[0]) == 1
            and len(groups[1]) == 1
            and groups[0][0] != groups[1][0]
        )
        resolved_count_mode = count_mode
        if resolved_count_mode == "auto" and pairwise_fastpath:
            resolved_count_mode = "partner_popcount"
        elif resolved_count_mode == "auto":
            resolved_count_mode = "anchor"
        if resolved_count_mode == "partner_popcount" and pairwise_fastpath:
            shard_total, shard_docs = near_partner_popcount(
                cur,
                groups[0][0],
                groups[1][0],
                req.window,
                use_filter,
                filter_json,
                schema_name,
                req.symmetric,
                req.excludeSelf,
            )
            total += int(shard_total)
            docs += int(shard_docs)
            if PROFILE_NEAR:
                perf_workers.append(
                    {
                        "pid": os.getpid(),
                        "shard": path,
                        "mode": "partner_popcount_fastpath",
                    }
                )
            con.close()
            conw.close()
            continue
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
            "count_mode": count_mode,
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
    rows: List[Tuple[int, int, object]] = []
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
                "render_mode": req.renderMode,
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
    if req.renderMode == "structured":
        out_rows: List[Dict[str, object]] = []
        for book_id, pos, payload in rows:
            if isinstance(payload, dict):
                row = dict(payload)
            else:
                row = {
                    "bookId": int(book_id),
                    "seqStart": int(pos),
                    "len": 1,
                    "before": "",
                    "hit": str(payload),
                    "after": "",
                    "surface": str(payload),
                }
            row.setdefault("bookId", int(book_id))
            row.setdefault("seqStart", int(pos))
            row.setdefault("len", 1)
            out_rows.append(row)
        out: Dict[str, Any] = {"rows": out_rows}
    else:
        out = {"rows": [{"bookId": b, "pos": p, "frag": f} for b, p, f in rows]}
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
        if req.renderMode == "structured" and req.includeFragments:
            rows, _ = _render_book_pos_rows(
                rows,
                int(req.before),
                int(req.after),
                render_mode="structured",
            )
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
        span_len = len(groups) if match_mode == "sequence" else 1
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
                "render_mode": req.renderMode,
                "span_len": span_len,
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
            use_sampled_agg_fn = req.perBook > 0 and _has_sql_function(
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
                if req.includeFragments:
                    rows.append(
                        _build_text_fragment_row(
                            cur,
                            curw,
                            book_id,
                            ipos,
                            req.before,
                            req.after,
                            render_mode=req.renderMode,
                            span_len=span_len,
                        )
                    )
                else:
                    rows.append({"bookId": book_id, "pos": ipos})
                if req.totalLimit and len(rows) >= req.totalLimit:
                    break
            if req.totalLimit and len(rows) >= req.totalLimit:
                break
        use_sample_json_fn = req.perBook > 0 and _has_sql_function(inner, "post_sample_positions_json")
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
                    if req.includeFragments:
                        rows.append(
                            _build_text_fragment_row(
                                cur,
                                curw,
                                book_id,
                                ipos,
                                req.before,
                                req.after,
                                render_mode=req.renderMode,
                                span_len=span_len,
                            )
                        )
                    else:
                        rows.append({"bookId": book_id, "pos": ipos})
                    if req.totalLimit and len(rows) >= req.totalLimit:
                        break
            else:
                cnt_row = inner.execute("SELECT post_count(?)", (common_blob,)).fetchone()
                total = int(cnt_row[0] or 0) if cnt_row else 0
                if total <= 0:
                    continue
                if req.perBook <= 0:
                    indices = range(total)
                else:
                    samples = min(req.perBook, total)
                    indices = random.sample(range(total), samples)
                for idx in indices:
                    pos_row = inner.execute("SELECT post_sample(?, ?)", (common_blob, idx)).fetchone()
                    if pos_row is None or pos_row[0] is None:
                        continue
                    pos = int(pos_row[0])
                    if req.includeFragments:
                        rows.append(
                            _build_text_fragment_row(
                                cur,
                                curw,
                                book_id,
                                int(pos),
                                req.before,
                                req.after,
                                render_mode=req.renderMode,
                                span_len=span_len,
                            )
                        )
                    else:
                        rows.append({"bookId": book_id, "pos": int(pos)})
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


def _normalize_content_keywords(raw_keywords: Optional[List[str]]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for raw in raw_keywords or []:
        term = str(raw or "").strip()
        if not term:
            continue
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(term)
    return out


def _resolve_content_keyword_books(
    keywords: List[str],
    base_filter_ids: Optional[List[int]] = None,
    max_variants: int = 200,
    operator: str = "AND",
) -> List[int]:
    """
    Resolve docs matching content keywords across shards.
    AND: all keywords must match.
    OR: at least one keyword must match.
    Wildcard terms are expanded in words table.
    """
    if not keywords:
        return []
    base_filter_set = set(int(x) for x in base_filter_ids) if base_filter_ids else None
    matched: set[int] = set()
    for shard_index, path in enumerate(CONFIG.postings_dbs):
        con = connect_postings(path, CONFIG.ext_path, shard_sidecar_path(path, shard_index))
        conw = connect_words(shard_words_path(path))
        try:
            cur = con.cursor()
            curw = conw.cursor()
            groups: List[List[int]] = []
            op = str(operator or "AND").strip().upper()
            if op == "OR":
                union_cf_ids: List[int] = []
                for term in keywords:
                    cf_ids, _ = expand_term_cf_ids_with_df(curw, term, max_variants)
                    if cf_ids:
                        union_cf_ids.extend(int(x) for x in cf_ids)
                merged = sorted(set(union_cf_ids))
                if not merged:
                    continue
                groups = [merged]
            else:
                failed = False
                for term in keywords:
                    cf_ids, _ = expand_term_cf_ids_with_df(curw, term, max_variants)
                    cf_ids = sorted(set(int(x) for x in cf_ids))
                    if not cf_ids:
                        failed = True
                        break
                    groups.append(cf_ids)
                if failed or not groups:
                    continue
            book_ids = docpost_book_ids(cur, groups)
            if book_ids is None:
                book_ids = candidate_books_for_groups(
                    cur,
                    groups,
                    schema=(CONFIG.default_schema or "unigrams"),
                )
            if base_filter_set is not None:
                for bid in book_ids:
                    ibid = int(bid)
                    if ibid in base_filter_set:
                        matched.add(ibid)
            else:
                matched.update(int(bid) for bid in book_ids)
        finally:
            con.close()
            conw.close()
    return sorted(matched)


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
    dhlabids = [int(row["dhlabid"]) for row in rows]

    # 2. Base Corpus Intersection (used for combining with search hits)
    if req.baseCorpus is not None:
        base_set = set(int(x) for x in req.baseCorpus)
        dhlabids = [bid for bid in dhlabids if bid in base_set]

    # 3. Content keyword intersection (fulltext in postings shards).
    keywords = _normalize_content_keywords(req.contentKeywords)
    content_operator = str(req.contentOperator or "AND").strip().upper()
    if content_operator not in {"AND", "OR"}:
        raise HTTPException(status_code=400, detail="contentOperator must be 'AND' or 'OR'")
    if keywords:
        keyword_hits = _resolve_content_keyword_books(
            keywords,
            base_filter_ids=dhlabids,
            operator=content_operator,
        )
        keyword_set = set(keyword_hits)
        dhlabids = [bid for bid in dhlabids if bid in keyword_set]
        
    final_set = set(dhlabids)
    stats = {
        "totalBooks": len(dhlabids),
        "uniqueAuthors": len(set([row["author"] for row in rows if int(row["dhlabid"]) in final_set])),
        "contentKeywordsApplied": len(keywords),
        "contentOperator": content_operator,
    }

    conn.close()
    return {"dhlabids": dhlabids, "stats": stats}


@app.post("/api/legacy/places")
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


@app.post("/api/legacy/places/details")
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
