#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Import resolved rows from geo_disambig.db into annotation_geo_nb.db "
            "(nb_places + geo_annotations_base)."
        )
    )
    p.add_argument("--source-db", required=True, help="Path to geo_disambig.db")
    p.add_argument("--annotation-db", required=True, help="Path to writable annotation_geo_nb.db")
    p.add_argument(
        "--run-id",
        default="geo_disambig_import",
        help="Run id stored on imported base rows",
    )
    return p.parse_args()


def _ensure_exists(path: Path, label: str) -> None:
    if not path.exists():
        raise FileNotFoundError(f"{label} not found: {path}")


def _table_exists(cur: sqlite3.Cursor, table_name: str) -> bool:
    row = cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table_name,),
    ).fetchone()
    return bool(row)


def _source_table_exists(cur: sqlite3.Cursor, table_name: str) -> bool:
    row = cur.execute(
        "SELECT 1 FROM src.sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table_name,),
    ).fetchone()
    return bool(row)


def _column_names(cur: sqlite3.Cursor, table_name: str) -> set[str]:
    return {str(row[1]) for row in cur.execute(f"PRAGMA table_info({table_name})")}


def ensure_target_schema(cur: sqlite3.Cursor) -> None:
    required_tables = ("nb_places", "geo_annotations_base")
    for table_name in required_tables:
        if not _table_exists(cur, table_name):
            raise RuntimeError(f"Missing required target table: {table_name}")

    nb_place_cols = _column_names(cur, "nb_places")
    for col_name, col_type in (
        ("ssr_id", "INTEGER"),
        ("wikidata_id", "TEXT"),
        ("uib_id", "TEXT"),
    ):
        if col_name not in nb_place_cols:
            cur.execute(f"ALTER TABLE nb_places ADD COLUMN {col_name} {col_type}")

    base_cols = _column_names(cur, "geo_annotations_base")
    if "token_len" not in base_cols:
        cur.execute("ALTER TABLE geo_annotations_base ADD COLUMN token_len INTEGER")


def _source_has_column(cur: sqlite3.Cursor, table_name: str, col_name: str) -> bool:
    rows = cur.execute(f"PRAGMA src.table_info({table_name})").fetchall()
    return any(str(row[1]) == col_name for row in rows)


def import_nb_places(cur: sqlite3.Cursor) -> int:
    cur.execute("DELETE FROM nb_places")
    if _source_table_exists(cur, "book_place_annotations") and _source_table_exists(cur, "geo_places"):
        has_feature_class = _source_has_column(cur, "geo_places", "feature_class")
        cur.execute(
            f"""
            INSERT INTO nb_places(
              nb_place_id, geonames_id, ssr_id, wikidata_id, uib_id, name,
              feature_class, feature_code, country_code, latitude, longitude
            )
            SELECT
              geonames_id AS nb_place_id,
              geonames_id,
              NULL AS ssr_id,
              NULL AS wikidata_id,
              NULL AS uib_id,
              COALESCE(NULLIF(TRIM(name), ''), CAST(geonames_id AS TEXT)) AS name,
              {"feature_class" if has_feature_class else "NULL"} AS feature_class,
              feature_code,
              country_code,
              lat AS latitude,
              lon AS longitude
            FROM src.geo_places
            """
        )
    elif _source_table_exists(cur, "nb_places"):
        has_wikidata = _source_has_column(cur, "nb_places", "wikidata_id")
        has_uib = _source_has_column(cur, "nb_places", "uib_id")
        cur.execute(
            f"""
            INSERT INTO nb_places(
              nb_place_id, geonames_id, ssr_id, wikidata_id, uib_id, name,
              feature_class, feature_code, country_code, latitude, longitude
            )
            SELECT
              nb_place_id,
              geonames_id,
              ssr_id,
              {"wikidata_id" if has_wikidata else "NULL"} AS wikidata_id,
              {"uib_id" if has_uib else "NULL"} AS uib_id,
              name,
              feature_class,
              feature_code,
              country_code,
              latitude,
              longitude
            FROM src.nb_places
            """
        )
    elif _source_table_exists(cur, "geo_places"):
        has_feature_class = _source_has_column(cur, "geo_places", "feature_class")
        cur.execute(
            f"""
            INSERT INTO nb_places(
              nb_place_id, geonames_id, ssr_id, wikidata_id, uib_id, name,
              feature_class, feature_code, country_code, latitude, longitude
            )
            SELECT
              geonames_id AS nb_place_id,
              geonames_id,
              NULL AS ssr_id,
              NULL AS wikidata_id,
              NULL AS uib_id,
              COALESCE(NULLIF(TRIM(name), ''), CAST(geonames_id AS TEXT)) AS name,
              {"feature_class" if has_feature_class else "NULL"} AS feature_class,
              feature_code,
              country_code,
              lat AS latitude,
              lon AS longitude
            FROM src.geo_places
            """
        )
    else:
        raise RuntimeError("Source db must provide either nb_places or geo_places")
    row = cur.execute("SELECT COUNT(*) FROM nb_places").fetchone()
    return int(row[0]) if row else 0


