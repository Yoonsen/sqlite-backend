from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests


BASE_URL = "http://sprakbankdb1.lx.nb.no:8000"


def _post(path: str, payload: Dict[str, Any], base_url: str = BASE_URL) -> Dict[str, Any]:
    url = f"{base_url.rstrip('/')}{path}"
    res = requests.post(url, json=payload, timeout=300)
    res.raise_for_status()
    return res.json()


def health(base_url: str = BASE_URL) -> Dict[str, Any]:
    url = f"{base_url.rstrip('/')}/health"
    res = requests.get(url, timeout=30)
    res.raise_for_status()
    return res.json()


def concordance(
    word_a: str,
    word_b: str = "",
    window: int = 5,
    before: int = 5,
    after: int = 5,
    per_book: int = 3,
    total_limit: int = 200,
    schema: str = "unigrams",
    use_filter: bool = False,
    filter_ids: Optional[List[int]] = None,
    symmetric: bool = True,
    exclude_self: bool = False,
    render_mode: str = "legacy",
    base_url: str = BASE_URL,
) -> Dict[str, Any]:
    payload = {
        "wordA": word_a,
        "wordB": word_b,
        "window": window,
        "before": before,
        "after": after,
        "perBook": per_book,
        "totalLimit": total_limit,
        "schema": schema,
        "useFilter": use_filter,
        "filterIds": filter_ids or [],
        "symmetric": symmetric,
        "excludeSelf": exclude_self,
        "renderMode": render_mode,
    }
    return _post("/concordance", payload, base_url=base_url)


def near_frequency(
    word_a: str,
    word_b: str,
    window: int = 5,
    schema: str = "unigrams",
    symmetric: bool = True,
    exclude_self: bool = False,
    use_filter: bool = False,
    filter_ids: Optional[List[int]] = None,
    base_url: str = BASE_URL,
) -> Dict[str, Any]:
    payload = {
        "wordA": word_a,
        "wordB": word_b,
        "window": window,
        "schema": schema,
        "symmetric": symmetric,
        "excludeSelf": exclude_self,
        "useFilter": use_filter,
        "filterIds": filter_ids or [],
    }
    return _post("/near_frequency", payload, base_url=base_url)


def near_query(
    terms: List[str],
    window: int = 5,
    schema: str = "unigrams",
    symmetric: bool = True,
    exclude_self: bool = False,
    use_filter: bool = False,
    filter_ids: Optional[List[int]] = None,
    max_variants: int = 10,
    count_mode: str = "auto",
    base_url: str = BASE_URL,
) -> Dict[str, Any]:
    payload = {
        "terms": terms,
        "window": window,
        "schema": schema,
        "symmetric": symmetric,
        "excludeSelf": exclude_self,
        "useFilter": use_filter,
        "filterIds": filter_ids or [],
        "maxVariants": max_variants,
        "countMode": count_mode,
    }
    return _post("/near_query", payload, base_url=base_url)


def near_fragments(
    terms: List[str],
    window: int = 5,
    before: int = 5,
    after: int = 5,
    per_book: int = 3,
    total_limit: int = 200,
    schema: str = "unigrams",
    symmetric: bool = True,
    exclude_self: bool = False,
    use_filter: bool = False,
    filter_ids: Optional[List[int]] = None,
    max_variants: int = 10,
    render_mode: str = "legacy",
    base_url: str = BASE_URL,
) -> Dict[str, Any]:
    payload = {
        "terms": terms,
        "window": window,
        "before": before,
        "after": after,
        "perBook": per_book,
        "totalLimit": total_limit,
        "schema": schema,
        "symmetric": symmetric,
        "excludeSelf": exclude_self,
        "useFilter": use_filter,
        "filterIds": filter_ids or [],
        "maxVariants": max_variants,
        "renderMode": render_mode,
    }
    return _post("/near_fragments", payload, base_url=base_url)


def collocations(
    word: str,
    before: int = 5,
    after: int = 5,
    per_book: int = 3,
    schema: str = "unigrams",
    use_filter: bool = False,
    filter_ids: Optional[List[int]] = None,
    base_url: str = BASE_URL,
) -> Dict[str, Any]:
    payload = {
        "word": word,
        "before": before,
        "after": after,
        "perBook": per_book,
        "schema": schema,
        "useFilter": use_filter,
        "filterIds": filter_ids or [],
    }
    return _post("/collocations", payload, base_url=base_url)
