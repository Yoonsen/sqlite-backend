from __future__ import annotations

import json
from typing import Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from api_python.config import load_config
from api_python.postings_queries import (
    connect_postings,
    connect_words,
    docpost_book_ids,
    fetch_window,
    get_cf_id,
    near_frequency,
    sample_collocations,
    sample_concordance_near,
    sample_concordance_single,
)

app = FastAPI(title="Postings API", version="0.1.0")


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


class ConcordanceRequest(BaseModel):
    wordA: str
    wordB: Optional[str] = ""
    window: int = Field(5, ge=1, le=50)
    before: int = Field(5, ge=1, le=50)
    after: int = Field(5, ge=1, le=50)
    perBook: int = Field(3, ge=1, le=20)
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


class NearQueryRequest(BaseModel):
    terms: List[str]
    window: int = Field(5, ge=1, le=50)
    schema: Optional[str] = None
    symmetric: bool = True
    excludeSelf: bool = False
    useFilter: bool = False
    filterIds: List[int] = []
    maxVariants: int = Field(10, ge=1, le=100)


class NearFragmentsRequest(BaseModel):
    terms: List[str]
    window: int = Field(5, ge=1, le=50)
    before: int = Field(5, ge=1, le=50)
    after: int = Field(5, ge=1, le=50)
    perBook: int = Field(3, ge=1, le=20)
    totalLimit: int = Field(200, ge=1, le=5000)
    schema: Optional[str] = None
    symmetric: bool = True
    excludeSelf: bool = False
    useFilter: bool = False
    filterIds: List[int] = []
    maxVariants: int = Field(10, ge=1, le=100)


class CollocationsRequest(BaseModel):
    word: str
    before: int = Field(5, ge=1, le=50)
    after: int = Field(5, ge=1, le=50)
    perBook: int = Field(3, ge=1, le=20)
    schema: Optional[str] = None
    useFilter: bool = False
    filterIds: List[int] = []


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok", "version": app.version}


