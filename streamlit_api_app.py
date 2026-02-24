#!/usr/bin/env python3
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List

import streamlit as st


def parse_terms_csv(s: str) -> List[str]:
    return [t.strip() for t in s.split(",") if t.strip()]


def parse_term_groups_text(s: str) -> List[List[str]]:
    """
    Input example:
      spise,spiser
      middag,frokost
    """
    groups: List[List[str]] = []
    for line in s.splitlines():
        terms = parse_terms_csv(line)
        if terms:
            groups.append(terms)
    return groups


def parse_query_to_groups(query: str) -> List[List[str]]:
    """
    Query syntax:
      spise
      spise middag
      spise [middag, frokost]
      [hamar, lillehammer]
    """
    q = (query or "").strip()
    if not q:
        return []

    groups: List[List[str]] = []
    buf = ""
    i = 0
    n = len(q)
    while i < n:
        ch = q[i]
        if ch == "[":
            if buf.strip():
                groups.append([buf.strip()])
                buf = ""
            j = q.find("]", i + 1)
            if j == -1:
                groups.append([q[i + 1 :].strip()])  # best-effort on unclosed bracket
                break
            inner = q[i + 1 : j]
            grp = [t.strip() for t in inner.split(",") if t.strip()]
            if grp:
                groups.append(grp)
            i = j + 1
            continue
        if ch.isspace():
            if buf.strip():
                groups.append([buf.strip()])
                buf = ""
            i += 1
            continue
        buf += ch
        i += 1
    if buf.strip():
        groups.append([buf.strip()])
    return groups


def api_post(base_url: str, path: str, payload: Dict[str, Any], timeout_s: int = 120) -> Dict[str, Any]:
    url = f"{base_url.rstrip('/')}{path}"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            dt_ms = (time.perf_counter() - t0) * 1000.0
            out = json.loads(raw) if raw else {}
            return {"ok": True, "status": resp.status, "ms": dt_ms, "data": out}
    except urllib.error.HTTPError as exc:
        dt_ms = (time.perf_counter() - t0) * 1000.0
        body_txt = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(body_txt)
        except Exception:
            detail = {"raw": body_txt}
        return {"ok": False, "status": exc.code, "ms": dt_ms, "error": detail}
    except Exception as exc:
        dt_ms = (time.perf_counter() - t0) * 1000.0
        return {"ok": False, "status": 0, "ms": dt_ms, "error": {"detail": str(exc)}}


st.set_page_config(page_title="Postings API tester", layout="wide")
st.title("Postings API tester (group-based payloads)")

with st.sidebar:
    st.header("Connection")
    profile = st.selectbox(
        "Runtime profile",
        [
            "Roaring test (port 8000, julia)",
            "Original test (port 8002, python)",
            "Custom",
        ],
        index=0,
    )
    default_base = "http://127.0.0.1:8000"
    default_engine = "julia"
    if profile == "Original test (port 8002, python)":
        default_base = "http://127.0.0.1:8002"
        default_engine = "python"
    elif profile == "Custom":
        default_base = st.session_state.get("base_url", "http://127.0.0.1:8000")
        default_engine = st.session_state.get("engine", "python")

    base_url = st.text_input("API base URL", value=default_base, key="base_url")
    endpoint = st.selectbox(
        "Endpoint",
        ["/auto", "/near_query", "/near_fragments", "/near_hits", "/or_query"],
        index=0,
    )
    engine_idx = 1 if default_engine == "julia" else 0
    engine = st.selectbox("Engine", ["python", "julia"], index=engine_idx, key="engine")
    timeout_s = st.number_input("Timeout (seconds)", min_value=5, max_value=600, value=120, step=5)

if base_url.rstrip("/") == "http://127.0.0.1:8000" and engine != "julia":
    st.warning("Port 8000 (Roaring config) typically needs `engine=julia` right now.")
if base_url.rstrip("/") == "http://127.0.0.1:8002" and engine != "python":
    st.info("Port 8002 is the original config; `engine=python` is usually the fastest baseline.")

st.subheader("Query")
query_text = st.text_input(
    "One query field",
    value="er de",
    help='Examples: "spise", "spise middag", "spise [middag, frokost]"',
)

st.subheader("Options")
c1, c2, c3, c4 = st.columns(4)
with c1:
    window = st.number_input("window", min_value=1, max_value=100, value=5)
    before = st.number_input("before", min_value=0, max_value=50, value=5)
    after = st.number_input("after", min_value=0, max_value=50, value=5)
with c2:
    per_book = st.number_input("perBook", min_value=0, max_value=200, value=2)
    doc_samples = st.number_input("docSamples", min_value=0, max_value=50000, value=10)
    total_limit = st.number_input("totalLimit", min_value=0, max_value=1000000, value=50)
with c3:
    max_variants = st.number_input("maxVariants", min_value=1, max_value=200, value=10)
    schema = st.text_input("schema", value="unigrams")
    symmetric = st.checkbox("symmetric", value=True)
    match_mode = st.selectbox("matchMode", ["near", "sequence"], index=0)
with c4:
    exclude_self = st.checkbox("excludeSelf", value=False)
    use_filter = st.checkbox("useFilter", value=False)
    filter_ids_text = st.text_input("filterIds (csv ints)", value="")
    parallel_shards = st.checkbox("parallelShards (Julia)", value=False)


payload: Dict[str, Any] = {
    "window": int(window),
    "before": int(before),
    "after": int(after),
    "perBook": int(per_book),
    "docSamples": int(doc_samples),
    "totalLimit": int(total_limit),
    "schema": schema,
    "symmetric": bool(symmetric),
    "excludeSelf": bool(exclude_self),
    "useFilter": bool(use_filter),
    "filterIds": [int(x) for x in parse_terms_csv(filter_ids_text)] if filter_ids_text.strip() else [],
    "maxVariants": int(max_variants),
}

groups = parse_query_to_groups(query_text)
groups = [g for g in groups if g]

effective_endpoint = endpoint
if endpoint == "/auto":
    if not groups:
        effective_endpoint = "/or_query"
    elif len(groups) == 1:
        effective_endpoint = "/or_query"
    else:
        effective_endpoint = "/near_fragments"

if effective_endpoint in {"/near_query", "/near_fragments", "/near_hits"}:
    payload["engine"] = engine
    payload["parallelShards"] = bool(parallel_shards)
    payload["matchMode"] = match_mode

has_or_group = any(len(g) > 1 for g in groups)
if has_or_group or len(groups) != 1:
    payload["termGroups"] = groups
else:
    payload["terms"] = [groups[0][0]]

st.subheader("Payload")
st.code(json.dumps(payload, ensure_ascii=False, indent=2), language="json")
st.caption(f"Effective endpoint: `{effective_endpoint}`")

if st.button("Run request", type="primary"):
    result = api_post(base_url, effective_endpoint, payload, timeout_s=int(timeout_s))
    st.write(f"Status: `{result['status']}` | Time: `{result['ms']:.1f} ms`")
    if result["ok"]:
        st.success("Request OK")
        st.json(result["data"])
    else:
        st.error("Request failed")
        st.json(result["error"])
