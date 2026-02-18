from __future__ import annotations

import json
import os
import random
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

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
    sample_urns,
    sample_collocations,
    sample_concordance_near,
    sample_concordance_single,
    sample_concordance_union,
)

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
    maxVariants: int = Field(10, ge=1, le=100)
    engine: Optional[str] = None
    parallelShards: Optional[bool] = None


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


class CollocationsRequest(BaseModel):
    word: str
    before: int = Field(5, ge=1, le=50)
    after: int = Field(5, ge=1, le=50)
    perBook: int = Field(3, ge=1, le=20)
    docSamples: Optional[int] = Field(None, ge=0, le=50000)
    schema: Optional[str] = None
    useFilter: bool = False
    filterIds: List[int] = []


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
        for path in postings_paths:
            con = connect_postings(path, CONFIG.ext_path)
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
    for path in postings_paths:
        con = connect_postings(path, CONFIG.ext_path)
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


def _apply_docpost_filter_and_sample(
    cur,
    cf_groups: List[List[int]],
    base_filter_ids: Optional[List[int]],
    doc_samples: Optional[int],
    total_limit: int,
    per_book: int,
) -> Optional[List[int]]:
    filter_ids = list(base_filter_ids) if base_filter_ids else None
    docpost_ids = docpost_book_ids(cur, cf_groups)
    if docpost_ids is not None:
        if filter_ids:
            filter_set = set(filter_ids)
            filter_ids = [bid for bid in docpost_ids if bid in filter_set]
        else:
            filter_ids = docpost_ids

    sample_n = _resolve_doc_samples(doc_samples, total_limit, per_book)
    if sample_n > 0:
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
        "docSamples": 0,
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


