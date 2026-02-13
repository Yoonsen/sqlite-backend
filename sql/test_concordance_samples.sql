-- Concordance sampling (doc_sample=10, per_doc=1, ctx=5, cut=200, top 5)
-- Run: sqlite3 "/mnt/disk4/imagination_shards/imag_00_words_full.db" < sql/test_concordance_samples.sql

.mode list
.separator '|'
.timer on
SELECT load_extension('/mnt/disk1/Github/sqlite-backend/postings_native.so','sqlite3_postings_init');

-- norge
SELECT 'norge (list, doc_sample=10, per_doc=1, ctx=5, cut=200)';
WITH
  sample AS (SELECT book_id FROM urns ORDER BY random() LIMIT 10),
  a AS (SELECT cf_id FROM words WHERE word='norge' LIMIT 1),
  hits AS (
    SELECT s.book_id, ua.post AS post_a
    FROM sample s
    JOIN unigrams ua ON ua.book_id = s.book_id AND ua.cf_id = (SELECT cf_id FROM a)
  ),
  pick AS (
    SELECT book_id, post_a AS blob, post_count(post_a) AS cnt
    FROM hits
  ),
  one AS (
    SELECT book_id,
           CASE WHEN cnt > 0 THEN post_sample(blob, abs(random()) % cnt) END AS seq
    FROM pick
  )
SELECT o.book_id, o.seq,
       substr(
         (SELECT group_concat(word, ' ')
          FROM (
            SELECT w.word
            FROM tokens t
            JOIN words w ON w.raw_id = t.raw_id
            WHERE t.book_id = o.book_id
              AND t.seq BETWEEN o.seq-5 AND o.seq+5
            ORDER BY t.seq
            LIMIT 11
          )),
         1, 200
       ) AS fragment
FROM one o
WHERE o.seq IS NOT NULL
LIMIT 5;

-- norge*
SELECT 'norge* (list, doc_sample=10, per_doc=1, ctx=5, cut=200)';
WITH
  sample AS (SELECT book_id FROM urns ORDER BY random() LIMIT 10),
  cfs AS (
    SELECT cf_id FROM words
    WHERE word >= 'norge' AND word < 'norge\uffff'
    ORDER BY total_tf DESC
  ),
  hits AS (
    SELECT s.book_id, post_union_agg(u.post) AS post_a
    FROM sample s
    JOIN unigrams u ON u.book_id = s.book_id AND u.cf_id IN (SELECT cf_id FROM cfs)
    GROUP BY s.book_id
  ),
  pick AS (
    SELECT book_id, post_a AS blob, post_count(post_a) AS cnt
    FROM hits
  ),
  one AS (
    SELECT book_id,
           CASE WHEN cnt > 0 THEN post_sample(blob, abs(random()) % cnt) END AS seq
    FROM pick
  )
SELECT o.book_id, o.seq,
       substr(
         (SELECT group_concat(word, ' ')
          FROM (
            SELECT w.word
            FROM tokens t
            JOIN words w ON w.raw_id = t.raw_id
            WHERE t.book_id = o.book_id
              AND t.seq BETWEEN o.seq-5 AND o.seq+5
            ORDER BY t.seq
            LIMIT 11
          )),
         1, 200
       ) AS fragment
FROM one o
WHERE o.seq IS NOT NULL
LIMIT 5;

-- og
SELECT 'og (list, doc_sample=10, per_doc=1, ctx=5, cut=200)';
WITH
  sample AS (SELECT book_id FROM urns ORDER BY random() LIMIT 10),
  a AS (SELECT cf_id FROM words WHERE word='og' LIMIT 1),
  hits AS (
    SELECT s.book_id, ua.post AS post_a
    FROM sample s
    JOIN unigrams ua ON ua.book_id = s.book_id AND ua.cf_id = (SELECT cf_id FROM a)
  ),
  pick AS (
    SELECT book_id, post_a AS blob, post_count(post_a) AS cnt
    FROM hits
  ),
  one AS (
    SELECT book_id,
           CASE WHEN cnt > 0 THEN post_sample(blob, abs(random()) % cnt) END AS seq
    FROM pick
  )
SELECT o.book_id, o.seq,
       substr(
         (SELECT group_concat(word, ' ')
          FROM (
            SELECT w.word
            FROM tokens t
            JOIN words w ON w.raw_id = t.raw_id
            WHERE t.book_id = o.book_id
              AND t.seq BETWEEN o.seq-5 AND o.seq+5
            ORDER BY t.seq
            LIMIT 11
          )),
         1, 200
       ) AS fragment
FROM one o
WHERE o.seq IS NOT NULL
LIMIT 5;
