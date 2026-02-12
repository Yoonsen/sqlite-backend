#!/usr/bin/env python3
from __future__ import annotations

import json
import random
import sqlite3
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd
import streamlit as st

LAST_DEBUG_BOOK_IDS: List[int] = []
LAST_DEBUG_BOOKS_WITH_POS: List[int] = []

def connect_postings(db_path: str, ext_path: str) -> sqlite3.Connection:
    db = (db_path or "").strip()
    con = sqlite3.connect(db)
    con.enable_load_extension(True)
    ext = (ext_path or "").strip()
    if ext:
        con.execute("SELECT load_extension(?, ?)", (ext, "sqlite3_postings_init"))
    return con


def connect_words(db_path: str) -> sqlite3.Connection:
    db = (db_path or "").strip()
    return sqlite3.connect(db)


def ensure_urn_filter(con: sqlite3.Connection, urns: List[int]) -> None:
    con.execute("DROP TABLE IF EXISTS urn_filter;")
    con.execute("CREATE TEMP TABLE urn_filter (urn INTEGER PRIMARY KEY) WITHOUT ROWID;")
    con.executemany("INSERT INTO urn_filter(urn) VALUES (?)", [(u,) for u in urns])


def filter_count(cur: sqlite3.Cursor) -> int:
    row = cur.execute("SELECT COUNT(*) FROM urn_filter").fetchone()
    return int(row[0]) if row else 0


def count_cf(
    cur: sqlite3.Cursor,
    cf_id: int,
    ngrams_table: str,
    use_filter: bool,
) -> int:
    if use_filter:
        row = cur.execute(
            f"""
            SELECT COUNT(*) FROM {ngrams_table} n
            JOIN urn_filter f ON f.urn = n.book_id
            WHERE n.cf_id = ?
            """,
            (cf_id,),
        ).fetchone()
    else:
        row = cur.execute(
            f"SELECT COUNT(*) FROM {ngrams_table} WHERE cf_id = ?", (cf_id,)
        ).fetchone()
    return int(row[0]) if row else 0

def load_corpus_ids(upload) -> List[int]:
    if upload is None:
        return []
    name = upload.name.lower()
    if name.endswith(".xlsx") or name.endswith(".xls"):
        df = pd.read_excel(upload)
    else:
        df = pd.read_csv(upload)
    if df.empty:
        return []
    for col in ("dhlabid", "urn_seq", "book_id"):
        if col in df.columns:
            series = df[col]
            break
    else:
        series = df.iloc[:, 0]
    ids = []
    for v in series.dropna().tolist():
        try:
            if isinstance(v, float):
                ids.append(int(v))
            else:
                ids.append(int(str(v).strip()))
        except (ValueError, TypeError):
            continue
    return ids


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
) -> List[Tuple[int, int, str]]:
    inner = cur.connection.cursor()
    debug_ids: List[int] = []
    if use_filter:
        sql = """
            SELECT u.book_id, u.tf, u.post
            FROM unigrams u
            JOIN urn_filter f ON f.urn = u.book_id
            WHERE u.cf_id = ?
        """
    else:
        sql = "SELECT book_id, tf, post FROM unigrams WHERE cf_id = ?"
    out = []
    for book_id, tf, post in cur.execute(sql, (cf_id,)):
        debug_ids.append(book_id)
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
    global LAST_DEBUG_BOOK_IDS
    LAST_DEBUG_BOOK_IDS = debug_ids[:50]
    global LAST_DEBUG_BOOKS_WITH_POS
    LAST_DEBUG_BOOKS_WITH_POS = debug_ids[:50]
    return out


