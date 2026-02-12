from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class AppConfig:
    postings_dbs: List[str]
    words_db: Optional[str]
    ext_path: str
    default_schema: str


def load_config() -> AppConfig:
    cfg_path = os.environ.get("POSTINGS_CONFIG", "").strip()
    if not cfg_path:
        raise RuntimeError("POSTINGS_CONFIG is not set.")
    path = Path(cfg_path)
    data = json.loads(path.read_text())
    postings_dbs = data.get("postings_dbs") or []
    if not postings_dbs:
        raise RuntimeError("postings_dbs is required in config.")
    words_db = data.get("words_db") or None
    for path in postings_dbs:
        if not Path(path).exists():
            raise RuntimeError(f"postings_db not found: {path}")
    if words_db and not Path(words_db).exists():
        raise RuntimeError(f"words_db not found: {words_db}")
    ext_path = data.get("ext_path") or ""
    default_schema = data.get("default_schema") or "unigrams"
    return AppConfig(
        postings_dbs=postings_dbs,
        words_db=words_db,
        ext_path=ext_path,
        default_schema=default_schema,
    )