def import_geo_annotations_base(cur: sqlite3.Cursor, run_id: str) -> int:
    cur.execute("DELETE FROM geo_annotations_base")
    if _source_table_exists(cur, "book_place_annotations"):
        cur.execute(
            """
            WITH ranked AS (
              SELECT
                bpa.dhlabid,
                bpa.seq_start,
                bpa.geonames_id AS nb_place_id,
                COALESCE(NULLIF(TRIM(bpa.surface), ''), gp.name) AS surface_text,
                bpa.len AS token_len,
                NULL AS confidence,
                'geo_disambig_bpa' AS resolver,
                'book_place_annotations' AS model_version,
                ROW_NUMBER() OVER (
                  PARTITION BY bpa.dhlabid, bpa.seq_start
                  ORDER BY
                    CASE
                      WHEN LOWER(TRIM(COALESCE(bpa.surface, ''))) = LOWER(TRIM(COALESCE(gp.name, ''))) THEN 0
                      ELSE 1
                    END,
                    bpa.len DESC,
                    bpa.geonames_id
                ) AS rn
              FROM src.book_place_annotations bpa
              JOIN src.geo_places gp
                ON gp.geonames_id = bpa.geonames_id
            )
            INSERT INTO geo_annotations_base(
              dhlabid, seq_start, nb_place_id, surface_text, confidence, resolver,
              model_version, run_id, token_len
            )
            SELECT
              dhlabid,
              seq_start,
              nb_place_id,
              surface_text,
              confidence,
              resolver,
              model_version,
              ?,
              token_len
            FROM ranked
            WHERE rn = 1
            """,
            (run_id,),
        )
    elif _source_table_exists(cur, "geo_annotations") and _source_table_exists(cur, "nb_places"):
        cur.execute(
            """
            WITH ranked AS (
              SELECT
                ga.dhlabid,
                ga.seq_start,
                np.nb_place_id,
                COALESCE(NULLIF(TRIM(ga.surface), ''), np.name) AS surface_text,
                c.token_len AS token_len,
                ga.confidence,
                COALESCE(NULLIF(TRIM(ga.source), ''), 'geo_disambig') AS resolver,
                'geo_disambig' AS model_version,
                ROW_NUMBER() OVER (
                  PARTITION BY ga.dhlabid, ga.seq_start
                  ORDER BY
                    COALESCE(ga.confidence, -1.0) DESC,
                    CASE COALESCE(ga.source, '')
                      WHEN 'unique' THEN 0
                      WHEN 'disambig' THEN 1
                      WHEN 'ppl_adm' THEN 2
                      ELSE 3
                    END,
                    np.nb_place_id
                ) AS rn
              FROM src.geo_annotations ga
              JOIN src.nb_places np
                ON np.geonames_id = ga.geonames_id
              LEFT JOIN src.concordances c
                ON c.dhlabid = ga.dhlabid
               AND c.seq_start = ga.seq_start
               AND c.surface = ga.surface
               AND c.geonames_id = ga.geonames_id
              WHERE ga.seq_start IS NOT NULL
            )
            INSERT INTO geo_annotations_base(
              dhlabid, seq_start, nb_place_id, surface_text, confidence, resolver,
              model_version, run_id, token_len
            )
            SELECT
              dhlabid,
              seq_start,
              nb_place_id,
              surface_text,
              confidence,
              resolver,
              model_version,
              ?,
              token_len
            FROM ranked
            WHERE rn = 1
            """,
            (run_id,),
        )
    else:
        raise RuntimeError(
            "Source db must provide either book_place_annotations+geo_places "
            "or geo_annotations+nb_places"
        )
    row = cur.execute("SELECT COUNT(*) FROM geo_annotations_base").fetchone()
    return int(row[0]) if row else 0


def summarize(cur: sqlite3.Cursor) -> dict[str, int]:
    summary_queries = {
        "nb_places_rows": "SELECT COUNT(*) FROM nb_places",
        "geo_annotations_base_rows": "SELECT COUNT(*) FROM geo_annotations_base",
        "geo_annotations_base_books": "SELECT COUNT(DISTINCT dhlabid) FROM geo_annotations_base",
        "geo_annotations_base_with_token_len": "SELECT COUNT(*) FROM geo_annotations_base WHERE token_len IS NOT NULL AND token_len > 0",
        "geo_annotations_base_without_token_len": "SELECT COUNT(*) FROM geo_annotations_base WHERE token_len IS NULL OR token_len <= 0",
    }
    out: dict[str, int] = {}
    for key, sql in summary_queries.items():
        row = cur.execute(sql).fetchone()
        out[key] = int(row[0]) if row and row[0] is not None else 0
    return out


def main() -> None:
    args = parse_args()
    source_db = Path(args.source_db)
    annotation_db = Path(args.annotation_db)
    _ensure_exists(source_db, "source db")
    _ensure_exists(annotation_db, "annotation db")

    con = sqlite3.connect(str(annotation_db))
    cur = con.cursor()
    cur.execute("ATTACH DATABASE ? AS src", (str(source_db),))
    ensure_target_schema(cur)

    nb_places_rows = import_nb_places(cur)
    base_rows = import_geo_annotations_base(cur, str(args.run_id))
    con.commit()

    summary = summarize(cur)
    con.close()

    print(f"imported_nb_places_rows={nb_places_rows}")
    print(f"imported_geo_annotations_base_rows={base_rows}")
    for key, value in summary.items():
        print(f"{key}={value}")


if __name__ == "__main__":
    main()