def sample_concordance_near(
    cur: sqlite3.Cursor,
    curw: sqlite3.Cursor,
    cf_a: int,
    cf_b: int,
    window: int,
    per_book: int,
    before: int,
    after: int,
    use_filter: bool,
    ngrams_table: str,
    off_min: int,
    off_max: int,
    exclude_self: bool,
) -> List[Tuple[int, int, str]]:
    inner = cur.connection.cursor()
    debug_ids: List[int] = []
    debug_with_pos: List[int] = []
    if use_filter:
        sql = f"""
            SELECT a.book_id, a.post, b.post
            FROM {ngrams_table} a
            JOIN {ngrams_table} b ON a.book_id = b.book_id
            JOIN urn_filter f ON f.urn = a.book_id
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
    for book_id, post_a, post_b in cur.execute(sql, (cf_a, cf_b)):
        debug_ids.append(book_id)
        if exclude_self and cf_a == cf_b and off_min == 0 and off_max == 0:
            row = inner.execute(
                "SELECT post_near_positions(?, ?, ?, ?)", (post_a, post_b, 1, 1)
            ).fetchone()
        else:
            row = inner.execute(
                "SELECT post_near_positions(?, ?, ?, ?)",
                (post_a, post_b, off_min, off_max),
            ).fetchone()
        if row is None:
            continue
        positions = json.loads(row[0]) if row[0] else []
        if not positions:
            continue
        debug_with_pos.append(book_id)
        samples = min(per_book, len(positions))
        for pos in random.sample(positions, samples):
            frag = fetch_window(cur, curw, book_id, int(pos), before, after)
            out.append((book_id, int(pos), frag))
    global LAST_DEBUG_BOOK_IDS
    LAST_DEBUG_BOOK_IDS = debug_ids[:50]
    global LAST_DEBUG_BOOKS_WITH_POS
    LAST_DEBUG_BOOKS_WITH_POS = debug_with_pos[:50]
    return out


def near_frequency(
    cur: sqlite3.Cursor,
    cf_a: int,
    cf_b: int,
    window: int,
    use_filter: bool,
    ngrams_table: str,
    symmetric: bool,
    exclude_self: bool,
) -> Tuple[int, int]:
    inner = cur.connection.cursor()
    if use_filter:
        sql = f"""
            SELECT a.book_id, a.post, b.post
            FROM {ngrams_table} a
            JOIN {ngrams_table} b ON a.book_id = b.book_id
            JOIN urn_filter f ON f.urn = a.book_id
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
    for _, post_a, post_b in cur.execute(sql, (cf_a, cf_b)):
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
    ngrams_table: str,
) -> Dict[str, int]:
    inner = cur.connection.cursor()
    if use_filter:
        sql = f"""
            SELECT u.book_id, u.tf, u.post
            FROM {ngrams_table} u
            JOIN urn_filter f ON f.urn = u.book_id
            WHERE u.cf_id = ?
        """
    else:
        sql = f"SELECT book_id, tf, post FROM {ngrams_table} WHERE cf_id = ?"
    counts: Dict[str, int] = {}
    for book_id, tf, post in cur.execute(sql, (cf_id,)):
        if tf <= 0:
            continue
        samples = min(per_book, tf)
        for _ in range(samples):
            idx = random.randrange(tf)
            row = cur.execute("SELECT post_sample(?, ?)", (post, idx)).fetchone()
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
            for raw_id, in rows:
                w = raw_map.get(raw_id, "?").casefold()
                counts[w] = counts.get(w, 0) + 1
    return counts


st.set_page_config(page_title="Postings Demo", layout="wide")
st.title("Postings Demo")