@app.post("/concordance")
def concordance(req: ConcordanceRequest):
    postings_paths = CONFIG.postings_dbs
    rows = []
    word_a_found = False
    word_b_found = True
    for path in postings_paths:
        con = connect_postings(path, CONFIG.ext_path)
        conw = connect_words(shard_words_path(path))
        cur = con.cursor()
        curw = conw.cursor()
        if req.useFilter and req.filterIds:
            use_filter = True
            filter_json = json.dumps(req.filterIds)
        else:
            use_filter = False
            filter_json = None
        cf_a = get_cf_id(curw, req.wordA)
        if cf_a is None:
            con.close()
            conw.close()
            continue
        word_a_found = True
        if req.wordB and req.wordB.strip():
            cf_b = get_cf_id(curw, req.wordB)
            if cf_b is None:
                word_b_found = False
                con.close()
                conw.close()
                continue
            if not use_filter:
                filter_ids = docpost_book_ids(cur, [[cf_a], [cf_b]])
                if not filter_ids:
                    con.close()
                    conw.close()
                    continue
                use_filter = True
                filter_json = json.dumps(filter_ids)
            if req.symmetric:
                off_min, off_max = -req.before, req.after
            else:
                off_min, off_max = 1, req.after
            rows.extend(
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
                filter_ids = docpost_book_ids(cur, [[cf_a]])
                if not filter_ids:
                    con.close()
                    conw.close()
                    continue
                use_filter = True
                filter_json = json.dumps(filter_ids)
            rows.extend(
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
    for path in postings_paths:
        con = connect_postings(path, CONFIG.ext_path)
        conw = connect_words(shard_words_path(path))
        cur = con.cursor()
        curw = conw.cursor()
        if req.useFilter and req.filterIds:
            use_filter = True
            filter_json = json.dumps(req.filterIds)
        else:
            use_filter = False
            filter_json = None
        cf_a = get_cf_id(curw, req.wordA)
        cf_b = get_cf_id(curw, req.wordB)
        if cf_a is None or cf_b is None:
            con.close()
            conw.close()
            continue
        if not use_filter:
            filter_ids = docpost_book_ids(cur, [[cf_a], [cf_b]])
            if not filter_ids:
                con.close()
                conw.close()
                continue
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
    if has_docfreq:
        row = curw.execute(
            "SELECT cf_id, docfreq FROM words WHERE word = ? ORDER BY raw_id LIMIT 1",
            (term.casefold(),),
        ).fetchone()
    else:
        row = curw.execute(
            "SELECT cf_id FROM words WHERE word = ? ORDER BY raw_id LIMIT 1",
            (term.casefold(),),
        ).fetchone()
    if row:
        return [row[0]], int(row[1]) if has_docfreq else 1
    if has_docfreq:
        row = curw.execute(
            "SELECT cf_id, docfreq FROM words WHERE word = ? ORDER BY raw_id LIMIT 1",
            (term,),
        ).fetchone()
    else:
        row = curw.execute(
            "SELECT cf_id FROM words WHERE word = ? ORDER BY raw_id LIMIT 1",
            (term,),
        ).fetchone()
    if not row:
        return [], 0
    return [row[0]], int(row[1]) if has_docfreq else 1


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


@app.post("/near_query")
def near_query(req: NearQueryRequest):
    if not req.terms or len(req.terms) < 2:
        raise HTTPException(status_code=400, detail="terms must contain at least two items")
    postings_paths = CONFIG.postings_dbs
    total = 0
    docs = 0
    found_any = False
    off_min = -req.window if req.symmetric else 1
    off_max = req.window
    for path in postings_paths:
        con = connect_postings(path, CONFIG.ext_path)
        conw = connect_words(shard_words_path(path))
        cur = con.cursor()
        curw = conw.cursor()
        if req.useFilter and req.filterIds:
            use_filter = True
            filter_json = json.dumps(req.filterIds)
        else:
            use_filter = False
            filter_json = None

        term_infos: List[Tuple[str, List[int], int]] = []
        for term in req.terms:
            cf_ids, df_sum = expand_term_cf_ids_with_df(curw, term, req.maxVariants)
            if not cf_ids:
                term_infos = []
                break
            term_infos.append((term, cf_ids, df_sum))
        if req.symmetric:
            term_infos.sort(key=lambda x: x[2])
        groups: List[List[int]] = [info[1] for info in term_infos]
        if not groups:
            con.close()
            conw.close()
            continue
        if not use_filter:
            filter_ids = docpost_book_ids(cur, groups)
            if not filter_ids:
                con.close()
                conw.close()
                continue
            use_filter = True
            filter_json = json.dumps(filter_ids)
        found_any = True
        prepare_term_cf_table(cur, groups)

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
        con.close()
        conw.close()
    if not found_any:
        raise HTTPException(status_code=404, detail="No terms matched")
    return {"total": total, "docs": docs}


@app.post("/near_fragments")
def near_fragments(req: NearFragmentsRequest):
    if not req.terms or len(req.terms) < 2:
        raise HTTPException(status_code=400, detail="terms must contain at least two items")
    postings_paths = CONFIG.postings_dbs
    rows: List[Dict[str, object]] = []
    off_min = -req.window if req.symmetric else 1
    off_max = req.window
    for path in postings_paths:
        con = connect_postings(path, CONFIG.ext_path)
        conw = connect_words(shard_words_path(path))
        cur = con.cursor()
        curw = conw.cursor()
        if req.useFilter and req.filterIds:
            use_filter = True
            filter_json = json.dumps(req.filterIds)
        else:
            use_filter = False
            filter_json = None

        term_infos: List[Tuple[str, List[int], int]] = []
        for term in req.terms:
            cf_ids, df_sum = expand_term_cf_ids_with_df(curw, term, req.maxVariants)
            if not cf_ids:
                term_infos = []
                break
            term_infos.append((term, cf_ids, df_sum))
        if req.symmetric:
            term_infos.sort(key=lambda x: x[2])
        groups: List[List[int]] = [info[1] for info in term_infos]
        if not groups:
            con.close()
            conw.close()
            continue
        if not use_filter:
            filter_ids = docpost_book_ids(cur, groups)
            if not filter_ids:
                con.close()
                conw.close()
                continue
            use_filter = True
            filter_json = json.dumps(filter_ids)

        prepare_term_cf_table(cur, groups)
        cte_sql, select_sql, _ = groups_sql(
            groups, req.schema or CONFIG.default_schema, use_filter
        )
        if use_filter:
            cte_sql = "filter AS (SELECT value AS urn FROM json_each(?)), " + cte_sql
        sql = f"""
        WITH
        {cte_sql}
        {select_sql}
        """
        inner = con.cursor()
        params = (filter_json,) if use_filter else ()
        for row in cur.execute(sql, params):
            book_id = row[0]
            blobs = row[1:]
            # compute near positions from anchor (b1) to each other group
            pos_sets: List[set] = []
            for idx in range(1, len(blobs)):
                res = inner.execute(
                    "SELECT post_near_positions(?, ?, ?, ?)",
                    (blobs[0], blobs[idx], off_min, off_max),
                ).fetchone()
                positions = json.loads(res[0]) if res and res[0] else []
                if not positions:
                    pos_sets = []
                    break
                pos_sets.append(set(int(p) for p in positions))
            if not pos_sets:
                continue
            # intersect all position sets
            common = pos_sets[0]
            for s in pos_sets[1:]:
                common = common.intersection(s)
                if not common:
                    break
            if not common:
                continue
            samples = min(req.perBook, len(common))
            for pos in list(common)[:samples]:
                frag = fetch_window(cur, curw, book_id, int(pos), req.before, req.after)
                rows.append({"bookId": book_id, "pos": int(pos), "frag": frag})
                if req.totalLimit and len(rows) >= req.totalLimit:
                    break
            if req.totalLimit and len(rows) >= req.totalLimit:
                break
        con.close()
        conw.close()
        if req.totalLimit and len(rows) >= req.totalLimit:
            break
    if not rows:
        raise HTTPException(status_code=404, detail="No near fragments found")
    return {"rows": rows}


@app.post("/collocations")
def collocations(req: CollocationsRequest):
    postings_paths = CONFIG.postings_dbs
    combined: Dict[str, int] = {}
    found_any = False
    for path in postings_paths:
        con = connect_postings(path, CONFIG.ext_path)
        conw = connect_words(shard_words_path(path))
        cur = con.cursor()
        curw = conw.cursor()
        if req.useFilter and req.filterIds:
            use_filter = True
            filter_json = json.dumps(req.filterIds)
        else:
            use_filter = False
            filter_json = None
        cf_id = get_cf_id(curw, req.word)
        if cf_id is None:
            con.close()
            conw.close()
            continue
        if not use_filter:
            filter_ids = docpost_book_ids(cur, [[cf_id]])
            if not filter_ids:
                con.close()
                conw.close()
                continue
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
