#!/usr/bin/env python3
from __future__ import annotations

import json
import random
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd
import streamlit as st


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


def shard_words_path(postings_path: str, words_db: str) -> str:
    words = (words_db or "").strip()
    return words if words else postings_path


def parse_shard_paths(primary: str, secondary: str) -> List[str]:
    paths = [p.strip() for p in (primary, secondary) if p.strip()]
    deduped: List[str] = []
    for p in paths:
        if p not in deduped:
            deduped.append(p)
    return deduped


def sample_ids_from_shards(
    postings_paths: List[str], ext_path: str, sample_size: int
) -> List[int]:
    if not postings_paths:
        return []
    per = max(1, sample_size // len(postings_paths))
    extra = sample_size - (per * len(postings_paths))
    ids: List[int] = []
    for idx, path in enumerate(postings_paths):
        limit = per + (1 if idx < extra else 0)
        con = connect_postings(path, ext_path)
        cur = con.cursor()
        rows = cur.execute(
            "SELECT book_id FROM urns ORDER BY RANDOM() LIMIT ?", (limit,)
        ).fetchall()
        con.close()
        ids.extend([r[0] for r in rows])
    random.shuffle(ids)
    return ids[:sample_size]


def run_on_shards(postings_paths: List[str], worker):
    if not postings_paths:
        return []
    max_workers = min(4, len(postings_paths))
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(worker, path) for path in postings_paths]
        return [f.result() for f in futures]


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
    ngrams_table: str,
    off_min: int,
    off_max: int,
    exclude_self: bool,
) -> List[Tuple[int, int, str]]:
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
    out = []
    for book_id, post_a, post_b in cur.execute(sql, (cf_a, cf_b)):
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
        samples = min(per_book, len(positions))
        for pos in random.sample(positions, samples):
            frag = fetch_window(cur, curw, book_id, int(pos), before, after)
            out.append((book_id, int(pos), frag))
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
            row = inner.execute("SELECT post_sample(?, ?)", (post, idx)).fetchone()
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
        "Postings DB (Shard 1)",
        value="/mnt/disk4/imagination_shards/imag_00_postings.db",
    )
    postings_db_2 = st.text_input(
        "Postings DB (Shard 2, optional)",
        value="/mnt/disk4/imagination_shards/imag_01_postings.db",
    )
    words_db = st.text_input(
        "Words DB (optional; leave blank to use per-shard words)",
        value="",
    )
    ext_path = st.text_input("Postings extension (.so)", value="postings_native.so")
    st.header("Corpus filter")
    upload = st.file_uploader("Upload CSV/XLSX with dhlabid", type=["csv", "xlsx", "xls"])
    if "corpus_ids" not in st.session_state:
        st.session_state["corpus_ids"] = []
    if "use_filter" not in st.session_state:
        st.session_state["use_filter"] = False
    if upload is not None:
        st.session_state["corpus_ids"] = load_corpus_ids(upload)
    if st.button("Sample 500 ids from urns"):
        try:
            postings_paths = parse_shard_paths(postings_db, postings_db_2)
            st.session_state["corpus_ids"] = sample_ids_from_shards(
                postings_paths, ext_path, 500
            )
            st.session_state["use_filter"] = True
        except Exception as e:
            st.error(f"Failed to sample ids: {e}")
    if st.button("Clear filter"):
        st.session_state["corpus_ids"] = []
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
    st.header("Schema")
    schema_choice = st.selectbox(
        "Token index table",
        options=["unigrams", "ngrams"],
        index=0,
    )

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

if st.button("Sample concordance"):
    postings_paths = parse_shard_paths(postings_db, postings_db_2)

    def worker(path: str):
        con = connect_postings(path, ext_path)
        conw = connect_words(shard_words_path(path, words_db))
        cur = con.cursor()
        curw = conw.cursor()
        if corpus_ids and use_filter_override:
            ensure_urn_filter(con, corpus_ids)
            use_filter = True
        else:
            use_filter = False
        cf_a = get_cf_id(curw, word_a)
        if cf_a is None:
            con.close()
            conw.close()
            return {"rows": [], "cf_a": False, "cf_b": True}
        if word_b.strip():
            cf_b = get_cf_id(curw, word_b)
            if cf_b is None:
                con.close()
                conw.close()
                return {"rows": [], "cf_a": True, "cf_b": False}
            if sym_near:
                off_min, off_max = -before, after
            else:
                off_min, off_max = 1, after
            rows = sample_concordance_near(
                cur,
                curw,
                cf_a,
                cf_b,
                per_book,
                before,
                after,
                use_filter,
                schema_choice,
                off_min,
                off_max,
                exclude_self,
            )
            con.close()
            conw.close()
            return {"rows": rows, "cf_a": True, "cf_b": True}
        rows = sample_concordance_single(
            cur, curw, cf_a, per_book, before, after, use_filter
        )
        con.close()
        conw.close()
        return {"rows": rows, "cf_a": True, "cf_b": True}

    results = run_on_shards(postings_paths, worker)
    rows = [r for res in results for r in res["rows"]]
    cf_a_found = any(res["cf_a"] for res in results)
    cf_b_found = any(res["cf_b"] for res in results)
    if not cf_a_found:
        st.error("Word A not found")
    elif word_b.strip() and not cf_b_found:
        st.error("Word B not found")
    else:
        st.write(f"Samples: {len(rows)}")
        for book_id, pos, frag in rows:
            st.write(f"{book_id} @ {pos}: {frag}")

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
    postings_paths = parse_shard_paths(postings_db, postings_db_2)

    def worker(path: str):
        con = connect_postings(path, ext_path)
        conw = connect_words(shard_words_path(path, words_db))
        cur = con.cursor()
        curw = conw.cursor()
        if corpus_ids and use_filter_override:
            ensure_urn_filter(con, corpus_ids)
            use_filter = True
        else:
            use_filter = False
        cf_a = get_cf_id(curw, freq_a)
        cf_b = get_cf_id(curw, freq_b)
        if cf_a is None or cf_b is None:
            con.close()
            conw.close()
            return {"total": 0, "docs": 0, "cf_found": False}
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
        con.close()
        conw.close()
        return {"total": total, "docs": docs, "cf_found": True}

    results = run_on_shards(postings_paths, worker)
    if not any(res["cf_found"] for res in results):
        st.error("Word not found")
    else:
        total = sum(res["total"] for res in results)
        docs = sum(res["docs"] for res in results)
        st.write(f"Total near hits: {total}")
        st.write(f"Docs with hits: {docs}")

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
    postings_paths = parse_shard_paths(postings_db, postings_db_2)

    def worker(path: str):
        con = connect_postings(path, ext_path)
        conw = connect_words(shard_words_path(path, words_db))
        cur = con.cursor()
        curw = conw.cursor()
        if corpus_ids and use_filter_override:
            ensure_urn_filter(con, corpus_ids)
            use_filter = True
        else:
            use_filter = False
        cf = get_cf_id(curw, coll_word)
        if cf is None:
            con.close()
            conw.close()
            return {"counts": {}, "cf_found": False}
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
        con.close()
        conw.close()
        return {"counts": counts, "cf_found": True}

    results = run_on_shards(postings_paths, worker)
    if not any(res["cf_found"] for res in results):
        st.error("Word not found")
    else:
        combined: Dict[str, int] = {}
        for res in results:
            for word, cnt in res["counts"].items():
                combined[word] = combined.get(word, 0) + cnt
        top = sorted(combined.items(), key=lambda x: x[1], reverse=True)[:50]
        st.write(pd.DataFrame(top, columns=["word", "count"]))