with st.sidebar:
    st.header("DB paths")
    postings_db = st.text_input(
        "Postings DB",
        value="/mnt/disk4/imagination_shards/imag_00_postings.db",
    )
    words_db = st.text_input("Words DB", value="")
    ext_path = st.text_input("Postings extension (.so)", value="postings_native.so")
    if st.button("Validate DBs"):
        try:
            con_p = connect_postings(postings_db, ext_path)
            cur_p = con_p.cursor()
            tables_p = [
                r[0]
                for r in cur_p.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                ).fetchall()
            ]
            con_p.close()
            st.write("Postings tables:", tables_p)
        except Exception as e:
            st.error(f"Postings DB error: {e}")
        try:
            con_w = connect_words(words_db)
            cur_w = con_w.cursor()
            tables_w = [
                r[0]
                for r in cur_w.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                ).fetchall()
            ]
            con_w.close()
            st.write("Words tables:", tables_w)
        except Exception as e:
            st.error(f"Words DB error: {e}")
    st.header("Corpus filter")
    upload = st.file_uploader("Upload CSV/XLSX with dhlabid", type=["csv", "xlsx", "xls"])
    if "corpus_ids" not in st.session_state:
        st.session_state["corpus_ids"] = []
    if "use_filter" not in st.session_state:
        st.session_state["use_filter"] = False
    if "upload_name" not in st.session_state:
        st.session_state["upload_name"] = ""
    if upload is not None:
        if upload.name != st.session_state["upload_name"]:
            st.session_state["upload_name"] = upload.name
            st.session_state["corpus_ids"] = load_corpus_ids(upload)
    if st.button("Sample 500 ids from urns"):
        try:
            con_tmp = connect_postings(postings_db, ext_path)
            cur_tmp = con_tmp.cursor()
            has_urns = cur_tmp.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='urns'"
            ).fetchone()
            has_tokens = cur_tmp.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tokens'"
            ).fetchone()
            has_unigrams = cur_tmp.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='unigrams'"
            ).fetchone()
            has_ngrams = cur_tmp.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ngrams'"
            ).fetchone()
            if has_urns:
                rows = cur_tmp.execute(
                    "SELECT book_id FROM urns ORDER BY RANDOM() LIMIT 500"
                ).fetchall()
            elif has_tokens:
                rows = cur_tmp.execute(
                    "SELECT DISTINCT book_id FROM tokens ORDER BY RANDOM() LIMIT 500"
                ).fetchall()
            elif has_unigrams:
                rows = cur_tmp.execute(
                    "SELECT DISTINCT book_id FROM unigrams ORDER BY RANDOM() LIMIT 500"
                ).fetchall()
            elif has_ngrams:
                rows = cur_tmp.execute(
                    "SELECT DISTINCT book_id FROM ngrams ORDER BY RANDOM() LIMIT 500"
                ).fetchall()
            else:
                raise RuntimeError("No urns/tokens/unigrams table found to sample ids")
            con_tmp.close()
            st.session_state["corpus_ids"] = [r[0] for r in rows]
            st.session_state["use_filter"] = True
        except Exception as e:
            st.error(f"Failed to sample ids: {e}")
    if st.button("Clear filter"):
        st.session_state["corpus_ids"] = []
        st.session_state["upload_name"] = ""
        st.session_state["use_filter"] = False
    if st.button("Force no filter (use full DB)"):
        st.session_state["corpus_ids"] = []
        st.session_state["upload_name"] = ""
        st.session_state["use_filter"] = False
    corpus_ids = st.session_state["corpus_ids"]
    use_filter_override = st.checkbox(
        "Use corpus filter", value=st.session_state["use_filter"]
    )
    st.session_state["use_filter"] = use_filter_override
    if corpus_ids:
        st.success(f"Loaded {len(corpus_ids)} ids")
        if len(corpus_ids) <= 10:
            st.write("IDs:", corpus_ids)
        else:
            st.write("First 10 ids:", corpus_ids[:10])
    else:
        st.info("No corpus filter loaded (uses full DB)")
    if corpus_ids and use_filter_override:
        try:
            con_tmp = connect_postings(postings_db, ext_path)
            cur_tmp = con_tmp.cursor()
            ensure_urn_filter(con_tmp, corpus_ids)
            overlap = cur_tmp.execute(
                f"""
                SELECT COUNT(DISTINCT n.book_id)
                FROM {schema_choice} n
                JOIN urn_filter f ON f.urn = n.book_id
                """
            ).fetchone()[0]
            con_tmp.close()
            st.info(f"Filter overlap in DB: {overlap}")
        except Exception as e:
            st.write(f"Overlap check failed: {e}")

    st.header("Schema")
    try:
        con_tmp = connect_postings(postings_db, ext_path)
        cur_tmp = con_tmp.cursor()
        has_unigrams = cur_tmp.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='unigrams'"
        ).fetchone()
        has_ngrams = cur_tmp.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ngrams'"
        ).fetchone()
        con_tmp.close()
    except Exception:
        has_unigrams = None
        has_ngrams = None

    options = ["unigrams", "ngrams"]
    if has_unigrams and not has_ngrams:
        default_index = 0
    elif has_ngrams and not has_unigrams:
        default_index = 1
    else:
        default_index = 0
    schema_choice = st.selectbox("Token index table", options=options, index=default_index)
    if (schema_choice == "unigrams" and not has_unigrams) or (
        schema_choice == "ngrams" and not has_ngrams
    ):
        st.warning(f"Selected table '{schema_choice}' not found in DB.")