@app.post("/near_query")
def near_query(req: NearQueryRequest):
    if req.termGroups:
        if len(req.termGroups) < 2:
            raise HTTPException(status_code=400, detail="termGroups must contain at least two items")
    elif not req.terms or len(req.terms) < 2:
        raise HTTPException(status_code=400, detail="terms must contain at least two items")
    engine = _select_engine(req.engine)
    if engine == "julia":
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
    off_min = -req.window if req.symmetric else 1
    off_max = req.window
    for path in postings_paths:
        con = connect_postings(path, CONFIG.ext_path)
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
            filter_ids = _apply_docpost_filter_and_sample(
                cur,
                groups,
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
        else:
            if len(groups) == 2:
                if use_filter:
                    from_clause = "FROM filter f JOIN {table} u ON u.book_id = f.urn"
                else:
                    from_clause = "FROM {table} u"
                sql = f"""
                WITH
                {("filter AS (SELECT value AS urn FROM json_each(?))," if use_filter else "")}
                combined AS (
                    SELECT u.book_id,
                           post_near_count_groups(t.grp, u.post, {off_min}, {off_max}) AS c
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
        con.close()
        conw.close()
    if not found_any:
        raise HTTPException(status_code=404, detail="No terms matched")
    return {"total": total, "docs": docs}


@app.post("/or_query")
def or_query(req: OrQueryRequest):
    if req.termGroups:
        if len(req.termGroups) < 1:
            raise HTTPException(status_code=400, detail="termGroups must contain at least one item")
    elif not req.terms:
        raise HTTPException(status_code=400, detail="terms must contain at least one item")

    postings_paths = CONFIG.postings_dbs
    rows: List[Tuple[int, int, str]] = []
    found_any = False
    for path in postings_paths:
        con = connect_postings(path, CONFIG.ext_path)
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
        or_cf_ids = sorted(set(cf for g in groups for cf in g))
        if not or_cf_ids:
            con.close()
            conw.close()
            continue
        if not use_filter:
            filter_ids = _apply_docpost_filter_and_sample(
                cur,
                [or_cf_ids],
                base_filter_ids,
                req.docSamples,
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
        found_any = True
        rows.extend(
            sample_concordance_union(
                cur,
                curw,
                or_cf_ids,
                req.perBook,
                req.before,
                req.after,
                use_filter,
                filter_json,
            )
        )
        con.close()
        conw.close()
        if req.totalLimit and len(rows) >= req.totalLimit:
            break

    if not found_any:
        raise HTTPException(status_code=404, detail="No terms matched")
    if not rows:
        raise HTTPException(status_code=404, detail="No results found")
    if req.totalLimit and len(rows) > req.totalLimit:
        rows = rows[: req.totalLimit]
    return {"rows": [{"bookId": b, "pos": p, "frag": f} for b, p, f in rows]}


@app.post("/near_fragments")
def near_fragments(req: NearFragmentsRequest):
    if req.termGroups:
        if len(req.termGroups) < 2:
            raise HTTPException(status_code=400, detail="termGroups must contain at least two items")
    elif not req.terms or len(req.terms) < 2:
        raise HTTPException(status_code=400, detail="terms must contain at least two items")
    engine = _select_engine(req.engine)
    if engine == "julia":
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
    req_t0 = time.perf_counter()
    perf_shards: List[Dict[str, object]] = []
    for path in postings_paths:
        shard_t0 = time.perf_counter()
        rows_before_shard = len(rows)
        shard_perf: Dict[str, object] = {
            "shard": path,
            "groups_ms": 0.0,
            "prefilter_ms": 0.0,
            "near_sql_ms": 0.0,
            "post_ms": 0.0,
            "candidates_json": 0,
            "candidates_blob": 0,
            "rows_added": 0,
            "total_ms": 0.0,
        }
        con = connect_postings(path, CONFIG.ext_path)
        conw = connect_words(shard_words_path(path))
        cur = con.cursor()
        curw = conw.cursor()
        base_filter_ids = req.filterIds if req.useFilter and req.filterIds else None
        use_filter = False
        filter_json = None

        groups_t0 = time.perf_counter()
        groups = _resolve_term_groups(
            curw, req.terms, req.termGroups, req.maxVariants, req.symmetric
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
            filter_ids = _apply_docpost_filter_and_sample(
                cur,
                groups,
                base_filter_ids,
                0,
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
        shard_perf["prefilter_ms"] = round((time.perf_counter() - pre_t0) * 1000.0, 3)

        # Fast near path for two singleton groups:
        # use the same lean two-term near engine used by concordance.
        if len(groups) == 2 and len(groups[0]) == 1 and len(groups[1]) == 1:
            near_rows = sample_concordance_near(
                cur,
                curw,
                groups[0][0],
                groups[1][0],
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
            if req.docSamples and req.docSamples > 0 and near_rows:
                doc_ids = sorted({b for b, _, _ in near_rows})
                if len(doc_ids) > req.docSamples:
                    keep_docs = set(random.sample(doc_ids, req.docSamples))
                    near_rows = [r for r in near_rows if r[0] in keep_docs]
            if req.totalLimit:
                remaining = max(req.totalLimit - len(rows), 0)
                if remaining <= 0:
                    near_rows = []
                elif len(near_rows) > remaining:
                    near_rows = near_rows[:remaining]
            rows.extend(
                [
                    {
                        "bookId": int(b),
                        "pos": int(p),
                        "frag": f if req.includeFragments else "",
                    }
                    for b, p, f in near_rows
                ]
            )
            con.close()
            conw.close()
            shard_perf["rows_added"] = len(rows) - rows_before_shard
            shard_perf["post_ms"] = round((time.perf_counter() - pre_t0) * 1000.0, 3)
            shard_perf["total_ms"] = round((time.perf_counter() - shard_t0) * 1000.0, 3)
            perf_shards.append(shard_perf)
            if req.totalLimit and len(rows) >= req.totalLimit:
                break
            continue

        # Optional explicit pre-union bitmap path for two-group CNF:
        # OR each group to bitmap first, then run near directly on bitmaps.
        if (
            PREUNION_GROUPS
            and USE_BITMAP_NEAR
            and len(groups) == 2
            and _has_sql_function(cur, "post_union_bitmap_agg")
            and _has_sql_function(cur, "bitmap_near_sample_positions_json")
        ):
            prepare_term_cf_table(cur, groups)
            if use_filter:
                from_clause = "FROM filter f JOIN {table} u ON u.book_id = f.urn"
            else:
                from_clause = "FROM {table} u"
            sql = f"""
            WITH
            {("filter AS (SELECT value AS urn FROM json_each(?))," if use_filter else "")}
            grouped AS (
                SELECT u.book_id, t.grp, post_union_bitmap_agg(u.post) AS bmap
                {from_clause.format(table=(req.schema or CONFIG.default_schema))}
                JOIN term_cf t ON t.cf_id = u.cf_id
                GROUP BY u.book_id, t.grp
            ),
            paired AS (
                SELECT g1.book_id, g1.bmap AS b1, g2.bmap AS b2
                FROM grouped g1
                JOIN grouped g2 ON g2.book_id = g1.book_id
                WHERE g1.grp = 1 AND g2.grp = 2
            )
            SELECT
                book_id,
                bitmap_near_sample_positions_json(b1, b2, {off_min}, {off_max}, {req.perBook}) AS pos_json
            FROM paired
            """
            params = (filter_json,) if use_filter else ()
            candidates_json: List[Tuple[int, str]] = []
            for book_id, pos_json in cur.execute(sql, params):
                if not pos_json or pos_json == "[]":
                    continue
                candidates_json.append((int(book_id), pos_json))
            if req.docSamples and req.docSamples > 0 and len(candidates_json) > req.docSamples:
                candidates_json = random.sample(candidates_json, req.docSamples)
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
            con.close()
            conw.close()
            if req.totalLimit and len(rows) >= req.totalLimit:
                break
            continue

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
                groups, req.schema or CONFIG.default_schema, use_filter
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
    if not rows:
        raise HTTPException(status_code=404, detail="No near fragments found")
    if PROFILE_NEAR:
        return {
            "rows": rows,
            "_perf": {
                "total_ms": round((time.perf_counter() - req_t0) * 1000.0, 3),
                "shards": perf_shards,
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
