#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from pyroaring import BitMap


@dataclass
class ResolvedRow:
    book_id: int
    seq_start: int
    nb_place_id: int
    geonames_id: Optional[int]
    surface_text: Optional[str]
    token_len: int
    confidence: Optional[float]
    provenance: str


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Build resolved NB geo annotation contract tables "
            "(geo_annotations_resolved, geo_mentions_v2, geo_postings_v2, geo_book_index_v2)."
        )
    )
    p.add_argument("--annotation-db", required=True, help="Path to writable annotation db")
    p.add_argument(
        "--drop-existing",
        action="store_true",
        help="Drop and recreate materialized output tables before rebuild",
    )
    return p.parse_args()


def _table_exists(cur: sqlite3.Cursor, name: str) -> bool:
    row = cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return bool(row)


def _table_columns(cur: sqlite3.Cursor, table_name: str) -> List[str]:
    rows = cur.execute(f"PRAGMA table_info({table_name})").fetchall()
    return [str(r[1]) for r in rows]


def _token_len_from_text(text: Optional[str]) -> int:
    # Contract decision: token length is computed via space split.
    parts = [p for p in str(text or "").strip().split(" ") if p]
    return len(parts) if parts else 1


def ensure_schema(cur: sqlite3.Cursor, drop_existing: bool) -> None:
    if drop_existing:
        cur.executescript(
            """
            DROP TABLE IF EXISTS geo_annotations_resolved;
            DROP TABLE IF EXISTS geo_mentions_v2;
            DROP TABLE IF EXISTS geo_postings_v2;
            DROP TABLE IF EXISTS geo_book_index_v2;
            DROP TABLE IF EXISTS places;
            DROP TABLE IF EXISTS place_variants;
            DROP TABLE IF EXISTS geo_spans;
            """
        )

    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS geo_annotations_resolved (
          dhlabid INTEGER NOT NULL,
          seq_start INTEGER NOT NULL,
          nb_place_id INTEGER NOT NULL,
          surface_text TEXT,
          token_len INTEGER NOT NULL CHECK (token_len > 0),
          confidence REAL,
          provenance TEXT NOT NULL DEFAULT 'base',
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (dhlabid, seq_start)
        );
        CREATE INDEX IF NOT EXISTS idx_geo_annotations_resolved_place
          ON geo_annotations_resolved(nb_place_id, dhlabid);

        CREATE TABLE IF NOT EXISTS geo_mentions_v2 (
          book_id INTEGER NOT NULL,
          seq_start INTEGER NOT NULL,
          token_len INTEGER NOT NULL,
          place_key_type TEXT NOT NULL,
          place_key TEXT NOT NULL,
          place_id INTEGER,
          geonames_id INTEGER,
          variant_id INTEGER,
          surface_text TEXT,
          UNIQUE (book_id, seq_start, place_key_type, place_key, token_len)
        );
        CREATE INDEX IF NOT EXISTS idx_geo_mentions_v2_key_book_seq
          ON geo_mentions_v2(place_key_type, place_key, book_id, seq_start);

        CREATE TABLE IF NOT EXISTS geo_postings_v2 (
          book_id INTEGER NOT NULL,
          place_key_type TEXT NOT NULL,
          place_key TEXT NOT NULL,
          token_len INTEGER NOT NULL,
          starts_roaring BLOB NOT NULL,
          count_mentions INTEGER NOT NULL,
          PRIMARY KEY (book_id, place_key_type, place_key, token_len)
        );
        CREATE INDEX IF NOT EXISTS idx_geo_postings_v2_key_len_book
          ON geo_postings_v2(place_key_type, place_key, token_len, book_id);

        CREATE TABLE IF NOT EXISTS geo_book_index_v2 (
          book_id INTEGER PRIMARY KEY,
          all_places_roaring BLOB NOT NULL,
          unique_places INTEGER NOT NULL,
          total_mentions INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS places (
          place_id INTEGER PRIMARY KEY,
          canonical_name TEXT NOT NULL,
          geonames_id INTEGER,
          feature_class TEXT,
          feature_code TEXT,
          lat REAL,
          lon REAL,
          country TEXT
        );

        CREATE TABLE IF NOT EXISTS place_variants (
          variant_id INTEGER PRIMARY KEY,
          place_id INTEGER NOT NULL,
          variant_text TEXT NOT NULL,
          norm_text TEXT,
          token_len INTEGER
        );

        CREATE TABLE IF NOT EXISTS geo_spans (
          book_id INTEGER NOT NULL,
          seq_start INTEGER NOT NULL,
          token_len INTEGER NOT NULL CHECK (token_len > 0),
          place_id INTEGER NOT NULL,
          variant_id INTEGER,
          score REAL,
          method TEXT,
          surface_text TEXT,
          surface_hash TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_geo_spans_book_seq
          ON geo_spans(book_id, seq_start);
        CREATE INDEX IF NOT EXISTS idx_geo_spans_place_book
          ON geo_spans(place_id, book_id);
        """
    )


def load_resolved_rows(cur: sqlite3.Cursor) -> List[ResolvedRow]:
    if not _table_exists(cur, "geo_annotations_base"):
        raise RuntimeError("Missing required table: geo_annotations_base")
    if not _table_exists(cur, "nb_places"):
        raise RuntimeError("Missing required table: nb_places")

    base_cols = set(_table_columns(cur, "geo_annotations_base"))
    edits_exists = _table_exists(cur, "geo_annotations_edits")
    edits_cols = set(_table_columns(cur, "geo_annotations_edits")) if edits_exists else set()

    has_base_surface = "surface_text" in base_cols
    has_base_confidence = "confidence" in base_cols
    has_base_token_len = "token_len" in base_cols
    has_edit_note = "note" in edits_cols

    select_surface_base = "b.surface_text" if has_base_surface else "NULL"
    select_conf_base = "b.confidence" if has_base_confidence else "NULL"
    select_token_len_base = "b.token_len" if has_base_token_len else "NULL"
    select_note_edit = "e.note" if has_edit_note else "NULL"

    if edits_exists:
        sql = f"""
        WITH latest_edits AS (
          SELECT
            e.dhlabid,
            e.seq_start,
            e.action,
            e.nb_place_id,
            {select_note_edit} AS note,
            ROW_NUMBER() OVER (
              PARTITION BY e.dhlabid, e.seq_start
              ORDER BY e.created_at DESC, e.edit_id DESC
            ) AS rn
          FROM geo_annotations_edits e
        ),
        base_keys AS (
          SELECT b.dhlabid, b.seq_start
          FROM geo_annotations_base b
          UNION
          SELECT le.dhlabid, le.seq_start
          FROM latest_edits le
          WHERE le.rn = 1
        ),
        merged AS (
          SELECT
            k.dhlabid,
            k.seq_start,
            b.nb_place_id AS base_place_id,
            {select_surface_base} AS base_surface_text,
            {select_token_len_base} AS base_token_len,
            {select_conf_base} AS base_confidence,
            le.action AS edit_action,
            le.nb_place_id AS edit_place_id,
            le.note AS edit_note
          FROM base_keys k
          LEFT JOIN geo_annotations_base b
            ON b.dhlabid = k.dhlabid AND b.seq_start = k.seq_start
          LEFT JOIN latest_edits le
            ON le.dhlabid = k.dhlabid AND le.seq_start = k.seq_start AND le.rn = 1
        )
        SELECT
          m.dhlabid,
          m.seq_start,
          CASE
            WHEN m.edit_action = 'clear' THEN NULL
            WHEN m.edit_action = 'set_place' THEN m.edit_place_id
            WHEN m.edit_action = 'set_uncertain' THEN COALESCE(m.edit_place_id, m.base_place_id)
            ELSE m.base_place_id
          END AS resolved_place_id,
          COALESCE(NULLIF(TRIM(m.base_surface_text), ''), p.name) AS resolved_surface_text,
          m.base_token_len,
          m.base_confidence,
          CASE
            WHEN m.edit_action = 'set_place' THEN 'edit:set_place'
            WHEN m.edit_action = 'set_uncertain' THEN 'edit:set_uncertain'
            WHEN m.edit_action = 'clear' THEN 'edit:clear'
            ELSE 'base'
          END AS provenance,
          p.geonames_id
        FROM merged m
        LEFT JOIN nb_places p
          ON p.nb_place_id = CASE
            WHEN m.edit_action = 'clear' THEN NULL
            WHEN m.edit_action = 'set_place' THEN m.edit_place_id
            WHEN m.edit_action = 'set_uncertain' THEN COALESCE(m.edit_place_id, m.base_place_id)
            ELSE m.base_place_id
          END
        WHERE CASE
          WHEN m.edit_action = 'clear' THEN NULL
          WHEN m.edit_action = 'set_place' THEN m.edit_place_id
          WHEN m.edit_action = 'set_uncertain' THEN COALESCE(m.edit_place_id, m.base_place_id)
          ELSE m.base_place_id
        END IS NOT NULL
        ORDER BY m.dhlabid, m.seq_start
        """
    else:
        sql = f"""
        SELECT
          b.dhlabid,
          b.seq_start,
          b.nb_place_id,
          COALESCE(NULLIF(TRIM({select_surface_base}), ''), p.name) AS resolved_surface_text,
            {select_token_len_base} AS base_token_len,
          {select_conf_base} AS base_confidence,
          'base' AS provenance,
          p.geonames_id
        FROM geo_annotations_base b
        LEFT JOIN nb_places p ON p.nb_place_id = b.nb_place_id
        ORDER BY b.dhlabid, b.seq_start
        """

    rows = cur.execute(sql).fetchall()
    out: List[ResolvedRow] = []
    for row in rows:
        book_id = int(row[0])
        seq_start = int(row[1])
        place_id = int(row[2]) if row[2] is not None else None
        if place_id is None:
            continue
        surface_text = str(row[3]) if row[3] is not None else None
        token_len_raw = row[4]
        token_len = int(token_len_raw) if token_len_raw is not None and int(token_len_raw) > 0 else _token_len_from_text(surface_text)
        confidence = float(row[5]) if row[5] is not None else None
        provenance = str(row[6] or "base")
        geonames_id = int(row[7]) if row[7] is not None else None
        out.append(
            ResolvedRow(
                book_id=book_id,
                seq_start=seq_start,
                nb_place_id=place_id,
                geonames_id=geonames_id,
                surface_text=surface_text,
                token_len=token_len,
                confidence=confidence,
                provenance=provenance,
            )
        )
    return out


def upsert_places_from_nb(cur: sqlite3.Cursor) -> int:
    cur.execute(
        """
        INSERT OR REPLACE INTO places(
          place_id, canonical_name, geonames_id, feature_class, feature_code, lat, lon, country
        )
        SELECT
          nb_place_id AS place_id,
          name AS canonical_name,
          geonames_id,
          feature_class,
          feature_code,
          latitude AS lat,
          longitude AS lon,
          country_code AS country
        FROM nb_places
        """
    )
    row = cur.execute("SELECT COUNT(*) FROM places").fetchone()
    return int(row[0]) if row else 0


def rebuild_materialized_tables(cur: sqlite3.Cursor, resolved: List[ResolvedRow]) -> None:
    cur.execute("DELETE FROM geo_annotations_resolved")
    cur.execute("DELETE FROM geo_mentions_v2")
    cur.execute("DELETE FROM geo_postings_v2")
    cur.execute("DELETE FROM geo_book_index_v2")
    cur.execute("DELETE FROM geo_spans")

    cur.executemany(
        """
        INSERT INTO geo_annotations_resolved(
          dhlabid, seq_start, nb_place_id, surface_text, token_len, confidence, provenance
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                r.book_id,
                r.seq_start,
                r.nb_place_id,
                r.surface_text,
                r.token_len,
                r.confidence,
                r.provenance,
            )
            for r in resolved
        ],
    )

    cur.executemany(
        """
        INSERT INTO geo_spans(
          book_id, seq_start, token_len, place_id, variant_id, score, method, surface_text, surface_hash
        )
        VALUES (?, ?, ?, ?, NULL, ?, ?, ?, NULL)
        """,
        [
            (
                r.book_id,
                r.seq_start,
                r.token_len,
                r.nb_place_id,
                r.confidence,
                r.provenance,
                r.surface_text,
            )
            for r in resolved
        ],
    )

    mention_rows: List[Tuple[int, int, int, str, str, int, Optional[int], None, Optional[str]]] = []
    postings_len: Dict[Tuple[int, str, str, int], BitMap] = {}
    postings_all_len: Dict[Tuple[int, str, str], BitMap] = {}
    book_all: Dict[int, BitMap] = {}
    book_place_set: Dict[int, set[Tuple[str, str]]] = {}

    for r in resolved:
        key_type = "nb"
        key = str(r.nb_place_id)
        mention_rows.append(
            (
                r.book_id,
                r.seq_start,
                r.token_len,
                key_type,
                key,
                r.nb_place_id,
                r.geonames_id,
                None,
                r.surface_text,
            )
        )

        k_len = (r.book_id, key_type, key, r.token_len)
        bm_len = postings_len.get(k_len)
        if bm_len is None:
            bm_len = BitMap()
            postings_len[k_len] = bm_len
        bm_len.add(r.seq_start)

        k_all = (r.book_id, key_type, key)
        bm_all = postings_all_len.get(k_all)
        if bm_all is None:
            bm_all = BitMap()
            postings_all_len[k_all] = bm_all
        bm_all.add(r.seq_start)

        b_all = book_all.get(r.book_id)
        if b_all is None:
            b_all = BitMap()
            book_all[r.book_id] = b_all
        b_all.add(r.seq_start)

        place_set = book_place_set.get(r.book_id)
        if place_set is None:
            place_set = set()
            book_place_set[r.book_id] = place_set
        place_set.add((key_type, key))

    cur.executemany(
        """
        INSERT INTO geo_mentions_v2(
          book_id, seq_start, token_len, place_key_type, place_key,
          place_id, geonames_id, variant_id, surface_text
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        mention_rows,
    )

    posting_rows: List[Tuple[int, str, str, int, bytes, int]] = []
    for (book_id, key_type, key, token_len), bm in postings_len.items():
        posting_rows.append((book_id, key_type, key, token_len, bm.serialize(), len(bm)))
    for (book_id, key_type, key), bm in postings_all_len.items():
        posting_rows.append((book_id, key_type, key, 0, bm.serialize(), len(bm)))
    cur.executemany(
        """
        INSERT INTO geo_postings_v2(
          book_id, place_key_type, place_key, token_len, starts_roaring, count_mentions
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        posting_rows,
    )

    book_rows: List[Tuple[int, bytes, int, int]] = []
    for book_id, bm in book_all.items():
        unique_places = len(book_place_set.get(book_id, set()))
        total_mentions = len(bm)
        book_rows.append((book_id, bm.serialize(), unique_places, total_mentions))
    cur.executemany(
        """
        INSERT INTO geo_book_index_v2(
          book_id, all_places_roaring, unique_places, total_mentions
        )
        VALUES (?, ?, ?, ?)
        """,
        book_rows,
    )


def main() -> None:
    args = parse_args()
    db_path = Path(args.annotation_db)
    if not db_path.exists():
        raise FileNotFoundError(f"annotation db not found: {db_path}")

    con = sqlite3.connect(str(db_path))
    cur = con.cursor()
    ensure_schema(cur, bool(args.drop_existing))

    resolved_rows = load_resolved_rows(cur)
    places_rows = upsert_places_from_nb(cur)
    rebuild_materialized_tables(cur, resolved_rows)
    con.commit()
    con.close()

    print(f"nb_places_rows={places_rows}")
    print(f"geo_annotations_resolved_rows={len(resolved_rows)}")
    print(f"geo_mentions_v2_rows={len(resolved_rows)}")
    print("geo_postings_v2_rows=see sqlite count")
    print("geo_book_index_v2_rows=see sqlite count")


if __name__ == "__main__":
    main()