st.header("1) Concordance sampling")
col1, col2, col3 = st.columns(3)
with col1:
    word_a = st.text_input("Word A", value="og")
with col2:
    word_b = st.text_input("Word B (optional)", value="")
with col3:
    window = st.number_input("Window (for near)", min_value=1, max_value=50, value=5)
col4, col5, col6 = st.columns(3)
with col4:
    per_book = st.number_input("Samples per book", min_value=1, max_value=10, value=3)
with col5:
    before = st.number_input("Words before", min_value=1, max_value=20, value=5)
with col6:
    after = st.number_input("Words after", min_value=1, max_value=20, value=5)
sym_near = st.checkbox("Symmetric near (orderless)", value=True)
exclude_self = st.checkbox("Exclude self matches", value=False)

debug_conc = st.checkbox("Debug concordance loop", value=False)
if st.button("Sample concordance"):
    con = connect_postings(postings_db, ext_path)
    conw = connect_words(words_db)
    cur = con.cursor()
    curw = conw.cursor()
    if corpus_ids and use_filter_override:
        ensure_urn_filter(con, corpus_ids)
        use_filter = True
        st.info(f"Filter rows: {filter_count(cur)}")
    else:
        use_filter = False
    cf_a = get_cf_id(curw, word_a)
    if cf_a is None:
        st.error("Word A not found")
    else:
        if word_b.strip():
            cf_b = get_cf_id(curw, word_b)
            if cf_b is None:
                st.error("Word B not found")
            else:
                count_a = count_cf(cur, cf_a, schema_choice, use_filter)
                count_b = count_cf(cur, cf_b, schema_choice, use_filter)
                st.write(f"Rows for A: {count_a}, rows for B: {count_b}")
                if sym_near:
                    off_min, off_max = -before, after
                else:
                    off_min, off_max = 1, after
                rows = sample_concordance_near(
                    cur,
                    curw,
                    cf_a,
                    cf_b,
                    window,
                    per_book,
                    before,
                    after,
                    use_filter,
                    schema_choice,
                    off_min,
                    off_max,
                    exclude_self,
                )
        else:
            rows = sample_concordance_single(
                cur, curw, cf_a, per_book, before, after, use_filter
            )
        if debug_conc:
            book_counts: Dict[int, int] = {}
            for book_id, _, _ in rows:
                book_counts[book_id] = book_counts.get(book_id, 0) + 1
            st.write(f"Distinct books: {len(book_counts)}")
            st.write("Book sample counts:", book_counts)
            st.write("Book ids iterated:", LAST_DEBUG_BOOK_IDS)
            st.write("Book ids with positions:", LAST_DEBUG_BOOKS_WITH_POS)
            st.write(
                {
                    "use_filter": use_filter,
                    "filter_ids": len(corpus_ids),
                }
            )
            try:
                if word_b.strip():
                    inner = con.cursor()
                    if use_filter:
                        cnt = inner.execute(
                            f"""
                            SELECT COUNT(DISTINCT a.book_id)
                            FROM {schema_choice} a
                            JOIN {schema_choice} b ON a.book_id = b.book_id
                            JOIN urn_filter f ON f.urn = a.book_id
                            WHERE a.cf_id = ? AND b.cf_id = ?
                            """,
                            (cf_a, cf_b),
                        ).fetchone()[0]
                    else:
                        cnt = inner.execute(
                            f"""
                            SELECT COUNT(DISTINCT a.book_id)
                            FROM {schema_choice} a
                            JOIN {schema_choice} b ON a.book_id = b.book_id
                            WHERE a.cf_id = ? AND b.cf_id = ?
                            """,
                            (cf_a, cf_b),
                        ).fetchone()[0]
                    st.write(f"Books matching pair: {cnt}")
            except Exception as e:
                st.write(f"Debug count failed: {e}")
        st.write(f"Samples: {len(rows)}")
        for book_id, pos, frag in rows:
            st.write(f"{book_id} @ {pos}: {frag}")
    con.close()
    conw.close()

