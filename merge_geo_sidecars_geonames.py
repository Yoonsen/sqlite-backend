#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
import tempfile
from pathlib import Path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Merge two geo sidecars into a GeoNames-based annotation_geo_nb.db "
            "staging database (nb_places + geo_annotations_base)."
        )
    )
    p.add_argument("--old-db", required=True, help="Path to older annotation_geo_nb.db")
    p.add_argument("--new-db", required=True, help="Path to newer annotation_geo_nb.db")
    p.add_argument("--output-db", required=True, help="Path to merged output annotation db")
    p.add_argument(
        "--run-id",
        default="geo_geonames_merge",
        help="Run id stored on merged base rows",
    )
    p.add_argument(
        "--replace-output",
        action="store_true",
        help="Replace output path if it already exists",
    )
    return p.parse_args()


def _ensure_exists(path: Path, label: str) -> None:
    if not path.exists():
        raise FileNotFoundError(f"{label} not found: {path}")


def _schema_sql() -> str:
    schema_path = Path(__file__).resolve().parent / "sql" / "annotation_geo_nb.sql"
    return schema_path.read_text(encoding="utf-8")


def init_db(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path))
    cur = con.cursor()
    cur.executescript(_schema_sql())
    con.commit()
    return con


def import_places(cur: sqlite3.Cursor) -> int:
    cur.execute("DELETE FROM nb_places")
    cur.execute(
        """
        WITH place_candidates AS (
          SELECT
            geonames_id AS gid,
            name,
            feature_class,
            feature_code,
            country_code,
            latitude,
            longitude,
            0 AS src_pri
          FROM newdb.nb_places
          WHERE geonames_id IS NOT NULL

          UNION ALL

          SELECT
            geonames_id AS gid,
            canonical_name AS name,
            feature_class,
            feature_code,
            country AS country_code,
            lat AS latitude,
            lon AS longitude,
            1 AS src_pri
          FROM olddb.places
          WHERE geonames_id IS NOT NULL
        ),
        ranked AS (
          SELECT
            gid,
            name,
            feature_class,
            feature_code,
            country_code,
            latitude,
            longitude,
            ROW_NUMBER() OVER (
              PARTITION BY gid
              ORDER BY
                src_pri,
                CASE WHEN TRIM(COALESCE(name, '')) = '' THEN 1 ELSE 0 END,
                CASE WHEN feature_code IS NULL THEN 1 ELSE 0 END,
                gid
            ) AS rn
          FROM place_candidates
        )
        INSERT INTO nb_places(
          nb_place_id, geonames_id, ssr_id, wikidata_id, uib_id, name,
          feature_class, feature_code, country_code, latitude, longitude
        )
        SELECT
          gid,
          gid,
          NULL,
          NULL,
          NULL,
          COALESCE(NULLIF(TRIM(name), ''), CAST(gid AS TEXT)),
          feature_class,
          feature_code,
          country_code,
          latitude,
          longitude
        FROM ranked
        WHERE rn = 1
        """
    )
    row = cur.execute("SELECT COUNT(*) FROM nb_places").fetchone()
    return int(row[0]) if row else 0


def import_base(cur: sqlite3.Cursor, run_id: str) -> int:
    cur.execute("DELETE FROM geo_annotations_base")
    cur.execute(
        """
        WITH exact_candidates AS (
          SELECT DISTINCT
            book_id AS dhlabid,
            seq_start,
            geonames_id,
            token_len,
            surface_text,
            0 AS src_pri
          FROM newdb.geo_mentions_v2
          WHERE geonames_id IS NOT NULL

          UNION

          SELECT DISTINCT
            book_id AS dhlabid,
            seq_start,
            geonames_id,
            token_len,
            surface_text,
            1 AS src_pri
          FROM olddb.geo_mentions_v2
          WHERE geonames_id IS NOT NULL
        ),
        geonames_freq AS (
          SELECT
            geonames_id,
            COUNT(DISTINCT CAST(dhlabid AS TEXT) || ':' || CAST(seq_start AS TEXT)) AS freq_n
          FROM exact_candidates
          GROUP BY geonames_id
        ),
        ranked AS (
          SELECT
            c.dhlabid,
            c.seq_start,
            c.geonames_id,
            c.token_len,
            c.surface_text,
            c.src_pri,
            f.freq_n,
            ROW_NUMBER() OVER (
              PARTITION BY c.dhlabid, c.seq_start
              ORDER BY
                f.freq_n ASC,
                c.src_pri,
                c.token_len DESC,
                CASE WHEN TRIM(COALESCE(c.surface_text, '')) = '' THEN 1 ELSE 0 END,
                c.geonames_id
            ) AS rn
          FROM exact_candidates c
          JOIN geonames_freq f USING(geonames_id)
        )
        INSERT INTO geo_annotations_base(
          dhlabid, seq_start, nb_place_id, surface_text, confidence, resolver,
          model_version, run_id, token_len
        )
        SELECT
          dhlabid,
          seq_start,
          geonames_id AS nb_place_id,
          surface_text,
          NULL AS confidence,
          CASE src_pri
            WHEN 0 THEN 'merge:new'
            ELSE 'merge:old'
          END AS resolver,
          'geo_sidecar_merge_geonames' AS model_version,
          ?,
          token_len
        FROM ranked
        WHERE rn = 1
        """,
        (run_id,),
    )
    row = cur.execute("SELECT COUNT(*) FROM geo_annotations_base").fetchone()
    return int(row[0]) if row else 0


def summarize(cur: sqlite3.Cursor) -> list[tuple[str, int]]:
    q = """
    SELECT 'nb_places_rows', COUNT(*) FROM nb_places
    UNION ALL
    SELECT 'geo_annotations_base_rows', COUNT(*) FROM geo_annotations_base
    UNION ALL
    SELECT 'geo_annotations_base_books', COUNT(DISTINCT dhlabid) FROM geo_annotations_base
    UNION ALL
    SELECT 'geo_annotations_base_places', COUNT(DISTINCT nb_place_id) FROM geo_annotations_base
    """
    return [(str(name), int(value)) for name, value in cur.execute(q)]


def build_merged_db(old_db: Path, new_db: Path, output_db: Path, run_id: str) -> None:
    output_db.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=str(output_db.parent)) as tmpdir:
        tmp_output = Path(tmpdir) / output_db.name
        con = init_db(tmp_output)
        cur = con.cursor()
        cur.execute("ATTACH DATABASE ? AS olddb", (str(old_db),))
        cur.execute("ATTACH DATABASE ? AS newdb", (str(new_db),))

        places_rows = import_places(cur)
        base_rows = import_base(cur, run_id)
        con.commit()

        for key, value in summarize(cur):
            print(f"{key}={value}")
        print(f"imported_nb_places_rows={places_rows}")
        print(f"imported_geo_annotations_base_rows={base_rows}")

        con.close()
        tmp_output.replace(output_db)
        print(f"output_db={output_db}")


def main() -> None:
    args = parse_args()
    old_db = Path(args.old_db)
    new_db = Path(args.new_db)
    output_db = Path(args.output_db)

    _ensure_exists(old_db, "old db")
    _ensure_exists(new_db, "new db")

    if output_db.exists() and not args.replace_output:
        raise FileExistsError(
            f"output already exists: {output_db} (use --replace-output to overwrite)"
        )

    build_merged_db(old_db, new_db, output_db, str(args.run_id))


if __name__ == "__main__":
    main()
