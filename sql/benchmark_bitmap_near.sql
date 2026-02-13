-- Benchmark bitmap-based near (2-group)
-- Run: sqlite3 "/mnt/disk4/imagination_shards/imag_00_postings.db" < sql/benchmark_bitmap_near.sql
.mode list
.separator '|'
.timer on
SELECT load_extension('/mnt/disk1/Github/sqlite-backend/postings_native.so','sqlite3_postings_init');

DROP TABLE IF EXISTS term_cf;
CREATE TEMP TABLE term_cf (grp INTEGER NOT NULL, cf_id INTEGER NOT NULL);
INSERT INTO term_cf(grp, cf_id)
SELECT 1, cf_id FROM words WHERE word IN ('spise','spiser')
UNION ALL
SELECT 2, cf_id FROM words WHERE word='middag';

SELECT 'bitmap_near_count_groups';
WITH combined AS (
  SELECT u.book_id,
         post_near_count_bitmap_groups(t.grp, u.post, -5, 5, 4096) AS c
  FROM unigrams u
  JOIN term_cf t ON t.cf_id = u.cf_id
  GROUP BY u.book_id
)
SELECT SUM(CASE WHEN c > 0 THEN c ELSE 0 END) AS total,
       SUM(CASE WHEN c > 0 THEN 1 ELSE 0 END) AS docs
FROM combined;