st.header("2) Near frequency")
col7, col8, col9 = st.columns(3)
with col7:
    freq_a = st.text_input("Word A (freq)", value="demokrati")
with col8:
    freq_b = st.text_input("Word B (freq)", value="og")
with col9:
    freq_window = st.number_input("Window n", min_value=1, max_value=50, value=5)
sym_freq = st.checkbox("Symmetric near frequency", value=True)
exclude_self_freq = st.checkbox("Exclude self matches (freq)", value=False)

if st.button("Compute near frequency"):
    con = connect_postings(postings_db, ext_path)
    conw = connect_words(words_db)
    cur = con.cursor()
    curw = conw.cursor()
    if corpus_ids and use_filter_override:
        ensure_urn_filter(con, corpus_ids)
        use_filter = True
        st.info(f"Filter rows: {filter_count(cur)}")
    else:
        use_filter = False
    cf_a = get_cf_id(curw, freq_a)
    cf_b = get_cf_id(curw, freq_b)
    if cf_a is None or cf_b is None:
        st.error("Word not found")
    else:
        count_a = count_cf(cur, cf_a, schema_choice, use_filter)
        count_b = count_cf(cur, cf_b, schema_choice, use_filter)
        st.write(f"Rows for A: {count_a}, rows for B: {count_b}")
        total, docs = near_frequency(
            cur,
            cf_a,
            cf_b,
            freq_window,
            use_filter,
            schema_choice,
            sym_freq,
            exclude_self_freq,
        )
        st.write(f"Total near hits: {total}")
        st.write(f"Docs with hits: {docs}")
    con.close()
    conw.close()

st.header("3) Collocations (sampled)")
col10, col11, col12 = st.columns(3)
with col10:
    coll_word = st.text_input("Word", value="og")
with col11:
    coll_before = st.number_input("Collocation before", min_value=1, max_value=50, value=5)
with col12:
    coll_after = st.number_input("Collocation after", min_value=1, max_value=50, value=5)
coll_per_book = st.number_input(
    "Samples per book (collocations)", min_value=1, max_value=10, value=3
)

if st.button("Sample collocations"):
    con = connect_postings(postings_db, ext_path)
    conw = connect_words(words_db)
    cur = con.cursor()
    curw = conw.cursor()
    if corpus_ids and use_filter_override:
        ensure_urn_filter(con, corpus_ids)
        use_filter = True
        st.info(f"Filter rows: {filter_count(cur)}")
    else:
        use_filter = False
    cf = get_cf_id(curw, coll_word)
    if cf is None:
        st.error("Word not found")
    else:
        count_a = count_cf(cur, cf, schema_choice, use_filter)
        st.write(f"Rows for word: {count_a}")
        counts = sample_collocations(
            cur,
            curw,
            cf,
            coll_per_book,
            coll_before,
            coll_after,
            use_filter,
            schema_choice,
        )
        top = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:50]
        st.write(pd.DataFrame(top, columns=["word", "count"]))
    con.close()
    conw.close()

st.header("4) Filter sanity check")
st.write("Samples one word per book_id from the selected table.")
if st.button("Sample 1 word per book"):
    con = connect_postings(postings_db, ext_path)
    cur = con.cursor()
    if corpus_ids and use_filter_override:
        ensure_urn_filter(con, corpus_ids)
        use_filter = True
    else:
        use_filter = False
    table = schema_choice
    if use_filter:
        rows = cur.execute(
            f"""
            SELECT n.book_id, MIN(n.cf_id)
            FROM {table} n
            JOIN urn_filter f ON f.urn = n.book_id
            GROUP BY n.book_id
            ORDER BY n.book_id
            LIMIT 50
            """
        ).fetchall()
    else:
        rows = cur.execute(
            f"""
            SELECT n.book_id, MIN(n.cf_id)
            FROM {table} n
            GROUP BY n.book_id
            ORDER BY n.book_id
            LIMIT 50
            """
        ).fetchall()
    con.close()
    st.write(f"Books sampled: {len(rows)}")
    if rows:
        cf_ids = [r[1] for r in rows]
        conw = connect_words(words_db)
        curw = conw.cursor()
        placeholders = ",".join("?" for _ in cf_ids)
        word_rows = curw.execute(
            f"""
            SELECT cf_id, word
            FROM words
            WHERE cf_id IN ({placeholders})
            GROUP BY cf_id
            """,
            cf_ids,
        ).fetchall()
        conw.close()
        cf_word = {cf_id: word for cf_id, word in word_rows}
        st.write(
            [
                (book_id, cf_id, cf_word.get(cf_id, "?"))
                for book_id, cf_id in rows
            ]
        )

st.header("5) Temp-table CF test")
st.write("Uses three random book_ids to test near counts.")
if st.button("Run temp-table CF test"):
    con = connect_postings(postings_db, ext_path)
    cur = con.cursor()
    # pick 3 random book_ids
    rows = cur.execute(
        f"SELECT DISTINCT book_id FROM {schema_choice} ORDER BY RANDOM() LIMIT 3"
    ).fetchall()
    book_ids = [r[0] for r in rows]
    if not book_ids:
        st.error("No book_ids found.")
    else:
        ensure_urn_filter(con, book_ids)
        st.info(f"Filter rows: {filter_count(cur)}")
        cf_rows = cur.execute(
            f"SELECT DISTINCT cf_id FROM {schema_choice} WHERE book_id = ? LIMIT 3",
            (book_ids[0],),
        ).fetchall()
        if len(cf_rows) < 2:
            st.error("Not enough cf_id values in sample book.")
        else:
            cf_a = cf_rows[0][0]
            cf_b = cf_rows[1][0]
            rows = cur.execute(
                f"""
                SELECT a.book_id, a.post, b.post
                FROM {schema_choice} a
                JOIN {schema_choice} b ON a.book_id = b.book_id
                JOIN urn_filter f ON f.urn = a.book_id
                WHERE a.cf_id = ? AND b.cf_id = ?
                """,
                (cf_a, cf_b),
            ).fetchall()
            hits = 0
            docs = 0
            for _, post_a, post_b in rows:
                cnt = cur.execute(
                    "SELECT post_near_count(?, ?, 1, ?)", (post_a, post_b, 5)
                ).fetchone()[0]
                hits += cnt
                if cnt > 0:
                    docs += 1
            st.write(
                {
                    "book_ids": book_ids,
                    "cf_a": cf_a,
                    "cf_b": cf_b,
                    "rows": len(rows),
                    "hits": hits,
                    "docs_with_hits": docs,
                }
            )
    con.close()
